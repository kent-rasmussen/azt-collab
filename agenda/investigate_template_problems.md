# Investigate template problems (language-picker new-project template)

- **Scope & relationships:** azt-collab / client (`azt_collab_client/ui/langpicker.py`) — the
  new-project template flow reached when a user picks a BCP-47 lang tag. **Load-bearing seam:**
  the picker is ours, but the template construction it triggers is *not yet* — `langpicker._on_continue`
  calls `app.new_from_template()` (line 584) which is **host-app code today** (recorder / desktop azt),
  with a planned migration into the daemon ("step 3", per the module docstring). So depending on where
  the bug actually is, this may be an azt-collab item (picker / daemon CAWL setup) OR a sister-app item
  (`new_from_template` construction). Determine which on first triage.
- **Vision / done-criteria:** the specific template breakage is identified and fixed at the right seam;
  a new project created via the language picker comes up with a correct template (LIFT scaffold + CAWL
  image wiring) — not just patched for one language.
- **Deadline:** 2026-07-04 (ASAP, urgent).

## Notes
- Symptom: TBD — user reports "template problems" via the language picker; exact failure not yet stated.
- Picker file: `azt_collab_client/ui/langpicker.py` (KV `_KV_TEMPLATE` is the *UI* layout template, not
  the project template — don't confuse the two). Project template entry point: `app.new_from_template()`.
- Lane note: langpicker + daemon-side CAWL ownership are azt-collab domain; `new_from_template` body
  currently lives in the host app pending the step-3 daemon migration.

## CAWL template — domain question (2026-07-04)
- **CAWL cache / index / per-project `image_repo` / prefetch = azt-collab (daemon) domain** — owned by
  the daemon per `azt_collab_client/docs/rationale/cawl.md`; setup/wiring is ours.
- **Peer-side CAWL scaffolding strip-out is a sister-app task** — tracked separately as
  "CAWL-prefetch Stage B peer strip-out" (azt_recorder/collab). The design seam is ours; the peer edit
  lands in the sister repo.
- **The new-project template itself** (`new_from_template`) is host-app today; the langpicker step-3
  plan is to move the template-download path into the daemon (which would then also be the natural home
  for CAWL-template setup). So: CAWL *mechanism* setup = ours now; template *assembly* = becomes ours
  only after step 3.

## Plans

## Triage result (2026-07-04) — it's a daemon-domain single-sourcing bug

Determined the seam. `new_from_template` is NOT recorder-side and was not the
issue: `pick_project()` (client `__init__.py:337`) spawns the daemon's own picker
subprocess (`python -m azt_collabd projects`); `LangPickerScreen._on_continue` →
`app.new_from_template()` runs in **azt_collabd/ui/picker_app.py** (L1366), which
calls `create_project_from_template` → `POST /v1/projects/from_template` →
`azt_collabd/projects.py::create_from_template` (L538). The recorder just parses
back `AZT_PICK\t<path>\t<langcode>` and `load_lift`s it.

Root cause: `create_from_template` writes the SILCAWL template **verbatim** (only
`_mint_fresh_guids`, L589) — no per-language cleanup. The intended cleaner is the
recorder's peer-side `clean_template` (`lift.py:724`), but it runs only on the
`_pending_vernlang` path (`main.py:7658`); picker-created projects arrive via
`_current_langcode`/authoritative and deliberately do NOT set `_pending_vernlang`
(main.py comment ~8861), so it never fires. Hence every picker-created project
carries full multilingual junk.

Fix = **NOTE filed** (`azt_collab_client/NOTES_TO_DAEMON.md`, REFACTOR item): add
the cleanup to `create_from_template` after `_mint_fresh_guids`, one server-side
place for all peers. Host-chosen rules: lexical-unit → vernlang-only; glosses/
definitions → drop empty only; citation → data-only. Preserve entry+element order.
Peer-side `clean_template` retires once this ships (separate task).

Also incidental: `clean_template` even when it *does* run only walks
citation/definition, never lexical-unit or `<gloss>` — a second reason it couldn't
have produced clean output. Moot once the daemon owns cleanup.

## IMPLEMENTED (0.52.32, 2026-07-04)

Daemon fix shipped: `azt_collabd/projects.py::_clean_template(xml_bytes, vernlang)`, called in
`create_from_template` right after `_mint_fresh_guids` (same bytes→bytes / parse-fail-fallback
shape, stdlib ET to match the sibling). Implements the host-decided rules: lexical-unit →
vernlang-only with no-loss gloss move + empty-headword add; drop empty glosses; leave
definition + citation; full-tag vernlang match; order-preserving; SILCAWL/grammatical-info/
semantic-domain/illustration/trait untouched. NOTE item deleted from NOTES_TO_DAEMON.md;
recorded in CHANGELOG.

Remaining:
- **Verify** on the next picker-created project (fresh clone shows vernlang-only headwords +
  populated glosses, no empty multilingual junk).
- **Peer follow-on** (separate, not azt-collab): retire the recorder's now-dead `clean_template`
  on the picker path; also fix that it only ever walked citation/definition (never lexical-unit
  or `<gloss>`) — moot once the daemon owns cleanup.
- **Deferred (not done):** XXE/billion-laughs hardening — `_clean_template` uses stdlib ET to
  match `_mint_fresh_guids`/`lift_merge`/`atomic_recovery`; a one-off swap here would be
  inconsistent. Track a suite-wide `defusedxml` pass separately if wanted (adds a p4a dep).

## Research
