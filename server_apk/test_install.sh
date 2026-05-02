#!/usr/bin/env bash
# test_install.sh — smoke-test the AZT Collaboration service APK
# on the attached Android device.
#
# Tests, in order of "would have caught a real regression":
#   1. APK installed
#   2. <permission> declaration merged into manifest
#   3. signature self-grant (proves keystore matches its own permission)
#   4. <provider> registered at expected authority
#   5. direct ContentResolver call (smoke test)
#   6. azt_collabd Python module actually bundled
#   7. Activity launches without immediate crash
#
# Usage:
#     bash server_apk/test_install.sh
#
# Exit code: 0 if all pass, 1 if any fail. Warnings don't fail.

set -uo pipefail

PKG=org.atoznback.aztcollab
AUTHORITY=org.atoznback.aztcollab
PERMISSION=org.atoznback.AZT_COLLAB_ACCESS

pass=0
fail=0
warn=0
ok()   { echo "  PASS: $*"; pass=$((pass + 1)); }
bad()  { echo "  FAIL: $*"; fail=$((fail + 1)); }
hmm()  { echo "  WARN: $*"; warn=$((warn + 1)); }
section() { echo; echo "── $* ──"; }

# Bail early if no device.
if ! adb get-state >/dev/null 2>&1; then
    echo "FAIL: no Android device reachable via adb." >&2
    exit 1
fi
echo "device: $(adb shell getprop ro.product.model 2>/dev/null | tr -d '\r') (Android $(adb shell getprop ro.build.version.release 2>/dev/null | tr -d '\r'))"

section "1. APK installed"
apk_path=$(adb shell pm path $PKG 2>/dev/null | tr -d '\r' | sed 's/^package://')
if [ -z "$apk_path" ]; then
    bad "$PKG not installed (build & adb install -r server_apk/bin/aztcollab-*-debug.apk)"
    echo
    echo "Summary: $pass passed, $fail failed, $warn warnings"
    exit 1
fi
ok "$PKG installed at $apk_path"

# One dumpsys; grep it several ways.
DUMP=$(adb shell dumpsys package $PKG 2>/dev/null | tr -d '\r')

section "2. <permission> declaration"
if echo "$DUMP" | awk '/declared permissions:/{p=1;next} p && /^[^ ]/{p=0} p' | grep -q "$PERMISSION"; then
    ok "$PERMISSION declared by the APK"
else
    bad "$PERMISSION not declared — server_apk/manifest_extras.xml didn't merge"
fi

section "3. signature self-grant (keystore sanity)"
if echo "$DUMP" | grep -E "$PERMISSION: granted=true" >/dev/null; then
    ok "$PERMISSION granted=true to $PKG (signing key matches its own <permission>)"
else
    bad "$PERMISSION not granted to itself — signing problem"
fi

section "4. <provider> at authority=$AUTHORITY (system view)"
# Three independent sources, in preference order. PASS if any shows
# the authority. Android 14's per-package dumpsys formatting can
# differ from older versions; the system-wide provider table and
# `pm dump` are reliable backups.
found_via=""

# A: per-package dumpsys. Match any registration signature:
#    1. authority=<auth>            (older Android format)
#    2. authorities=<auth>          (some variants)
#    3. [<auth>]:                   (Android 14 "ContentProvider Authorities" section)
#    4. <pkg>/.<X>Provider          (Android 14 "Registered ContentProviders" section)
if echo "$DUMP" | grep -E "(authority=|authorities=)$AUTHORITY|\[$AUTHORITY\]:|$PKG/\.[A-Za-z]+Provider" >/dev/null; then
    found_via="dumpsys package $PKG"
fi

# B: system-wide provider table — check for the bracketed-authority
#    form OR the Provider{...$pkg/.<X>Provider} pattern, both of which
#    only appear when the system has registered our provider.
sys_prov=$(adb shell dumpsys package providers 2>/dev/null | tr -d '\r')
if [ -z "$found_via" ] && echo "$sys_prov" \
        | grep -E "\[$AUTHORITY\]:|$PKG/\.[A-Za-z]+Provider" >/dev/null; then
    found_via="dumpsys package providers"
fi

# C: pm dump fallback (sometimes carries provider info even when the
#    sectioned dumpsys output omits it).
pm_dump=$(adb shell pm dump "$PKG" 2>/dev/null | tr -d '\r')
if [ -z "$found_via" ] && echo "$pm_dump" \
        | grep -E "\[$AUTHORITY\]:|$PKG/\.[A-Za-z]+Provider|authority=$AUTHORITY" >/dev/null; then
    found_via="pm dump $PKG"
