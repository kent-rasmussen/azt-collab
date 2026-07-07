# Deblock synchronization for a user (unknown bug)

- **Scope & relationships:** azt-collab/sync. A specific user's sync is blocked; root cause not yet known. Diagnosis-first — could land anywhere in the commit/push/merge/LAN/credential path. Uses the daemon-log share path (the very thing the share-archive item is about, so diagnostics delivery may itself be in play if the user is behind the Dome-strips-zip problem).
- **Vision / done-criteria:** the user's project syncs to github again; root cause identified and (if it's a code bug) fixed, not just cleared for one user.
- **Deadline:** 2026-07-01 (today, urgent).
- **Waiting on:** user response.

## Notes
- **User/device:** peer `7aeb3fac`, device `aztobt1-sudo`, github login `aztobt1-sudo`. Daemon 0.52.20.
- **Two projects:** `baf` (remote `audioword-ui/baf.git`) syncs fine; `nml` (remote `aztobt2-ui/nml.git`) is the blocked one.
- **Symptom (user):** "they aren't pushing to github." User's prior: it's the `.azt_atomic_orphans/unmergeable/*.lift` DATA_LOSS_RISK files.

## Research — root cause (2026-07-01)

**Root cause: the `azt-collaboration` GitHub App is NOT installed on the `aztobt2-ui` account that owns `nml`.** So this user's user-to-server token has no fetch/push access to `aztobt2-ui/nml`.

Evidence chain from the log:
- `[check_app_installed] /user/installations returned 2 entries: [('azt-collaboration','audioword-ui',131665272), ('azt-collaboration','aztobt1-sudo',137698243)]` — **`aztobt2-ui` is absent.**
- `get_valid_github_token()` (store.py:216) returns ONE user OAuth token; `get_sync_credentials` (store.py:303) does NOT scope by owner. A GitHub-App user-to-server token can only touch repos whose owner has the app installed. → nml (aztobt2-ui) = no access; baf (audioword-ui) = access. Exactly matches which one syncs.
- `[wan-unshared] 'nml': origin URL configured + no tracking ref (never-fetched or fetch-always-failed) → walk-from-HEAD = 2808` and `remote_refs_present: []`, `tracking_ref_sha: null`. **nml has NEVER successfully fetched.** baf has `refs/remotes/origin/*` present → was cloned/fetched OK.
- `[scheduler] drain skipped: 'nml' wan_backoff next=2026-07-02T08:18:38 (24 consecutive failure(s))` → 24 failed push attempts pushed the WAN backoff curve out ~22h. That's why "they aren't pushing" right now: the drain is *skipped*, not attempting.
- `diagnose_403` (auth.py:283) scopes `check_app_installed` to the repo **owner** and returns `APP_NOT_INSTALLED` when the owner has no install — so a **user-tap Sync** on nml WOULD surface the correct `APP_NOT_INSTALLED` status. Auto-drain doesn't, because auto-sync is silent by contract.

**The orphans are a red herring for the push block** (but a real separate issue):
- `_surface_uncommittable` (repo.py:5595) only *adds* an advisory `DATA_LOSS_RISK` code; it never blocks commit or push.
- Every commit ends `codes=['DATA_LOSS_RISK','NOTHING_TO_COMMIT']` → the LIFT/audio IS committed; only the 12 `.azt_atomic_orphans/unmergeable/*.lift` sit outside staged dirs and get flagged.
- Push is a wholly separate path (scheduler `_drain_pending_push`), gated by wan_backoff + credentials — untouched by DATA_LOSS_RISK.
- BUT: those 12 unmergeable orphans are genuinely at-risk data (from past failed merges) AND nml has never reached github → real data-loss exposure. Track as its own concern.

