# Daemon write durability: does it fsync before it renames?

- **Scope & relationships:** azt-collab/daemon — whatever writes the LIFT into the
  project working tree (submit handling, merge output, post-receive reset).
  - Counterpart of the azt-side fix shipped in azt 1.14.0: `io_put/lift.py` now
    `fsync`s the `.part` before `os.replace`, `fsync`s the directory after, and FAILS
    the save if the fsync fails. On the collab path azt does none of that — it hands the
    staged file to `submit()` and returns ("No replace on this path: the daemon consumed
    the staged file"), so the daemon owns the durability of what actually lands.
- **Vision / done-criteria:** a power loss or crash cannot leave a project's LIFT
  half-written. Either the previous file or the whole new one, on disk, durably.
- **Deadline:** none
- **Waiting on:** Nothing

## The question

`atomic_open_write` exists in the client contract (`docs/rationale/lift_access.md`), so
there is an atomic-write notion. What is not established:

1. Does the daemon **fsync the file before the rename**? Atomic rename gives ATOMICITY,
   not DURABILITY: the rename can reach disk while the new file's data is still in the
   page cache, so a crash leaves the right filename with a missing tail.
2. Does it **fsync the directory after**, so the rename itself survives?
3. What happens if an fsync **fails** (ENOSPC)? azt now fails the save; a daemon that
   logs and proceeds would replace good data with data that isn't on disk.
4. The same three questions for the **other** writers into the working tree: merge
   output, and the post-receive hard reset.

## Why it matters more here than on the azt side

The daemon writes the file that teammates then receive. A half-written LIFT on a peer is
one machine's bad night; a half-written LIFT that gets committed and pushed is everyone's.

Note the azt-side gate does NOT cover this: azt parses the `.part` before `submit`, but
the daemon merges base-aware and writes a DIFFERENT file. See the sibling item
`daemon_parse_check_before_commit.md`.

## Notes

- Filed 2026-08-26, split out of azt's `lift_corruption_rollback.md`. NOT investigated —
  this is a question about azt-collab code, asked from the azt side.

## Research
