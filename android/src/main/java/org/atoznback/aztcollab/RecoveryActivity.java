package org.atoznback.aztcollab;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.util.ArrayList;
import java.util.List;

/**
 * Pure-Java recovery Activity for a stuck
 * {@code _python_bundle/}.
 *
 * <p>Distinct launcher icon ("AZT Recovery") so the user can
 * reach it even when "AZT Collaboration" (the normal picker
 * Activity) is in a launch loop / crash cycle from the cascade-
 * kill described in
 * {@code BundleResetReceiver.java}'s class docs. Pure Java —
 * no SDL, no Kivy, no Python — so it launches even when every
 * Python entrypoint in the APK is unrunnable. The UI is built
 * programmatically (no layout XML resource) so adding it
 * required no resource-pipeline changes.</p>
 *
 * <p><b>What "doesn't depend on anything" means here.</b> The
 * Activity:</p>
 * <ul>
 *   <li>Doesn't read or import any Python module.</li>
 *   <li>Doesn't bind to {@code AZTCollabProvider} or
 *       {@code AZTServiceProviderhost}, so the cascade-kill that
 *       takes down the picker Activity can't take this one down.</li>
 *   <li>Doesn't need network, permission grants, or any data
 *       outside the APK itself.</li>
 *   <li>Loads from the APK's {@code classes.dex}, never from
 *       {@code _python_bundle/}.</li>
 * </ul>
 *
 * <p><b>Usage.</b> Field user with a stuck device: open the app
 * drawer, tap the "AZT Recovery" icon, tap the "Repair sync
 * service" button. The Activity fires the same broadcast
 * action ({@code RESET_PYTHON_BUNDLE}) that
 * {@code BundleResetReceiver} handles, then self-finishes so
 * the user can reopen "AZT Collaboration" — which triggers
 * p4a's Activity-side extract-on-missing and lays down fresh
 * Python code from the APK assets.</p>
 *
 * <p><b>$AZT_HOME is preserved.</b> The wipe path runs through
 * the same {@code BundleResetReceiver}, which scopes deletion
 * strictly to {@code files/app/_python_bundle/}.
 * {@code files/azt/} (projects, jobs.json, credentials,
 * daemon.log) is untouched.</p>
 *
 * <p>Manifest declaration with the LAUNCHER intent-filter is
 * injected by {@code p4a_hook.py}'s
 * {@code _inject_bundle_reset_receiver} step (gated on
 * {@code dist_name == 'aztcollab'} — peer APKs don't need it).</p>
 */
public class RecoveryActivity extends Activity {
    private static final String TAG = "AZTRecovery";
    private static final String RESET_ACTION =
        "org.atoznback.aztcollab.RESET_PYTHON_BUNDLE";

