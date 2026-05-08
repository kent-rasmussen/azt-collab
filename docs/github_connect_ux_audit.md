# GitHub-connect UX audit

Audit of the path-to-GitHub-connection flow for less-computer-
literate users (SIL field linguists, the suite's target audience).
Captured here so improvements can be planned and prioritised
later — none of these are blocking for v0.28.x ship.

Re-evaluate this list whenever the GitHub flow changes. If you
implement a fix, strike it through (don't delete) so the audit
trail of what was learned stays visible.

## Highest-impact (worth fixing first)

### 1. ~~Manual code-copy step in browser~~ — done

GitHub's device-flow response includes both ``verification_uri``
(bare URL — ``https://github.com/login/device``) and
``verification_uri_complete`` (URL with the code prefilled —
``https://github.com/login/device?user_code=ABCD-1234``). The
current implementation in
``azt_collabd/ui/app.py:GitHubConnectScreen._worker`` uses the
bare URL:

```python
verify_uri = resp.get('verification_uri',
                      'https://github.com/login/device')
```

So the user lands on the code-entry page and has to paste/retype.
Switching to ``verification_uri_complete`` (with bare-URL fallback)
makes the device-page show "Authorize?" directly, code already
filled in.

**One-line fix; eliminates the most error-prone step in the flow.**
``device_flow_start()`` in ``azt_collabd/auth.py`` already returns
the field; just plumb it through.

**Implemented:** ``_worker`` now prefers
``verification_uri_complete`` and falls back to ``verification_uri``
then the bare URL.

### 2. ~~"Connected" message overstates progress~~ — done

After device flow completes, the message reads:

> Connected as {username}. Install the GitHub App so the daemon
> can push your project, then tap Test connection.

For a non-technical user, "Connected" sounds like done — but
without the App installed, every push fails later in confusing
ways. The "Install GitHub App" button appears below, but it's
another step the user has to know to take.

**Restructure as a single "connect and verify" flow with explicit
stages**: Step 1 (Authorize) → Step 2 (Install App) → Step 3
(Verify). Don't show "Connected" until all three are done. Each
step gates the next; the user can't tap past an incomplete step.

Caveat: it's worth tracking what has been done, as far as we can,
in case things don't finish in one attempt. The user needs to make
an account, the internet needs to work; lots of failure possibilities. 
So when we enter this page, the next step in the process is what 
happens (maybe with "Continue" button?)

**Implemented:** ``GitHubConnectScreen`` now renders a 3-step
indicator (1. Authorize → 2. Install GitHub App → 3. Verify
setup) plus a single state-aware "primary" button whose label
matches the current step. Step state is derived from server
flags (``connected`` / ``app_installed`` / ``confirmed``), so a
partial setup that picks back up later resumes from where it
stopped. "Setup complete" replaces "Connected" as the
all-done message.

### 3. ~~No pre-flight explanation~~ — done

Tapping "Connect to GitHub" jumps directly into device flow — code,
browser, polling. A field linguist might not know what GitHub
*is*. Add a brief explanation panel at the top of the connect
screen:

> GitHub is a free service for backing up your project to the
> cloud. You'll need a free account; we'll walk you through setup
> if you don't have one yet.

caveat: it shouldn't jump _directly_ into device flow, except when 
there are no settings. But this pre-flight text means we probably 
should always wait for the user to click "begin" (and tell him 
"click 'begin' when you're ready").

**Implemented:** screen now opens with the pre-flight body text
and never auto-fires the device flow. The primary button label
("Begin" / "Install GitHub App" / "Verify setup") gates each
step; the user always opts in explicitly.

### 4. ~~"Test connection" button doesn't say what it tests~~ — done

The current label sounds like an optional diagnostic; it's
actually the gate that flips ``confirmed=True``. Better label:
"Verify setup"

**Implemented:** both GitHub and GitLab "Test connection"
buttons are now labelled "Verify setup".

### 5. ~~No path for users without a GitHub account~~ — done

If the user doesn't have an account, the device-flow page asks
them to sign in or sign up — but our flow gives no warning that
an account is needed. A pre-flight "Don't have a GitHub account?
Tap here to create one (free)." link with target
``https://github.com/signup`` would prevent the dead-end where
the user lands on GitHub's sign-in page and bails.

Given that we want this process to be failure-tolerant, I hope we
can risk a user bailing on the signup. But it would be good to 
pre-flight instruct the user that signing up if they don't have 
an account will be part of the deal.

**Implemented:** a "Create a GitHub account (free)" link lives
just below the pre-flight panel and opens
``https://github.com/signup`` in the browser. Pre-flight text
also names the account-required precondition.

## Medium-impact

### 6. ~~GitHub vs. GitLab choice is unexplained~~ — done

Two equally-prominent buttons on the settings screen; non-
technical user has no basis for choosing. **Recommendation:** pick
GitHub as default-recommended (more familiar to most users;
better mobile UX), demote GitLab to "Other options" or hide
behind an expander.

Response: no demotion. But simplifying GitHub buttons to only show 
"connect..." OR "Disconnect...", since only one should be 
relevant (i.e., connection settings have been verified or 
not) at a time, and GitLab really is just settings, so simplify 
button to "GitLab"; connection status is shown below.

**Implemented:** SettingsScreen now shows a single state-aware
GitHub button (label flips Connect↔Disconnect from
``credentials_status``) and a single ``GitLab`` button that
opens the GitLab settings form. Connection details for both
hosts remain in the Status block below.


### 7. ~~Disconnect button is one tap from prominent~~ — declined

No "Are you sure?" confirmation. Easy to fat-finger; consequences
(re-auth required, potentially losing project access) are non-
obvious. Wrap in a Yes/No popup before action.

Answer: I personally detest those popups. If we can eliminate the
code pasting, uninstall would be a simple click to fix --given that
uninstall doesn't uninstall the github app from the github account.

**Resolution:** declined per maintainer preference. With #1
landed (no code paste), an accidental Disconnect costs one tap
to redo; the GitHub App on the GitHub account is untouched, so
re-Authorize is the only step needed. No popup added.

### 8. Re-authenticate has the same problem

Both "Re-authenticate" and "Disconnect" are NavBtns, same visual
prominence as "Test connection." For destructive actions, less
prominence is appropriate.

### 9. Device-flow timeout (15 min default) isn't surfaced

If the user starts the flow then sets the phone down for 20 min,
polling silently exhausts. The UI just says "Starting device
flow…" indefinitely. Should show "Code expires in N min"
countdown, plus clearer "Start over" affordance after timeout.

### 10. "Verified" badge is subtle

After successful test, just a parenthetical appended to the
status line: ``Connected as alice (verified).`` For a non-
technical user finishing a multi-step flow, a more prominent
"✓ Setup complete" moment would feel reward-shaped.

## Low-impact / nice-to-have

### 11. Jargon in status messages

"Starting device flow…", "Token rejected by GitHub" are jargon.
Plain-language alternatives ("Setting things up…", "GitHub didn't
accept your sign-in; please try again") would land better.

### 12. OAuth scope grant requires user understanding

GitHub's authorization page (controlled by GitHub) asks "AZT
Collaboration wants to access repositories. Allow?" Some users
will hesitate. A pre-explanation in our popup ("On the next page,
GitHub will ask for permission to access your repositories. This
lets the AZT Collaboration service back up your work.") would set
expectations.

### 13. No "I'm stuck" path

If the user gets confused, there's no help button or "Skip and
use without backup" option. They have to back out via the Back
button. Consider a "Skip for now" option that lets the user use
the peer without GitHub setup, with clear messaging that backup
is disabled until they connect.

## Recommended implementation order

When this comes back into focus:

1. **#1 (``verification_uri_complete``)** — clearest single win.
   ~5 lines of code, no UX redesign. Do first.
2. **#3 (pre-flight explanation)** — small text addition above
   the device-flow box.
3. **#7, #8 (confirmation dialogs)** — wrap Disconnect / Re-auth
   in a Yes/No before action.
4. **#4 (rename "Test connection")** — single string change.
5. **#5 (create-account link)** — single button addition.
6. **#2 (restructure connect screen as Step 1/2/3)** — biggest
   UX investment; needs design pass before coding.
7. **#6 (GitHub vs GitLab choice)** — design call: hide GitLab
   or rephrase the comparison.
8. **#9 (timeout countdown)** — small but adds polish.
9. **#10 (success moment)** — small UX win.
10. **#11, #12 (plain language, scope explanation)** —
    incremental polish.
11. **#13 (skip-for-now path)** — design call: do we want users
    running without backup at all?
