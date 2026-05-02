#!/usr/bin/env bash
# test_peer.sh — verify each AZT-suite peer app on the attached Android
# device has the right manifest plumbing to reach the standalone server APK.
#
# Run AFTER server_apk/test_install.sh passes — this script tests the
# *peer* side of the round trip and assumes the server APK itself is
# installed and healthy.
#
# Tests, per peer:
#   1. <uses-permission> for AZT_COLLAB_ACCESS declared
#   2. permission granted (signature match against the server APK's key)
#   3. <queries> visibility for the server APK (Android 11+ requirement)
#   4. peer's signing certificate matches the suite fingerprint, if known
#
# Discovery: any installed package under domain `org.atoznback` that
# isn't the server APK itself. Override by passing peer package names
# as args, e.g.:
#     bash azt_collab_client/test_peer.sh org.atoznback.aztrecorder
#
# Exit code: 0 if every checked peer passes all tests; 1 otherwise.

set -uo pipefail

PERMISSION=org.atoznback.AZT_COLLAB_ACCESS
SERVER_PKG=org.atoznback.aztcollab
SUITE_DOMAIN=org.atoznback

pass=0
fail=0
warn=0
ok()   { echo "    PASS: $*"; pass=$((pass + 1)); }
bad()  { echo "    FAIL: $*"; fail=$((fail + 1)); }
hmm()  { echo "    WARN: $*"; warn=$((warn + 1)); }

if ! adb get-state >/dev/null 2>&1; then
    echo "FAIL: no Android device reachable via adb." >&2
    exit 1
fi

# Resolve the suite fingerprint if the canonical file is reachable.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
expected_fp_file="$SCRIPT_DIR/../android/SUITE_FINGERPRINT"
expected_fp=
if [ -f "$expected_fp_file" ]; then
    expected_fp=$(grep -oE '[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){31}' "$expected_fp_file" | head -1)
fi

# Sanity: server APK present?
server_path=$(adb shell pm path $SERVER_PKG 2>/dev/null | tr -d '\r' | sed 's/^package://')
if [ -z "$server_path" ]; then
    echo "FAIL: server APK ($SERVER_PKG) not installed. Run server_apk/test_install.sh first."
    exit 1
fi

# Build the peer list.
if [ $# -gt 0 ]; then
    peers="$*"
else
    peers=$(adb shell "pm list packages $SUITE_DOMAIN" 2>/dev/null \
        | tr -d '\r' \
        | sed 's/^package://' \
        | grep -v "^$SERVER_PKG$" \
        | tr '\n' ' ')
fi

if [ -z "$peers" ]; then
    echo "no peer apps found under $SUITE_DOMAIN. Install one (e.g., the recorder)."
    echo "Or pass package names explicitly:"
    echo "    bash $0 com.example.peer1 com.example.peer2"
    exit 1
fi

echo "Server APK at $server_path"
echo "Peers to check: $peers"

n_checked=0
for peer in $peers; do
    n_checked=$((n_checked + 1))
    echo
    echo "── peer $n_checked: $peer ──"
    DUMP=$(adb shell dumpsys package "$peer" 2>/dev/null | tr -d '\r')
    if [ -z "$DUMP" ]; then
        bad "could not dumpsys $peer (not installed?)"
        continue
    fi

    # 1. uses-permission for AZT_COLLAB_ACCESS
    if echo "$DUMP" | awk '/requested permissions:/{p=1;next} p && /^[^ ]/{p=0} p' \
            | grep -q "$PERMISSION"; then
        ok "$PERMISSION requested"
    else
        bad "$PERMISSION not in requested permissions — peer's android.permissions line missing it?"
    fi

    # 2. permission granted (signature match)
    if echo "$DUMP" | grep -E "$PERMISSION: granted=true" >/dev/null; then
        ok "$PERMISSION granted=true (signature key matches the server APK's)"
    else
        if echo "$DUMP" | grep -q "$PERMISSION"; then
            bad "$PERMISSION mentioned but not granted — peer is signed with a different key than the server APK"
        else
            bad "$PERMISSION not granted (and not requested)"
        fi
    fi

    # 3. <queries> visibility for the server APK. dumpsys 'queries:' / 'Queries:'
    #    section format varies by Android version — match either.
    queries_block=$(echo "$DUMP" | awk '
        BEGIN{p=0}
        tolower($0) ~ /^[ \t]*queries:/ {p=1; next}
        p && /^[A-Z]/ {p=0}
        p {print}
    ')
    if echo "$queries_block" | grep -q "$SERVER_PKG"; then
        ok "<queries> includes $SERVER_PKG (visible on Android 11+)"
    else
        # Android 10 and below ignore <queries> but allow visibility anyway.
        api=$(adb shell getprop ro.build.version.sdk 2>/dev/null | tr -d '\r')
        if [ -n "$api" ] && [ "$api" -lt 30 ] 2>/dev/null; then
            hmm "<queries> for $SERVER_PKG not declared, but device is API $api (pre-11) so visibility isn't restricted"
        else
            bad "no <queries> entry for $SERVER_PKG — manifest_extras.xml symlink set up?"
        fi
    fi

    # 4. signing fingerprint matches the suite (if we know it)
    if [ -n "$expected_fp" ]; then
        peer_fp=$(echo "$DUMP" \
            | grep -iE 'Signature|signing' \
            | grep -oE '[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){31}' \
            | head -1)
        if [ -z "$peer_fp" ]; then
            # dumpsys often hides cert hashes; try keytool on the pulled APK instead.
            peer_path=$(adb shell pm path "$peer" 2>/dev/null | tr -d '\r' | sed 's/^package://')
            if [ -n "$peer_path" ] && command -v keytool >/dev/null; then
                pulled=$(mktemp -t peer-apk.XXXXXX.apk)
                adb pull "$peer_path" "$pulled" >/dev/null 2>&1
                peer_fp=$(keytool -printcert -jarfile "$pulled" 2>/dev/null \
                    | grep -i "SHA256:" \
                    | grep -oE '[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){31}' \
                    | head -1)
                rm -f "$pulled"
            fi
        fi
        if [ -z "$peer_fp" ]; then
            hmm "couldn't read peer signing fingerprint to verify against suite"
        elif [ "${peer_fp,,}" = "${expected_fp,,}" ]; then
            ok "signing fingerprint matches android/SUITE_FINGERPRINT"
        else
            bad "signing fingerprint mismatch:"
            echo "        peer:     $peer_fp"
            echo "        expected: $expected_fp"
        fi
    fi
done

echo
echo "════════════════════════════════════════"
echo "  $n_checked peer(s) checked: $pass passed, $fail failed, $warn warnings"
echo "════════════════════════════════════════"
[ $fail -eq 0 ]
