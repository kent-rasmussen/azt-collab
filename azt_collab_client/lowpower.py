"""Low-power adaptive policy helpers.

Single source of truth for the OS signals peers use to decide
whether to be eager (pre-warm, full-resolution, persistent
polls) or degrade transparently (skip warm, downsample, suspend
polls). The conformity contract — when to call each helper, what
to gate on the result — lives in ``CLIENT_INTEGRATION.md`` § 18.
This module is the *implementation* of that contract.

Public API:

    total_ram_mb()              one-shot, cached
    memory_state()              fresh each call
    is_low_memory()             combined: lowMemory ∨ ratio < threshold
    is_metered_network()        fresh; mobile-data / hotspot / etc.
    have_room_for_prefetch()    combined: ¬low_memory ∧ ¬metered
    ram_tier()                  'low' | 'mid' | 'high'
    densityDpi()                one-shot, cached
    dpi_to_bucket(dpi)          'ldpi' | … | 'xxxhdpi'

Threshold constants (override before first call to take effect on
the cached one-shots):

    RAM_TIER_LOW_MB         3072    ≤ this → 'low' tier
    RAM_TIER_MID_MB         6144    ≤ this → 'mid' tier; > this → 'high'
    AVAIL_RATIO_LOW         0.15    availMem / totalMem under this → low
    DISPLAY_DOWNSAMPLE_PX   720     suggested max long edge on lowMemory

Desktop fallback: returns "comfortable" values so dev runs don't
accidentally take the gated paths. Set ``AZT_FORCE_LOW_MEMORY=1``
in the environment to force every signal to its budget-device
value for manual testing on any platform.
"""

import os
import sys
from dataclasses import dataclass


# Thresholds — module-level so peers can override before the first
# call. The constants reflect the first-peer implementation; survey
# field data should motivate changes, not gut feel.
RAM_TIER_LOW_MB = 3072
RAM_TIER_MID_MB = 6144
AVAIL_RATIO_LOW = 0.15
DISPLAY_DOWNSAMPLE_PX = 720

_FORCE_ENV = 'AZT_FORCE_LOW_MEMORY'


@dataclass(frozen=True)
class MemoryState:
    low_memory: bool        # OS-supplied ActivityManager.MemoryInfo.lowMemory
    avail_ratio: float      # availMem / totalMem in [0, 1]
    avail_mb: int           # availMem in MB


def _force() -> bool:
    return os.environ.get(_FORCE_ENV, '') == '1'


def _is_android() -> bool:
    try:
        from kivy.utils import platform
    except Exception:
        return False
    return platform == 'android'


def _android_activity():
    """Return PythonActivity.mActivity or None."""
    try:
        from jnius import autoclass
        return autoclass('org.kivy.android.PythonActivity').mActivity
    except Exception as ex:
        print(f'[lowpower] cannot get Android Activity: {ex}',
              file=sys.stderr)
        return None


