package org.atoznback.aztcollab;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
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

    public static void registerCallbacks(DispatchCallback dispatch,
                                         OpenFileCallback openFile) {
        sDispatch = dispatch;
        sOpenFile = openFile;
    }

    @Override
    public boolean onCreate() {
        return true;
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
        DispatchCallback cb = sDispatch;
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
        OpenFileCallback cb = sOpenFile;
        if (cb == null) {
            throw new FileNotFoundException("daemon_not_ready");
        }
        String rel = uri.getPath();
        if (rel == null || rel.isEmpty()) {
            throw new FileNotFoundException("missing_path");
        }
        String abs = cb.resolveAbsPath(rel, mode);
        if (abs == null) {
            throw new FileNotFoundException("forbidden: " + rel);
        }
        int flags = ParcelFileDescriptor.parseMode(mode);
        return ParcelFileDescriptor.open(new File(abs), flags);
    }

    // --- ContentProvider methods we don't use ---

    @Override
    public Cursor query(Uri uri, String[] proj, String sel, String[] args,
                        String order) {
        return null;
    }

    @Override
    public String getType(Uri uri) { return null; }

    @Override
    public Uri insert(Uri uri, ContentValues v) { return null; }

    @Override
    public int update(Uri uri, ContentValues v, String s, String[] a) {
        return 0;
    }

    @Override
    public int delete(Uri uri, String s, String[] a) { return 0; }
}