### Immediate deblock (no code change)
1. Install the `azt-collaboration` GitHub App on the **`aztobt2-ui`** account and grant it the `nml` repo (or add aztobt1-sudo to that installation). Confirm intent first: is aztobt1-sudo meant to push nml to github at all, or should aztobt2-ui (a LAN peer here, `db033cd4`) own the github push while aztobt1-sudo syncs nml over LAN? Note nml is currently NOT LAN-shared either (`reject 'nml': not in any peer's shared_projects; shared_anywhere=[]`), so neither transport works today.
2. After fixing access, have the user **tap Sync on nml** — the user gesture calls `wan_backoff.nudge()`, clearing the 22h backoff and forcing an immediate push (invariant #10). Without the tap, auto-retry won't fire for ~22h.

### Candidate code fixes (bugs worth filing)
- **Silent auto-backoff on permanent access failure.** `_drain_pending_push` (scheduler.py:1052-1067) treats a 403/`APP_NOT_INSTALLED`/`REPO_NOT_AUTHORIZED` identically to a transient network failure → `record_failure` → up to 24h backoff, and never surfaces the diagnosis (auto-sync is silent). The user can't self-diagnose without knowing to tap Sync. Consider: on a definitively-permanent push failure (diagnose_403 → APP_NOT_INSTALLED/REPO_NOT_AUTHORIZED/APP_SUSPENDED), (a) do NOT advance the exponential curve the same way, and (b) surface a typed at-risk state on `project_status` so the picker can show a persistent (not transient) banner even without a user tap. See sync-status-red memory: this is a persistent-bad, not a transient.
- **Orphan data-at-risk has no user-visible escalation** when the project has also never synced. 12 parked unmergeable fragments + 2808 unshared commits = data that exists only on this one device.

## Research — device 2 (aztobt2-ui, peer `db033cd4`) added 2026-07-01

Both phones have `nml` (remote `aztobt2-ui/nml.git`) + `baf` (remote `audioword-ui/baf.git`). baf syncs on both. nml is broken on both, differently. User's framing: "one behind on LAN, both behind WAN."

**Device 2 = the repo OWNER's phone, and it has the real bug.**
- App IS installed on `aztobt2-ui` (installation 137699668, all_repos, not suspended) — `check_app_installed` confirms. So device 2 HAS github access to nml (unlike device 1).
- nml: `local=eb88a56b1e9f remote(tracking)=7c42ae4808fa: walk excluding tracking → 2167`, `remote_refs_present` has origin/main. So fetch worked once; **2167 local commits are unpushed to github.**
- **Every nml commit and every user Sync returns `codes=['BUSY']`** (10 s lock-acquire timeout). On each daemon boot: `drain pushes: ['nml']` → `[sync-trace] fetch begin remote='…nml.git'` **with no completion ever logged**. The WAN drain's push (`_push_repo` = fetch+merge+push under `project_lock`) starts, holds the lock, and never finishes within the process lifetime → everything else starves with BUSY → the 2167-commit divergence never converges.
- The daemon is short-lived (idle-stop 300 s; app relaunches) → the long fetch/merge/push is repeatedly killed mid-flight and restarted from scratch. nml is 2615 commits / 1350 audio files — the diverged pack is large; this is exactly the topic-branch-chunked-push territory (see [[project_topic_branch_push]]), and it's not completing.

**This is the "unknown bug":** a large diverged nml history whose WAN sync (a) never completes in one daemon lifetime and (b) holds `project_lock` so hard that commits/syncs return BUSY and pile up. The github repo stays frozen at 7c42ae while the owner's phone accumulates 2167 unpushed commits.

**LAN is broken too (both phones):** nml is NOT paired/shared (`lan_unshared=0` via "no paired peers", `reject 'nml': not in any peer's shared_projects; shared_anywhere=[]`). The device-to-device pushes in the logs are arrival-sweep attempts that fail with EHOSTUNREACH / ECONNREFUSED / NotGitRepository because the two listeners keep rebinding ports (38703→35851→37123…) and are often down. So the LAN safety net isn't catching nml either.

### Revised root-cause summary
- **Device 1 (aztobt1-sudo):** no app install on `aztobt2-ui` for its token → nml never fetched, push 403 → 24 failures → 22 h WAN backoff. (orphans = advisory red herring, but real data-at-risk.)
- **Device 2 (aztobt2-ui):** HAS access, but WAN push of the 2167-commit divergence is stuck/lock-holding → BUSY storm → never converges. **Fix target.**
- **Both:** LAN not configured for nml, listeners flapping → no LAN convergence.
- **Meta:** nml diverged from its own github remote by thousands of commits, meaning nml's push has been failing to converge for a long time on the owner's device.

### Actions
1. **Unblock device 2 first** (it's the only one with github access → it's the path to get nml onto github). Needs: let the diverged push actually complete. Options to weigh — force the chunked/topic-branch push to run to completion outside the idle-stop window; raise/adjust the idle-stop so a large push isn't killed; or do a one-time assisted push of nml from a desktop clone of aztobt2-ui's working tree.
2. Once github nml is current, device 1's fix (install app on aztobt2-ui OR LAN-share) lets it catch up.
3. Configure LAN pairing + `nml` share both directions so the safety net works and the phones stop flapping.

### SMOKING GUN (device 2)
`server_apk/service.py:715-724` idle-stop loop stops the service when `bound==0 and idle_for>300s`. `idle_for = cp_service.seconds_since_last_touch()` counts ONLY ContentProvider touches (RPC/UI polls) — it is blind to an in-flight WAN fetch/merge/push running in a scheduler thread. Close the UI → polls stop → 300 s later the service self-stops mid-push. On device 2 `lan.allow_sync=False` so there's no FGS/wakelock shielding it either. Result: the nml push is killed before it reaches the resumable `topic-push begin`, so it restarts from `fetch begin` every lifetime and never converges. The chunked/topic push (`repo.py:_push_chunked_to_ref`, [[project_topic_branch_push]]) IS resumable via `refs/remotes/origin/azt-pending-<lang>-<device>` — it just never gets to run.

### THE FIX — "notice this is happening (not just offline) and push it through"
One coherent feature, three layers:

**Layer 1 (primary — stops the killing): in-flight-sync guard on idle-stop.**
Scheduler sets a "sync in flight" flag when it enters `_push_repo`/merge, clears it when done. The `service.py` idle-stop loop adds a third gate: never `stopSelf()` while a sync is in flight (and, on Android, hold a partial wakelock for the duration so Android's own OOM is less likely). This alone lets the resumable chunked push reach `topic-push begin` and make persistent progress.

**Layer 2 (detection + escalation): distinguish "stuck big backlog online" from "no internet."**
Escalate to run-to-completion when ALL of: `is_online_cached` True (NOT offline) + `pending_push` set + the push has been attempted-but-killed across ≥2 lifetimes (persist an "interrupted before topic-push begin" marker in `wan_state.json`/`jobs.json`) OR `wan_unshared` is large and not decreasing. Offline → do nothing (normal wait, radio-friendly). This is the "notice" the user asked for.

**Layer 3 (do whatever it takes): run-to-completion mode.**
On escalation, promote to a WAN foreground service + wakelock (mirror `lan_fgs.py`; FGS-legal reason: "Backing up N pending changes…" notification so the user sees it), keep the process alive, and drive `_push_chunked_to_ref` chunk-after-chunk to completion, bypassing the radio-friendly `wan_backoff` curve. Consider releasing `project_lock` between chunks so local commits can interleave instead of BUSY-failing (secondary — completing the push resolves BUSY anyway).

**Immediate no-tools mitigation for the user (device 2, today):** open the aztcollab app on aztobt2-ui and **leave it in the foreground, phone charging, on wifi.** The UI's ~10 s `project_status` polls keep `idle_for` under 300 s → the service isn't idle-stopped → the 30 s watcher-drain tick keeps retrying, and each resumable chunk advances the remote topic ref, so it should grind the 2167-commit backlog through over time even before any code change. This is the only lever available given no adb/desktop.

### Device-1 real error CONFIRMED (2026-07-01, full sync-trace at 21:16)
Device 1 (aztobt1) WAN push of nml fails with **`NotGitRepository()`** on `git-upload-pack` (fetch) → `push raised: NotGitRepository()` ×11 → `consecutive_failures cap reached` → `drain push 'nml' codes=['PULL_FAILED','PUSH_FAILED']` → wan_backoff 25 fails / 24h. `NotGitRepository` from an HTTPS remote = GitHub returns 404 (repo hidden — no access), which **confirms** the "app not installed on aztobt2-ui for aztobt1's token" diagnosis with the actual exception (it's a 404, not a 403).
- **GAP:** the push loop treats `NotGitRepository` as a `non-network exception` and retries it 11× (holding the lock ~3 min → commits BUSY) before backing off. It's a *permanent* access failure. `diagnose_403` only fires on `_is_http_403`, so this never routes to `APP_NOT_INSTALLED`. Fix: recognize `NotGitRepository`/404-on-fetch as permanent → short-circuit like 403 (emit APP_NOT_INSTALLED/REPO_NOT_AUTHORIZED, skip the 11× retry). Also add it to `_PERMANENT_PUSH_CODES` handling so run-to-completion won't churn on it.
- Device 1 does NOT escalate (Layer 3): the push *completes* (permanent-fail return) in ~3 min before idle-stop, so `mark_push_finished` clears the marker, interrupted_count stays 0. Correct.

### 0.52.23 field result (2026-07-01 21:56–22:22, device 2) — fix LIVE, but MIUI kills through FGS
The 3-layer fix is confirmed running on device 2:
- `reconcile_on_startup: push interrupted mid-flight for ['nml'] (will escalate)` on every boot (Layer 2 ✓)
- `run-to-completion 'nml': escalating (interrupted=2 … 3 … 4 … 5, visits=0)` (Layer 3 ✓)
So detection + escalation work. **But the process is still killed every ~5–6 min with NO `idle-stop:` line** → it's not our idle-stop; it's the OS evicting the `:provider` process **despite the foreground service**. Devices are **Redmi Note 15 Pro+ (MIUI/HyperOS)**, which is notorious for killing even FGS unless the app is whitelisted (Autostart + no battery restriction + locked in recents). The github `fetch begin` never reaches `fetch done`/`fetch failed` before the kill (the `ConnectionReset` storms are all LAN-push to the peer at 192.168.100.32 — cross-subnet noise, NOT github), so fetch+merge never completes → nml still doesn't sync.
- **Implication:** FGS-prevention is necessary but NOT sufficient on MIUI. `visits=0` every boot (never increments) because the process dies mid-visit before the per-visit loop can bump the giveup counter — so it never gives up, but also never finishes.
- **Action (user, no code):** whitelist AZT Collaboration in MIUI — Autostart ON, battery = No restrictions, lock the app in recents. Likely THE thing that lets fetch+merge complete.
- **Deeper fix if whitelist insufficient:** fetch+merge isn't resumable/checkpointable, and it dies before reaching the resumable chunked push. Options: skip the heavy fetch+merge when remote is strictly an ancestor (go straight to chunked push), or cap memory. Prediction (onTrimMemory) can't rescue this — nothing to checkpoint mid-fetch, and MIUI may kill without a trim callback.

### OOM prediction / prevention (device 2)
Both phones now on 0.52.23 (Kivy 2.3.1, fp e711f03f). Device 1 killer = our idle-stop (`idle-stop: idle_for=303s`); device 2 killer = Android eviction (no idle-stop line, lan off → no FGS).
- **Predict:** `ComponentCallbacks2.onTrimMemory(level)` — `TRIM_MEMORY_RUNNING_CRITICAL`/`COMPLETE` = imminent kill; `ActivityManager.getMyMemoryState().importance` (poll: CACHED/BACKGROUND = kill candidate) and `MemoryInfo.lowMemory`. Register via `context.registerComponentCallbacks`.
- **Prevent (better):** an FGS process is near-OOM-immune. Real fix = arm FGS on push-START for a big-backlog project (skip the 2-interruption wait), so the *first* post-boot push is protected. fetch+merge is NOT resumable, so prevention matters more than prediction there.
- Use onTrimMemory as belt-and-suspenders: on CRITICAL mid-push, log it (confirms OOM is the killer) + ensure FGS up.

### Invite auto-accept + no-access surfacing (SHIPPED 0.52.24–0.52.25)
- Auto-accept pending GitHub invite on 404 (`auth.try_accept_repo_invitation`), honest `REPO_NO_ACCESS` (never false "app not installed"), short-circuits the 11× churn.
- A: event-nudge on creds-saved + grant-collaborator. B: cheap re-probe (`GET /repos` permissions.push + invitations) decoupled from push backoff → nudge on flip-to-OK. Codes: REPO_NO_ACCESS/REPO_NOT_AUTHORIZED/APP_NOT_INSTALLED/APP_SUSPENDED/ACCESS_DENIED (not NOT_A_REPO).
- Surfacing: `project_status.last_sync_error` (typed), cleared on success.
- Fallback (0.52.25): `ui.open_url` + `ui.repo_access_popup` (browser → invitations page) when no invite to auto-accept. Peer routes `REPO_NO_ACCESS` here — peer-UI wiring is the peers' part.
- Field test recipe: aztobt2 Grant collaborator → aztobt1 tap Sync (or wait for the 5-min re-probe) → `[sync-trace] 404 → accepted pending invite`.

### Separate defects (NOT part of the above fix — track independently)
- Device 1 silent 22 h backoff on permanent 403/APP_NOT_INSTALLED (surface a persistent reason; don't advance curve on permanent failures).
- `project_status at_risk=0` despite thousands of commits off every remote — at-risk is LAN-convention-only (see [[feedback_sync_status_red_semantics]], [[feedback_lanok_n_is_intentional_friction]]).
- 12 unmergeable orphans on device 1 = real data-at-risk from past failed merges.

## IMPLEMENTED (0.52.21) — all three layers
Daemon-side, no wire-format change, no client floor bump.
- **Layer 1:** `azt_collabd/sync_flight.py` (new) in-memory guard; `_attempt_push` wraps every drain push; `server_apk/service.py` idle loop defers `stopSelf()` while `sync_flight.in_flight()`.
- **Layer 2:** `wan_backoff.py` `push_inflight_since`/`interrupted_count`; `scheduler.reconcile_on_startup` → `note_interrupted_on_startup()`.
- **Layer 3:** `scheduler._run_to_completion` — FGS+WifiLock via `lan_fgs.arm_for_transfer`, loops resumable chunked push bypassing backoff; permanent-failure + `_ESCALATE_MAX_VISITS` giveup valve.

Self-heal path: Layer 1 alone should let device 2's push complete (idle-stop was the killer); Layers 2-3 catch the OOM-kill case within ~2 lifetimes.

### Follow-ups
- **DONE:** user-Sync path (`server.py:_h_project_sync` → `sync_repo`) now wrapped in `sync_flight.guard()`.
- Device 1 (aztobt1-sudo) remains a **separate config issue** (app not installed on aztobt2-ui) — unaffected by this fix; needs the app install or LAN share.
- Layer-3 FGS reuses `lan_fgs.arm_for_transfer` whose notification says "sharing with nearby devices" — misleading for a WAN backup. Left as-is pending decision on generalizing the shared LAN copy.
- Consider surfacing an escalation/at-risk state on `project_status` so the UI shows "backing up large backlog" rather than silence.

## ROOT CAUSE FOUND + FIXED (0.52.28) — hung fetch, not access (2026-07-02)

Full-day db033cd4 (aztobt2-ui, the github-owner phone) log, 0.52.24→0.52.27,
00:00–18:17. This is the real bug; earlier sections narrowed it, this nails it.

**Confirmed the user DID tap Sync** — on device 2, not device 1 (answers the
"would a Sync show" question): two `[sync-rpc] 'nml' … done: codes=['BUSY']` at
17:53:51 and 18:16:35. Both BUSY. That's why device 1's log had no `[sync-rpc]`.

**Access is fine, not the problem:** `[check_app_installed] … installed:True,
all_repos:True` + `[_h_test_github] app_installed=True confirmed=True` (17:40:30).

**The bug = a hung `porcelain.fetch` holding `project_lock`:**
- Every escalated drain: `[sync-trace] fetch begin remote='…nml.git'` with NO
  matching `fetch done`/`fetch failed`. In the 12:40→14:05 lifetime it ran the
  fetch for **85 minutes** and never returned; `idle-stop deferred: WAN sync in
  flight` fired throughout.
- `_FETCH_TIMEOUT_S=60` is via `socket.setdefaulttimeout` = **per-`recv`, not
  wall-clock** → a slow/negotiating fetch never trips it. (My first guess "no
  timeout" was wrong — there is one, it just doesn't bound this.)
- The single fetch call never returns → escalation's `_ESCALATE_MAX_VISITS`
  giveup valve (downstream) is **unreachable**; `visits=0` forever;
  `interrupted_count` climbed 20→54 across 34+ MIUI restarts.
- Fetch holds `project_lock` its whole run → user Sync + commits = BUSY. The
  resumable chunked *push* (the thing that could progress) is never reached.
- Remote (`7c42ae48`) has **never advanced** (no push ever succeeded, 2 phones
  only) → the fetch was pulling nothing useful. Pure overhead that hangs.
- Data is NOT lost: LAN converged both phones to merged HEAD `fc3da9a4819b`,
  `at_risk=0`, `lan_unshared=0`. Only the github backup is frozen (2367 behind).

**FIX (0.52.28, daemon-only, no wire/client change):**
- `repo._ls_remote_main_tip` (new): one bounded `GET info/refs` peek. In
  `_push_step_locked`, when the remote branch tip == our tracking mirror, SKIP
  the fetch and go straight to the resumable chunked push. Confident-equality
  only; any peek failure / missing mirror falls through to the normal fetch (so
  first-ever pushes + genuinely-advanced remotes still reconcile, and 403/404
  access errors still surface through the fetch path unchanged).
- `scheduler._run_to_completion` wall-clock ceiling `_RUN_TO_COMPLETION_DEADLINE_S
  =120s`, checked between iterations → yields `project_lock` after ~one in-flight
  chunk (≤ `_PUSH_TIMEOUT_S`) instead of the whole 8-iter budget. Distinct
  `'yielded'` outcome so a slow-but-transferring visit does NOT count against the
  battery giveup valve. Ends the BUSY starvation.

**Still needs field verify on device 2 after 0.52.28 deploy:** expect
`[sync-trace] fetch skipped: remote tip … == mirror` then `topic-push begin` /
chunk progress, and user Sync no longer BUSY. MIUI whitelist still recommended
(process longevity), but escalation now makes resumable progress per lifetime.

## CONVERGING — fetch-skip confirmed, oversize-blob wall fixed (0.52.29, 2026-07-02)

Both phones on 0.52.28 confirmed the fetch-skip works (`fetch skipped: remote tip
== mirror` → `topic-push begin`, no hang). Device 1 (aztobt1-sudo, which regained
github access) drove real github progress: topic ref `azt-pending-nml-aztobt1-sudo`
advanced `e25c192 → 0a04558` (hundreds of objects, 14 MiB pulled). `main` still at
old tip `7c42ae` — Phase B (tiny FF push of `fc3da9a4` to main) only fires once ALL
reachable objects are on the topic ref; until then a desktop clone shows the old
tree (~257 audio files) — **not data loss**, objects are on the topic ref + both
phones via LAN.

**Final blocker found + fixed (0.52.29):** nml audio blobs ~4.3 MB > 3 MB
`commit_pack_byte_budget`. On a transient 408 at chunk_n=1, `_preseed_oversize_blobs`
refused the >budget blob as terminal `BLOB_EXCEEDS_BUDGET` → 24 h backoff → stuck at
the first oversize file (only advancing on app restart / re-escalation). Proof of
false veto: identical ~4.3 MB packs pushed fine seconds earlier. 0.52.29: (1) push
an atomic oversize blob alone in its own side-ref batch (never refuse); (2) chunk_n=1
bail is transient (`PUSH_FAILED` resume), not terminal 24 h backoff; (3) estimate-
based initial chunk_n (skip the per-lifetime 50→25→12→… 408 walk). See CHANGELOG.

**State: deblock mechanism proven; github filling in bursts; 0.52.29 makes it grind
continuously to completion unattended.** Watch for `origin/main` advancing to
`fc3da9a4` + `topic-branch deleted` (janitor) = fully converged.

## Post-merge divergence wedge found + fixed (0.52.30, 2026-07-03)

Full-day logs from both phones — **still on 0.52.28** (diag `daemon_version: 0.52.28`),
so 0.52.29 was never deployed; the pervasive `BLOB_EXCEEDS_BUDGET` bails are the
pre-0.52.29 behavior. But the logs surfaced a **second, independent blocker that
0.52.29 does not fix**:

Device 1 (aztobt1-sudo) made large real progress (`remaining` 2573 → 861, ~1700
commits on the topic ref). Then a LAN merge moved its HEAD onto merge commit
`3cefc3e0`, and the topic-push **wedged for ~5 h** (`remaining=861`,
`server_topic_tip=913fedc4`, every chunk → `DivergedBranches(913fedc4, 3305a38e…)`).

Root cause (confirmed in code): `_pick_intermediate_sha` walked
`get_walker(include=[tip], exclude=[base])`, which for a merge-commit target yields
commits from BOTH parent lines. It handed back a commit that is an ancestor of the
target but NOT a descendant of the current topic tip → the FF push is rejected with
`DivergedBranches`; the loop (which had no diverge handling — docstring said "can't
happen") halved → re-picked the same DAG → re-diverged → bailed transient → next
drain re-entered the identical wedge, forever.

Fix (0.52.30, daemon-only, no wire change):
1. `_pick_intermediate_sha` walks **first-parent only** → every intermediate is a
   first-parent descendant of base → FF push always valid. If base is off the tip's
   first-parent spine, returns tip directly (still a valid FF). Linear histories
   unchanged.
2. Explicit **bounded `DivergedBranches` handling** in the topic loop: re-anchor on
   the server's authoritative tip (from the exception) and continue; if that tip
   isn't an ancestor of target (HEAD moved), bail transient to rebuild next drain.

Device 2 (aztobt2-ui, db033cd4): separate problem — uplink so weak it 408s even on a
single 4.3 MB commit, so its topic ref never left `(none)`. 0.52.29's pre-shrink
stops it wasting ~25 min/visit on the 50→25→12→6→3 timeout ladder, but the radio is
the limit; expect slow convergence there regardless.

Both phones LAN-converged at `3cefc3e0`, `at_risk=0` throughout — **no data at risk**;
only the github backup is behind. Stack to deploy: **0.52.30** (carries .28 fetch-skip
+ .29 oversize-blob + .30 divergence fix). Watch again for `origin/main → fc3da9a4` +
topic-branch deletion = fully converged.

## 0.52.30 CONFIRMED WORKING on device 1; 0.52.31 fixes device-2 chunk regression (2026-07-03)

Both phones now on 0.52.30. **The wedge fix works:** device 1 (aztobt1-sudo) at 16:47:24
broke the DivergedBranches loop — `topic-push chunk OK (advanced to 9641a2a7)` then steady
one-commit-per-~15s, `remaining` 861 → 860 → 859 → …. Its topic ref held `913fedc4` (on the
merge tip's first-parent spine), so the .30 first-parent picker advanced cleanly. **Overall
convergence is now happening via device 1** — it will grind the ~861 remaining commits onto
the topic ref, FF `main`, and delete the topic branch; device 2 then converges free on fetch.

**But .30's first-parent-only rule regressed device 2** (aztobt2-ui, empty topic ref →
chunk base = old origin/main `7c42ae48`, which is OFF the merge spine): `_pick_intermediate_sha`
returned the tip → estimate `11900 objects, 9.3 GB` → pre-shrink 50→1 → still the whole
9.3 GB (`target=3cefc3e0 chunk_n=1`). Degenerated a fresh ref to one un-chunkable brick.

0.52.31: keep first-parent spine as fast path (device 1 unchanged), fall back to
"n-th oldest commit that descends from base" when base is off-spine (device 2 chunks in
~200 MB steps again, no divergence). Device 2's radio 408s on even 4 MB so it won't finish
its own WAN push, but it doesn't need to — device 1 carries convergence; device 2 gets it on
fetch. Both LAN-converged at `3cefc3e0`, at_risk=0. Deploy stack: **0.52.31** (.28 fetch-skip
+ .29 oversize-blob + .30 divergence fix + .31 off-spine chunk fallback). Still watching for
`origin/main → 3cefc3e0` + topic-branch deletion = fully converged.

## 0.52.31 live on device 1, converging (2026-07-03 ~18:50)

Device 1 (aztobt1-sudo) running 0.52.31 (fingerprint f08b98a3f53e3fd8). Steady FF-clean
topic-push, `remaining` 861 → 656 over ~2 h, ~1 commit/14 s raw (~35 s effective incl.
preseed round-trips + MIUI restart overhead). Each >3 MB file preseeds then `chunk OK`;
restarts resume from server topic tip (no lost progress). origin/main NOT yet FF'd (waits
for topic ref to reach 3cefc3e0). ETA several more connected hours.

Device 2 (aztobt2-ui) still on **0.52.30** — still the pre-.31 whole-tip regression:
preseed enumerated `4869 blobs, 9.28 GB → 3243 batches`, 408 on batch 1, every drain.
Harmless (LAN-converged, at_risk=0; device 1 carries github) but wastes its battery/radio.
Action: push 0.52.31 to device 2 to stop the thrash, OR just let it converge on fetch after
main FFs. Not blocking.

## Plans
1. Confirm with Kent which recovery path for device 2 (assisted desktop push vs. daemon-side fix to let the big push complete). — SUPERSEDED: shipped the daemon-side fix (0.52.21); 0.52.28 fixes the deeper hung-fetch cause.
2. Deliver device-1 deblock (app install on aztobt2-ui / LAN share) — but only meaningful AFTER device 2 gets nml onto github.
3. Set up LAN pairing + nml share on both phones.
4. Decide which candidate code fixes to ship (lock-hold budget, idle-stop-vs-push, backoff classification, at-risk semantics).
