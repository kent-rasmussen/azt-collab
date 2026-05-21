package org.atoznback.aztcollab;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Process;
import android.util.Log;
import java.io.File;

/**
 * Java-side recovery hatch for a corrupted or stale
 * {@code files/app/_python_bundle/}.
 *
 * <p><b>Problem this exists to solve.</b> p4a's C bootstrap extracts
 * {@code assets/private.*} to {@code _python_bundle/} only when the
 * directory is missing. On APK reinstall the directory persists, so
 * the daemon keeps importing the previous version's {@code .pyc}
 * files even though the on-disk APK is current. The service-side
 * {@code _maybe_reextract_python_bundle} (added in 0.43.22) is the
 * normal recovery, but it runs <i>inside</i> Python — if the bundle
 * is broken in a way that prevents Python from booting (e.g. the
 * 0.43.22–0.43.31 bz2 import bug, or any future module-load
 * failure), it cannot fire and the daemon is locked into the
 * broken code path forever.</p>
 *
 * <p><b>Why a Java receiver works.</b> BroadcastReceivers declared
 * in the manifest load from the APK's {@code classes.dex} — pure
 * Java, no dependency on {@code _python_bundle/}. So even if every
 * Python entrypoint in the APK is unrunnable, this receiver still
 * fires and can wipe the bundle. The next Activity launch then
 * triggers p4a's Activity-side extract-on-missing (which is
 * separate from the service-side path and has always worked),
 * which extracts fresh code from the running APK's assets. The
 * {@code :provider} service then lazy-spawns from the now-fresh
 * bundle and the daemon recovers.</p>
 *
 * <p><b>Why this is safe (unlike the 0.43.12 attempt).</b> 0.43.12
 * fired bundle wipe automatically on {@code ACTION_MY_PACKAGE_REPLACED}.
 * That broke the {@code :provider} service because Android's
 * lazy-spawn for a ContentProvider doesn't run the Activity's
 * extract-on-missing — and at the time, the service didn't have its
 * own extract either, so it crash-looped under START_STICKY.
 * Reverted in 0.43.14.</p>
 *
 * <p>This receiver is fired <i>manually</i> via
 * {@code adb shell am broadcast -n
 * org.atoznback.aztcollab/.BundleResetReceiver}, never
 * automatically. The user is expected to open the picker Activity
 * (server.ui) <i>after</i> firing the broadcast so the Activity-
 * side extract runs before any peer triggers a service spawn. The
 * service-side {@code _maybe_reextract_python_bundle} continues to
 * exist for the normal mtime-mismatch case; this receiver is
 * specifically for when that path itself is broken.</p>
 *
 * <p><b>Preserves $AZT_HOME.</b> The wipe is scoped strictly to
 * {@code files/app/_python_bundle/}. The daemon's data directory
 * {@code files/azt/} (projects, jobs.json, credentials, daemon.log)
 * is untouched.</p>
 *
 * <p><b>Usage:</b>
 * <pre>
 *   adb shell am broadcast -n \
 *     org.atoznback.aztcollab/.BundleResetReceiver
 *   # tap the AZT Collaboration launcher icon to open server.ui;
 *   # the Activity launch re-extracts the bundle from the new APK
 * </pre></p>
 *
 * <p>Manifest declaration is injected by p4a_hook.py's
 * {@code _inject_bundle_reset_receiver} step (gated on
 * {@code dist_name == 'aztcollab'} — peer APKs don't need it,
 * their daemon-spawned code lives in the server APK).</p>
 */
public class BundleResetReceiver extends BroadcastReceiver {
    private static final String TAG = "BundleReset";

    @Override
    public void onReceive(Context context, Intent intent) {
        String pkg = context != null ? context.getPackageName() : "?";
        Log.i(TAG, "received bundle reset broadcast for " + pkg);
        try {
            File appDir = new File(context.getFilesDir(), "app");
            long total = 0;

            // Wipe the bundle itself.
            File bundleDir = new File(appDir, "_python_bundle");
            if (bundleDir.exists()) {
                total += countBytes(bundleDir);
                deleteRecursive(bundleDir);
                Log.i(TAG, "_python_bundle/ removed");
            }

            // Wipe the .version markers PythonUtil.unpackAsset and
            // unpackPyBundle use to short-circuit the extract. If
            // they survive, PythonActivity.UnpackFilesTask compares
            // them against the APK's private_version string resource
            // — and because the APK didn't change (this is in-app
            // recovery, not a reinstall), the comparison says "disk
            // matches APK, no extract needed" and the bundle stays
            // gone forever. We need to invalidate the markers so the
            // next picker launch re-extracts. Both unpackAsset and
            // unpackPyBundle key off ``private_version``, so we
            // delete ``private.version`` (used by both) plus
            // ``libpybundle.version`` (the pybundle path's own
            // sentinel, just in case).
            for (String name : new String[] {
                    "private.version",
                    "libpybundle.version"}) {
                File f = new File(appDir, name);
                if (f.exists()) {
                    total += f.length();
                    f.delete();
                    Log.i(TAG, name + " marker removed");
                }
            }

            if (total > 0) {
                Log.i(TAG, "wiped " + total + " bytes; open the "
                        + "AZT Collaboration launcher icon to "
                        + "re-extract from APK assets");
            } else {
                Log.i(TAG, "nothing to wipe — next Activity launch "
                        + "will extract fresh");
            }
        } catch (Throwable ex) {
            Log.e(TAG, "wipe failed: " + ex);
        }
        // Self-kill the receiver process. Any other process in this
        // package (the :provider service that might still be hanging
        // on stale code) is left to its own lifecycle — its next
        // import attempt against the now-missing bundle will fail,
        // it'll die, and the next lazy-spawn comes up clean.
        Process.killProcess(Process.myPid());
    }

    private static void deleteRecursive(File f) {
        if (f.isDirectory()) {
            File[] children = f.listFiles();
            if (children != null) {
                for (File c : children) {
                    deleteRecursive(c);
                }
            }
        }
        f.delete();
    }

    private static long countBytes(File f) {
        if (f.isFile()) {
            return f.length();
        }
        long total = 0;
        File[] children = f.listFiles();
        if (children != null) {
            for (File c : children) {
                total += countBytes(c);
            }
        }
        return total;
    }
}
