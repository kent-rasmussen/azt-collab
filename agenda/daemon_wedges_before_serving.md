# Start-of-day log snapshot blocks daemon boot before the socket binds

- **Scope & relationships:** `azt-collab` — `azt_collabd/server.py`
  `install_stdio_tee` (6899–6910), `_dump_lan_debug_snapshot` (5379–5411),
  `_h_lan_debug` (the per-project payload, incl. an ancestor count),
  `run()` ordering (6954–7097), and `loopback.py:253–286` (the
  `SERVICE_WEDGED` branch that then refuses to respawn). Related to
  [[daemon_activity_signal]] ("busy ≠ dead" once serving) and to
  [[desktop_collab_unavailable_visible]] (the peer-side silence this
  produced). Supersedes the parked *"Daemon boot-phase state in 503"*.
- **Vision / done-criteria:** No diagnostic can prevent the daemon from
  serving. `/v1/health` answers from the moment the port accepts. The
  start-of-day snapshot still gets written, but off the boot path and
  bounded. Both field machines recover without hand surgery.
- **Deadline:** none
- **Waiting on:** Nothing.

## What actually happens

`run()` calls `maybe_install_stdio_tee()` at **6965** — before the
single-instance flock (6969), before the socket bind (6977), before
`server.json` (6986). Inside it:

```python
if pre_size == 0:          # server.py:6899
    _dump_lan_debug_snapshot()
```

`pre_size == 0` means **today's per-day log file is brand new** — i.e.
this is the first daemon start of a new day. `_dump_lan_debug_snapshot`
then calls `_h_lan_debug(langcode, {})` for every registered project,
which computes HEAD branch/SHA, **the ancestor count**, origin URL,
tracking ref, all local branches and all remote refs. The ancestor count
is a full history walk, reading loose objects through dulwich.

On a large repo that walk is very slow — and it is happening with the
socket not yet bound.

## Why this looked like "we broke something"

**Nothing was committed that broke it.** The trigger is the *day
rollover*: this path fires once per day, on the first daemon start after
a fresh log file is opened. Both machines crossed midnight into
2026-07-31 and both wedged on the first boot of the day, on a repo whose
history had grown past the point where the walk completes in reasonable
time. Yesterday's identical code was fine because yesterday's snapshot
ran against a smaller history — and, on any later boot the same day, does
not run at all.

## Field evidence (2026-07-31, both of Kent's machines)

- azt: `WARNING collab server unavailable or project baf not registered:
  legacy this session` — nothing shown to the user.
- Machine 1 (Windows), server UI started explicitly: `WinError 10061`
  (`WSAECONNREFUSED`). Consistent: the hang is *before* the bind, so
  nothing is listening; the client is dialling a stale `server.json`.
- Machine 2: `SERVICE_WEDGED`, `pid 24280 running but not answering` —
  a stale `server.json` naming a pid that is alive (plausibly a hung
  boot from an earlier autospawn) but never bound.
- `python -m azt_collabd` in the foreground: last console line is
  `[lan-debug] snapshot start: 1 project(s)`, then nothing. That is
  `server.py:5398` — the line immediately before the per-project loop.
- Today's log contains a dulwich stack: `objects.py:718`
  (`ShaFile.from_path` → `with GitFile(path, "rb")`) into `file.py:145`
  (the bare `open()`), under a caller passing
  `max_size=self.loose_object_size_limit`. Line numbers verified against
  the installed dulwich. Consistent with the loose-object reads the
  ancestor walk performs; the exception type was eaten by escape codes.

Because the hang precedes the flock guard, repeated autospawn attempts
can each stall in the same place and pile up hung processes.

Chicken-and-egg worth noting: the 0.55.147 maintenance repack — which
would collapse the loose objects making this walk slow — runs on the
scheduler thread, which never starts because boot is blocked here.

## Two things that made this hard to see

1. **`listening on …` (6989) is printed before `serve_forever` (7097)** —
   CLAUDE.md invariant #15, announce at the point of action. Not the
   cause here, but it is why a daemon that gets further than this one
   still reads as "started fine". Fix it in the same pass.
2. **The daemon log is ANSI-corrupted.** The `35m` fragments are
   `\x1b[35m` — Python 3.13 colourises tracebacks and the colour is going
   into the file. It ate the exception type, which is the one thing
   needed to finish the dulwich half.

## Immediate recovery (no code change)

The snapshot only fires when today's log file is empty. Appending any
byte to it before starting the daemon skips the whole path:

```
echo x >> <$AZT_HOME>/daemon-<peer>-2026-07-31_log.txt
```

## Plans

**Status 2026-07-31.** Shipped: 1, 4, 5, 6 in 0.55.173; 3 in 0.55.173 and
then properly in 0.55.174 (env var only covered client-spawned daemons —
the ANSI strip in `_StdioTee.write` covers every launch path). 0.55.175
added `$AZT_HOME/server_crippled`, a hand-created boot refusal for
reproducing "no daemon" on demand while working the client side.

**Live-verified 13:27:58** on a desktop restart: `serving on` at `,875`,
first boot task (`publish-reconcile`) at `,895` — serving is up 20 ms
before boot work begins, which is the whole point of the change.

**NOT yet verified: the actual outage path.** `pre_size == 0` was false on
that run, so the start-of-day snapshot never fired and neither did the
dulwich failure. Forcing it = stop daemon, move today's log aside, start
daemon. Otherwise it next fires at the 2026-08-01 rollover.

Remaining: 2 (bound the ancestor walk), 7 (the dulwich `open()`), the
Android divergence below, and the new suspect immediately below.

**New suspect (2026-07-31, late).** Kent confirms there was **no log at
all** from the hung run — not a corrupted one, none. Since the tee installs
before the bind, a hung daemon should always leave a file. That points at
`maybe_install_stdio_tee` swallowing its own failure: if `install_stdio_tee`
raises (permissions, disk, bad `$AZT_HOME`), the error goes to
`sys.__stderr__`, which is `DEVNULL` for a client-autospawned child. Result:
a running daemon with no on-disk capture and no indication anywhere. Same
"silent exactly when it matters" class as the wedge itself. Worth checking
before trusting any future boot log.

1. **Get the snapshot off the boot path.** It is a diagnostic; it must
   never gate serving. Run it on a background thread after
   `serve_forever()` is up.
2. **Bound it.** An ancestor count per project is unbounded work in a
   log line. Cap it, or drop the count in favour of something O(1).
3. **`PYTHON_COLORS=0` / `NO_COLOR=1` in `build_spawn_env`** so daemon
   logs are legible.
4. **Serve immediately** — `serve_forever()` before the five startup
   steps at 7007–7077, which are the *next* thing to block boot once
   this one is fixed. Non-health endpoints can 503 with a boot phase.
5. **Wrap `reconcile_on_startup()` (7007)** — the one startup step with
   no `try/except`; an exception there is fatal where others are logged.
6. **Fix the 6989 wording** per invariant #15.
7. **Then** chase the dulwich `open()` failure with a readable trace.
   Do not guess it — get the trace.

## Notes

Added 2026-07-31 at #1, mid-session, from a live two-machine outage.
Kent flies 2026-08-01 14:00, so recovery on those two machines outranks
the structural fix.

First diagnosis in this session misattributed the block to
`diagnose_and_repair_registry_on_startup` (7047) on the strength of the
"snapshot" wording; the console line Kent supplied is from a different
snapshot, upstream of the bind. The distinction is what explains the
10061.

## Research
