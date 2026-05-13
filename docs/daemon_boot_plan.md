# Daemon-boot + connection-wait — status & residual work

Background: peer cold-start on Android originally waited up to 60
seconds for the daemon to become reachable on slow tablets. The
plan was sequenced as Phase A (peer-side), Phase B (server APK +
peer Java glue), Phase C (daemon-side reductions).

## What shipped

### Phase A — `azt_collab_client 0.32.x`
- Adaptive backoff in the warmup retry loop (`0.2 / 0.4 / 0.8 /
  1.6 / 2.0…s` schedule) replaces the fixed 2 s interval.
- `ServerUnavailable.kind` field with `daemon_not_ready /
  null_bundle / server_apk_not_installed / http_5xx /
  transport_error`. Threaded through `check_server_compat → compat['kind']`.
- Connecting popup shows retry count + elapsed time + last-error
  kind, refreshed each retry. Unresponsive popup includes
  installed server APK versionName.
- Fail-fast on 3 consecutive `null_bundle` responses (~0.6 s)
  rather than waiting the full warmup.

### Phase B2 — `azt_collab_client 0.33.x` + `azt_collabd 0.33.0`
- Peer-side `AZTServiceConnector.java` holds a `bindService`
  with `BIND_AUTO_CREATE | BIND_ABOVE_CLIENT` for the full peer
  lifetime, defeating Android 15's app freezer.
- Server APK's `AZTServiceProviderhost.onCreate` self-delivers
  `onStartCommand` so `bindService` alone bootstraps Python.
  Side-steps Android 12+'s background-service-start restriction
  that was blocking the original `Provider.onCreate → startService`
  path.
- `bootstrap.prewarm()` peer-callable hook pre-binds from the
  peer's `App.build()` so daemon Python loads in parallel with
  Kivy init.
- Boot-trace instrumentation on both peer and daemon
  (`[boot-trace-peer/daemon] phase=… t=…`) plus
  `tests/integration/measure_boot.sh` + `parse_boot_traces.py`
  harness.

### Measured outcomes (R500-class slow tablet, post-Phase-B2)

| Interval | Steady-state | First cold start |
|---|---|---|
| Peer wait until daemon answered | ~50–60 ms | ~100 ms |
| Daemon Python boot to dispatcher live | ~600 ms | ~1.1 s |
| `import azt_collabd` cost | ~120 ms | ~260 ms |
| `reconcile_on_startup` cost | ~150 ms | ~150 ms |
| Prewarm overlap window | ~1.9 s | — |

Doze runs are statistically indistinguishable from baseline —
freezer was the issue, not doze.

## What's left

### B1. Provider state field in `daemon_not_ready` 503 — not shipped

Phase B2 dropped the user-visible wait to ~50 ms steady-state, so
B1's "show python_loading vs callbacks_pending in the popup"
delivers diminishing returns — the user barely sees the
connecting popup at all anymore. Worth doing only if a
regression makes the wait visible again, OR if a future
device class hits a different freezer/bind issue and we need
phase-grained diagnostics in the field.

When/if shipped: add an enum `DaemonState` with `volatile static`
slot in `AZTServiceProviderhost`, transition through phases via
a `markPhase(String)` JNI call from `service.py`, embed `state`
in the 503 body, surface in the connecting popup. Detail is in
git history for this file (commit before 2026-05-09 cleanup) if
needed.

### Phase C. Daemon-side reductions — not worth shipping

Goal was to lazy-import `azt_collabd` submodules so dulwich /
ssl / scheduler don't load until first handler use. The
measurement showed `import azt_collabd` is ~120 ms on the slow
tablet — invisible behind the bind+overlap that already gives
sub-100 ms peer wait. The 120 ms savings would not be felt by
users.

Don't ship Phase C unless a future regression makes the import
the long pole again, OR a new peer architecture needs the
daemon to start faster from a different entry point.

## Open question (deferred Phase A loose end)

Loopback transport's `ServerUnavailable.kind` defaults to `''`
rather than the values used by the Android transport
(`daemon_not_ready` / `null_bundle` / etc.). Loopback is
desktop-only where boot is fast and retries are bounded by
spawn-and-test, so this hasn't bitten anyone. Address only if a
desktop user hits a long-wait symptom that benefits from the
diagnostic surface.

## Where the harness data lives

`tests/integration/measurements/<scenario>-<timestamp>/` —
captured 2026-05-09 on the R500 tablet. Reference for "how the
suite performs on slow tablets" going forward; future regressions
should be caught by re-running `measure_boot.sh` and comparing
against these numbers.
