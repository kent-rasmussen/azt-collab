Setting up an AZT suite client app
==================================

The server APK (``org.atoznback.aztcollab``) owns ``$AZT_HOME`` on
Android and is the only component that bundles ``azt_collabd``. Peer
apps are pure ``azt_collab_client`` consumers.


1. Layout
---------

From your peer app's repo root:

    for x in azt_collab_client examples android; do
        ln -s "../azt-collab/$x" "$x"
    done

Do NOT symlink ``azt_collabd``. Peers don't need it and shouldn't
import it.


2. Identify the client at startup
---------------------------------

    import azt_collab_client
    azt_collab_client.configure(app_id='azt-my-app')

That's the only configure() a peer makes. The GitHub App identity
(slug, client_id, collaborator) lives in the server APK; peers
never call ``azt_collabd.configure``.


3. Compatibility check
----------------------

Once at startup, before the first real RPC:

    from azt_collab_client import check_server_compat
    compat = check_server_compat()
    if not compat['ok']:
        if compat['error'] == 'server_too_old':
            # surface "Please update the AZT Collaboration service"
            ...
        elif compat['error'] == 'server_unreachable':
            # server APK not installed → install prompt
            ...

Bumping ``azt_collab_client.MIN_SERVER_VERSION`` is how we obsolete
an old server APK without coordinating a release across peers.


4. Android manifest
-------------------

    <uses-permission android:name="org.atoznback.AZT_COLLAB_ACCESS" />
    <queries>
        <package android:name="org.atoznback.aztcollab" />
    </queries>

The ``<queries>`` element is required on Android 11+ so
``PackageManager.queryContentProviders`` can see the server APK.

Don't declare a ``<provider>`` of your own. The server APK exports
the only one.


5. Signing
----------

The custom permission is ``protectionLevel="signature"``. Sign your
APK with the suite keystore (SHA-256 in
``android/SUITE_FINGERPRINT``). A peer signed with a different key
installs fine but the install-time grant is denied, and provider
calls silently fail.


6. Optional: "Open Sync Settings" button
----------------------------------------

    from azt_collab_client import open_server_ui
    result = open_server_ui()
    if not result['ok']:
        # 'desktop_only' on Android (until Intent dispatch lands)
        # or 'spawn_failed: ...' on desktop
        ...

This helper hides the platform branching so your button code stays
the same on desktop and Android.