    private TextView mStatus;
    private Button mRepairBtn;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        int pad = dp(24);
        root.setPadding(pad, pad, pad, pad);
        root.setBackgroundColor(Color.parseColor("#fffefb"));
        root.setLayoutParams(new ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT));

        TextView title = new TextView(this);
        title.setText("AZT Collaboration — Recovery");
        title.setTextSize(22);
        title.setTypeface(null, Typeface.BOLD);
        title.setTextColor(Color.parseColor("#1a1a1a"));
        title.setGravity(Gravity.CENTER);
        root.addView(title, layout(0, dp(8)));

        TextView desc = new TextView(this);
        desc.setText(
            "Use this if the AZT Collaboration sync service isn't "
          + "starting, or if peer apps (like AZT Recorder) can't "
          + "reach the daemon.\n\n"
          + "Tapping Repair will refresh the daemon's Python code "
          + "from the installed APK. The app will close — wait a "
          + "few seconds, then reopen AZT Collaboration normally.\n\n"
          + "Your projects, recordings, credentials, and settings "
          + "are NOT affected.");
        desc.setTextSize(15);
        desc.setTextColor(Color.parseColor("#333333"));
        desc.setLineSpacing(0, 1.25f);
        root.addView(desc, layout(dp(8), dp(24)));

        mRepairBtn = new Button(this);
        mRepairBtn.setText("Repair sync service");
        mRepairBtn.setTextSize(16);
        mRepairBtn.setAllCaps(false);
        mRepairBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                doRepair();
            }
        });
        root.addView(mRepairBtn, layout(0, dp(16)));

        mStatus = new TextView(this);
        mStatus.setText("");
        mStatus.setTextSize(14);
        mStatus.setTextColor(Color.parseColor("#1e7a1e"));
        mStatus.setGravity(Gravity.CENTER);
        root.addView(mStatus, layout(dp(8), dp(24)));

        // Diagnostic log surfacing. service.py writes phase markers
        // to this file at every module-load checkpoint; if the
        // daemon's :provider process keeps dying silently, the file
        // tells us WHERE it dies (last line before death). The user
        // doesn't need adb — they can read it right here.
        Button diagBtn = new Button(this);
        diagBtn.setText("Show service boot log");
        diagBtn.setTextSize(14);
        diagBtn.setAllCaps(false);
        diagBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showDiagLog();
            }
        });
        root.addView(diagBtn, layout(dp(8), dp(8)));

        // Scrollable text area for the log contents. Empty until
        // the user taps Show service boot log.
        mDiagScroll = new ScrollView(this);
        mDiagScroll.setBackgroundColor(Color.parseColor("#f0f0f0"));
        mDiagView = new TextView(this);
        mDiagView.setTextSize(11);
        mDiagView.setTypeface(Typeface.MONOSPACE);
        mDiagView.setTextColor(Color.parseColor("#222222"));
        int dpad = dp(8);
        mDiagView.setPadding(dpad, dpad, dpad, dpad);
        mDiagView.setText("");
        mDiagScroll.addView(mDiagView);
        LinearLayout.LayoutParams scrollParams =
            new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1.0f);
        scrollParams.topMargin = dp(8);
        mDiagScroll.setLayoutParams(scrollParams);
        root.addView(mDiagScroll);

        setContentView(root);
    }

    private ScrollView mDiagScroll;
    private TextView mDiagView;

    private void showDiagLog() {
        StringBuilder sb = new StringBuilder();
        // Read the .prev rotation first (older trace), then current.
        // The Python side rotates when service_boot.log exceeds 100 KB.
        for (String name : new String[] {
                "service_boot.log.prev", "service_boot.log"}) {
            File f = new File(getFilesDir(), name);
            if (!f.exists()) {
                continue;
            }
            sb.append("=== ").append(name)
              .append(" (").append(f.length()).append(" bytes) ===\n");
            BufferedReader reader = null;
            try {
                reader = new BufferedReader(new FileReader(f));
                // Keep only the LAST ~200 lines so the view doesn't
                // get unmanageable.
                List<String> tail = new ArrayList<String>();
                int max = 200;
                String line;
                while ((line = reader.readLine()) != null) {
                    tail.add(line);
                    if (tail.size() > max) {
                        tail.remove(0);
                    }
                }
                for (String s : tail) {
                    sb.append(s).append('\n');
                }
            } catch (Throwable ex) {
                sb.append("(read error: ").append(ex).append(")\n");
            } finally {
                try {
                    if (reader != null) reader.close();
                } catch (Throwable ignored) { }
            }
            sb.append('\n');
        }
        if (sb.length() == 0) {
            sb.append("No boot log yet. This file is written by "
                    + "service.py when :provider tries to start. "
                    + "If it's empty, :provider hasn't been "
                    + "lazy-spawned since the bundle was extracted.");
        }
        mDiagView.setText(sb.toString());
        Log.i(TAG, "displayed service boot log (" + sb.length()
                + " chars)");
    }

    private void doRepair() {
        mRepairBtn.setEnabled(false);
        mStatus.setText("Refreshing…");

        // Reach into the receiver's wipe by firing the broadcast it
        // listens for. Same effect as `adb shell am broadcast -a
        // org.atoznback.aztcollab.RESET_PYTHON_BUNDLE -p <pkg>`,
        // but from inside the app's own UID — no adb required.
        Intent intent = new Intent(RESET_ACTION);
        intent.setPackage(getPackageName());
        sendBroadcast(intent);
        Log.i(TAG, "sent " + RESET_ACTION + " broadcast");

        // The receiver fires on the main process; this Activity
        // is the main process, so the receiver's Process.killProcess
        // will reach us too. Update the status one last time and
        // give the broadcast a moment to land before self-finishing
        // — the receiver will kill us anyway, but the explicit
        // finish() lets the user see the "Done" line if the kill
        // is delayed by Android's broadcast queue.
        new Handler(Looper.getMainLooper()).postDelayed(
            new Runnable() {
                @Override
                public void run() {
                    mStatus.setText(
                        "Done. Close this and reopen "
                      + "AZT Collaboration to finish.");
                    mRepairBtn.setEnabled(true);
                    mRepairBtn.setText("Close");
                    mRepairBtn.setOnClickListener(
                        new View.OnClickListener() {
                            @Override
                            public void onClick(View v) {
                                finishAndRemoveTask();
                                Process.killProcess(Process.myPid());
                            }
                        });
                }
            }, 750);
    }

    private LinearLayout.LayoutParams layout(int topMargin, int botMargin) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT);
        p.topMargin = topMargin;
        p.bottomMargin = botMargin;
        return p;
    }

    private int dp(int dps) {
        float density = getResources().getDisplayMetrics().density;
        return (int) (dps * density + 0.5f);
    }
}
