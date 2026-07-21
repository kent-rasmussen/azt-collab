# WAN push dead: SSH-shaped origin URL hits SubprocessSSHVendor password error

- **Scope & relationships:** azt-collab/daemon (repo.py push paths). Related to `strip_lan_origin_if_present` (invariant 11 — LAN origins leaking into `.git/config`) and the topic-branch push architecture.
- **Vision / done-criteria:** WAN pushes for the affected project succeed again; the daemon either normalizes or refuses SSH-shaped origin URLs with a typed Status instead of dying in `NotImplementedError`; root cause of how the SSH-shaped URL got into `.git/config` identified and closed.
- **Deadline:** none
- **Waiting on:** Nothing — FIELD-VERIFIED 2026-07-21 ~11:32 (ready to close):
  phone (0.54.12) drained the full backlog over the wan-normalized
  URL — preseed batches, topic-push chunks, phase-B/C promote,
  phase-D topic-branch delete — ending `wan_unshared=0, at_risk=0`,
  `codes=['NOTHING_TO_COMMIT','PUSHED','EXTRA_REMOTE_PUSHED']`.
  Residual observations: (a) baf carries an `extra_remotes` entry
  that is the SAME repo in ssh spelling (dual-publish artifact of
  the remote-conflict decision) — RESOLVED in 0.54.13 with no user
  action needed: `add_extra_remote` refuses wan-equal duplicates and
  `_push_extras_step` skips them at use, so the stored entry is
  inert (CLAUDE.md invariant #14); (b) `azt-blob-seed-*` side refs
  await the next daemon lifetime's janitor pass (by design).

## Plans

Fix shipped as **0.54.11** (2026-07-21), pending build + field verify:

- `repo.wan_url(url)` — pure converter: `git@host:path` /
  `ssh://[user@]host[:port]/path` / `git+ssh://…` → `https://host/path`.
  Everything else (https, LAN URLs, local paths) passes through.
- **Stored URLs keep the user's spelling** (Kent uses `git@` for his
  own command-line ssh auth) — conversion happens at use time only,
  at every WAN dulwich touchpoint in `repo.py`.
- Name-based fetch/pull (`'origin'`, needed for tracking-ref updates)
  routes through new `_fetch_origin` / `_pull_origin`: remote NAME
  when config URL is https (unchanged path), wan URL + manual
  `_import_origin_heads` tracking-ref import when ssh-shaped.
- `lan_listener` share-offer compare is now `wan_url`-normalized both
  sides → the "two remotes" popup (same repo, two spellings) is gone;
  logged no-op instead.
- `_ensure_remote_repo` normalizes before owner/repo urlparse.
- `MIN_SERVER_VERSION` floor raised to 0.54.11 (any device can catch
  an ssh-shaped URL via LAN share-offer adoption; old daemons fail
  silently on it).
- Tests: `tests/test_wan_url.py`.

Verify (field): rebuild + deploy server APK to the phone; on baf
expect `[sync-trace] origin is ssh-shaped (...); using
'https://github.com/audioword-ui/baf.git' for WAN ops`, then fetch /
topic-push / preseed proceeding instead of NotImplementedError. The
already-stashed remote-conflict decision on the phone can be resolved
either way — both spellings now push identically.

## Notes

Field repro 2026-07-21 (logcat, Android device):

```
07-21 06:15:00.376  3380 20453 E python  :  [sync-trace] preseed batch 1 push failed: NotImplementedError('Setting password not supported by SubprocessSSHVendor.')
```

Triage findings (2026-07-21, code read-only):

- dulwich raises this only when `get_transport_and_path` resolved the
  remote URL to its **SSH client**; the daemon passes
  `username`/`password` on every `porcelain.push` (e.g. the preseed
  batch push, repo.py ~4566), and `SubprocessSSHVendor` cannot take a
  password → `NotImplementedError`.
- Two URL shapes trip it: a literal SSH remote
  (`git@github.com:owner/repo.git`, `ssh://…`) or **any scheme-less
  `host:path` string** — dulwich parses those as scp-style SSH. A LAN
  endpoint like `192.168.x.x:34501/...` written as origin would do it.
- `remote_url` is read straight from `.git/config` origin at every
  push entry point (repo.py:3778, :3825, :2882, :2921, :6657 …);
  nothing in the push path normalizes SSH-form URLs.
- The preseed line is the tail symptom: preseed only fires after the
  topic-push chunks failed down to chunk_n=1, so those earlier chunk
  pushes almost certainly failed with the same error — **every WAN
  push for that project on that device is failing**, i.e. no github
  backup while this persists.

Evidence still needed:

- The actual origin `remote_url` on the affected device/project —
  from more of the daemon log (the `[sync-trace]` topic-push entry
  lines above the pasted one), or from `project_status` /
  settings UI on the device.
- Which device + which project (langcode).

## Research
