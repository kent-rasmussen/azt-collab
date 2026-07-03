# Notes to the daemon

**Live queue only.** Outstanding items peers have noticed and want
the `azt_collabd` / server-APK side to fix. Filed here (inside
`azt_collab_client/`) rather than the per-peer CHANGELOG so:

- the symlink propagates them into every sister app's tree
- the daemon team sees them in one canonical place
- the note moves with the package if the canonical home ever
  changes

**When you act on an item, delete it from this file** — the
CHANGELOG is the historical record. This file holds only the
queue.

**Standing rules don't belong here.** Promote them to
`CLAUDE.md` (architecture/rationale) or `CLIENT_INTEGRATION.md`
(peer-facing contract). Shipped fixes live in the relevant
`CHANGELOG`. Anything left here is a live queue item.

---

## Live queue

### INVESTIGATE: merge churn from bulk ASR-draft annotations

The desktop AZT app is gaining a **bulk ASR** stage that writes machine-
transcription drafts into LIFT as `<annotation>` children of the
`<form lang="{analang}-x-audio">` node (the form that already holds the recorded
audio filename) — one annotation per ASR model/lane:

```xml
<form lang="gnd-x-audio">
  <text>dive.wav</text>
  <annotation name="allosaurus"     value="dìve"/>
  <annotation name="whisper-base"   value="dive"/>
  <annotation name="tone-katyayego" value="HL"/>
  <annotation name="md5"            value="<md5 of dive.wav>"/>
</form>
```

At project scale this is ~1700 entries × up to ~8 models = 10k+ small annotation
nodes, written incrementally during a long batch and rewritten whenever audio is
re-recorded (md5 mismatch wipes + rewrites that form's ASR annotations).

Check against the merge path:
1. Sub-element diff granularity — do per-annotation additions merge cleanly, or
   does a touched `<form>`/`<entry>` merge as a blob (spurious conflicts when two
   machines draft different models)?
2. Bulk runs change many entries per session — does the desktop `maybewrite`
   batching cadence interact badly with the daemon's change detection / lock window?
3. md5-mismatch wipe-and-rewrite deletes annotation nodes — confirm deletions
   merge sanely and don't resurrect stale drafts.
4. Worth marking ASR-draft annotations as machine-generated so merge policy can
   treat them as regenerable (prefer-latest / drop-on-conflict) vs hand-merge?

Owner: desktop ASR-split work. Resolve before bulk ASR ships to multi-machine
collab projects.
