# USB backup / offline sneakernet transport in the daemon

- **Scope & relationships:** azt-collab/daemon. Add USB-drive backup + sync as a
  daemon-owned transport alongside github (WAN) and LAN — so a repo can be pushed to /
  pulled from a removable drive when there's no internet and no LAN. **Field-parity
  dependency of** [[azt_run_with_server]]: the desktop app's own `backend/core/vcs.py`
  currently does USB sneakernet (push/pull to USB drives + arbitrary remotes); the daemon
  is the only thing allowed to touch dulwich, so removing azt's VCS regresses the offline
  field workflow unless the daemon gains USB. Desktop-primary; also phones with a USB-C OTG
  drive where one can mount.
- **Vision / done-criteria:** a user with no internet/LAN can back up and converge a
  project through a physical drive — plug in, "back up to USB" (and, ideally, another
  device plugs the same drive in and pulls the changes), all coordinated by the daemon
  (dulwich-owned), not by peer code. Github stays authoritative; USB is another
  opportunistic transport, like LAN.
- **Deadline:** before [[azt_run_with_server]] ships (hard dependency for field parity).

## Plans
- **DECIDED (2026-07-06): bare repo on the drive** (`<lang>.git`), not a bundle — matches
  what the suite already does everywhere else (github + LAN are bare-repo-shaped), so the
  daemon pushes to / pulls+merges from it as just another `file://` remote, reusing the
  existing merge path (`repo._merge_diverged`).
- **REQUIRED: drop a plain-language README on the drive** so a field user unfamiliar with
  bare repos isn't baffled by the git internals they see on mount. A bare repo looks like
  `HEAD` / `objects/` / `refs/` / `config` — NOT their files — so without a note it reads as
  "the drive is empty / broken." Put a human-facing `README.txt` at the **drive root** (what
  they see when they plug in): what this drive is (an AZT project backup in git "bare
  repository" format), that the files are packed inside and not directly browsable, how to
  restore (AZT → restore-from-USB, or `git clone <lang>.git`), and "don't delete these
  folders." Write it idempotently on each backup; consider bilingual (EN/FR) per the SIL
  user base.

## Notes
- Two shapes weighed → **(a) chosen** (bare repo; see Plans). (a) a **git remote on the drive** (bare repo at `file://…/nml.git`),
  push/fetch via dulwich; simple, but needs the drive present + writable and handles the
  merge like any diverged remote. (b) a **git bundle** file per project on the drive
  (`git bundle`-style single-file pack) — better for FAT/exFAT drives and one-way "carry
  it to the other machine," but needs bundle create/verify/unbundle plumbing.
- **USB never clears `pending_push`** — only a successful github push does (same
  "github convergence" property LAN honors). USB + LAN are opportunistic; WAN is the
  safety net.
- Desktop: mount points are filesystem paths — straightforward. **Phone (Android): USB-C
  OTG is the hard part** — Storage Access Framework / scoped storage means no raw
  filesystem path; a git remote at a `file://` path may not work through SAF. Likely needs
  a document-tree URI + stream-based bundle read/write, or is desktop-only for v1. Don't
  over-invest in the phone path until desktop works.

## Research — effort estimate (2026-07-06)

Tiered; the fork is "backup" (one-way) vs "sync" (two-way convergence), and bare-repo vs bundle.

- **Desktop one-way backup ≈ 1 day (0.5–1.5).** Drive as `file://` bare repo, reuse the push
  path. Not free: `_push_extras_step` rejects a no-credentials target → needs a local-path
  credential bypass (S); bare-repo-init on drive + confirm dulwich local push (S);
  mount selection + "Back up to USB" gesture (M); removable-media robustness — yank
  mid-write / read-only / full / FAT-exFAT quirks (M); tests (S). Alt: `git bundle`
  single-file (FAT-safe, cleaner artifact) but dulwich has no turnkey bundle writer →
  +0.5–1 day plumbing. **Bare-repo vs bundle is the main fork.**
- **Desktop two-way USB sync ≈ 2.5–4 days.** Above + fetch/merge-from-local-remote via
  `repo._merge_diverged`, a transport module in the LAN-sync shape (fan-out, FF/divergence,
  "drive has newer"), + tests.
- **Android USB-C OTG: separate later increment, multi-day, uncertain.** SAF/scoped storage
  → no `file://` git remote; needs document-tree URI + streamed bundle. Desktop-only v1.

Bottom line: one-way desktop backup fits before Cameroon (~1 day) if wanted; full
convergence is a 2.5–4 day feature; phones later.

## Research
- **Existing partial mechanism:** the daemon already has `Project.extra_remotes` (push the
  branch tip to arbitrary URLs — see `repo._push_extras_step`). A writable bare repo on a
  USB drive modeled as an extra_remote is the cheapest possible v0 for the *push/backup*
  half (no pull/merge). Check whether `file://` paths flow through that path today.
- **Design analog:** LAN sync (`lan_push` / `lan_listener`) already does "opportunistic
  transport that merges via the shared `repo._merge_diverged` and never clears
  pending_push" — USB should follow the same shape (a transport module + fan-out hook),
  not a bespoke path.
- **Workflow reference:** azt's `backend/core/vcs.py` (`Repository.push`/`pull`, USB/remote
  handling) is the field workflow to preserve — read it (azt repo) when designing, to match
  what field users already rely on (which drives, one-way vs two-way, confirm-diff UX).
