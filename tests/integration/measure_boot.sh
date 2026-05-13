#!/bin/bash
# Cold-start boot measurement for the Phase B/C plan.
#
# Captures [boot-trace-peer] + [boot-trace-daemon] lines from
# logcat across four scenarios:
#
#   baseline   — normal cold start
#   doze       — device forced into doze (Q2 in
#                  docs/daemon_boot_plan.md)
#   prewarm    — peer expected to call prewarm() in App.build()
#                  (Q3)
#   doze+prewarm — combination (sanity check of the worst case)
#
# This script doesn't itself implement prewarm in the peer — that's
# a peer-repo change (recorder / viewer). Run scenarios where the
# peer build under test does or doesn't call prewarm and tag the
# output accordingly via $SCENARIO.
#
# Usage:
#   tests/integration/measure_boot.sh <peer-package> [iterations] [scenario]
#
# Example:
#   tests/integration/measure_boot.sh org.atoznback.aztrecorder 5 baseline
#   tests/integration/measure_boot.sh org.atoznback.aztrecorder 5 doze
#
# Requires:
#   - adb in PATH; device connected and unlocked.
#   - server APK + peer APK installed and signed by the suite keystore.
#   - The peer logs to stderr (Kivy default); the daemon's
#     :provider process bridges stdio→logcat via service.py.
#
# Output: one log file per iteration in $OUT_DIR, then runs the
# Python parser to render a per-iteration summary.

set -euo pipefail

PEER_PKG="${1:-}"
ITERATIONS="${2:-5}"
SCENARIO="${3:-baseline}"
SERVER_PKG="org.atoznback.aztcollab"
OUT_DIR="${OUT_DIR:-tests/integration/measurements/$SCENARIO-$(date +%Y%m%d-%H%M%S)}"
SETTLE_SECONDS="${SETTLE_SECONDS:-30}"
# ``KILL_SERVER=0`` keeps the server APK alive between iterations.
# Default (``1``) force-stops it for true cold-start; flip to 0 on
# devices where the Android 15 freezer prevents lazy-spawn from
# completing during a peer-only window — there, you'd be measuring
# "daemon never booted" forever. With KILL_SERVER=0, you get
# peer-cold-start against a warm daemon, which is what users
# actually feel after the first launch of the day. Caller-side
# warm-up: open the server APK launcher manually before invoking
# the harness so its main process + ``:provider`` are both
# already alive.
KILL_SERVER="${KILL_SERVER:-1}"

if [[ -z "$PEER_PKG" ]]; then
    echo "usage: $0 <peer-package> [iterations] [scenario]" >&2
    echo "  e.g. $0 org.atoznback.aztrecorder 5 baseline" >&2
    exit 2
fi
case "$SCENARIO" in
    baseline|doze|prewarm|doze+prewarm) ;;
    *) echo "scenario must be one of: baseline doze prewarm doze+prewarm" >&2
       exit 2 ;;
esac

mkdir -p "$OUT_DIR"
echo "scenario=$SCENARIO peer=$PEER_PKG iterations=$ITERATIONS out=$OUT_DIR"

setup_doze() {
    # adb shell dumpsys deviceidle force-idle requires the device to
    # be unplugged and screen off; emulate via dumpsys. Restored at
    # script exit by teardown_doze.
    echo "  forcing device into doze…"
    adb shell dumpsys battery unplug > /dev/null
    adb shell dumpsys deviceidle force-idle > /dev/null || \
        echo "  (force-idle returned nonzero; some ROMs reject it — proceeding anyway)"
    sleep 2
}

teardown_doze() {
    echo "  restoring normal device state…"
    adb shell dumpsys deviceidle unforce > /dev/null || true
    adb shell dumpsys battery reset > /dev/null || true
}

setup_prewarm_off() {
    # Drop a sentinel inside the peer's private azt_home so
    # bootstrap.prewarm() takes the early-return path. Survives
    # peer process restarts; teardown removes it.
    echo "  disabling prewarm via sentinel for $PEER_PKG…"
    adb shell "run-as $PEER_PKG mkdir -p files/azt" 2>/dev/null || true
    adb shell "run-as $PEER_PKG touch files/azt/_no_prewarm" \
        || echo "  (run-as failed — peer APK may not be debuggable; sentinel skipped)"
}

