# `submit_file` needs a bounded timeout (300 s reads as a hang)

- **Scope & relationships:** azt-collab — `azt_collab_client/rpc.py` (`call`'s
  `timeout=300` default) and the write-path wrappers in
  `azt_collab_client/__init__.py` (`submit_file`, ~line 3105). Split out of
  `azt/agenda/busy_is_not_unavailable.md` on 2026-07-30 (Kent: "if part of that is
  for another team, split it off and hand it to them"), whose azt half — BUSY retry
  + honest wording — shipped the same day. Filed into the hand-off queue at
  `azt_collab_client/NOTES_TO_DAEMON.md`. Same shape as the first item in that
  queue (recorder's share-offer popup blocking on a main-thread RPC), so one
  argument serves both peers. Neighbour: `daemon_lock_across_network_io.md` — fewer
  long holds means fewer chances to hit this, but it doesn't bound the call.
- **Symptom (field 2026-07-29, desktop azt):** a save hit a held `project_lock`
  and the user was left with no usable interface for minutes, then had to restart.
- **Vision / done-criteria:** a peer can choose to fail a write in seconds and fall
  back to disk, instead of blocking for up to five minutes. Done when
  `submit_file` accepts a timeout (or a lowerable default exists) and desktop azt
  sets a short one on its save path.
- **Deadline:** none
- **Waiting on:** Nothing — it's the daemon/client team's to schedule.

## Why the peer can't fix it

`rpc.call(method, path, body=None, timeout=300)` (`rpc.py:19`) is the only place
the timeout is set, and `submit_file(langcode, rel_path, staged_path, base_sha,
message='')` doesn't take one to pass down. A save is on the editor's UI thread by
nature (serialize the whole file, then submit), so an unbounded RPC there IS a
frozen app — and the user can't even choose what to do next, which is worse than
an unsaved file.

azt already did what it could on its side (1.13.x): BUSY is retried 3× 0.7 s and
never reported as "server unavailable", which covers the common one-second
post-receive absorb. Nothing on the peer side can make a five-minute call return.

## The ask (as filed in NOTES_TO_DAEMON.md)

1. `timeout=` on `submit_file`, passed through to `rpc.call` — ideally on the
   other write-path wrappers too.
2. Or a module-level default a peer can lower once at startup, if a per-call
   argument is unwelcome.
3. Either way keep the BUSY answer distinguishable from a timeout: they want
   different peer behaviour (retry vs. fall back to disk).

## Then, on the azt side

Desktop lowers its save timeout on sight of the argument — a small change in
`CollabSession.submit`. The remaining azt-lane work (moving the save off the UI
thread entirely) stays in `busy_is_not_unavailable.md`; it's a real refactor,
because the fallback path ends in the caller's `os.replace`
(`azt/io_put/lift.py:1262`), not in the session.

## Notes
- Grooming observation while filing: the "commit metadata since a sha" ASK in
  NOTES_TO_DAEMON looks satisfied (daemon 0.54.92 `changes_since`; desktop renders
  it in `changes_summary`). Left in place — that queue's entries are the daemon
  team's to delete.

## Research