fi

if [ -n "$found_via" ]; then
    ok "<provider authority=$AUTHORITY> registered (visible via $found_via)"
    # Class-name verification lives in §13 (aapt dump xmltree of the
    # APK's actual manifest). Don't re-check here — dumpsys formats the
    # class field inconsistently across Android versions, leading to
    # noisy false-warns when §13 already confirms the manifest is right.
else
    bad "no provider with authority=$AUTHORITY in any system view"
    echo "    Tried: dumpsys package $PKG, dumpsys package providers, pm dump $PKG"
    # Dump diagnostic context so we can see WHAT the system thinks of
    # this package's providers (if anything).
    echo "    --- dumpsys package $PKG: any 'provider' or 'authority' lines ---"
    matches=$(echo "$DUMP" | grep -iE 'provider|authorit|aztcollab' | head -20)
    if [ -n "$matches" ]; then
        printf '%s\n' "$matches" | sed 's/^/      /'
    else
        echo "      (none)"
    fi
    echo "    --- dumpsys package providers grep aztcollab ---"
    aztcollab_in_sys=$(echo "$sys_prov" | grep -i aztcollab | head -10)
    if [ -n "$aztcollab_in_sys" ]; then
        printf '%s\n' "$aztcollab_in_sys" | sed 's/^/      /'
    else
        echo "      (none — system-wide table doesn't know about this package's providers)"
    fi
    echo "    --- last 50 logcat lines mentioning PackageManager + $PKG ---"
    pm_log=$(adb logcat -d 2>/dev/null \
        | grep -iE "PackageManager|PackageParser|PackageInstaller|aztcollab" \
        | tail -50)
    if [ -n "$pm_log" ]; then
        printf '%s\n' "$pm_log" | sed 's/^/      /'
    else
        echo "      (logcat buffer empty for these tags — try uninstall + install + this script in sequence)"
    fi
fi

section "5. direct ContentResolver call"
# adb shell is unsigned by your suite key; "Permission Denial" here is the
# correct success signal — it proves the provider is registered AND the
# signature permission is being enforced. "Could not find provider" means
# the provider isn't registered.
qout=$(adb shell "content query --uri content://$AUTHORITY/ping" 2>&1 | tr -d '\r')
if echo "$qout" | grep -qi "could not find provider"; then
    bad "provider not exported: $qout"
elif echo "$qout" | grep -qi "permission denial"; then
    ok "Permission Denial — provider registered, signature enforcement working"
elif echo "$qout" | grep -qiE "no result|^row "; then
    ok "provider responded with data"
else
    hmm "unexpected response: $qout"
fi

section "6. azt_collabd / azt_collab_client modules bundled"
tmp=$(mktemp -t aztcollab.apk.XXXXXX)
trap 'rm -f "$tmp"' EXIT
adb pull "$apk_path" "$tmp" >/dev/null 2>&1
# Stream assets through tar via a temp file — bash command substitution
# strips null bytes and corrupts binary data.
listing_file=$(mktemp -t aztcollab.list.XXXXXX)
asset_used=
for asset in private.tar.gz private.tar private.mp3; do
    : >"$listing_file"
    # Try gzip-tar first, then plain tar; whichever produces output wins.
    unzip -p "$tmp" "assets/$asset" 2>/dev/null | tar -tz 2>/dev/null >"$listing_file"
    if [ ! -s "$listing_file" ]; then
        unzip -p "$tmp" "assets/$asset" 2>/dev/null | tar -t 2>/dev/null >"$listing_file"
    fi
    if [ -s "$listing_file" ]; then
        asset_used="$asset"
        break
    fi
done
trap 'rm -f "$tmp" "$listing_file"' EXIT
if [ -z "$asset_used" ]; then
    bad "could not read any private.* asset from APK — APK structure unexpected"
else
    if grep -q '^azt_collabd/\|^./azt_collabd/' "$listing_file"; then
        ok "azt_collabd/ in assets/$asset_used"
    else
        bad "azt_collabd not bundled (run server_apk/setup.sh to create symlinks?)"
    fi
    if grep -q '^azt_collab_client/\|^./azt_collab_client/' "$listing_file"; then
        ok "azt_collab_client/ in assets/$asset_used"
    else
        bad "azt_collab_client not bundled (run server_apk/setup.sh?)"
    fi
fi

section "7. Launcher icon (not default Kivy logo)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
src_icon="$SCRIPT_DIR/icons/icon.png"
if [ ! -f "$src_icon" ]; then
    hmm "source icon $src_icon doesn't exist — can't compare; skipping"
