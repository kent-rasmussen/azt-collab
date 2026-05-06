# tests/ — pytest scaffolding for the install/update path

Established v0.28.1. The first automated tests in the suite —
covers `azt_collab_client.ui.update`, `ui.bootstrap`,
`azt_collabd.store` (the new `github.confirmed` lifecycle), and
translation-coverage drift detection. See
`docs/test_plan.md` for the full failure-mode matrix; this
directory implements the **Auto** and **Mocked-Auto** rows.

## Running

From the `azt-collab/` repo root:

```bash
pip install pytest
pytest tests/ -q
```

No CI is configured yet — caller wires up GitHub Actions / Drone /
whatever they prefer. Each test file is independently runnable
(`pytest tests/test_version_tuple.py`).

## What's covered

| File | Plan reference | What it locks down |
|---|---|---|
| `test_version_tuple.py` | §3.3, 3.4, 3.7 | semver-tuple corner cases; no-downgrade invariant |
| `test_translation_coverage.py` | §9.3 | every `_(...)`/`_tr(...)` msgid in source is in the .po (Python AST + KV regex) |
| `test_store_confirmed.py` | §10 (regression for v0.27.0) | `github.confirmed` reset-on-settings-change semantics |
| `test_check_for_update.py` | §1, 3, 10.7 | network failures, missing-asset error path, prerelease/draft filtering |
| `test_bootstrap.py` | §6.1, 7.1, 10.3, 10.4, 10.5 | desktop no-op, idempotence guard, decline memory, package-presence disambiguation |

## What's NOT covered

Manual matrix in `docs/test_plan.md` §8 and §10 — anything that
needs a real Android device, real GitHub, real keystore, or real
network. Run the cold-install-path / stale-peer / stale-server
matrices on Android 8 (lowest supported) and Android 16 (newest,
restricted-settings + verification dance) once per release.

## How the mocks work

- **`urllib.request.urlopen`** is patched per-test with a
  `_FakeResponse` returning canned `release.json` payloads. No
  network access; tests run in milliseconds.
- **jnius** is stubbed in `conftest.py` so import-time references
  resolve. Tests that need install-Intent dispatch patch
  `update._trigger_install` / `_can_install_packages` /
  `_media_store_uri` directly.
- **`Clock.schedule_once`** is patched to run the callback
  inline (`fn(0)`), so the Worker → UI thread marshaling fires
  synchronously inside the test thread. The `inline_clock`
  fixture in `test_bootstrap.py` is the canonical pattern.
- **`kivy.utils.platform`** is patched via the `android` /
  `desktop` fixtures in `conftest.py`. Default is whatever the
  host OS reports, so opt in explicitly when the code path
  is platform-gated.

## Extending

If you add a new translatable string, `test_translation_coverage`
fails until it's in `azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`.
That's the intent.

If you add a new bootstrap state, add a test for the prompt
dispatch path. The shape to copy is in `test_bootstrap.py` —
patch over `_prompt_*` and assert the right one was called.

If you add a new gap in `docs/test_plan.md` §10, write the test
that would catch it before fixing the gap.
