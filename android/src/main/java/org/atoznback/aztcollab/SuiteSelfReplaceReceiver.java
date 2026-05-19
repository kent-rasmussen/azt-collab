package org.atoznback.aztcollab;

import android.app.ActivityManager;
import android.app.ActivityManager.RunningAppProcessInfo;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Process;
import android.util.Log;

import java.util.List;

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
 * killed. Lazy services are the same.
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
 * Three steps in order:
 *
 *   1. {@code Process.killProcess(pid)} for every running
 *      process in this package other than the receiver itself.
 *      Same UID, so no permission needed. This is the load-
 *      bearing step: it reaps old-code processes regardless of
 *      their importance state. {@code killBackgroundProcesses}
 *      alone does NOT work for the server APK's {@code :provider}
 *      process, because the {@code AZTServiceProviderhost}
 *      sticky-bound service pins it at {@code IMPORTANCE_SERVICE},
 *      which is above the BACKGROUND threshold
 *      {@code killBackgroundProcesses} operates on. The explicit
 *      per-PID kill bypasses that gate.
 *
 *   2. {@code killBackgroundProcesses(getPackageName())} as a
 *      belt-and-braces fallback. On OEMs where step 1 enumeration
 *      returns less than the full process list (Android &gt;=5.0
 *      restricts {@code getRunningAppProcesses} but documents
 *      that the caller's own processes are always returned), this
 *      catches anything that slipped through and was already in a
 *      reapable state.
 *
 *   3. {@code Process.killProcess(myPid())} on the fresh
 *      receiver process — clean shutdown of the new-code process
 *      cold-started just to deliver this broadcast. Next peer
 *      bind lazy-spawns fresh new-code from a known-clean state.
 *
 * <b>NOTE on stale p4a unpack.</b> 0.43.12 added a Step 1 that
 * recursively wiped {@code files/app/_python_bundle/} so the next
 * spawn would force p4a to re-extract from the new APK's assets,
 * fixing the "{@code :provider} keeps reading the old version's
 * Python code" loop. That worked for the Activity bootstrap (which
 * has an extract-on-missing branch) but broke the
 * {@code :provider} service bootstrap, which expects
 * {@code _python_bundle/} to already exist and just imports from
 * it. After the wipe, every lazy-respawn of {@code :provider} hit
 * "{@code _python_bundle does not exist...should we expect a crash
 * soon?}" and crash-looped under {@code START_STICKY}. Reverted in
 * 0.43.14. The stale-unpack issue is real but needs a different
 * fix (Activity-launch-from-receiver, or a service-side
 * extract-on-missing branch).
 *
 * Lives in the shared org.atoznback.aztcollab Java package so
 * every suite APK references the same FQN
 * ({@code org.atoznback.aztcollab.SuiteSelfReplaceReceiver})
 * regardless of its own Android package id. Per-APK manifest
 * declaration is injected by p4a_hook.py's
 * {@code _inject_self_replace_receiver} step (NOT gated on
 * dist_name — every APK in the suite gets it). The
 * {@code KILL_BACKGROUND_PROCESSES} permission is injected on the
 * server APK only and now serves only the step-2 fallback; peers
 * do not declare it and reach the same end state via step 1.
 */
public class SuiteSelfReplaceReceiver extends BroadcastReceiver {
    private static final String TAG = "SuiteSelfReplace";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent != null ? intent.getAction() : null;
        String pkg = context != null ? context.getPackageName() : "?";
        int myPid = Process.myPid();
        Log.i(TAG, "received " + action + " for " + pkg
                + " — reaping sibling processes then killing self "
                + "(pid=" + myPid + ")");
        ActivityManager am = null;
        try {
            am = (ActivityManager) context
                    .getSystemService(Context.ACTIVITY_SERVICE);
        } catch (Throwable ex) {
            Log.w(TAG, "ActivityManager unavailable: " + ex);
        }
        // Step 1: enumerate same-UID processes and kill each non-self
        // PID directly. Same-UID killProcess needs no permission and
        // ignores process importance, so the :provider process that
        // killBackgroundProcesses can't touch (sticky service pins it
        // at IMPORTANCE_SERVICE) gets reaped here.
        if (am != null) {
            try {
                List<RunningAppProcessInfo> procs =
                        am.getRunningAppProcesses();
                if (procs != null) {
                    for (RunningAppProcessInfo p : procs) {
                        if (p.pid == myPid) {
                            continue;
                        }
                        Log.i(TAG, "reaping sibling pid=" + p.pid
                                + " (" + p.processName + ")");
                        try {
                            Process.killProcess(p.pid);
                        } catch (Throwable ex) {
                            Log.w(TAG, "killProcess pid=" + p.pid
                                    + " failed: " + ex);
                        }
                    }
                }
            } catch (Throwable ex) {
                Log.w(TAG, "getRunningAppProcesses failed: " + ex);
            }
            // Step 2: fallback for anything the enumeration missed but
            // is already in a reapable state. SecurityException is
            // expected on peer APKs (no KILL_BACKGROUND_PROCESSES) and
            // not fatal — step 1 already handled the load-bearing case.
            try {
                am.killBackgroundProcesses(pkg);
            } catch (SecurityException ex) {
                Log.i(TAG, "killBackgroundProcesses denied "
                        + "(expected on peer APKs): " + ex);
            } catch (Throwable ex) {
                Log.w(TAG, "killBackgroundProcesses failed: " + ex);
            }
        }
        // Step 3: self-kill so the next bind/launch lazy-spawns fresh
        // new-code from a known-clean state.
        Process.killProcess(myPid);
    }
}