else
    # Look for any icon.png inside res/mipmap-* in the APK.
    icon_entries=$(unzip -l "$tmp" 2>/dev/null \
        | awk '/res\/mipmap.*icon\.png$/ {print $NF}')
    if [ -z "$icon_entries" ]; then
        bad "no mipmap icon.png in APK — buildozer.spec's icon.filename was ignored"
    else
        # Prefer highest-res bucket; fall back to whatever exists.
        entry=
        for variant in xxxhdpi xxhdpi xhdpi hdpi mdpi anydpi-v26 anydpi; do
            entry=$(printf '%s\n' "$icon_entries" | grep "mipmap-$variant/" | head -1)
            [ -n "$entry" ] && break
        done
        [ -z "$entry" ] && entry=$(printf '%s\n' "$icon_entries" | head -1)
        out=/tmp/installed-icon-$$.png
        trap 'rm -f "$tmp" "$out"' EXIT
        unzip -p "$tmp" "$entry" >"$out" 2>/dev/null
        sz=$(wc -c <"$out" 2>/dev/null || echo 0)
        if [ "$sz" -eq 0 ]; then
            hmm "could not extract $entry"
        elif cmp -s "$out" "$src_icon" 2>/dev/null; then
            ok "bundled icon ($entry) matches source byte-for-byte"
        elif [ "$sz" -lt 4000 ]; then
            bad "bundled icon is only $sz bytes — almost certainly the default Kivy logo"
            echo "    extracted to $out for inspection"
        else
            ok "bundled icon ($entry, $sz B) differs from source ($(wc -c <"$src_icon") B) — likely aapt-crunched, not the default"
            echo "    extracted to $out — eyeball if you want to be sure"
        fi
    fi
fi

section "8. Activity launches"
adb shell am force-stop $PKG >/dev/null 2>&1 || true
adb logcat -c >/dev/null 2>&1 || true
adb shell am start -n $PKG/org.kivy.android.PythonActivity >/dev/null 2>&1
sleep 4
log=$(adb logcat -d 2>&1)

# Three independent crash signatures, gathered together so we can show
# whichever one is actually present (Java stacktrace / Python traceback /
# bare process-death).
fatal=$(printf '%s\n' "$log" | grep -A 30 'FATAL EXCEPTION' | head -50)
# Python tracebacks: capture LOTS of trailing context so we see the
# actual crash, not just the first non-fatal warning. Kivy logs many
# benign tracebacks (e.g., logo-copy permission denied) before the
# real one.
py_err=$(printf '%s\n' "$log" \
    | grep -B 1 -A 30 -E 'ModuleNotFoundError|ImportError|AttributeError|RuntimeError|Traceback \(most recent' \
    | tail -60)
death=$(printf '%s\n' "$log" | grep -E "$PKG.*has died|Process.*$PKG.*killed|signal.*$PKG" | head -3)

# Last 30 python-tag lines — usually the most diagnostic snapshot of
# what was happening right before the process died.
py_tail=$(printf '%s\n' "$log" \
    | grep -E "^.*[[:space:]][IEDWFV][[:space:]]+python[[:space:]]" \
    | tail -30)

# python tag at E severity specifically.
py_log=$(printf '%s\n' "$log" \
    | grep -E "^.*[[:space:]]E[[:space:]]+python" \
    | head -10)

# Process dead AND no recognized error pattern? Show the python tail
# so the user sees the last thing that happened before the process died.
proc_alive=$(adb shell "pidof $PKG" 2>/dev/null | tr -d '\r')

if [ -n "$fatal" ] || [ -n "$death" ] || [ -z "$proc_alive" ]; then
    bad "activity didn't reach a running steady state"
    if [ -n "$fatal" ]; then
        echo "    --- Java FATAL EXCEPTION ---"
        printf '%s\n' "$fatal" | sed 's/^/    /'
    fi
    if [ -n "$py_err" ]; then
        echo "    --- Python error(s) (last in window) ---"
        printf '%s\n' "$py_err" | sed 's/^/    /'
    fi
    if [ -n "$py_tail" ]; then
        echo "    --- last 30 python-tagged log lines ---"
        printf '%s\n' "$py_tail" | sed 's/^/    /'
    fi
    if [ -n "$death" ]; then
        echo "    --- process death ---"
        printf '%s\n' "$death" | sed 's/^/    /'
    fi
    if [ -z "$fatal" ] && [ -z "$py_err" ] && [ -z "$py_tail" ] && [ -z "$death" ]; then
        echo "    No diagnostic markers caught. Manual capture:"
        echo "      adb logcat -c"
        echo "      adb shell am start -n $PKG/org.kivy.android.PythonActivity"
        echo "      adb logcat AndroidRuntime:E python:* '*:S'"
    fi
