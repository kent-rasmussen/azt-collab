package org.atoznback.aztcollab;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import android.util.Log;

import java.io.File;
import java.io.FileNotFoundException;
import java.io.IOException;

/**
 * Glue ContentProvider for the A-Z+T collab daemon.
 *
 * Two methods matter:
 *
 *   call(method, arg, extras)
 *       Funnels every RPC through one Bundle exchange. extras["body"]
 *       carries the JSON-encoded request body. Returns a Bundle with
 *       int "status" and string "json" (the response body).
 *
 *   openFile(uri, mode)
 *       Hands out a ParcelFileDescriptor for binary blobs (audio,
 *       images). Resolves the path through the Python callback so the
 *       daemon decides what's accessible.
 *
 * The Python side registers two callbacks at app startup via the
 * ServiceCallbacks static slots; this class is otherwise inert.
 */
public class AZTCollabProvider extends ContentProvider {
    private static final String TAG = "AZTCollabProvider";

    /** Returns int[] {status, ...} and bytes via Bundle. Implemented in
     *  Python; registered at app startup. */
    public interface DispatchCallback {
        Bundle dispatch(String method, String path, String bodyJson);
    }

    public interface OpenFileCallback {
        String resolveAbsPath(String relativePath, String mode);
    }

    private static volatile DispatchCallback sDispatch;
    private static volatile OpenFileCallback sOpenFile;
    /** Reference to the running Provider's context, captured in
     *  onCreate so static methods (notifyStatusChanged) can access
     *  the ContentResolver without an instance handle. Volatile
     *  read on call paths is fine — set once, never overwritten,
     *  null only before onCreate runs. */
    private static volatile Context sContext;

    public static void registerCallbacks(DispatchCallback dispatch,
                                         OpenFileCallback openFile) {
        sDispatch = dispatch;
        sOpenFile = openFile;
    }

    /**
     * Push-notify any peer that registered a ContentObserver on the
     * status URI for *langcode*. Peers can register on either:
     *
     *   content://org.atoznback.aztcollab/status/&lt;langcode&gt;
     *      → fired for that one project; observer registered with
     *        notifyForDescendants=false catches only this URI.
     *
     *   content://org.atoznback.aztcollab/status
     *      → fired by passing langcode = empty/null (daemon-wide
     *        events: toggle flips, peer-list mutations). An
     *        observer registered with notifyForDescendants=true
     *        ALSO catches the per-project notifications above —
     *        same registration receives both, so a project-list
     *        UI subscribes once and gets every wakeup.
     *
     * Called from Python via jnius (notify.py). Safe from any
     * thread — ContentResolver.notifyChange dispatches
     * asynchronously to registered observers.
     */
    public static void notifyStatusChanged(String langcode) {
        Context ctx = sContext;
        if (ctx == null) return;
        Uri uri;
        if (langcode == null || langcode.isEmpty()) {
            uri = Uri.parse("content://org.atoznback.aztcollab/status");
        } else {
            uri = Uri.parse(
                "content://org.atoznback.aztcollab/status/" + langcode);
        }
        try {
            ctx.getContentResolver().notifyChange(uri, null);
        } catch (Throwable t) {
            Log.e(TAG, "notifyChange failed for " + uri, t);
        }
    }

    @Override
    public boolean onCreate() {
        // ContentProvider lazy-spawn brings up the :provider host
        // process and instantiates this Provider, but does NOT start
        // any Service in the process. Without booting Python (which
        // happens via AZTServiceProviderhost / PythonService), the
        // Python dispatch + openFile callbacks never register, so
        // every call() falls through to the "daemon_not_ready" 503
        // branch below. The user-visible symptom is "after
        // installing the server APK, peers can't connect unless the
        // user opens the server APK first."
        //
        // Calling AZTServiceProviderhost.start() here triggers
        // PythonService.onStartCommand → spawn Python service
        // thread → run service.py:main() → install_callbacks() →
        // registerCallbacks() so the static slots fill in. The
        // boot is async: the very first call() may still race the
        // boot and return 503; the peer's transport surfaces that
        // as ServerUnavailable, rpc.call retries once, and the
        // second attempt sees the populated callbacks.
        Context ctx = getContext();
        if (ctx != null) {
            // Capture for notifyStatusChanged's static call path.
            // Set before start() — the Service might fire notify
            // calls during its own startup.
            sContext = ctx.getApplicationContext();
            try {
                AZTServiceProviderhost.start(ctx, "");
            } catch (Throwable t) {
                Log.e(TAG, "AZTServiceProviderhost.start failed", t);
            }
        }
        return true;
    }

