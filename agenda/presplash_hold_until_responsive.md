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

Follow-ups:
- Recorder wiring (2 lines) in azt_recorder — peer-side change,
  rides the recorder's own release cadence.
- The measured boot costs stay as-is by Kent's scope decision; if
  "longer" keeps growing, the next lever is moving the boot-time
  GitHub update probe off the pre-interactive path (~2.3 s online,
  per boot-trace-peer 2026-07-21 07:01 vs 12:43 offline boot).

## Notes

Origin (2026-07-21): "I've noticed the UI load is getting longer,
before the screen is responsive… It's hard to think that the app is
broken, which it's just loading still."

## Research
