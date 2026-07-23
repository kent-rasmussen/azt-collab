# Peer UI startup: keep splash until the screen responds

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
