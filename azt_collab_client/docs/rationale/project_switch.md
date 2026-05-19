# Project-switch reconciliation rationale

> **Conformity contract** — when peers MUST reload, exact
> `on_resume` shape, what comparison to make — is in
> `CLIENT_INTEGRATION.md` § 14a. This file is the *why*.

The daemon owns `last_project()` (see "Daemon-owned state" in
`CLAUDE.md`). Any RPC path that mutates a project's identity in a
user-visible way — picker submission, future rename, future
delete-then-pick-next — writes the new langcode to
`$AZT_HOME/config.json :: recent.last_langcode` server-side.
Peers polling `last_project()` get the authoritative answer.

What the daemon CANNOT do: push that change to the peer's
loaded UI. The peer's view is built from the LIFT bytes plus
peer-side caches (entry list, scroll position, open panels,
filter state); only the peer can tear that down and rebuild
against the new project's bytes. There's no Android channel
the daemon can use to invoke a method on a Kivy App that
happens to be in the background.

So the contract has to live peer-side. The peer's `on_resume`
is the natural hook: Android raises it whenever the peer
Activity comes back to the foreground after another Activity
(picker, daemon settings UI, other app) took focus. The peer
reads `last_project()`, compares to its in-memory
`_current_langcode`, and reloads if they differ. Same code
path as the initial project-load; just a different trigger.

## Why not poll on every cache_status tick

The cache_status banner already polls at 1 Hz. Adding "and
also reconcile project langcode" to that tick would work
mechanically, but the user-facing "switch happened" gesture
is bounded by Activity lifecycle — Android suspends the
peer when another Activity takes the foreground, resumes
when it leaves. Hooking lifecycle is cheaper than polling,
and the daemon-side state is consistent by the time
on_resume fires (the picker exit + last_project write are
both on the picker Activity's main thread, completing
before the picker finishes).

A misbehaving peer that polls works too, just with more
wakeups. on_resume is the *right* hook; polling is the
permissible degradation.

## Why the two-stage migration (peer contract first, daemon button second)

Same shape as CAWL Stage A / Stage B. Until peers ship the
`on_resume` hook, a daemon-side "Switch project" button
silently fails to take effect on resume — user lands back in
the previous project. Documenting the contract first lets
peer maintainers adopt the hook in their next release; the
daemon-side button lights up cleanly once enough peers are
on the new contract.

Peers that miss the contract get exactly the old behaviour —
the button is a no-op on resume but not destructive. The
daemon doesn't lose data; the user just sees the previous
project and learns to launch the picker the old way.