else
    ok "process running (pid $proc_alive)"
fi

section "9. Source-tree symlinks (only meaningful from a source checkout)"
for name in azt_collabd azt_collab_client; do
    p="$SCRIPT_DIR/$name"
    if [ -L "$p" ]; then
        target=$(readlink "$p")
        if [ -e "$p" ]; then
            ok "$name -> $target"
        else
            bad "$name symlink broken: -> $target (target missing)"
        fi
    elif [ -d "$p" ]; then
        hmm "$name is a real dir, not a symlink — fine if intentional, but setup.sh would have made it a symlink"
    else
        bad "$name absent from $SCRIPT_DIR — run server_apk/setup.sh"
    fi
done

section "10. Dist AndroidManifest.xml (the file that was packaged)"
# Resolve build_dir from buildozer.spec; fall back to ~/.buildozer.
BUILD_DIR=$(grep -E '^[[:space:]]*build_dir[[:space:]]*=' "$SCRIPT_DIR/buildozer.spec" 2>/dev/null \
    | head -1 \
    | sed -E 's/^[^=]*=[[:space:]]*//' \
    | sed -E 's/[[:space:]]+$//')
[ -z "$BUILD_DIR" ] && BUILD_DIR="$HOME/.buildozer"
DIST_MANIFEST=$(ls "$BUILD_DIR"/android/platform/build-*/dists/aztcollab/AndroidManifest.xml 2>/dev/null | head -1)
if [ -z "$DIST_MANIFEST" ] || [ ! -f "$DIST_MANIFEST" ]; then
    hmm "no dist AndroidManifest.xml under $BUILD_DIR/android/platform/build-*/dists/aztcollab/ — fresh checkout? clean build?"
else
    echo "  manifest: $DIST_MANIFEST"
    if grep -q 'aztcollab-provider-injection' "$DIST_MANIFEST"; then
        ok "injection sentinel present (the hook ran on the most recent build)"
    else
        bad "no 'aztcollab-provider-injection' marker — _inject_aztcollab_provider didn't fire"
    fi
    if grep -q '<provider' "$DIST_MANIFEST"; then
        ok "<provider> element present in dist manifest"
    else
        bad "no <provider> in dist manifest — installed APK can't have one either"
    fi
fi

section "11. Build hook traces (informational — supplements §10)"
# §10 is the authoritative answer for whether the hook ran. §11 only adds
# context if /tmp/buildlog.txt happens to be a fresh tee of the last build.
# Always informational (never FAIL); section 10's verdict is what counts.
BUILDLOG=/tmp/buildlog.txt
if [ ! -f "$BUILDLOG" ]; then
    echo "  no $BUILDLOG — re-run with 'buildozer android debug 2>&1 | tee /tmp/buildlog.txt' if you want hook traces here"
else
    # Compare buildlog age vs. dist manifest age. If the buildlog is older
    # than the manifest, the buildlog is from a previous build and any
    # absence of hook traces is meaningless.
    if [ -n "$DIST_MANIFEST" ] && [ -f "$DIST_MANIFEST" ] \
            && [ "$BUILDLOG" -ot "$DIST_MANIFEST" ]; then
        echo "  $BUILDLOG is older than the dist manifest — likely stale from an earlier build; skipping"
    else
        hook_lines=$(grep -E "Hook: (execute|ignore) (before_apk_build|before_apk_assemble|after_apk_build|after_apk_assemble)|inject_aztcollab|aztcollab-provider-injection|^\[hook\]" "$BUILDLOG" 2>/dev/null)
        if [ -z "$hook_lines" ]; then
            echo "  no hook traces in $BUILDLOG — odd if §10 passed; otherwise expected"
        else
            echo "$hook_lines" | sed 's/^/    /' | head -20
        fi
    fi
fi

section "12. Installed APK matches the freshest build artifact"
# Compares the installed APK's hash to the newest debug APK in bin/.
# Catches "I rebuilt but forgot to adb install" — the most common
# silent regression after a manifest/hook fix.
fresh_apk=$(ls -t "$SCRIPT_DIR"/bin/aztcollab-*-debug.apk 2>/dev/null | head -1)
if [ -z "$fresh_apk" ] || [ ! -f "$fresh_apk" ]; then
    hmm "no APK in $SCRIPT_DIR/bin/aztcollab-*-debug.apk — can't compare"