## Implementation plan (grounded in daemon recon, 2026-07-06)

**Phase 1 — one-way backup to a bare repo on the drive (desktop). ~1 day.**
1. **Local-remote credential bypass.** `_push_extras_step` (`repo.py:4534-4603`) rejects a
   no-token target at its credential gate (`:4579-4583`). Detect a `file://`/local path →
   skip `get_sync_credentials`, call `porcelain.push(repo, url, refspec, username=None,
   password=None)` (`:4587`). dulwich routes `file://` to local transport transparently —
   no HTTP assumptions in the way. (S)
2. **Bare-repo init + README.** New helper (`usb_backup.py` or in `repo.py`):
   `porcelain.init(<mount>/<lang>.git, bare=True)` if absent; write the drive-root
   `README.txt` (see DECIDED plan) idempotently on every backup. Reuse pattern:
   `ensure_initial_commit`. (S)
3. **USB is NOT a plain `extra_remote`** (key recon finding). `_push_extras_step` fires on
   **every sync drain** — a USB target left in `extra_remotes` means the daemon hammers an
   unplugged drive with `EXTRA_REMOTE_PUSH_FAILED` every drain. USB must be
   **gesture/presence-gated**: push only on user request or when the drive is detected
   present. So do NOT reuse the every-drain extras loop for USB — make it a one-shot. (design)
4. **RPC + client wrapper.** `POST /v1/projects/<lang>/usb_backup {mount_path}` → init-if-
   absent + push-now, under `project_lock`. Client wrapper `usb_backup(langcode, mount_path)`.
   Follow the add-endpoint checklist (`server.py` dispatch ~:229+; wrapper in client
   `__init__.py`; status codes in BOTH status mirrors; translation). New codes
   `USB_BACKUP_OK`/`USB_BACKUP_FAILED` (or reuse `EXTRA_REMOTE_*`). (M)
5. **Removable-media robustness** — drive yanked mid-write / read-only / full / FAT-exFAT
   (no symlinks → set `core.symlinks=false` on init; case-insensitivity) → typed failures,
   never crash the caller. (M)
6. **pending_push unaffected** — confirmed: extras push runs *after* the github push in
   `_sync_repo_locked` (`repo.py:3277-3302`); the one-shot USB backup likewise must not
   clear `pending_push` (github stays authoritative, like LAN). (verify)
7. Tests. (S)

**Phase 2 — two-way convergence (later). +1.5–3 days.**
- Fetch from USB (`porcelain.fetch('file://…/<lang>.git', repo)`), extract its HEAD, then
  `_merge_diverged(repo, project_dir, branch, local_sha, usb_head)` (`repo.py:553-713`,
  remote-agnostic; WAN caller `:4810`, LAN caller `lan_push.py:854`), re-push the merged tip.
- Mirror the LAN transport shape (`lan_push.fan_out` `:1309`, `_push_to_peer` `:160-479`) as
  a USB transport module — but **presence-gated**, not the every-drain fan-out.
- Track per-project last-USB SHA (analog to `last_lan_pushed_sha`, `projects.py:429-443`) for
  a "USB up-to-date/behind" indicator; conflicts via the existing `CONFLICTS`/`Result` path.

**Phase 3 — Android USB-C OTG (much later, separate).** SAF → no `file://` git remote;
document-tree URI + streamed bundle. Out of v1.

## Open questions
1. **Trigger model (biggest).** Explicit "Back up to USB" gesture vs auto-on-mount detection
   — but **not** every-drain (drive usually absent → failure spam). How does the daemon learn
   a drive is present + its mount path: enumerate OS mounts, or the user supplies the path
   each time?
2. **Config storage.** Reuse `Project.extra_remotes` (Option A, zero schema — but conflates
   with internet extras AND rides the every-drain push, which is wrong for USB) vs a dedicated
   `usb_backup_paths` field / separate gesture-gated handling (Option B). Recon points to B.
3. **Where the gesture lives** (desktop): azt UI (ties to [[azt_run_with_server]]) vs daemon
   settings UI. (Phone, later: server-APK UI.)
4. **Backup vs sync for v1.** Is one-way enough for field parity? Depends what azt's
   `backend/core/vcs.py` USB workflow actually does — push-only, or push+pull sneakernet
   between two field machines. **Read azt `vcs.py` before committing to one-way.**
5. **Drive layout.** One `<lang>.git` bare per project at the drive root, alongside the
   README; a multi-project drive = several bare repos + one README. Confirm.
6. **FAT/exFAT correctness** — bare repo generally fine; confirm no symlink/case/permission
   issues and set safe git config at init.
7. **Multiple / rotated drives** — track per-drive last-pushed SHA, or stateless "push
   current tip"?
8. **README** — bilingual EN/FR; exact restore instructions (AZT restore-from-USB vs
   `git clone <lang>.git`).
