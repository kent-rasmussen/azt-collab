package org.atoznback.aztcollab;

import android.app.ActivityManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Process;
import android.util.Log;

/**
 * BroadcastReceiver registered by every suite APK to handle
 * Intent.ACTION_MY_PACKAGE_REPLACED.
 *
 * APK install != process upgrade on Android. When a package is
 * reinstalled (adb install -r, file-manager sideload, browser
 * ACTION_INSTALL_PACKAGE, Play Store update), Android may keep
 * the existing process alive serving from the old code until
 * something kills it. Lazy ContentProviders are the worst case:
 * once a process is up serving the provider, it stays until
 * killed (memory pressure, device reboot, explicit
 * killBackgroundProcesses). Lazy services are the same.
 *
 * PackageManager dispatches MY_PACKAGE_REPLACED to the receiving
 * package AFTER the new code is on disk; every install pathway
 * converges on the same PackageInstaller commit step, so all
 * install channels trigger it. Manifest-declared receivers cold-
 * start the process to deliver the broadcast — exactly what we
 * need: bring up the NEW APK's code, run the kill-myself
 * handler, exit. The very-next peer ContentResolver call (for
 * the server APK) or the user's next launch (for a peer APK)
 * lazy-spawns again, also from the new code. Two short process
 * cycles, but the user never sees stale-process symptoms.
 *
 * Two steps in order:
 *
 *   1. {@code killBackgroundProcesses(getPackageName())} reaps
 *      any OLD-code process that Android kept alive across the
 *      install. The receiver process itself is foreground
 *      priority during broadcast dispatch, so it survives this
 *      call; only the background old-code daemon (sticky-bound
 *      service, lazy provider) gets killed. This is the
 *      load-bearing step on OEMs that don't auto-kill on
 *      replace; without it, the new APK's bytes are on disk but
 *      every peer call keeps routing to the old running daemon.
 *
 *   2. {@code Process.killProcess(myPid())} on the fresh
 *      receiver process — clean shutdown of the new-code process
 *      cold-started just to deliver this broadcast. Next peer
 *      bind lazy-spawns fresh new-code from a known-clean state.
 *
 * Lives in the shared org.atoznback.aztcollab Java package so
 * every suite APK references the same FQN
 * ({@code org.atoznback.aztcollab.SuiteSelfReplaceReceiver})
 * regardless of its own Android package id. Per-APK manifest
 * declaration is injected by p4a_hook.py's
 * {@code _inject_self_replace_receiver} step (NOT gated on
 * dist_name — every APK in the suite gets it). The
 * {@code KILL_BACKGROUND_PROCESSES} permission is required for
 * step 1 to work; p4a_hook.py injects it on every suite APK
 * alongside the receiver declaration (normal-protection, no
 * runtime prompt). Peers do NOT need to declare the permission
 * themselves — each APK self-handles its own replacement.
 */
public class SuiteSelfReplaceReceiver extends BroadcastReceiver {
    private static final String TAG = "SuiteSelfReplace";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent != null ? intent.getAction() : null;
        String pkg = context != null ? context.getPackageName() : "?";
        Log.i(TAG, "received " + action + " for " + pkg
                + " — reaping background processes then killing self "
                + "(pid=" + Process.myPid() + ")");
        try {
            ActivityManager am = (ActivityManager) context
                    .getSystemService(Context.ACTIVITY_SERVICE);
            if (am != null) {
                am.killBackgroundProcesses(pkg);
            }
        } catch (SecurityException ex) {
            // KILL_BACKGROUND_PROCESSES not granted — manifest
            // injection failed or was overridden. Fall through to
            // the self-kill so the new APK code is at least the
            // version routing through the receiver process.
            Log.w(TAG, "killBackgroundProcesses denied: " + ex);
        } catch (Throwable ex) {
            Log.w(TAG, "killBackgroundProcesses failed: " + ex);
        }
        Process.killProcess(Process.myPid());
    }
}