def _android_memory_info():
    """Return (MemoryInfo, total_mb, avail_mb, low_flag) or None."""
    activity = _android_activity()
    if activity is None:
        return None
    try:
        from jnius import autoclass
        Context = autoclass('android.content.Context')
        MemoryInfo = autoclass('android.app.ActivityManager$MemoryInfo')
        am = activity.getSystemService(Context.ACTIVITY_SERVICE)
        mi = MemoryInfo()
        am.getMemoryInfo(mi)
        total_mb = int(mi.totalMem // (1024 * 1024))
        avail_mb = int(mi.availMem // (1024 * 1024))
        return (mi, total_mb, avail_mb, bool(mi.lowMemory))
    except Exception as ex:
        print(f'[lowpower] MemoryInfo probe failed: {ex}', file=sys.stderr)
        return None


def _desktop_total_ram_mb() -> int:
    """Best-effort total-RAM read on non-Android platforms."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    # Last resort — pretend comfortable so gated paths don't engage.
    return RAM_TIER_MID_MB * 4


_total_ram_cache = None


def total_ram_mb() -> int:
    """Total RAM in MB. Cached after first successful read."""
    global _total_ram_cache
    if _total_ram_cache is not None:
        return _total_ram_cache
    if _force():
        _total_ram_cache = RAM_TIER_LOW_MB
        return _total_ram_cache
    if _is_android():
        info = _android_memory_info()
        if info is not None:
            _total_ram_cache = info[1]
            return _total_ram_cache
    _total_ram_cache = _desktop_total_ram_mb()
    return _total_ram_cache


def memory_state() -> MemoryState:
    """Fresh memory snapshot. Not cached — values change at runtime."""
    if _force():
        return MemoryState(low_memory=True, avail_ratio=0.05, avail_mb=128)
    if _is_android():
        info = _android_memory_info()
        if info is not None:
            _mi, total_mb, avail_mb, low_flag = info
            ratio = (avail_mb / total_mb) if total_mb > 0 else 1.0
            return MemoryState(low_memory=low_flag,
                               avail_ratio=ratio,
                               avail_mb=avail_mb)
    # Desktop / probe-failed fallback: pretend comfortable.
    return MemoryState(low_memory=False, avail_ratio=1.0, avail_mb=8192)


def is_low_memory() -> bool:
    """Combined low-memory predicate.

    True when EITHER the OS reports ``lowMemory`` OR the available-
    memory ratio is below ``AVAIL_RATIO_LOW``. Either signal alone
    is enough — the OS flag handles "system is about to start
    killing background apps", the ratio handles "lots of RAM total
    but most of it is committed."
    """
    s = memory_state()
    return s.low_memory or s.avail_ratio < AVAIL_RATIO_LOW


def is_metered_network() -> bool:
    """ConnectivityManager.isActiveNetworkMetered() on Android.

    True for mobile data, tethered hotspots, and any network the
    user has marked metered in Settings. False for unmetered Wi-Fi
    / Ethernet. False on desktop unless forced.

    **Permission requirement.** Callers must list
    ``ACCESS_NETWORK_STATE`` in their APK's ``android.permissions``
    (``buildozer.spec``). Without it, the JNI call raises
    ``SecurityException: Neither user N nor current process has
    android.permission.ACCESS_NETWORK_STATE`` and this helper
    silently returns False — which biases the caller toward
    "eager" / "network is fine" decisions, which is the safer
    default (don't skip work the user might want) but masks the
    fact that we never actually checked.
    """
    if _force():
        return True
    if _is_android():
        activity = _android_activity()
        if activity is not None:
            try:
                from jnius import autoclass
                Context = autoclass('android.content.Context')
                cm = activity.getSystemService(Context.CONNECTIVITY_SERVICE)
                return bool(cm.isActiveNetworkMetered())
            except Exception as ex:
                print(f'[lowpower] isActiveNetworkMetered failed: {ex}',
                      file=sys.stderr)
    return False


def have_room_for_prefetch() -> bool:
    """Combined predicate for "eager prefetch is OK right now."

    Requires: not low-memory AND not metered. Use to gate any
    bulk warm operation that fetches data the user hasn't asked
    for yet (CAWL prefetch, periodic resync of unchanged remotes,
    speculative tile loads, etc.).
    """
    return (not is_low_memory()) and (not is_metered_network())


def ram_tier() -> str:
    """Coarse device class — 'low' / 'mid' / 'high'.

    ``low``  : totalMem ≤ ``RAM_TIER_LOW_MB`` (default 3 GB)
    ``mid``  : ``RAM_TIER_LOW_MB`` < totalMem ≤ ``RAM_TIER_MID_MB``
    ``high`` : totalMem > ``RAM_TIER_MID_MB`` (default 6 GB)

    Pick per-tier values yourself (cache size, prewarm gating,
    etc.) — this helper only classifies the hardware.
    """
    mb = total_ram_mb()
    if mb <= RAM_TIER_LOW_MB:
        return 'low'
    if mb <= RAM_TIER_MID_MB:
        return 'mid'
    return 'high'


_density_cache = None


def densityDpi() -> int:
    """DisplayMetrics.densityDpi. Cached after first successful read.

    Returns 160 (mdpi baseline) on desktop or when the probe fails,
    so callers can use ``dpi_to_bucket(densityDpi())`` without
    branching on platform.
    """
    global _density_cache
    if _density_cache is not None:
        return _density_cache
    if _is_android():
        activity = _android_activity()
        if activity is not None:
            try:
                metrics = activity.getResources().getDisplayMetrics()
                _density_cache = int(metrics.densityDpi)
                return _density_cache
            except Exception as ex:
                print(f'[lowpower] densityDpi probe failed: {ex}',
                      file=sys.stderr)
    _density_cache = 160
    return _density_cache


# Standard Android density-bucket cutpoints. These are the canonical
# boundaries Android uses internally when picking a drawable-<bucket>
# resource — keep them in sync if Google ever extends the table.
_BUCKET_CUTPOINTS = [
    (120, 'ldpi'),
    (160, 'mdpi'),
    (240, 'hdpi'),
    (320, 'xhdpi'),
    (480, 'xxhdpi'),
    (640, 'xxxhdpi'),
]


def dpi_to_bucket(dpi: int) -> str:
    """Map a densityDpi to its Android bucket name.

    Used together with ``densityDpi()`` to log which bucket landed
    on the device (the diagnostic recipe in
    ``CLIENT_INTEGRATION.md`` § 18 "Diagnostic logging — which
    bucket landed?").
    """
    for cutpoint, name in _BUCKET_CUTPOINTS:
        if dpi <= cutpoint:
            return name
    return 'xxxhdpi'


# Suite-canonical (source_dpi, native_w) for each presplash bucket.
# Matches the mdpi 320×533 baseline that generate_presplash.py emits
# and that CLIENT_INTEGRATION.md § 18 documents. Peers shipping a
# different baseline (e.g. a non-presplash multi-density asset) pass
# their own ``bucket_table`` to identify_drawable_variant /
# log_presplash_variant.
_DEFAULT_PRESPLASH_BUCKETS = {
    'ldpi':    (120, 240),
    'mdpi':    (160, 320),
    'hdpi':    (240, 480),
    'xhdpi':   (320, 640),
    'xxhdpi':  (480, 960),
    'xxxhdpi': (640, 1280),
}


def identify_drawable_variant(resource_name='presplash', bucket_table=None):
    """Identify which density-bucket variant of a drawable Android picked.

    Android does NOT surface variant selection in logcat. The
    obvious approaches both lie:

    - ``Drawable.getIntrinsicWidth/Height()`` returns device-scaled
      pixels, so every bucket collapses to the same number on any
      given device.
    - ``BitmapDrawable.getBitmap().getDensity() / .getWidth()``
      reports post-scaling state — Android has already pre-scaled
      the bitmap to the device target density by the time you call
      ``getDrawable``.

    Working recipe: ``BitmapFactory.decodeResource`` with
    ``inJustDecodeBounds=true`` (skips the bitmap allocation) and
    ``inScaled=false`` (the load-bearing flag — without it the
    width gets pre-scaled like the old recipes). After the call,
    ``opts.outWidth`` is the native pixel width of the resource
    file Android actually picked, and ``opts.inDensity`` is the
    source folder's density.

    Returns a dict:

        device_dpi       int    DisplayMetrics.densityDpi
        device_bucket    str    'ldpi'..'xxxhdpi'
        source_dpi       int|None
        native_w         int|None
        native_h         int|None
        variant          str    bucket name, 'fallback', or 'unknown'

    ``fallback`` means Android picked the unqualified ``drawable/``
    asset (no bucket matched). ``unknown`` means the probe ran but
    the (source_dpi, native_w) didn't match any entry in
    ``bucket_table`` — likely a peer-specific asset that should
    pass its own table.
    """
    table = bucket_table if bucket_table is not None else _DEFAULT_PRESPLASH_BUCKETS
    device_dpi = densityDpi()
    result = {
        'device_dpi': device_dpi,
        'device_bucket': dpi_to_bucket(device_dpi),
        'source_dpi': None,
        'native_w': None,
        'native_h': None,
        'variant': 'unknown',
    }
    if not _is_android():
        return result
    activity = _android_activity()
    if activity is None:
        return result
    try:
        from jnius import autoclass
        BitmapFactory = autoclass('android.graphics.BitmapFactory')
        Options = autoclass('android.graphics.BitmapFactory$Options')
        resources = activity.getResources()
        pkg_name = activity.getPackageName()
        res_id = resources.getIdentifier(resource_name, 'drawable', pkg_name)
        if res_id == 0:
            return result
        opts = Options()
        opts.inJustDecodeBounds = True
        opts.inScaled = False
        BitmapFactory.decodeResource(resources, res_id, opts)
        result['source_dpi'] = int(opts.inDensity)
        result['native_w'] = int(opts.outWidth)
        result['native_h'] = int(opts.outHeight)
        for name, (src_dpi, src_w) in table.items():
            if (result['source_dpi'] == src_dpi
                    and result['native_w'] == src_w):
                result['variant'] = name
                break
        else:
            # No bucket match — Android picked the unqualified
            # drawable/ asset (presplash.filename fallback) or the
            # peer ships a different baseline than the suite default.
            result['variant'] = 'fallback'
    except Exception as ex:
        print(f'[lowpower] identify_drawable_variant failed: {ex}',
              file=sys.stderr)
    return result


def log_presplash_variant(tag='presplash', resource_name='presplash',
                          bucket_table=None):
    """Log the picked drawable bucket for diagnostic verification.

    Pick a distinct ``tag`` per APK so a combined logcat is
    grep-able:

        log_presplash_variant(tag='presplash:server')   # server APK
        log_presplash_variant(tag='presplash')          # peer

    Sample line:

        [presplash:server] device densityDpi=420 (xxhdpi); native
        960x1599 source=480dpi (xxhdpi variant)
    """
    info = identify_drawable_variant(resource_name, bucket_table)
    print(
        f'[{tag}] device densityDpi={info["device_dpi"]} '
        f'({info["device_bucket"]}); '
        f'native {info["native_w"]}x{info["native_h"]} '
        f'source={info["source_dpi"]}dpi '
        f'({info["variant"]} variant)',
        file=sys.stderr, flush=True,
    )


def _reset_caches_for_tests():
    """Clear cached one-shots so threshold overrides take effect mid-test."""
    global _total_ram_cache, _density_cache
    _total_ram_cache = None
    _density_cache = None
