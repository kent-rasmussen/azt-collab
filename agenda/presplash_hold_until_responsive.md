# Peer UI startup: keep splash until the screen responds

## Field 2026-07-27: 50 s+ load, splash gone, swipe jumped to GitHub

Two distinct findings from one report.

### 0. MEASURED 2026-07-27 17:05 (logcat) — the hold does not hold pixels

Kent: 5 s of splash, then ~56 s of dead UI (61 s total). Log, launch 2
(pid 26390, start 17:05:12.847):

| t | event |
|---|---|
| +1.5 s | `[presplash-hold] holding presplash until release() (watchdog 45s)` |
| +1.8 s | `Start application main loop` |
| +1.8 s | `AZTServiceConnector.ensureBound failed: ClassNotFoundException` |
| +1.8 s | picker renders 3 buttons — **screen built already** |
| +3.5 s | `_probe_server_version` answers (fires TWICE) |
| — | **44 s of log silence** |
| +62 s | `[settings] publish candidate: 'baf'` |
| +74 s | `[presplash-hold] presplash released` |

Launch 1 (pid 26234) instead hit the cap: `watchdog: release() not
called within 45s`, with the load continuing to ~112 s.

**Conclusion: `release()` ran at +74 s while the user saw the splash
vanish at ~5 s.** The hold is not keeping the visible presplash up on
this build — by the time release() runs it is only emitting a log line.
So the premise of this whole item ("keep splash until the screen
responds") is not in force at all, which is why a dead UI was exposed
with nothing covering it. Fix the HOLD before anything about readiness:
find what actually removes the presplash (p4a's own teardown when the
GL surface comes up?) and make the hold own that, or draw our own cover
widget we control. Verify by watching the screen against the
arm/release log lines, not by reasoning about them.

Two contributors visible in the same log, both worth their own
treatment:

- **`AZTServiceConnector` missing from the dex** (both launches). Same
  ClassNotFoundException recorded 2026-07-23 in
  [[sync_status_board]] §8 and attributed there to a bad build — with
  the explicit note that a recurrence after a CLEAN build means
  reopening it as a p4a dep-propagation problem. It also means
  `:provider` never gets bind-priority protection, so Android is free
  to freeze the daemon this UI is waiting on.
- **NSD resolve storm** at 17:05:08–09: the same two peers resolved 8+
  times per second, alternating. Each feeds `_record` and can trigger a
  sweep. Needs a dedupe/debounce at the resolve callback.

### 1. Also true: "ready" is declared before the screen can respond

**Kent's testimony (accepted, and it corrects my first reading): he saw
50 s+ of EXPOSED, unresponsive UI — a page without settings, no
response to touch.** My initial claim was that the 45 s
`HOLD_MAX_S` cap covered most of it and left a ~5 s tail; that was me
defending a model against the observation. Wrong, and the correction
matters because it moves the bug.

If 50 s+ was exposed, the hold either never armed on that launch or —
more likely — **released long before the screen was usable**.
`release()` is called by the app when it believes it is ready, so an
early release is entirely available: the screen can finish its startup
function, declare ready, and *then* spend a minute blocked on the
serialized main-thread RPCs in `refresh()`. That fits the whole report,
including a swipe landing on a widget whose layout hadn't settled, and
it makes the 45 s cap irrelevant to this case.

**Found while reading `refresh()` (2026-07-27) — the release is
already tied to the right thing, and that makes the diagnosis
sharper.** `app.py` ~1818 states: *"Presplash release is tied to
DAEMON-ANSWERED, not to refresh() merely returning"* — it fires once
`get_credentials_status()` returns a real answer (not the cold-start
fallback dict). So the hold can release correctly and EARLY, while the
same main thread then blocks on everything after that point in
`refresh()`, foremost `is_online()` — an RPC whose daemon side runs a
real connectivity probe. That is a fully sufficient explanation for
50 s+ of exposed dead UI with no cap involved.

**Conversion order (the actual work, NOT yet done):**

1. `_refresh_debug_503_state`, `_refresh_cawl_variants_state` — small,
   independent, mechanical `_rpc_then` conversions.
2. The `get_credentials_status()` + `is_online()` pair — the real
   blocker, and the delicate one: everything after it in `refresh()`
   consumes `status`/`online` to set button text, and the
   daemon-answered retry ladder (`_credentials_retry_count`,
   `_CRED_RETRY_MAX`) plus the presplash release both hang off its
   result. Fetch BOTH in one worker, then apply the whole downstream
   block on the main thread, keeping (a) the deliberate split so a
   failing `is_online` doesn't skip the button update, (b) the retry
   schedule, (c) release-on-daemon-answered. Deliberately left for a
   session with room to do it carefully: it is the load path Kent is
   about to test, and a botched restructure breaks the screen outright
   rather than merely slowing it.
3. Only then revisit "ready": with nothing blocking after the release,
   daemon-answered becomes a defensible readiness signal — and if it
   still isn't, tie release to first paint.

**So the fix is not tuning the cap.** "Ready" must mean the screen has
painted AND its blocking work is done — i.e. finish moving
`refresh()`'s RPCs through `_rpc_then` (below), then tie release to
real readiness rather than to reaching the end of a startup function.
Diagnostic to confirm which of the two happened: the log carries
`presplash_hold`'s arm/release lines plus a distinct
`[presplash-hold] watchdog: release() not called within …` warning when
the cap fires. Warning present ⇒ cap hit; release logged early with no
warning ⇒ premature release, which is the hypothesis above.

Which makes the real question **why 50 s**, and there's a named
suspect: `SettingsScreen.refresh()` runs on the Kivy main thread and
still fires several synchronous RPCs. 0.54.86 moved
`_refresh_lan_state` and `_refresh_work_offline_state` off-thread and
explicitly listed the rest as unfixed — the credentials fetch, the
`is_online` probe, `_refresh_cawl_variants_state`,
`_refresh_debug_503_state`. Each blocks for as long as the daemon takes
to answer, and they serialize, so a busy or wedged daemon turns
screen-entry into tens of seconds. Finish that conversion (all of them
through `_rpc_then`) BEFORE touching the splash cap: the cap is a
symptom-hider and raising it would hide more.

Open question if it recurs after that: make the hold *adaptive*
(release on first successful paint) rather than tuning 45 s.

### 2. Swipe-up during load activates "Connect to GitHub"

The primary-action button is bound on **`touch_down`**, on purpose —
the code comment says it fires "before ScrollView has a chance to
claim … so the action triggers regardless of how" the touch arrives.
The cost of that choice is exactly the symptom: a swipe whose finger
*starts* on the button fires the action immediately, with no chance to
become a scroll. It shows up "mostly when the UI was unresponsive"
because that's when a user swipes at a screen that isn't reacting —
and when layout hasn't settled, so the button sits where the list
should be.

**CORRECTED after Kent pushed back** (*"the button is at the top of the
screen, so I'm pretty sure I've not been starting there"*): the widget
whose comment I quoted is `gh_primary_btn` on the GITHUB screen
(`on_press: root.primary_action()`), and Kivy's
`ButtonBehavior.on_touch_down` checks `collide_point`, so a touch
merely *crossing* it cannot fire it. That mechanism is real but is not
what he's hitting — he's being NAVIGATED TO the GitHub page, so the
culprit is the settings screen's "Connect to GitHub" row.

Better-fitting hypothesis: **during load the layout hasn't settled, so
collision boxes aren't where the widgets appear.** Row heights in that
screen are stamped from data by `refresh()` (`_actions_row_base_h`, the
peer-sync box, the gated actions row); until those land, a button can
cover a region far larger or lower than its final strip, and a touch
well below it is genuinely inside it. That predicts the clustering
"mostly when the UI was unresponsive" — that's exactly when the frame
which would fix the layout hasn't run.

If that holds, the fix is NOT a touch-vs-scroll threshold; it's
preventing touches from reaching a half-laid-out screen: the adaptive
presplash hold (release on first real paint rather than the 45 s timer)
plus gating interactive rows on a ready flag. Verify first by logging
`gh_row.pos/size` at the moment of the stray activation — cheap, and it
distinguishes the two hypotheses outright.

- **Scope & relationships:** azt_collab_client/ui (shared helper) +
  azt_collabd/ui/picker_app (server-APK wiring); peers wire the same
  two calls per CLIENT_INTEGRATION.md § 18 "Presplash hold".
  Deliberately NOT restructuring boot work (update probe stays where
  it is for now) per Kent: "Just keep the splash until the screen
  will respond. Nothing fancy."
- **Vision / done-criteria:** no dead-looking window between first
  frame and interactivity on the server-APK picker; recorder wired
  the same way in its own repo; users stop reading loading as
  broken.
- **Deadline:** none
- **Waiting on:** Nothing — shipped 0.54.17 (pending build/verify)

## Plans

Shipped 0.54.17:
- `azt_collab_client/ui/presplash_hold.py` — `hold()` intercepts
  Kivy's first-frame `android.remove_presplash` call (seam:
  kivy/base.py schedules `EventLoop.remove_android_splash` right
  after `EventLoop.start()`); `release()` performs the real removal
  on the Kivy main thread; 45 s watchdog so a failed load path can
  never leave the splash stuck.
- Server-APK picker wired: `hold()` in `picker_app.main()` before
  `app.run()`; `release()` scheduled one frame after `on_start`
  completes.
- Peer seam documented in CLIENT_INTEGRATION.md § 18.

Fixed 0.54.28 (drawer-launch regression): the on_start next-frame
release raced the settings screen's blocking first `refresh()` (cred /
online / project_status RPCs on the main thread) and dropped the splash
onto a frozen UI on a cold-spawning daemon. Now the settings screen
releases at the end of its `_ready` (after refresh), and on_start only
blind-releases on the external/picker-initial path. Watchdog unchanged.

Follow-ups:
- ~~Recorder wiring (2 lines) in azt_recorder~~ **DONE 2026-07-21
  (recorder 1.61.0, pending build):** `hold()` in the
  `__main__` block before `App().run()`; `release()` scheduled
  next-frame in `on_start`, deliberately BEFORE the bootstrap
  schedule so the FIFO queue clears the splash before any
  bootstrap popup.
- The measured boot costs stay as-is by Kent's scope decision; if
  "longer" keeps growing, the next lever is moving the boot-time
  GitHub update probe off the pre-interactive path (~2.3 s online,
  per boot-trace-peer 2026-07-21 07:01 vs 12:43 offline boot).

## Notes

Origin (2026-07-21): "I've noticed the UI load is getting longer,
before the screen is responsive… It's hard to think that the app is
broken, which it's just loading still."

## Research