    // Wait up to this many ms for the Python callbacks to register
    // after a fresh process spawn. Android lazy-spawns the provider's
    // process on demand; binder threads handling incoming peer calls
    // can land before Python's ``install_callbacks()`` finishes its
    // setup work. Returning "daemon_not_ready" immediately forces the
    // peer to crash on the first call after a respawn (the
    // "first-try-fails-second-try-works" pattern). Polling here for a
    // few seconds lets the first call queue behind Python init and
    // succeed normally; on a true daemon-down state the timeout
    // expires and the call surfaces the same error it did before.
    private static final long CALLBACK_WAIT_MS = 3000;
    private static final long CALLBACK_POLL_MS = 50;

    private static DispatchCallback awaitDispatch() {
        DispatchCallback cb = sDispatch;
        if (cb != null) return cb;
        long deadline = System.currentTimeMillis() + CALLBACK_WAIT_MS;
        while (cb == null && System.currentTimeMillis() < deadline) {
            try {
                Thread.sleep(CALLBACK_POLL_MS);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                break;
            }
            cb = sDispatch;
        }
        return cb;
    }

    private static OpenFileCallback awaitOpenFile() {
        OpenFileCallback cb = sOpenFile;
        if (cb != null) return cb;
        long deadline = System.currentTimeMillis() + CALLBACK_WAIT_MS;
        while (cb == null && System.currentTimeMillis() < deadline) {
            try {
                Thread.sleep(CALLBACK_POLL_MS);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                break;
            }
            cb = sOpenFile;
        }
        return cb;
    }

    @Override
    public Bundle call(String method, String arg, Bundle extras) {
        // method maps to the HTTP verb; arg is the path; extras["body"]
        // is the JSON request body.
        // Special-case "ping" so discovery probes work even before
        // the Python callback registers (e.g., during APK install warm-up).
        if ("ping".equals(method)) {
            Bundle b = new Bundle();
            b.putInt("status", 200);
            b.putString("json", "{\"ok\":true,\"transport\":\"android_cp\"}");
            return b;
        }
        DispatchCallback cb = awaitDispatch();
        if (cb == null) {
            Bundle b = new Bundle();
            b.putInt("status", 503);
            b.putString("json",
                "{\"ok\":false,\"error\":\"daemon_not_ready\"}");
            return b;
        }
        String body = extras != null ? extras.getString("body", "") : "";
        try {
            return cb.dispatch(method, arg != null ? arg : "", body);
        } catch (Throwable t) {
            Log.e(TAG, "dispatch threw", t);
            Bundle b = new Bundle();
            b.putInt("status", 500);
            b.putString("json",
                "{\"ok\":false,\"error\":\"dispatch_exception\"}");
            return b;
        }
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode)
            throws FileNotFoundException {
        Log.i(TAG, "openFile() uri=" + uri + " mode=" + mode);
        OpenFileCallback cb = awaitOpenFile();
        if (cb == null) {
            Log.i(TAG, "openFile() cb=null, daemon_not_ready");
            throw new FileNotFoundException("daemon_not_ready");
        }
        String rel = uri.getPath();
        if (rel == null || rel.isEmpty()) {
            Log.i(TAG, "openFile() missing path");
            throw new FileNotFoundException("missing_path");
        }
        String abs = cb.resolveAbsPath(rel, mode);
        if (abs == null) {
            Log.i(TAG, "openFile() forbidden: " + rel);
            throw new FileNotFoundException("forbidden: " + rel);
        }
        int flags = ParcelFileDescriptor.parseMode(mode);
        Log.i(TAG, "openFile() returning FD for " + abs);
        return ParcelFileDescriptor.open(new File(abs), flags);
    }

    // --- ContentProvider metadata methods ---
    //
    // Receivers like Signal validate attachment URIs by calling
    // getContentResolver().getType(uri) and .query(uri, ...) BEFORE
    // reading. A null return for either signals "unknown" and the
    // receiver silently rejects (Signal's ShareActivity opens then
    // self-finishes within ~300ms — field-diagnosed 2026-06-22).
    // Both implementations route through the same resolveAbsPath
    // callback openFile uses, so a URI either has full metadata
    // and is openable, or both fail consistently.

