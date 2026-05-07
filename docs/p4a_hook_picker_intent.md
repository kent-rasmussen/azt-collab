# p4a_hook.py change: PICK_PROJECT intent-filter on PythonActivity

The server APK (`org.atoznback.aztcollab`) needs to expose a
`org.atoznback.aztcollab.PICK_PROJECT` Intent so peer APKs can call
`startActivityForResult` on it for the project picker. The matching
`pick_project()` helper in `azt_collab_client` already builds and
dispatches this Intent; the server APK's `main.py` already reads the
Intent action at startup and mounts `picker_app` when it's
`PICK_PROJECT`. The only missing piece is the manifest declaration so
Android resolves the Intent to the server APK's `PythonActivity`.

This change lives in `$P4A_HOOK` (the env-var-resolved path to
`p4a_hook.py`, see the suite's shared `~/bin/build.sh` setup) —
outside both `azt_recorder/` and `azt-collab/` — so it isn't
applied by the in-repo edits for picker-migration step 7.

## What to add

`p4a_hook.py` already has an `_inject_aztcollab_provider` step that
injects the `<provider>` declaration inside `<application>` of the
server APK's generated `AndroidManifest.xml`. Add a sibling step
that injects an additional `<intent-filter>` into the existing
`<activity android:name="org.kivy.android.PythonActivity">` element
when `dist_name == 'aztcollab'`:

```xml
<intent-filter>
    <action android:name="org.atoznback.aztcollab.PICK_PROJECT"/>
    <category android:name="android.intent.category.DEFAULT"/>
</intent-filter>
```

The `DEFAULT` category lets Android resolve the Intent without the
caller having to specify a category.

## Why the existing PythonActivity (no second Activity)

`python-for-android` only ships one `PythonActivity`. Adding a
second Activity element with `android:name="org.kivy.android.PythonActivity"`
or an `<activity-alias>` is possible but creates two entries in
the manifest that point at the same class, which has caused issues
with `singleTask` launchMode and with p4a's task-affinity defaults
in past experiments.

The simpler approach (and what `server_apk/main.py` already
implements) is: same Activity, but it inspects the launching Intent
and mounts a different Kivy app based on the action. The
`<intent-filter>` block above tells Android "yes, I can handle
PICK_PROJECT," and the existing LAUNCHER intent-filter still handles
the default case.

## Suite-signing requirement

The custom `org.atoznback.AZT_COLLAB_ACCESS` signature-permission
already gates ContentProvider access; the picker Activity is
piggy-backing on the same signing requirement. If a peer APK isn't
signed with the suite keystore, the Intent will resolve but
`startActivityForResult` may fail at runtime — not a security boundary
the new picker introduces, just a property of how the server APK is
already set up.

## Verification

Once `p4a_hook.py` is updated and a clean `buildozer android debug`
rebuild finishes:

```
adb shell pm dump org.atoznback.aztcollab | grep -A2 PICK_PROJECT
```

should show the action listed under PythonActivity's intent-filters.
Then from the recorder app:

1. With both APKs installed, tap "Start over" → server APK's picker
   Activity comes foreground.
2. Pick a project → recorder reloads with the new LIFT.
3. Cancel → recorder stays on the previous LIFT.
4. Uninstall the server APK and try Start over → recorder surfaces
   "AZT collab service not installed" and opens the install URL.
