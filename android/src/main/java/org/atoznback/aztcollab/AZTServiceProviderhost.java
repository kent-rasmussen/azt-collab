package org.atoznback.aztcollab;

import android.content.Context;
import android.content.Intent;
import android.os.Binder;
import android.os.IBinder;
import org.kivy.android.PythonService;

/**
 * Sticky-bound Android service that hosts AZTCollabProvider's Python
 * interpreter for the standalone aztcollab APK.
 *
 * Two roles:
 *
 *   1. Pin the host process while peers may still need the provider —
 *      URI grants outstanding, openFileDescriptor pending. A peer
 *      bindService() raises the OOM adjustment of this service host
 *      so Android prefers to kill other processes first.
 *
 *   2. Survive memory-pressure kills via START_STICKY: Android will
 *      recreate the service when memory frees, and the next peer
 *      ContentResolver call against AZTCollabProvider also lazy-spawns
 *      the host (Android's unconditional contract for ContentProvider
 *      authorities). On every fresh start, service.py runs
 *      reconcile_on_startup() so any in-flight scheduler jobs surface
 *      as JOB_INTERRUPTED to peers polling stale job_ids.
 *
 * The IBinder returned from onBind is a no-op stub. Peers don't need
 * to talk to it — bind exists purely for the OOM hint. RPC and file
 * I/O continue to flow through AZTCollabProvider.
 *
 * Manifest registration: injected post-render by p4a_hook.py's
 * _inject_aztcollab_service step (gated on dist_name == 'aztcollab',
 * so peer APKs don't accidentally inherit the &lt;service&gt; declaration).
 *
 * Lives under the suite's signature-protected
 * org.atoznback.AZT_COLLAB_ACCESS so only suite-signed peers can bind.
 */
public class AZTServiceProviderhost extends PythonService {
    private final IBinder binder = new Binder();
    private static volatile int sBoundCount = 0;

    /** Number of peers currently bound to this service. Read from
     *  Python via pyjnius for the idle-stop policy in service.py. */
    public static int getBoundCount() {
        return sBoundCount;
    }

    /** Build an Intent that starts THIS class with the right
     *  PythonService extras (entrypoint, androidPrivate, etc.) and
     *  start it. Use from Python via:
     *      cls = autoclass('org.atoznback.aztcollab.AZTServiceProviderhost')
     *      cls.start(activity, '')
     *  This is the same pattern p4a's auto-generated Service${Name}
     *  classes use; we replicate it here so we don't need
     *  `services = ` in buildozer.spec (which would generate a parallel
     *  ServiceProviderhost class we'd have to layer over).
     */
    public static void start(Context ctx, String pythonServiceArgument) {
        Intent intent = getDefaultIntent(ctx, "service.py",
                                         pythonServiceArgument);
        intent.setClassName(ctx, AZTServiceProviderhost.class.getName());
        ctx.startService(intent);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // PythonService.onStartCommand spins up the Python thread that
        // runs service.py. Force START_STICKY so Android recreates us
        // after a memory-pressure kill (some p4a builds default to
        // START_NOT_STICKY).
        super.onStartCommand(intent, flags, startId);
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        synchronized (AZTServiceProviderhost.class) {
            sBoundCount++;
        }
        return binder;
    }

    @Override
    public boolean onUnbind(Intent intent) {
        synchronized (AZTServiceProviderhost.class) {
            if (sBoundCount > 0) sBoundCount--;
        }
        return false;
    }

    @Override
    public boolean canDisplayNotification() {
        return false;  // not a foreground service; sticky-bound only
    }

    public int getServiceId() {
        return 1337;  // unique within this APK
    }
}
