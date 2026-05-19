# Low-power adaptive policy rationale

> **Conformity contract** — three rules, the gate-vs-don't-gate
> inventory, the multi-density splash + diagnostic logging recipe,
> verification steps — is in `CLIENT_INTEGRATION.md` § 18. This
> file is the *why*.

## Why automatic, not user-toggleable

Devices in the field span flagships to 2–3 GB budgets. A
user-facing "low-power mode" toggle pushes the burden of
device introspection onto users who don't know (and
shouldn't have to learn) what `availMem / totalMem` means.
Android already classifies the device through
`ActivityManager.MemoryInfo.lowMemory`, `availMem`,
`totalMem`, and `ConnectivityManager.isActiveNetworkMetered()`.
Use those signals; let the user steer content and workflow.

The split — *resource decisions automatic, content/workflow
decisions user-facing* — falls out of one question:

> Is this about what the device CAN do, or about what the
> user WANTS?

Image cache size, prefetch eagerness, prewarm gating, poll
cadence: "can". Gloss-count display, sync-on-swipe: "wants".
Mixing the two means the user has to think about both, which
either crashes their budget phone or shows them controls they
shouldn't care about.

## Why build-time work belongs in the build

The anti-pattern we're explicitly rejecting: ship one
high-resolution asset and ask the device to downscale at
runtime. PIL-resize the presplash on first boot; regenerate
density buckets in `App.build`; recompile gettext `.mo` on
cold start. Each moves work onto the *least capable* devices
at the *worst possible moment* (splash screen, before Python
is warm; first-launch when the user is forming their first
impression).

The discipline:

> The build is the right place to do work that depends on
> the build artefact. The device is the right place to do
> work that depends on runtime state.

Density buckets, gettext compilation, CAWL pre-rendering —
all build-artefact-dependent. They belong in the build (or,
for CAWL, in the daemon, which is the suite's "build for
runtime data"). The device handles the runtime-state work:
which project is loaded, which language the user just
selected, which audio file was just recorded.

Same logic forbids "regenerate the CAWL cache from a tarball
on first launch" (the daemon already ships it, pre-rendered)
and "recompute the Charis SIL fallback on every cold start"
(the `.mo` files are pre-bundled). When a tempting
implementation has the shape "do build-artefact work at
runtime on each device", the cost is exactly the
distribution of work it implies — N devices × the per-device
cost — and the build is doing it once.

## Why `lowpower` helpers belong in the client, not each peer

The JNI plumbing for `ActivityManager.MemoryInfo` and
`ConnectivityManager.isActiveNetworkMetered()` plus the
thresholds (`< 0.15` of total memory, `≤ 3072 MB` total, etc.)
would drift between peer codebases if each peer re-derived
them. `azt_collab_client.lowpower` (shipped 0.41.21) is the
single source of truth: one tested jnius dance, one set of
threshold constants overridable per peer if field data
motivates. The diagnostic recipe (`identify_drawable_variant`
/ `log_presplash_variant`) also lives here so a future
correction (the kind that already happened to the first-pass
`Drawable.getIntrinsicWidth/Height()` and
`BitmapDrawable.getBitmap().getDensity()` recipes) ships in
one place rather than waiting for N peers to update
independently.

## Why we sized the suite's presplash baseline at mdpi 320×533

Android's resource resolver picks the bucket whose qualifier
matches the device's `densityDpi`. For physical-size
consistency across the suite, all peers + the server APK use
mdpi 320×533 as the 1.0× baseline; bucket sizes scale by
the standard Android factors (ldpi 0.75×, hdpi 1.5×,
xhdpi 2×, xxhdpi 3×, xxxhdpi 4×). xxhdpi (the most common
modern phone bucket) thus lands at 960×1599, matching the
diagnostic line peers log at startup. A suite-wide baseline
keeps splash visual sizing predictable across the recorder /
viewer / server-APK boundary.