    /** Extension-based MIME lookup for the files this provider
     *  serves. Returns null for unrecognised extensions — caller
     *  treats null as "unknown" the same way it would for a
     *  missing URI.
     *
     *  Diagnostic share files (.log, .txt) return
     *  ``application/octet-stream`` since 0.52.17 rather than
     *  ``text/plain``. Field-diagnosed via 0.52.15-0.52.16
     *  logcat captures: Signal's ``ShareActivity`` for
     *  ACTION_SEND_MULTIPLE calls ``getType`` on each URI,
     *  sees ``text/plain``, routes to its text-snippet path
     *  (which expects ``EXTRA_TEXT`` strings, not
     *  ``EXTRA_STREAM`` URIs), finds no text, and bails. The
     *  files are share-attachments, not in-message text
     *  snippets — ``application/octet-stream`` puts them in
     *  Signal's binary-attachment path. Receivers that want
     *  to preview as text still can; the MIME hint is for
     *  routing, not capability. */
    private String mimeForPath(String path) {
        if (path == null) return null;
        String lower = path.toLowerCase();
        int dot = lower.lastIndexOf('.');
        if (dot < 0 || dot == lower.length() - 1) return null;
        String ext = lower.substring(dot + 1);
        if (ext.equals("txt")) return "text/plain";
        if (ext.equals("log")) return "text/plain";
        if (ext.equals("json")) return "application/json";
        if (ext.equals("lift")) return "application/xml";
        if (ext.equals("xml")) return "application/xml";
        if (ext.equals("zip")) return "application/zip";
        if (ext.equals("png")) return "image/png";
        if (ext.equals("jpg") || ext.equals("jpeg")) return "image/jpeg";
        if (ext.equals("webp")) return "image/webp";
        if (ext.equals("gif")) return "image/gif";
        if (ext.equals("wav")) return "audio/wav";
        if (ext.equals("mp3")) return "audio/mpeg";
        if (ext.equals("ogg")) return "audio/ogg";
        if (ext.equals("m4a") || ext.equals("mp4")) return "audio/mp4";
        if (ext.equals("opus")) return "audio/opus";
        return null;
    }

    @Override
    public Cursor query(Uri uri, String[] proj, String sel, String[] args,
                        String order) {
        Log.i(TAG, "query() uri=" + uri + " proj=" +
              (proj == null ? "null" : java.util.Arrays.toString(proj)));
        OpenFileCallback cb = awaitOpenFile();
        if (cb == null) {
            Log.i(TAG, "query() cb=null, returning null");
            return null;
        }
        String rel = uri.getPath();
        if (rel == null || rel.isEmpty()) {
            Log.i(TAG, "query() empty path, returning null");
            return null;
        }
        String abs = cb.resolveAbsPath(rel, "r");
        if (abs == null) {
            Log.i(TAG, "query() resolveAbsPath returned null for "
                  + rel);
            return null;
        }
        File f = new File(abs);
        if (!f.exists()) {
            Log.i(TAG, "query() file does not exist: " + abs);
            return null;
        }
        // Honour the requested projection; default to OpenableColumns
        // when the caller asked for "everything".
        String[] cols = proj;
        if (cols == null) {
            cols = new String[] {
                OpenableColumns.DISPLAY_NAME,
                OpenableColumns.SIZE,
            };
        }
        MatrixCursor cursor = new MatrixCursor(cols, 1);
        Object[] row = new Object[cols.length];
        for (int i = 0; i < cols.length; i++) {
            if (OpenableColumns.DISPLAY_NAME.equals(cols[i])) {
                row[i] = f.getName();
            } else if (OpenableColumns.SIZE.equals(cols[i])) {
                row[i] = f.length();
            } else {
                row[i] = null;
            }
        }
        cursor.addRow(row);
        Log.i(TAG, "query() returning cursor for " + f.getName()
              + " size=" + f.length());
        return cursor;
    }

    @Override
    public String getType(Uri uri) {
        if (uri == null) {
            Log.i(TAG, "getType() uri=null, returning null");
            return null;
        }
        String path = uri.getPath();
        if (path == null) {
            Log.i(TAG, "getType() empty path, returning null");
            return null;
        }
        String mime = mimeForPath(path);
        Log.i(TAG, "getType() uri=" + uri + " mime=" + mime);
        return mime;
    }

    // --- ContentProvider methods we don't use ---

    @Override
    public Uri insert(Uri uri, ContentValues v) { return null; }

    @Override
    public int update(Uri uri, ContentValues v, String s, String[] a) {
        return 0;
    }

    @Override
    public int delete(Uri uri, String s, String[] a) { return 0; }
}
