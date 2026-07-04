# p4a upgrade analysis — moving the suite to v2026.05.09

> Question: should we pin `python-for-android` to **v2026.05.09**
> (the latest tagged release, May 10 2026) instead of `master`?

This is a research-only document. No code or build configuration
has been changed. It compares the suite's current pin
(`p4a.branch = master`, cached clone from ~March 2026) against
v2026.05.09, audits the three recipe overrides and the
build hook, and lists what would change.

---

## 1. Current state (what we ship today)

### Pin in every suite `buildozer.spec`

All AZT-suite apps share these build-tooling lines (from
`server_apk/buildozer.spec` and per `.buildozer/CLAUDE.md`):

```
p4a.branch        = master
p4a.hook          = /home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py
p4a.local_recipes = /home/kentr/bin/raspy/buildozer_tweaks/recipes
android.ndk       = 29
android.api       = 36
android.minapi    = 26
```

### Cached p4a snapshot

`/home/kentr/bin/AZT/.buildozer/android/platform/python-for-android/`
is a real `git clone` of `https://github.com/kivy/python-for-android`,
checked out at master, frozen at commit
**`957a3e5f8c270f7aa648ba185e5a68c1077a798d`**
(clone log timestamp ≈ March 2026 — buildozer reuses the clone
between builds and does not refresh on every `buildozer android
debug`, so the cached HEAD is the *clone date* HEAD, not "today's
master").

`pythonforandroid/__init__.py` reads `__version__ = '2024.01.21'`
because that's the last release tag merged into master at clone
time. The actual code is master from March 2026 — somewhere
between v2024.01.21 and v2026.05.09.

### Three recipe overrides in `/home/kentr/bin/raspy/buildozer_tweaks/recipes/`

All three are documented in `.buildozer/CLAUDE.md`. Summary of
what each one patches and why:

| Override | Targets | Reason |
|---|---|---|
| `recipes/sdl2/__init__.py` | `SDL_androidsensor.c`: replaces `ALooper_pollAll(timeout=0, ...)` with `ALooper_pollOnce(...)` | NDK r29 removed `ALooper_pollAll`. SDL2 ≥ 2.30.5-ish fixed it upstream; older SDL2 versions still ship the broken call. |
| `recipes/sdl2_ttf/__init__.py` | Bundled harfbuzz `Android.mk`: adds `-DHB_NO_PRAGMA_GCC_DIAGNOSTIC_ERROR -Wno-error=cast-function-type-strict` | SDL2_ttf 2.20.2 bundles an old harfbuzz; `hb.hh` has `#pragma GCC diagnostic error "-Wcast-function-type"` and `hb-ft.cc` casts `void(*)(FT_Face)` → `FT_Generic_Finalizer`. NDK r29 clang escalates `-Wcast-function-type-strict` to an error and the pragma binds it. |
| `recipes/kivy/__init__.py` | (a) `setup.py`: gates `merge(flags, sdl2_flags)` on `not kivy_sdl2_path`. (b) `get_recipe_env`: appends `-Wno-error=incompatible-function-pointer-types` to CFLAGS. (c) `get_recipe_env`: clears `PKG_CONFIG_*` env. | (a) Host pkg-config leaks `/usr/include` into cross-compile. (b) Kivy 2.3.0 `cgl_gl.c` assigns `glShaderSource` with `const GLchar **` where OpenGL ES 3 declares `const GLchar *const *`. (c) Belt-and-braces against host pkg-config repopulating from defaults. |

All three are idempotent and log a `[<name>-patch] pattern not
found, skipping` line if the upstream source has drifted past the
anchor string — which is exactly the failure mode an upgrade
might trip.

### `p4a_hook.py`

Still doing real work post-upgrade:

- **`before_apk_build` → `_fix_libpython_symlink`** — creates
  `libpython3.X.so` → `libpythonbin.so` so kivy and other recipes
  linking against `-lpython3.X` can find the library. Detects the
  Python version from `Include/patchlevel.h`. Independent of p4a
  version.
- **`before_apk_build` → `_patch_harfbuzz_werror` and
  `_patch_kivy_setup_host_headers`** — dead code, kept as
  harmless backups. The recipe overrides (above) actually deliver
  these patches; the hook variants glob the build tree at the
  wrong phase and silently no-op.
- **`before_apk_assemble` → 5 manifest injection helpers** —
  injects `<provider>`, `<service>`, `PICK_PROJECT` intent-filter,
  `SuiteSelfReplaceReceiver`, and `BundleResetReceiver` (server
  APK only). Independent of p4a version — operates on the rendered
  `AndroidManifest.xml` after p4a's template fills it in.

### Patches we have NOT taken on

`patch_p4a.sh` (a sed-based script that used to patch the in-tree
kivy recipe) is in the buildozer_tweaks directory but obsoleted —
all three of its concerns are now in `recipes/kivy/__init__.py`.

---

## 2. What v2026.05.09 actually is

Released **May 10 2026**, tag name **`v2026.05.09`** (note the
double-zero `05`; the user's "2026.5.9" is the same release).
Twenty-seven commits past the develop branch's previous head,
with prior release **v2024.01.21** (Jan 2024). So this tag
represents ~16 months of accumulated develop-branch work, then a
sync of master + a release tag.

Headline changes relevant to the suite:

### Dependency bumps (from upstream release notes)

| Component | Before (v2024.01.21) | v2026.05.09 | Our cached master ~ March 2026 | Suite-side patch affected |
|---|---|---|---|---|
| SDL2 | 2.28.x | **2.30.11** | likely 2.30.x | SDL2 patch becomes a **no-op** — 2.30.11's `SDL_androidsensor.c` already uses `ALooper_pollOnce` natively (verified). |
| SDL2_ttf | 2.20.2 | **2.22.0** | unknown | Still bundles harfbuzz, still has the strict-cast issue in `hb.hh` at the pinned commit `516b7ed` (verified). **harfbuzz patch still needed.** Risk: SDL2_ttf 2.22 may have moved from `Android.mk` to CMake — our patch anchors on `Android.mk` (need to verify on the first rebuild). |
| Kivy | 2.3.0 | **2.3.1** | likely 2.3.0 | Unknown whether `cgl_gl.c` strict-cast is fixed in 2.3.1. Our CFLAG patch is a soft demotion and will keep working either way. |
| NDK target | r25 default | "improved compatibility for r28c" | (we force r29 via spec) | r29 not specifically called out by upstream. Our build forces r29 via spec; we'd be on a less-tested NDK. |
| AGP / Gradle | 8.x | **AGP 8.11.0 / Gradle 8.14.3** | unknown | Newer toolchain; suite's `android.gradle_dependencies` should remain compatible but a verification gate. |
| Target API in tests | 33–34 | **35** | (we force 36) | Less of a delta than the doc suggests since we override. |

### Structural changes (potentially-breaking for our overrides)

- **PyProjectRecipe system** — kivy, pyjnius, materialyoucolor,
  and android recipes now use isolated PyProjectRecipe builds.
  Our `recipes/kivy/__init__.py` subclasses `KivyRecipe`. If the
  class hierarchy changed in v2026.05.09 (e.g., to a
  `PyProjectRecipe`-based shape), our import may break or our
  `prebuild_arch` may patch a path that's no longer in use.
  **Highest-risk drift point.** Verified that the v2026.05.09
  kivy recipe pins `version = '2.3.1'` and that
  `get_recipe_env` does NOT clear PKG_CONFIG (so we still need
  to add it).
- **SDL3 bootstrap support** — new option for Kivy 3.0.0;
  optional, doesn't affect SDL2 path.
- **`foregroundServiceType` in AndroidManifest templates** — p4a
  now supports declaring this via spec rather than manifest
  injection. We currently inject it via the `:provider` service
  block in `p4a_hook._inject_aztcollab_service`. We *could*
  switch to spec-driven, but the injection is doing more than
  just FGS (it carries `android:permission`, `android:process`,
  and the `<property>` subtype) — not a clean simplification.
- **Display cutout support** — new manifest plumbing. Not used
  by the suite.
- **Removed `patchelf` dependency** — one less host tool needed.
- **Python 3.14 support** — we're on 3.13. Not relevant unless we
  also bump host Python.

### Bug fixes / new features

- Display cutouts in manifest, `android.touch` module, prebuilt
  wheel support, hardware-accelerated FFmpeg codecs, foreground
  service type support — none directly impact the suite.
- ~20 new recipes added (materialyoucolor, coincurve, etc.) —
  not on our requirements list.

---

## 3. What "upgrading" actually means

Two steps:

1. **Change the pin** in every suite `buildozer.spec` (and
   `.buildozer/CLAUDE.md`'s documentation block):
   ```
   p4a.branch = v2026.05.09
   ```
   (Or, equivalently, `p4a.commit = <SHA>` if we want
   immutability beyond what a tag offers.)

2. **Force a clean reclone** of the cached p4a, because buildozer
   reuses the existing clone:
   ```
   rm -rf /home/kentr/bin/AZT/.buildozer/android/platform/python-for-android
   ```
   Then `buildozer android clean && buildozer android debug` on
   each suite app.

No code changes to our recipe overrides or hook unless the
validation rebuild surfaces drift.

---

## 4. Pros

1. **Reproducibility.** Pinned tag → today's build = next
   month's build. Today the cached clone is at a March-2026
   master SHA; if it ever gets refetched (e.g., new dev machine,
   or `.buildozer` deletion), the suite would resync to whatever
   master is *that day* — no signal that anything changed.
2. **Tested release combo.** v2026.05.09 has been verified end-to-
   end by upstream CI (runtime app testing now in CI). Master
   carries unreleased breakage on average; tags don't.
3. **One redundant patch becomes a confirmed no-op.** SDL2
   2.30.11 has `ALooper_pollOnce` natively. Our SDL2 override
   would log `[sdl2-patch] … already patched` and otherwise do
   nothing — useful as a safety net if SDL2 ever regresses, but
   no longer load-bearing.
4. **Newer Kivy (2.3.0 → 2.3.1).** Mostly bug fixes; our CFLAG
   demotion (`-Wno-error=incompatible-function-pointer-types`)
   would either become unnecessary (if the const-mismatch was
   fixed upstream) or continue to silently apply.
5. **Newer SDL2_ttf bundled harfbuzz** vs. the 2020-era
   harfbuzz in SDL2_ttf 2.20.2. Still bundled (commit `516b7ed`),
   still has the pragma, but a more recent codebase that's likely
   to converge with the strict cast fixes harfbuzz upstream
   already shipped.
6. **Less drift between cached p4a and what upstream considers
   stable.** Issues we file would be against a known release,
   not "master at SHA xxxxxx of 2026-03-26."
7. **Removed `patchelf` host dependency.** One less thing the
   build prereqs script needs to ensure is installed.
8. **Forward-compatible with SDL3/Kivy 3.0.0 path.** The release
   carries SDL3 bootstrap recipes; we can keep the SDL2 bootstrap
   we use today, but a future Kivy 3 migration is one
   `p4a.bootstrap = sdl3` flip away.
9. **CI test surface includes runtime app behavior, not just
   build.** Means regressions that we'd previously catch only by
   running our APK might now be caught by upstream.
10. **`MIN_CLIENT_VERSION` / suite-wide release discipline match.**
    We pin daemon ↔ client versions for the wire format; pinning
    the build toolchain matches that discipline.

## 5. Cons

1. **Forced clean rebuild on every suite APK.** `~10–25 min` per
    app × N apps. Discretionary cost, one-time per upgrade.
2. **Recipe override drift — high risk.** Our overrides
    subclass `LibSDL2Recipe`, `LibSDL2TTF`, `KivyRecipe`. If
    v2026.05.09 changed:
    - Class names (especially likely for kivy under
      PyProjectRecipe)
    - `get_build_dir(arch.arch)` signature or layout
    - Anchor strings in `setup.py` / `Android.mk`
    The recipes degrade gracefully with `pattern not found,
    skipping`, but a silent skip leaves the underlying issue
    unpatched and the build fails further along.
3. **PyProjectRecipe migration on kivy specifically.** v2026.05.09
    moves kivy to the new PyProjectRecipe shape. Our
    `prebuild_arch` patches a top-level `setup.py`; if
    PyProjectRecipe builds don't use the same `setup.py` entry
    point (e.g., now built via `pyproject.toml` + `build` PEP 517),
    our setup.py patch silently misses, and the host-pkg-config
    leak it prevents may resurface.
4. **SDL2_ttf 2.22 build system uncertainty.** If 2.22 has moved
    from `Android.mk` (which p4a's BootstrapNDKRecipe drives via
    `ndk-build`) to CMake, our `Android.mk` patch silently misses
    and we hit the strict-cast error we'd been suppressing. Not
    confirmed; first rebuild would surface it.
5. **NDK r29 less tested upstream.** Upstream release notes
    mention "improved NDK r28c support" — our `android.ndk = 29`
    pin is one notch ahead of upstream's tested target. Our
    patches were specifically for r29 quirks (where r28c didn't
    trip them); we may discover *new* r29-specific issues in
    upstream's r28c-targeted changes.
6. **AGP 8.11.0 / Gradle 8.14.3 sensitivity.** Our gradle deps
    (zxing-android-embedded 4.3.0, appcompat 1.6.1, fragment 1.6.2,
    kotlin-stdlib 1.8.20) all date from the AGP 8.0–8.5 era. They
    likely still work but it's a verification gate.
7. **No path-back without a clean rebuild.** Once we cut over,
    going back to master requires another `rm -rf` of the cached
    clone. Cheap in absolute terms but worth noting if a
    regression surfaces and we want to bisect.
8. **`patchelf` removal could be a regression vector.** Marginal,
    but if any of our shipped recipes (or a transitive dependency)
    expected `patchelf` to be in PATH, removal would surface a
    later build error.
9. **Static "release" cadence.** Releases are 16 months apart in
    p4a's recent history. If a critical regression surfaces in
    v2026.05.09, we wait for the next release (or fork patches
    locally). On master we'd naturally roll forward to a fix —
    pin trades drift-risk for fix-availability-latency.
10. **Tests target API 35; we target 36.** The release ships with
    tests at API 35. Our `android.api = 36` is ahead of upstream's
    tested target by one level (Android 15 → 16). Manifest /
    permission / FGS semantics changed at Android 16 in ways the
    upstream test matrix may not exercise.

---

## 6. Risk-prioritized validation checklist (if we proceed)

Run on a side branch — don't blow the working build until clean.

1. **Pin and reclone:**
   ```
   # in buildozer.spec
   p4a.branch = v2026.05.09
   # then
   rm -rf /home/kentr/bin/AZT/.buildozer/android/platform/python-for-android
   buildozer android clean
   buildozer android debug 2>&1 | tee /tmp/buildlog-2026.05.09.txt
   ```
2. **Look for `[sdl2-patch]`, `[sdl2_ttf-patch]`, `[kivy-patch]`
    lines in the early build output.** Per `.buildozer/CLAUDE.md`,
    each recipe override prints either `… patched` (success),
    `already patched` (idempotent re-run), or `pattern not found,
    skipping` (upstream drift). A `pattern not found, skipping`
    on **any** of these means we have follow-up work before the
    build is fit to ship.
3. **Confirm `[kivy-patch] gated sdl2_flags merge on
    KIVY_SDL2_PATH in …/setup.py`** — if it logs
    `pattern not found`, kivy is now PyProjectRecipe and the
    host-pkg-config defence has to be redesigned.
4. **Confirm `[sdl2_ttf-patch] added … to harfbuzz Android.mk`**
    — if `pattern not found`, SDL2_ttf 2.22 has moved to CMake
    and the cast-function-type-strict suppression has to be
    redelivered through cmake `add_compile_options` or similar.
5. **`[sdl2-patch]` is expected to log `already patched`** —
    SDL2 2.30.11 has the fix natively. Confirms our patch is now
    a backup; no action required.
6. **`[hook] created libpython3.13.so → libpythonbin.so`** —
    confirms the python-version-detection path still works
    against whatever Python `hostpython3` recipe v2026.05.09
    ships. (Currently 3.13; could be 3.14 if the suite's
    `requirements` doesn't pin.)
7. **`[hook] injected …` lines for the 5 manifest injections** —
    verifies the manifest-injection hook still finds its anchor
    strings (`</application>`, `<application `,
    `<activity android:name="org.kivy.android.PythonActivity"`).
    p4a templates are stable in this area; low risk but worth
    confirming.
8. **Install + smoke-test the resulting APK.** Server APK boot,
    daemon spawn, picker open, sync nudge, LAN pair if possible.
    The runtime regressions most likely to land would be in
    `_python_bundle/` extraction or the Activity → :provider
    process split (we have history with crashes around the
    Python-interpreter-per-process boundary; see CLAUDE.md
    invariant #1 on Android specifics).
9. **If everything passes, repeat the rebuild for every suite
    APK** (`server_apk`, `azt_recorder`, `azt-viewer`,
    sister apps if applicable) and update
    `.buildozer/CLAUDE.md`'s documentation block to reflect the
    new pin.

---

## 7. Recommendation

**Worth doing, with the validation discipline above.** The
upside (reproducibility, tested-combo, one patch becomes
redundant safety-net, forward path to SDL3/Kivy 3) outweighs
the downside (one-time rebuild cost, drift verification work).
The dominant risk is the PyProjectRecipe migration affecting
our kivy override — if that surfaces, the fix is contained
(redesign one recipe override).

**Not worth doing right now if:**
- A field release is imminent and we don't want a tooling
  variable in flight.
- We're about to swap NDK or host Python (combining changes
  multiplies the diagnosis surface if anything fails).
- We have no need for the new features and the current build
  is stable.

**Reasonable middle path:** pin to v2026.05.09 in a feature
branch, run the full rebuild + smoke on the server APK + one
peer APK, hold for a week of bench testing, then merge. The
manifest-injection part of the build hook is the most stable
piece across p4a versions; the highest entropy is in the
kivy/sdl2_ttf recipe overrides; both are well-instrumented for
silent-drift detection (`pattern not found, skipping` lines).
The validation cost is bounded.

---

## 8. Open questions for further investigation

Before flipping the pin in production, the following should be
verified by reading the v2026.05.09 source (not just release
notes):

1. Is the v2026.05.09 `kivy` recipe still a plain `KivyRecipe`
   subclass, or has it moved to PyProjectRecipe? (Direct
   inspection of `pythonforandroid/recipes/kivy/__init__.py` on
   the tag.)
2. Does SDL2_ttf 2.22.0 still drive its build through
   `Android.mk`, or has it moved to CMake? (Direct inspection of
   the tarball structure.)
3. Is `_fix_libpython_symlink` still needed in v2026.05.09, or
   does the new build flow produce `libpython3.X.so` directly?
4. Does the new `foregroundServiceType` manifest support remove
   the need for `_inject_aztcollab_service`? (Probably no, given
   the service injection carries
   `android:permission="org.atoznback.AZT_COLLAB_ACCESS"` and
   `android:process=":provider"` plus the `<property>` subtype.)
5. What's the v2026.05.09 `hostpython3` recipe's Python version?
   (Could implicitly bump Python from 3.13 → 3.14, which is a
   wholly separate compatibility surface for our `azt_collab_client`
   wire protocol — worth knowing up front.)

---

## 9. Bundled sub-task: suite-wide `defusedxml` XXE-hardening pass

Added 2026-07-04. Bundled here because it's a **new build dependency +
requirements churn + forced rebuild** — exactly the batch this p4a
flip already pays for. Do it in the same cutover so one clean rebuild
validates both.

**What.** Every XML *parse* of external/untrusted bytes in the daemon
uses stdlib `xml.etree.ElementTree`, which is XXE- and
billion-laughs-exposed by default (flagged when `_clean_template`
shipped in 0.52.32). Swap the **parse** sites to
`defusedxml.ElementTree` (guards entity expansion + external-entity /
DTD fetch). Keep `ET.tostring` (stdlib) for **serialization** —
defusedxml only hardens parsing, not output, so this is a
parse-only swap; the serialize path is unchanged.

**Dependency.** `defusedxml` is **pure Python** → just add it to each
app's buildozer `requirements` line. No recipe override, no NDK
surface, no manifest change. Verify it imports inside the p4a bundle
on first rebuild.

**Daemon call sites (azt-collab, in-lane) — grep 2026-07-04:**

| File:line | Input | Priority |
|---|---|---|
| `projects.py:514` (`_mint_fresh_guids`), `:573` (`_clean_template`) | downloaded SILCAWL template (configured URL, but external) | **high** |
| `lift_surgery.py:238` | peer-streamed entry bytes | **high** |
| `lift_merge.py:194/196/199/886` | fetched / merged LIFT content | **high** |
| `atomic_recovery.py:83/84` | on-disk orphan / current LIFT | **high** |
| `lift_merge.py:268/276/332` (`_canon` re-parse) | daemon's own just-serialized bytes (trusted) | low — swap for uniformity only |

(`ui/picker_app.py:830` is `Uri.parse`, not XML — exclude.)

**Out of lane (note only, parallel peer sweep):** the desktop app
(`azt/io_put/lift.py`) and recorder parse LIFT with stdlib ET too;
same swap belongs there but lands in the sister repos, not azt-collab.

**Scope discipline.** Parse-only swap; do not touch `tostring` calls,
the `_canon`/merge logic, or the bytes→bytes fallback contracts —
`defusedxml.ElementTree.fromstring` raises on a defused payload, so
the existing `except ...: return original_bytes` guards must catch
`defusedxml`'s exception types too (or a broad `Exception`) so a
hostile template still falls back rather than crashing the path.
