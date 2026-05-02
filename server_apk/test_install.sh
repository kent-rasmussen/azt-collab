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

section "4. <provider> at authority=$AUTHORITY"
if echo "$DUMP" | grep "authority=" | grep -q "$AUTHORITY"; then
    ok "<provider authority=$AUTHORITY> registered"
    if echo "$DUMP" | grep -q AZTCollabProvider; then
        ok "  provider class is AZTCollabProvider"
    else
        hmm "provider registered but class name unexpected — manifest hook may have rendered differently"
    fi
else
    bad "no provider with authority=$AUTHORITY — _inject_aztcollab_provider hook didn't fire"
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
sleep 3
crash=$(adb logcat -d 2>&1 | grep -E "FATAL EXCEPTION|$PKG.*has died|Process.*$PKG.*killed" | head -3)
if [ -n "$crash" ]; then
    bad "activity crashed:"
    printf '%s\n' "$crash" | sed 's/^/    /'
elif adb shell "pidof $PKG || ps -A 2>/dev/null | grep -F $PKG" 2>/dev/null | grep -q .; then
    ok "process running"
else
    hmm "process not running but no crash logged — may have exited cleanly"
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

section "11. Build hook traces (last buildlog at /tmp/buildlog.txt)"
BUILDLOG=/tmp/buildlog.txt
if [ ! -f "$BUILDLOG" ]; then
    hmm "no $BUILDLOG. Re-run with: 'buildozer android debug 2>&1 | tee /tmp/buildlog.txt'"
else
    hook_lines=$(grep -E "Hook: (execute|ignore) (before_apk_build|before_apk_assemble|after_apk_build|before_apk_assemble|after_apk_assemble)|inject_aztcollab|aztcollab-provider-injection|^\[hook\]" "$BUILDLOG" 2>/dev/null)
    if [ -z "$hook_lines" ]; then
        bad "no hook traces in $BUILDLOG — p4a's --hook= arg may not be reaching the apk() phase"
    else
        # Just count what we found; print the unique kinds.
        echo "$hook_lines" | sed 's/^/    /' | head -20
        if echo "$hook_lines" | grep -q "Hook: execute before_apk_assemble"; then
            ok "before_apk_assemble fired"
        else
            bad "before_apk_assemble never fired — _inject_aztcollab_provider couldn't run"
        fi
        if echo "$hook_lines" | grep -q "injected AZTCollabProvider"; then
            ok "provider injection ran successfully"
        elif echo "$hook_lines" | grep -q "already has aztcollab provider"; then
            ok "provider already injected (re-run skip)"
        elif echo "$hook_lines" | grep -q "skipping aztcollab provider"; then
            bad "provider injection skipped — see preceding line for reason"
        fi
    fi
fi

echo
echo "════════════════════════════════════════"
echo "  $pass passed, $fail failed, $warn warnings"
echo "════════════════════════════════════════"
[ $fail -eq 0 ]
