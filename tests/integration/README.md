# Integration measurement harness

Device-required scripts for measuring the cold-start cost-model
(daemon-boot plan §"Phase B + C"). Not run as part of `pytest`; the
unit-test scaffold lives at `tests/test_*.py`.

## What's here

- `measure_boot.sh` — drives a real Android device, captures boot-trace
  lines from logcat across one of four scenarios, and produces
  per-iteration tables + summaries.
- `parse_boot_traces.py` — consumes raw logcat and emits a tab-
  separated timing table + a key-interval summary.

## First-time setup

```bash
chmod +x tests/integration/measure_boot.sh
```

Requires `adb` in PATH and a device with the suite-signed peer +
server APKs already installed and granted the
`org.atoznback.AZT_COLLAB_ACCESS` permission. The peer must be
debuggable (so `adb shell run-as` works) for the prewarm-toggle to
function — release-keystore peers will skip the sentinel and the
script logs a notice.

## Scenarios

| Scenario | What it measures | Answers |
|---|---|---|
| `baseline` | Cold start, prewarm forced off, no doze | Anchor for the others |
| `doze` | Cold start with device in forced doze | Q2: does ContentProvider lazy-spawn work under doze? |
| `prewarm` | Cold start with prewarm() actually running | Q3: how much does pre-warming the daemon save? |
| `doze+prewarm` | Combined worst-case sanity check | Both at once |

## Running

```bash
# 5 iterations of each scenario; default settle 30s; default 5 iterations.
tests/integration/measure_boot.sh org.atoznback.aztrecorder 5 baseline
tests/integration/measure_boot.sh org.atoznback.aztrecorder 5 doze
tests/integration/measure_boot.sh org.atoznback.aztrecorder 5 prewarm
tests/integration/measure_boot.sh org.atoznback.aztrecorder 5 doze+prewarm
```

Outputs land under
`tests/integration/measurements/<scenario>-<timestamp>/`:

- `run-N.log` — raw `[boot-trace-peer]` and `[boot-trace-daemon]`
  lines from logcat.
- `run-N.log.table` — tab-separated, wall-clock-sorted detail.
- `run-N.log.summary` — key-interval summary
  (peer wait until daemon answered, daemon Python boot,
  prewarm overlap window, etc.).

Compare summaries across scenarios to answer Q2 / Q3:

- **Q2**: if `doze`'s "daemon Python boot to dispatcher live"
  is dramatically longer than `baseline`'s, ContentProvider
  lazy-spawn is fighting doze; the Phase B `bindService`
  story (or a foreground-service variant) needs adjustment.
- **Q3**: if `prewarm`'s "peer wait until daemon answered" is
  noticeably shorter than `baseline`'s, prewarming is worth
  shipping for production peers; if it's a wash, B2's
  `bindService` keep-alive likely subsumes the benefit and
  prewarm can stay opt-in for the slow-tablet case only.

## Adding new scenarios

Edit `measure_boot.sh`'s case block where it parses `$SCENARIO` —
keep names hyphen-/underscore-free so the directory layout stays
clean. The parser reads any `[boot-trace-peer]` / `[boot-trace-daemon]`
line, so adding new instrumentation phases in
`azt_collab_client/ui/bootstrap.py` or `server_apk/service.py` flows
through without further harness changes.
