# Security policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security
vulnerabilities. Email the maintainer directly at kent_rasmussen@sil.org
or open a private security advisory through the GitHub repository's 
"Security" tab.

We aim to respond within seven days. For coordinated disclosure
involving the suite signing key or a vulnerability that lets a
malicious peer escalate against the server APK, please give us
30 days before public disclosure so we can ship a fix and bump
`MIN_CLIENT_VERSION` / `MIN_SERVER_VERSION` floors across the suite.

## Scope

In scope:

- The daemon (`azt_collabd`) and its HTTP / ContentProvider
  transports.
- The thin client (`azt_collab_client`).
- The server APK packaging (`server_apk/`).
- The Android peer integration recipes documented in CLAUDE.md.

Out of scope:

- Vulnerabilities in upstream dependencies (Kivy, dulwich, jnius,
  buildozer, python-for-android). Please report those upstream;
  if you need help routing, we're happy to relay.
- Issues that require a malicious peer APK that wasn't signed
  with the suite keystore — the
  `org.atoznback.AZT_COLLAB_ACCESS` signature-level permission is
  the authentication boundary for the local suite. A non-suite-
  signed APK has no more access than any other unrelated
  installed app.

## What's in the repo vs. what's not

Public:

- The SHA-256 fingerprint of the suite signing certificate
  (`android/SUITE_FINGERPRINT`). This is a *public commitment* to
  the signing identity and is meant to be visible — it's the
  reference value every peer APK build is verified against.
- The GitHub App `client_id`. The suite uses GitHub's OAuth device
  flow, which by design requires only the public `client_id`;
  there is no `client_secret` anywhere in this repo or in any
  released artifact.

Never published:

- The suite signing keystore (`*.keystore`) and its passwords.
- User credentials (`credentials.json`, GitHub access tokens,
  GitLab PATs). These live exclusively under the user's
  `$AZT_HOME` and never leave the device.
- Per-user project state and LIFT XML (linguistic data lives only
  on user devices and the user's own git remotes).
