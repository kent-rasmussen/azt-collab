# Daemon fd leak + EMFILE hardening

- **Scope & relationships:** azt-collab/daemon. Field incident 2026-07-10
  (karlap desktop): daemon hit `OSError(24, 'Too many open files')` after ~1
  day uptime. Cascade: LAN listener rejected the phone (peers.json unreadable
  → read as empty allowlist), LAN pushes failed (`SSLError(EMFILE)`),
  `/v1/health` 500'd (`_last_crash_summary` opens crash.log unguarded), which
  made azt's client auto-spawn a new daemon every ~5 s into the held
  `server.lock` for 8+ minutes, and repo-open failures were typed NOT_A_REPO
  and fed 10 consecutive failures into `wan_backoff` (bogus 24 h backoff on
  nml/en). SIGTERM + respawn cleared everything. Related: the healthy respawn
  then ran the 0.54.0 merge that field-verified [[lift_merge_robustness]].
- **Vision / done-criteria:**
  1. **Find and fix the leak** — fd count stays bounded over days of normal
     polling (project_status every ~10 s, drain every 5 min, LAN listener up).
     Prime suspect: dulwich `Repo` objects opened on hot paths (status walks,
     drain, lan-debug snapshot) without `.close()` — each Repo holds pack
     fds/mmaps.
  2. **Health never raises.** `/v1/health` must respond even at zero fds —
     alive-but-degraded beats 500 (a 500 on a lock-holding daemon manufactures
     the spawn storm).
  3. **Spawn storm damping.** Client autospawn should detect "lock held by a
     live instance" / back off instead of retrying every poll.
  4. **EMFILE reads are transient, not semantic.** peers.json read error ≠
     empty allowlist (listener should 503, not reject); repo-open OSError ≠
     NOT_A_REPO (must not advance wan_backoff).
- **Deadline:** 2026-07-15 (before Cameroon; HARD stop 07-17)
- **Waiting on:** Nothing

## Plans

1. ~~Find + fix the leak~~ DONE 0.54.1: un-closed dulwich `Repo`s. Dominant:
   `repo_status_summary` (per ~10 s poll) + LAN listener `open_repository`
   (per phone request; dulwich web never closes backend repos) + the whole
   commit/sync/publish entry-point family. Fixed via
   `repo._track_opened_repos()` thread-local scope on every entry point,
   explicit closes on non-lock sites, and a listener WSGI
   `_repo_closing_middleware` (closes per-request repos on PEP 3333
   `close()`).
2. ~~Guard `_h_health`~~ DONE: `_last_crash_summary` returns `{'unreadable':…}`
   instead of raising; plus dispatch-level catch-all in `_serve` (any handler
   raise → typed JSON 500 `internal_error`, not a socket traceback).
3. ~~Spawn backoff~~ DONE: loopback `_server_alive` treats an HTTPError health
   answer as alive-but-degraded (no respawn, no `server.json` deletion of a
   live daemon — pre-fix the client DELETED it); failed spawn → 60 s cooldown.
4. ~~EMFILE reads transient~~ PARTIAL: `peers.list_peers(strict=True)` +
   listener transient-refusal (unreadable registry ≠ empty allowlist);
   `_get_repo` logs `[repo-open]` on OSError. REMAINING: don't advance
   `wan_backoff` when the underlying failure was an OSError-class repo-open
   (the bogus NOT_A_REPO ×10 → 24 h backoff); auto-init (`porcelain.init`)
   repos in recovery paths still untracked (rare).
5. Kent verification: restart the desktop daemon to load 0.54.1
   (`pkill -f "python -m azt_collabd"` — next azt RPC respawns it), then watch
   fd boundedness over a day with the monitors below.

## Notes

- Kent 2026-07-10: "we need it fixed now."
- **Status 2026-07-10: implemented (0.54.1, uncommitted) + tests in
  `tests/test_fd_hygiene.py`; awaiting Kent's pytest run + a day of fd
  monitoring.**
- Spawn-storm full mechanics (from the second field log): wedged daemon held
  `server.lock` AND answered health with 500 (`_last_crash_summary` raised
  EMFILE) → client read failed-health as dead → DELETED live daemon's
  `server.json` → spawned → new daemon exited on held lock → azt's ~5 s poll
  repeated the cycle 8+ min until SIGTERM.
- fd monitors:
  `for p in $(pgrep -f azt_collabd); do echo "$p $(ls /proc/$p/fd | wc -l)"; done`
  `p=$(pgrep -f azt_collabd | head -n1); ls -l /proc/$p/fd | awk '{print $NF}' | sort | uniq -c | sort -rn | head -25`

## Research
