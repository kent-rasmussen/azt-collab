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
    # Strip any "SHA[- ]?256:" / "SHA[- ]?1:" labels first so the
    # regex can't latch onto the "56" inside "SHA256:" and produce
    # an off-by-one fingerprint.
    expected_fp=$(sed -E 's/SHA[- ]?(256|1)[: ]+//g' "$expected_fp_file" \
        | grep -oE '[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){31}' \
        | head -1)
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
exclude woa, list?
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
    if [ "$peer" == "org.atoznback.woa" ] #Not a client app!
    	then continue
    fi
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

    # 4. signing fingerprint matches the suite (if we know it).
    #    Three fallbacks because dumpsys hides cert hashes on modern
    #    Android and keytool / apksigner aren't always installed.
    if [ -n "$expected_fp" ]; then
        peer_fp=
        fp_source=
        fp_diag=
        peer_is_debug=0
        # Append a message to fp_diag, preserving any earlier ones so
        # the final WARN reflects every tool that was tried.
        diag_add() {
            if [ -z "$fp_diag" ]; then
                fp_diag="$1"
            else
                fp_diag="$fp_diag; $1"
            fi
        }

        # 4a. dumpsys (works on older Android / unusual configs).
        peer_fp=$(echo "$DUMP" \
            | grep -iE 'Signature|signing' \
            | sed -E 's/SHA[- ]?(256|1)[: ]+//g' \
            | grep -oE '[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){31}' \
            | head -1)
        [ -n "$peer_fp" ] && fp_source=dumpsys

        # Pull the APK once if we still need it (4b / 4c).
        peer_path=$(adb shell pm path "$peer" 2>/dev/null \
            | tr -d '\r' | sed 's/^package://')
        pulled=
        if [ -z "$peer_fp" ] && [ -n "$peer_path" ]; then
            pulled=$(mktemp -t peer-apk.XXXXXX.apk)
            if ! adb pull "$peer_path" "$pulled" >/dev/null 2>&1; then
                diag_add "adb pull $peer_path failed"
                rm -f "$pulled"
                pulled=
            fi
        elif [ -z "$peer_path" ]; then
            diag_add "pm path $peer returned no APK"
        fi

        # 4b. keytool (Java JDK). Reads v1 (JAR) signatures only —
        #     APKs signed v2/v3-only will return no certs here.
        if [ -z "$peer_fp" ] && [ -n "$pulled" ]; then
            if command -v keytool >/dev/null; then
                peer_fp=$(keytool -printcert -jarfile "$pulled" 2>/dev/null \
                    | sed -E 's/SHA[- ]?(256|1)[: ]+//g' \
                    | grep -oE '[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){31}' \
                    | head -1)
                if [ -n "$peer_fp" ]; then
                    fp_source=keytool
                else
                    diag_add "keytool: no v1 cert (likely v2/v3-only signing)"
                fi
            else
                diag_add "keytool not in PATH (install a JDK)"
            fi
        fi

        # 4c. apksigner (Android SDK build-tools). Understands v2/v3.
        if [ -z "$peer_fp" ] && [ -n "$pulled" ]; then
            apksigner_bin=
            # Prefer apksigner on PATH; fall back to the highest
            # build-tools version under ANDROID_HOME / ANDROID_SDK_ROOT
            # / buildozer's bundled SDK.
            if command -v apksigner >/dev/null; then
                apksigner_bin=$(command -v apksigner)
            else
                for sdk in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" \
                        "$HOME/.buildozer/android/platform/android-sdk"; do
                    [ -z "$sdk" ] && continue
                    candidate=$(ls "$sdk"/build-tools/*/apksigner 2>/dev/null \
                        | sort -V | tail -1)
                    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
                        apksigner_bin="$candidate"
                        break
                    fi
                done
            fi
            if [ -n "$apksigner_bin" ] && [ -x "$apksigner_bin" ]; then
                apksigner_out=$("$apksigner_bin" verify --print-certs "$pulled" 2>/dev/null)
                peer_fp=$(echo "$apksigner_out" \
                    | grep -i "SHA-256 digest" \
                    | grep -oE '[0-9A-Fa-f]{64}' \
                    | head -1 \
                    | sed 's/\(..\)/\1:/g; s/:$//')
                # Debug-signed APKs come out of `buildozer android
                # debug` with the default Android Debug keystore
                # (CN=Android Debug). The suite-fingerprint match only
                # applies to release builds; flag the cert subject
                # so the comparison below can skip cleanly.
                if echo "$apksigner_out" | grep -q 'CN=Android Debug'; then
                    peer_is_debug=1
                fi
                if [ -n "$peer_fp" ]; then
                    fp_source=apksigner
                else
                    diag_add "apksigner: ran but no SHA-256 digest in output"
                fi
            else
                diag_add "apksigner not found (set ANDROID_HOME or apt install apksigner)"
            fi
        fi

        # 4d. openssl on the unzipped APK META-INF cert. v2/v3
        #     signatures live in the APK Signing Block, not META-INF,
        #     so this also fails on v2/v3-only APKs — but it's a
        #     quick sanity fallback when neither keytool nor apksigner
        #     is available.
        if [ -z "$peer_fp" ] && [ -n "$pulled" ] && command -v openssl >/dev/null \
                && command -v unzip >/dev/null; then
            cert_dir=$(mktemp -d -t peer-cert.XXXXXX)
            unzip -qq -o "$pulled" 'META-INF/*.RSA' 'META-INF/*.DSA' 'META-INF/*.EC' \
                -d "$cert_dir" 2>/dev/null
            cert_file=$(ls "$cert_dir"/META-INF/*.RSA "$cert_dir"/META-INF/*.DSA \
                "$cert_dir"/META-INF/*.EC 2>/dev/null | head -1)
            if [ -n "$cert_file" ]; then
                peer_fp=$(openssl pkcs7 -inform DER -in "$cert_file" -print_certs \
                    2>/dev/null \
                    | openssl x509 -noout -fingerprint -sha256 2>/dev/null \
                    | sed -E 's/SHA[- ]?(256|1)[: ]+//g; s/Fingerprint=//' \
                    | grep -oE '[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){31}' \
                    | head -1)
                if [ -n "$peer_fp" ]; then
                    fp_source=openssl
                else
                    diag_add "openssl: cert in $cert_file not parseable"
                fi
            else
                diag_add "openssl: no META-INF/*.{RSA,DSA,EC} (v2/v3-only APK)"
            fi
            rm -rf "$cert_dir"
        fi

        [ -n "$pulled" ] && rm -f "$pulled"

        if [ -z "$peer_fp" ]; then
            hmm "couldn't read peer signing fingerprint: ${fp_diag:-unknown}"
        elif [ "$peer_is_debug" = "1" ]; then
            hmm "peer is signed with the Android Debug keystore (CN=Android Debug); SUITE_FINGERPRINT check skipped (only meaningful for release builds)"
        elif [ "${peer_fp,,}" = "${expected_fp,,}" ]; then
            ok "signing fingerprint matches android/SUITE_FINGERPRINT (via $fp_source)"
        else
            bad "signing fingerprint mismatch (via $fp_source):"
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
