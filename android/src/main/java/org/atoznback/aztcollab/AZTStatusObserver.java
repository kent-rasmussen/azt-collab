package org.atoznback.aztcollab;

import android.database.ContentObserver;
import android.net.Uri;
import android.os.Handler;
import android.util.Log;

/**
 * ContentObserver bridge for peer apps subscribing to status-URI
 * wakeups. Pyjnius can implement Java *interfaces* from Python
 * via PythonJavaClass, but not subclass concrete classes like
 * ContentObserver — so this thin Java subclass routes onChange
 * to a Python-implemented OnChangeCallback.
 *
 * Lifetime: peers construct one of these per subscription, hold
 * a strong reference (pyjnius proxies must survive Python GC),
 * pass it to ContentResolver.registerContentObserver, and call
 * ContentResolver.unregisterContentObserver on teardown.
 *
 * Threading: ContentObserver dispatches onChange on the Handler
 * passed at construction. Peers passing null get the binder
 * thread (which is fine — the Python callback just queues a
 * follow-up project_status RPC). Passing a main-thread Handler
 * routes onChange to the UI thread, which is useful if the
 * callback updates Kivy widgets directly.
 */
public class AZTStatusObserver extends ContentObserver {

    private static final String TAG = "AZTStatusObserver";

    /** Python-implemented callback. Pyjnius proxies a
     *  PythonJavaClass that implements this. */
    public interface OnChangeCallback {
        void onChanged(String uri);
    }

    private final OnChangeCallback cb;

    public AZTStatusObserver(Handler handler, OnChangeCallback cb) {
        super(handler);
        this.cb = cb;
    }

    @Override
    public void onChange(boolean selfChange, Uri uri) {
        if (cb == null) return;
        try {
            cb.onChanged(uri != null ? uri.toString() : "");
        } catch (Throwable t) {
            // Never let a Python-side exception bubble into the
            // ContentResolver dispatch chain — that can wedge other
            // observers in the same process.
            Log.e(TAG, "onChange callback raised", t);
        }
    }

    @Override
    public void onChange(boolean selfChange) {
        // Older Android API (<16) doesn't carry the URI. Forward
        // an empty string; peers re-poll without scoping.
        if (cb == null) return;
        try {
            cb.onChanged("");
        } catch (Throwable t) {
            Log.e(TAG, "onChange (legacy) callback raised", t);
        }
    }
}
