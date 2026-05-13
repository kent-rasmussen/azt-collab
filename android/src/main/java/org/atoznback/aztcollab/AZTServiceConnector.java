package org.atoznback.aztcollab;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.os.IBinder;
import android.util.Log;

/**
 * Peer-side connector that holds a bindService against the suite's
 * AZTServiceProviderhost (which lives in the server APK's
 * <code>:provider</code> process). Two effects, both load-bearing on
 * Android 15:
 *
 * <ol>
 *   <li><b>Un-freezer.</b> Android 15's app freezer can suspend
 *       cached processes, including the server APK's
 *       <code>:provider</code> where the daemon's Python interpreter
 *       runs. While a peer holds an active bind with
 *       BIND_ABOVE_CLIENT, <code>:provider</code>'s priority inherits
 *       from the (foregrounded) peer, so the freezer leaves it alone.
 *       Without this bind, peers on R500-class Android-15 tablets
 *       could lazy-spawn <code>:provider</code> fine but Python's
 *       <code>install_callbacks()</code> never completed because the
 *       process was being suspended mid-init; symptom was 60+ seconds
 *       of <code>daemon_not_ready</code> 503s and the user-visible
 *       "AZT Collaboration not responding" popup.</li>
 *
 *   <li><b>Warm-cache the daemon across the peer's session.</b> Once
 *       the first bind raises priority, <code>:provider</code> stays
 *       alive between RPC calls — second / third / Nth peer
 *       compat probes within the same peer session don't re-pay
 *       daemon-cold-start.</li>
 * </ol>
 *
 * The connector is a static singleton, populated lazily on first call
 * to {@link #ensureBound(Context)} (which the Python transport
 * invokes from {@code discover()} after a successful ping). Process
 * death tears the bind down naturally; a fresh peer process does
 * {@code ensureBound} again. We never explicitly unbind — the daemon
 * has its own idle-stop logic that handles natural shutdown after
 * the peer process exits.
 *
 * Lives in the suite's signature-protected
 * <code>org.atoznback.AZT_COLLAB_ACCESS</code> permission space;
 * peers signed with the wrong key fail the bind at install-grant
 * time and the connector silently logs the SecurityException.
 *
 * <p>Shipped per Phase B2 of <code>docs/daemon_boot_plan.md</code>
 * (azt_collab_client 0.33.0). The server APK side (this same Service
 * class file) needed no change — its {@code onBind} has returned a
 * stub Binder + tracked <code>sBoundCount</code> since the original
 * sticky-bound design landed; only the peer-side connector was
 * missing.</p>
 */
public class AZTServiceConnector implements ServiceConnection {
    private static final String TAG = "AZTServiceConnector";
    private static final String SERVER_PKG =
        "org.atoznback.aztcollab";
    private static final String SERVICE_CLS =
        "org.atoznback.aztcollab.AZTServiceProviderhost";

    private static AZTServiceConnector sInstance;
    private boolean mBound = false;

    /**
     * Idempotent. Safe to call repeatedly; second+ invocations no-op
     * while the bind is alive. Returns immediately — the bind is
     * async; {@code onServiceConnected} fires when Android has
     * actually connected. The transport doesn't need to wait on
     * that — its retry loop is independently driven by the peer's
     * compat probe.
     *
     * <p>Single-step: {@code bindService} with
     * {@code BIND_AUTO_CREATE | BIND_ABOVE_CLIENT}. The
     * {@code BIND_AUTO_CREATE} flag triggers
     * {@code Service.onCreate} on the server-APK side, where
     * {@code AZTServiceProviderhost} self-delivers an
     * {@code onStartCommand} call to bootstrap Python (see that
     * class's {@code onCreate} override). {@code BIND_ABOVE_CLIENT}
     * raises the bound process's OOM priority above ours,
     * defeating Android 15's freezer that otherwise suspends
     * {@code :provider} mid-Python-init on R500-class tablets.</p>
     *
     * <p>We do NOT call {@code startService} here. Android 12+
     * blocks cross-package {@code startService} from background
     * contexts, including a peer process mid-cold-start
     * ({@code App.build()} runs before the peer's UID has been
     * promoted to foreground), so it would always fail with
     * {@code BackgroundServiceStartNotAllowedException}.
     * {@code bindService} is allowed from background; that's the
     * only path that works at this point in the peer's lifecycle.
     * The server-APK's {@code AZTServiceProviderhost.onCreate}
     * override carries the Python-startup work that
     * {@code onStartCommand} would normally do.</p>
     */
    public static synchronized void ensureBound(Context ctx) {
        if (sInstance == null) sInstance = new AZTServiceConnector();
        if (sInstance.mBound) return;
        Intent intent = new Intent();
        intent.setComponent(
            new ComponentName(SERVER_PKG, SERVICE_CLS));
        try {
            boolean ok = ctx.bindService(
                intent, sInstance,
                Context.BIND_AUTO_CREATE | Context.BIND_ABOVE_CLIENT);
            if (!ok) {
                // bindService returns false when the service can't
                // be resolved (server APK absent / wrong package
                // name) or permission grant denied. Either is a
                // structural problem; logging is enough — the rest
                // of the transport still works against the
                // ContentProvider.
                Log.w(TAG, "bindService returned false; service "
                          + "unreachable or permission denied");
            }
        } catch (SecurityException e) {
            Log.e(TAG, "bindService refused (signature mismatch?)",
                  e);
        }
    }

    /** Diagnostic; not used by the transport. */
    public static synchronized boolean isBound() {
        return sInstance != null && sInstance.mBound;
    }

    @Override
    public synchronized void onServiceConnected(
            ComponentName name, IBinder binder) {
        mBound = true;
        Log.i(TAG, "bound to " + name.flattenToShortString());
    }

    @Override
    public synchronized void onServiceDisconnected(ComponentName name) {
        // Server APK process died (kill -9, OOM, uninstall). Android
        // will re-deliver onServiceConnected when the service
        // respawns under our auto-create flag, but the bind state
        // has temporarily lapsed. Don't try to rebind here — the
        // next peer call goes through ensureBound naturally.
        mBound = false;
        Log.w(TAG, "unbound from " + name.flattenToShortString());
    }
}
