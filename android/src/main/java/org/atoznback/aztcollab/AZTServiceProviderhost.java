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
 * to talk to it — bind exists purely for the OOM hint. Real RPC and
 * file I/O continue to flow through AZTCollabProvider.
 *
 * Manifest registration: injected post-render by p4a_hook.py's
 * _inject_aztcollab_service step (gated on dist_name == 'aztcollab',
 * so peer APKs don't accidentally inherit the &lt;service&gt; declaration).
 *
 * Lives under the suite's signature-protected
 * org.atoznback.AZT_COLLAB_ACCESS so only suite-signed peers can bind.
 *
 * API note: this matches p4a's PythonService class as shipped in the
 * SDL2 bootstrap (see PythonService.java in the same dist tree).
 * That class exposes a {@code startType()} hook (not the typical
 * {@code onStartCommand}) and uses Intent extras
 * (androidPrivate / androidArgument / serviceEntrypoint /
 * pythonName / pythonHome / pythonPath / pythonServiceArgument /
 * serviceStartAsForeground) to pass configuration to the Python
 * thread. We replicate p4a's auto-generated Service&lt;Name&gt;.start
 * pattern (templates/Service.tmpl.java) here so we don't need
 * {@code services = } in buildozer.spec, which would generate a
 * parallel ServiceProviderhost we'd have to layer over.
 */
public class AZTServiceProviderhost extends PythonService {
    private final IBinder binder = new Binder();
    private static volatile int sBoundCount = 0;

    /** Number of peers currently bound to this service. Read from
     *  Python via pyjnius for the idle-stop policy in service.py. */
    public static int getBoundCount() {
        return sBoundCount;
    }

    /** Build the Intent that points PythonService at service.py with
     *  the right androidPrivate / androidArgument / pythonHome / etc.
     *  Called by start() and by getThisDefaultIntent() (which Android
     *  calls when recreating the service after a STICKY kill — the
     *  delivered Intent is null in that case, and PythonService asks
     *  us to rebuild it). */
    public static Intent getDefaultIntent(Context ctx,
                                          String pythonServiceArgument) {
        Intent intent = new Intent(ctx, AZTServiceProviderhost.class);
        String app_root = ctx.getFilesDir().getAbsolutePath() + "/app";
        intent.putExtra("androidPrivate",
                        ctx.getFilesDir().getAbsolutePath());
        intent.putExtra("androidArgument", app_root);
        intent.putExtra("serviceTitle", "AZT Collaboration");
        intent.putExtra("serviceEntrypoint", "service.py");
        intent.putExtra("pythonName", "providerhost");
        // Sticky-bound, no foreground notification — we rely on bind
        // priority + START_STICKY for kill-resistance, not on the
        // notification-driven foreground status. Setting
        // serviceStartAsForeground to "false" keeps PythonService from
        // calling startForeground (which would require a notification
        // and FOREGROUND_SERVICE permission).
        intent.putExtra("serviceStartAsForeground", "false");
        intent.putExtra("pythonHome", app_root);
        intent.putExtra("pythonPath", app_root + ":" + app_root + "/lib");
        intent.putExtra("pythonServiceArgument", pythonServiceArgument);
        // smallIconName / contentTitle / contentText are only consumed
        // when serviceStartAsForeground == "true"; pass empties to be
        // safe under future PythonService changes that read them
        // unconditionally.
        intent.putExtra("smallIconName", "");
        intent.putExtra("contentTitle", "");
        intent.putExtra("contentText", "");
        return intent;
    }

    /** Mirror of p4a's auto-generated Service&lt;Name&gt;.start, targeting
     *  this concrete class. Use from Python:
     *      cls = autoclass('org.atoznback.aztcollab.AZTServiceProviderhost')
     *      cls.start(activity, '')
     */
    public static void start(Context ctx, String pythonServiceArgument) {
        Intent intent = getDefaultIntent(ctx, pythonServiceArgument);
        ctx.startService(intent);
    }

    /** Stop the service. Invokes onDestroy on the running instance,
     *  which in PythonService also kills the host process via
     *  Process.killProcess — that's the path we want at idle-stop. */
    public static void stop(Context ctx) {
        Intent intent = new Intent(ctx, AZTServiceProviderhost.class);
        ctx.stopService(intent);
    }

    /** PythonService.onStartCommand calls startType() to decide what
     *  to return. Override to ask Android to recreate us after a
     *  memory-pressure kill (default in PythonService is
     *  START_NOT_STICKY). */
    @Override
    public int startType() {
        return START_STICKY;
    }

    /** PythonService.onStartCommand calls getThisDefaultIntent when
     *  Android recreates a STICKY service (the delivered Intent is
     *  null in that case and PythonService needs the configuration
     *  extras to know which Python to run). */
    @Override
    protected Intent getThisDefaultIntent(Context ctx,
                                          String pythonServiceArgument) {
        return AZTServiceProviderhost.getDefaultIntent(
                ctx, pythonServiceArgument);
    }

    @Override
    protected int getServiceId() {
        return 1337;  // unique within this APK
    }

    /**
     * Self-bootstrap Python in {@code onCreate} so {@code bindService}
     * alone is sufficient to start the daemon. Android 12+ blocks
     * {@code startService} from background contexts (the
     * {@code :provider} lazy-spawn from
     * {@code AZTCollabProvider.onCreate}, AND a peer's
     * cross-package start while the peer is mid-cold-start, both
     * surface as {@code BackgroundServiceStartNotAllowedException}).
     * {@code bindService} is allowed from background, but it only
     * triggers {@code onCreate}, not {@code onStartCommand} —
     * which is where {@code PythonService} normally reads the
     * {@code serviceEntrypoint} extras and launches the Python
     * thread. Self-delivering an {@code onStartCommand} call here
     * gets Python booting on the first bind.
     *
     * <p>Idempotent: {@code PythonService.onStartCommand} short-
     * circuits when {@code mService != null}, so any subsequent
     * {@code startService} call (legacy callers, retries) hits the
     * existing Python interpreter and does nothing harmful.</p>
     */
    @Override
    public void onCreate() {
        super.onCreate();
        try {
            Intent intent = getDefaultIntent(this, "");
            onStartCommand(intent, 0, getServiceId());
        } catch (Throwable t) {
            android.util.Log.e(
                "AZTServiceProviderhost",
                "self-bootstrap from onCreate failed", t);
        }
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
}
