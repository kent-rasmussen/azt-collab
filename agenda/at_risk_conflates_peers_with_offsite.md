# at_risk counts peer copies as safety; off-site is not the same thing

- **Scope & relationships:** `azt-collab` — `repo._at_risk` and the sync
  indicator that renders it. Related: [[merge_aware_chunk_ordering]] (why
  work sits un-pushed for long stretches) and the LANOK +N friction
  decision, which deliberately surfaces "no github backup" for LAN-only
  projects — this item is the same concern for projects that HAVE a
  remote but are far behind it.
- **Vision / done-criteria:** the indicator distinguishes "every copy is
  in this room" from "a copy is off-site". A user glancing at it can tell
  whether losing the room loses the data. Whatever the resolution, it
  must not cry wolf during the routine minutes after a commit — the
  reason peer coverage was allowed to count in the first place.
- **Deadline:** none
- **Waiting on:** Kent's call on the question below. Do not implement
  before that: what counts as "safe" is a risk judgement, not an
  engineering one.

## The observation

Field 2026-07-28, desktop, all evening:

```
[wan-unshared] nml … → 816
[lan-unshared] nml … peers=6 … → 0
[at-risk]      nml … excludes=8 (peers=6 …) → 0
```

`at_risk` read **0** while 816 commits (nml) plus 448 (baf) existed
nowhere but on devices — because six paired peers held copies. That is
the documented definition (commits on neither channel), and by it the
number was correct.

But every one of those six devices was on one table, in one room, on one
hotspot. A theft, a fire, a power event, or a bag left in a taxi loses
all six copies simultaneously. The indicator that exists to answer "is my
data safe" reported "yes" for a state where nothing was off-site.

Kent's framing for the whole sync effort — *"the ability to leave these
people with a functional server that will be able to get their data
online without me or you tweaking and handholding it"* — makes this
sharper: the people left behind will read this indicator. If it says 0
while 1,264 commits are one accident from gone, it is teaching them not
to worry at exactly the wrong moment.

## The question for Kent

Peer redundancy is real protection against the common case (one device
lost, dropped, wiped). Off-site is protection against the correlated
case. Options, roughly in increasing intrusiveness:

1. **Leave it.** `at_risk` keeps its current meaning; the "N to go"
   github figure is already visible next to it, so the information is on
   screen for anyone who reads both.
2. **Add a separate reading** — e.g. "off-site: N behind" — alongside
   `at_risk`, changing nothing about existing semantics.
3. **Weight the channels**: peers count for less than github, so a
   project entirely un-pushed never reads a clean 0 however many peers
   hold it.
4. **Time-decay peer coverage**: peer copies count fully for the first
   hours, then decay — encoding "we meant to get this off-site by now."

(2) looks least disruptive and most honest; (3) risks re-creating the
LANOK-style permanent friction that was accepted deliberately for
LAN-only projects but might be noise for projects that are merely
behind. Not my call.

## Notes

The definition itself is not a bug — it does what it says. This is about
whether what it says is what a user in a workshop needs to know.

## Research