else
    installed_md5=$(adb shell md5sum "$apk_path" 2>/dev/null | awk '{print $1}' | tr -d '\r')
    fresh_md5=$(md5sum "$fresh_apk" 2>/dev/null | awk '{print $1}')
    if [ -z "$installed_md5" ] || [ -z "$fresh_md5" ]; then
        hmm "couldn't compute md5 of one or both APKs"
    elif [ "$installed_md5" = "$fresh_md5" ]; then
        ok "installed APK matches $(basename "$fresh_apk")"
    else
        bad "installed APK is STALE — does not match $(basename "$fresh_apk")"
        echo "        adb install -r '$fresh_apk'"
        echo "        bash $0   # then re-run"
    fi
fi

section "13. Installed APK's actual manifest (authoritative)"
# Pulls the live APK and uses aapt to inspect its embedded manifest.
# Resolves the disagreement when §10 says "dist manifest patched"
# but §4 says "no provider visible to dumpsys". If the APK manifest
# itself lacks the provider, gradle is reading a different manifest
# than the one our hook patches (see §14, §15).
aapt_bin=$(command -v aapt 2>/dev/null || command -v aapt2 2>/dev/null || true)
if [ -z "$aapt_bin" ]; then
    hmm "neither aapt nor aapt2 on PATH — can't introspect APK manifest"
elif [ -z "${tmp:-}" ] || [ ! -s "$tmp" ]; then
    hmm "APK not pulled (§6 may have failed); skipping"
else
    if [ "$(basename "$aapt_bin")" = "aapt2" ]; then
        manifest_text=$("$aapt_bin" dump xmltree --file AndroidManifest.xml "$tmp" 2>/dev/null)
    else
        manifest_text=$("$aapt_bin" dump xmltree "$tmp" AndroidManifest.xml 2>/dev/null)
    fi
    if [ -z "$manifest_text" ]; then
        hmm "$aapt_bin produced no output — manifest unreadable"
    elif printf '%s' "$manifest_text" \
            | awk '/E: provider/{p=1} p{print} p && /^[[:space:]]*E:/ && !/E: provider/{exit}' \
            | grep -q "$AUTHORITY"; then
        ok "<provider authorities=\"$AUTHORITY\"> is in the APK's own manifest"
        echo "    (so §4's dumpsys FAIL is misleading — provider IS in the APK)"
    else
        bad "no <provider> for $AUTHORITY in the APK's manifest"
        echo "    The dist manifest had it (§10) but gradle didn't bundle it."
        echo "    See §14 and §15 for which manifest gradle is actually reading."
    fi
fi

section "14. All AndroidManifest*.xml in the dist (which carry the patch?)"
if [ -z "${DIST_MANIFEST:-}" ]; then
    hmm "no dist dir resolved earlier; skipping"
else
    DIST_DIR=$(dirname "$DIST_MANIFEST")
    found=0
    while IFS= read -r m; do
        found=1
        if grep -q 'aztcollab-provider-injection' "$m" 2>/dev/null; then
            mark="patched"
        else
            mark="UNPATCHED"
        fi
        if grep -q '<provider' "$m" 2>/dev/null; then
            prov="<provider>"
        else
            prov="no <provider>"
        fi
        echo "    [$mark / $prov] $m"
    done < <(find "$DIST_DIR" -name "AndroidManifest*.xml" 2>/dev/null)
    [ "$found" -eq 0 ] && hmm "no AndroidManifest*.xml found under $DIST_DIR"
    echo "    A row marked UNPATCHED is a candidate for the file gradle reads instead."
fi

section "15. Gradle manifest source (where gradle is configured to read)"
if [ -z "${DIST_MANIFEST:-}" ]; then
    hmm "no dist dir resolved earlier; skipping"
else
    DIST_DIR=$(dirname "$DIST_MANIFEST")
    GRADLE="$DIST_DIR/build.gradle"
    if [ ! -f "$GRADLE" ]; then
        hmm "no build.gradle at $GRADLE"
    else
        echo "    $GRADLE:"
        matches=$(grep -nE 'manifest|srcFile|sourceSets' "$GRADLE" 2>/dev/null)
        if [ -n "$matches" ]; then
            echo "$matches" | sed 's/^/      /'
        else
            echo "      (no explicit manifest/srcFile config — gradle uses default 'src/main/AndroidManifest.xml')"
            echo "      That likely points at $DIST_DIR/src/main/AndroidManifest.xml,"
            echo "      not $DIST_MANIFEST."
        fi
    fi
fi

echo
echo "════════════════════════════════════════"
echo "  $pass passed, $fail failed, $warn warnings"
echo "════════════════════════════════════════"
[ $fail -eq 0 ]
