# Commit identity rationale — contributor + device_name

> **Conformity contract** — `set_contributor` / `set_device_name`
> API + refusal-status handling — is in `CLIENT_INTEGRATION.md`
> § 12. This file is the *why*.

Git author = NAME + EMAIL. NAME is the user's display name
verbatim (GitHub groups commits by NAME across devices);
EMAIL is `<safe_name>@<safe_device>` (`git log --format='%ae'`
differentiates the same human across phone/tablet/laptop, the
email is non-routable — it's an identifier). One composed
string like "Marie Dubois (tablet)" would defeat GitHub's
author-aggregation; two fields leverages git's native split.

Daemon-owned, no peer pass-through (since 0.40). Pre-0.40
peers passed contributor on every RPC and won by default
over the daemon's stored value — so the user typing their name
in the daemon UI didn't help if the peer was hard-coding a
placeholder. Removed the wire surface; unset state now
surfaces explicitly as `S.CONTRIBUTOR_UNSET`. `device_name`
auto-populates from `Settings.Global.DEVICE_NAME` /
`socket.gethostname()` on first read; empty stored value
re-triggers detection. The `@unknown-device` last-resort is
the explicit sentinel for de-Googled Android etc. — visibly a
placeholder rather than a silent `'Recorder'`-style
substitution.
