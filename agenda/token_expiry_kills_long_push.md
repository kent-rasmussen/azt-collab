# A 401 mid-push must be recoverable, not a permanent failure

- **Scope & relationships:** `azt-collab` — `repo.py` (`_push_chunked_to_ref`,
  `_preseed_oversize_blobs`, `_attempt_push`), `credentials` /
  installation-token refresh, `scheduler._drain_pending_push`'s
  permanent-vs-transient classification. Direct blocker for
  [[merge_aware_chunk_ordering]]: the 4 GB `nml` upload cannot finish inside
  one token lifetime, so it will hit this every hour by construction.
- **Vision / done-criteria:** a multi-hour upload runs to completion
  unattended across as many token expiries as it takes. Nobody taps a button
  to keep it going. A 401 refreshes the installation token and retries the
  batch; only a *refresh* failure (revoked install, wrong repo, no network)
  is permanent.
- **Deadline:** none
- **Waiting on:** Nothing.

## The observation

Field 2026-07-29 11:04, desktop, mid-`nml`:

```
preseed: 1261 blob(s) already seeded on the server will be skipped
preseed: 512 blob(s), 4,053,554,057 bytes → 378 batch(es)
preseed batch 1/378: 1 blob(s) ~23,330 bytes → c74765f4
preseed batch 1 push failed: HTTPUnauthorized('No valid credentials provided')
topic-push: pre-seed did not shrink the unit (status=AUTH_REQUIRED); attempting the oversize push anyway
topic-push raised: HTTPUnauthorized('No valid credentials provided')
ls-remote peek failed: HTTPUnauthorized('No valid credentials provided')
fetch failed with 401 — aborting before push
run-to-completion 'nml' iter=0 codes=['AUTH_REQUIRED']
run-to-completion 'nml': permanent failure, reverting to normal backoff
```

Progress was real and is preserved — 1261 blobs banked, the remaining set
down from 1085 blobs / 8.9 GB / 817 batches to 512 / 4.05 GB / 378. Then the
token aged out and the whole run parked.

## Why this is structural, not bad luck

**GitHub App installation tokens last one hour.** The remaining 4 GB, at the
63 KB/s–270 KB/s this link has actually delivered, is many hours of transfer.
So a token WILL expire mid-run, repeatedly, every time. The current
classification treats that as `AUTH_REQUIRED` → permanent → backoff, so an
unattended machine stops itself hourly and waits for a human.

That is precisely the failure mode the whole sync effort exists to remove
(Kent: *"the ability to leave these people with a functional server that will
be able to get their data online without me or you tweaking and handholding
it"*). Everything else about the push is now resumable and self-healing; this
one classification undoes it.

## Plans

1. **Distinguish "token stale" from "auth is broken".** A 401 on a request
   whose credentials were minted more than ~55 min ago is expiry. A 401 on a
   freshly minted token is a real authorization problem (revoked install,
   repo removed, wrong account) and stays permanent.
2. **Refresh + retry once, in place**, at the point of failure — inside the
   preseed batch loop and the chunk push, so a batch that 401s resumes rather
   than unwinding the whole run. The banked work is already safe; only the
   in-flight batch needs replay.
3. **Consider proactive refresh**: if a push is expected to outlive the token
   (we know the payload size and the observed rate), mint a fresh token before
   starting each batch rather than discovering expiry through a failure.
4. **Log at the point of action** (invariant #15): say "token expired
   mid-transfer — refreshing and retrying batch N", not "auth required".
   The current line sends diagnosis toward credentials configuration, which
   is not the problem.

## Notes

Immediate unblock while this is unbuilt: re-verify GitHub in settings. That
mints a fresh token and fires the drain nudge; banked seeds are kept, so it
resumes rather than restarting.

Kent 2026-07-29 placed this immediately after the in-flight contributor-field
fix, ahead of [[remote_settings_over_lan]].

## Research
