# Minimum device specification

Aimed at potential users picking a phone or tablet for field work
with the A-Z+T suite, and at suppliers being asked to bulk-source
devices for a project. Three tiers — the **minimum** works; the
**recommended** is where the app starts to feel right; the
**comfortable** tier lifts the resource gates and runs everything
eagerly.

The thresholds in the "RAM" row are not arbitrary — they're the
same numbers the suite itself uses (`azt_collab_client/lowpower.py`,
`RAM_TIER_LOW_MB = 3072`, `RAM_TIER_MID_MB = 6144`) to decide at
runtime whether to gate eager prefetch, full-resolution drawables,
prewarm threads, etc.

## Tier table

| Spec | Minimum | Recommended | Comfortable |
|---|---|---|---|
| **RAM** | 3 GB | 4 GB | 6 GB+ |
| **Android version** | 8.0 (API 26) | 11 (API 30) | 13 (API 33)+ |
| **Storage free** | 8 GB | 16 GB | 32 GB+ |
| **CPU architecture** | armeabi-v7a (32-bit) | arm64-v8a (64-bit) | arm64-v8a |
| **CPU class** | Octa-core, ≥ 2× perf cores @ 1.6 GHz (Cortex-A53 or newer); examples: Unisoc T606, MediaTek Helio G35 / G36 | Octa-core, 2× Cortex-A75/A76 @ 2.0 GHz + 6× A55; examples: MediaTek Helio G81 / G88, Snapdragon 685 / 695 | Octa-core with Cortex-A78+ big cores @ 2.4 GHz+; examples: Snapdragon 7 Gen 1, Helio G99, Dimensity 7000-series |
| **Network** | Wi-Fi 4 (802.11 n) or LTE | Wi-Fi 5 or LTE | Wi-Fi 5 or 5G |
| **Microphone** | Required — recording is part of the workflow | Quality mic; phone's built-in is usually fine | Quality mic |
| **Display density** | Any (suite ships ldpi → xxxhdpi drawables) | xhdpi+ comfortable | — |

## What each tier means in practice

**Minimum (3 GB RAM, slow CPU).** The suite runs. Eager
optimisations turn off automatically: CAWL image prefetch is gated
on `have_room_for_prefetch()`, full-resolution presplash is gated
on density bucket, daemon pre-warm is conditional. Daemon
cold-spawn after an idle-stop can take 15–30 s on the
slowest-tier hardware (observed on R500-class Android 15 tablets);
the bootstrap retry budget covers this, but the user sees a
"Connecting…" popup during the wait. Sync of a large LIFT project
will be slow because dulwich's pack-building is single-threaded
and CPU-bound — budget on the order of one second per 50–100
commits on the slowest tier.

**Recommended (4 GB RAM, mid-range CPU).** The Tecno KN4 class.
Where the app starts to feel responsive: cold-spawn 5–10 s,
prefetch runs in the background, sync of a typical-size project
completes in seconds. This is the floor for SIL-style intensive
LIFT editing sessions where the user expects sub-second UI
response to a recording stop / next-entry tap.

**Comfortable (6+ GB RAM, modern CPU).** Lifts every internal
gate: prefetch eager, no downsampling, daemon prewarm always on.
Sub-3 s cold-spawn. Headroom for large CAWL collections (multiple
image_repo entries) and multi-project workflows without
swap-thrash.

## CPU-architecture detail

The suite ships both `armeabi-v7a` (32-bit ARM) and `arm64-v8a`
(64-bit ARM) APK artefacts. **64-bit is strongly preferred** —
all currently-sold mid-range and up phones are arm64-only, and
some Android 11+ features (full app-startup optimization in
particular) are disabled on 32-bit. The 32-bit fallback is for
genuine field-budget devices where arm64 isn't an option.

x86 / x86_64 is not shipped — no field devices use it, only
emulators.

Single-thread performance matters more than core count for the
suite's workload. The sync chain (dulwich fetch, merge, push) and
LIFT XML manipulation all run on one thread. A four-core SoC
with strong cores will beat an eight-core SoC with weak ones for
sync throughput.

## Avoid

- **Devices with Android 15 on entry-level SoCs.** Android 15's
  app freezer is aggressive about pausing the server APK's
  `:provider` process. Workarounds exist
  (`install_server_apk_popup`'s "Open AZT Collaboration" prompt,
  the `AZTServiceConnector` bind), but the experience is choppy.
  Either pick Android 14- on a budget device, or step up to the
  Recommended tier on Android 15+.
- **Devices with under 2 GB free user-accessible storage.** The
  daemon needs scratch space for git pack-receive (peak ≈ pack
  size, roughly 1.5× the project's working-tree size), and CAWL
  cache scales with image_repo size (50–100 MB for the default
  CAWL set).
- **No-microphone devices** — the recorder is core to the
  workflow; some budget tablets ship without one.

## Invariants regardless of tier

- The suite APKs must all be signed with the same keystore
  (signature-level permission gates inter-app calls — peers and
  server APK with mismatched signatures silently fall back to no
  daemon). Suppliers preinstalling the suite need to match the
  canonical suite keystore fingerprint
  (`android/SUITE_FINGERPRINT`).
- Side-loading must be enabled (Settings → Apps → Special access
  → Install unknown apps → the source app the user installs
  from). The suite is not on the Play Store; updates are
  distributed via GitHub releases and an in-app self-update flow
  (`azt_collab_client/ui/update.py`).
- The server APK (`org.atoznback.aztcollab`) must be installed
  before any peer. Peers refuse to bootstrap without it and
  prompt the user through the install flow.

## How to verify a candidate device

Run the suite on the candidate for a representative session
(open a project, browse 50 CAWL images, make 5 recordings,
sync). Watch for:

- Cold-spawn time (should be ≤ 10 s on Recommended, ≤ 30 s on
  Minimum).
- Sync completion (should not bail on the daemon's
  `MAX_CONSECUTIVE_FAILURES = 12` adaptive-batching budget; if
  it does, the CPU is too slow for the project's pack size).
- Reproduce the session → picker's `Share diagnostics`:
  daemon logging is always-on (since 0.52.7), and the
  `[boot-trace-daemon]` `t=…` lines tell you exactly how long
  each daemon-boot phase took on the candidate. Devices whose
  `after_install_callbacks` lands beyond ~3 s are below the
  Recommended tier.