teardown_prewarm_off() {
    adb shell "run-as $PEER_PKG rm -f files/azt/_no_prewarm" \
        2>/dev/null || true
}

case "$SCENARIO" in
    doze|doze+prewarm) setup_doze ;;
esac
case "$SCENARIO" in
    baseline|doze)
        # Force prewarm off so a peer build that calls prewarm()
        # in App.build() still measures the unhelped path.
        setup_prewarm_off
        ;;
    prewarm|doze+prewarm)
        # Make sure no stale sentinel from a previous run is in
        # place.
        teardown_prewarm_off
        ;;
esac

# Add prewarm-off teardown to the exit trap.
restore_all() {
    teardown_doze
    teardown_prewarm_off
}
trap 'restore_all' EXIT

for i in $(seq 1 "$ITERATIONS"); do
    LOG="$OUT_DIR/run-$i.log"
    echo "iteration $i → $LOG"

    # Force-stop the peer for a true cold start. Server APK is
    # killed by default (``KILL_SERVER=1``); set ``KILL_SERVER=0``
    # to leave it alive across iterations so peer-cold-start is
    # measured against a warm daemon (useful on Android-15-freezer-
    # affected devices where the daemon can't cold-start cleanly).
    # ``|| true`` because some adb / Android combos report nonzero
    # on these even when the kill succeeded.
    adb shell am force-stop "$PEER_PKG" || true
    if [[ "$KILL_SERVER" == "1" ]]; then
        adb shell am force-stop "$SERVER_PKG" || true
    fi
    sleep 1

    # Clear logcat so we only capture this run's lines.
    adb logcat -c || true

    # Launch the peer via its launcher activity. We resolve the
    # actual launcher activity through PackageManager rather than
    # using ``monkey`` (which reports nonzero exits in cases that
    # are actually fine — its exit code reflects internal event
    # counts, not just dispatch success — and would kill the
    # script under ``set -e``). ``am start -W`` returns 0 on
    # successful launch and is quieter.
    ACTIVITY=$(adb shell cmd package resolve-activity \
        --brief -c android.intent.category.LAUNCHER \
        "$PEER_PKG" 2>/dev/null \
        | tail -1 | tr -d '\r')
    if [[ -z "$ACTIVITY" || "$ACTIVITY" != *"/"* ]]; then
        echo "  (could not resolve launcher activity for $PEER_PKG; falling back to monkey)" >&2
        adb shell monkey -p "$PEER_PKG" \
            -c android.intent.category.LAUNCHER 1 \
            > /dev/null 2>&1 || true
    else
        adb shell am start -W -n "$ACTIVITY" \
            > /dev/null 2>&1 || true
    fi

    # Wait for the peer to settle. SETTLE_SECONDS should comfortably
    # exceed the worst-case cold start observed on the slowest
    # device under test (≈30s on R500). Bump if you see the parser
    # missing late phases.
    sleep "$SETTLE_SECONDS"

    # Pull boot-trace lines (and only those — keeps file small,
    # parser robust). Threadtime format is the default;
    # ``-d`` reads the buffer and exits.
    adb logcat -d -v threadtime \
        | grep -E '\[boot-trace-(peer|daemon)\]' \
        > "$LOG" || true

    echo "  $(wc -l < "$LOG") trace lines captured"
done

# Restore doze + sentinel immediately after iterations so the device
# is normal while we run analysis (the trap also catches ctrl-C).
restore_all

# Per-iteration summary table. The Python parser prints both a
# detail table to stdout and an interval summary to stderr.
echo
echo "=== Per-iteration summaries ==="
for log in "$OUT_DIR"/run-*.log; do
    echo
    echo "--- $log ---"
    python3 tests/integration/parse_boot_traces.py < "$log" \
        > "$log.table" 2> "$log.summary" || true
    cat "$log.summary"
done

echo
echo "Run files: $OUT_DIR/"
echo "  *.log     — raw boot-trace lines from logcat"
echo "  *.table   — wall-clock-ordered table (tab-separated)"
echo "  *.summary — key-interval summary"
