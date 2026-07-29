"""Entrypoints:

    python -m azt_collabd            — start the loopback server (daemon)
    python -m azt_collabd ui         — start the Kivy settings UI
    python -m azt_collabd ui --peer <peer_id>
                                     — drive ANOTHER paired device's
                                       settings over the LAN. Requires
                                       that device to have granted this
                                       one remote settings (a toggle on
                                       its own peer list — pairing alone
                                       is not enough). Refuses rather
                                       than falling back to local.
    python -m azt_collabd projects   — start the Kivy project picker
                                       (helper subprocess; sister apps
                                       call azt_collab_client.pick_project()
                                       to spawn this and read the chosen
                                       path from stdout)
    python -m azt_collabd fingerprint
                                     — print the SHA-256 content
                                       fingerprint of the daemon code in
                                       the current source tree, so it can
                                       be compared against the deployed
                                       daemon's `/v1/health` `fingerprint`
                                       field to confirm a deploy actually
                                       picked up the latest bytes (catches
                                       p4a stale-unpack and similar
                                       deployment-cache failures that
                                       `__version__` alone can't detect).
    python -m azt_collabd fingerprint --modules
                                     — per-module breakdown of the
                                       fingerprint. One ``<hash>  <module>``
                                       line per .py / .pyc file in
                                       azt_collabd + azt_collab_client.
                                       Diff against the deployed daemon's
                                       `/v1/health.modules` dict to find
                                       the specific files that didn't
                                       update across a redeploy. (The
                                       combined fingerprint can shift from
                                       a single one-line edit; per-module
                                       hashes don't have that blind spot.)
"""

import sys


def _print_help():
    print(__doc__.strip())


if __name__ == '__main__':
    args = sys.argv[1:]
    if args and args[0] in ('-h', '--help', 'help'):
        _print_help()
        sys.exit(0)
    if args and args[0] == 'ui':
        # ``ui --peer <peer_id>`` drives ANOTHER device's daemon over the
        # LAN (0.55.117). Fixed for the life of the process: start a
        # second window rather than retargeting a running one, so no
        # window is ever ambiguous about which machine it changes.
        peer = ''
        for i, a in enumerate(args):
            if a == '--peer' and i + 1 < len(args):
                peer = args[i + 1]
        if peer:
            from . import lan_admin_client
            from azt_collab_client import transports as _t
            try:
                tr = lan_admin_client.make_transport(peer)
            except Exception as ex:
                # Refuse loudly instead of silently falling back to the
                # LOCAL daemon: a window that says it is driving Ndemli
                # while editing this machine's settings is worse than no
                # window at all.
                print(f'[ui] cannot target peer {peer!r}: {ex}',
                      file=sys.stderr, flush=True)
                sys.exit(2)
            # PROVE WE MAY ADMINISTER IT BEFORE OPENING A WINDOW
            # (0.55.121). ``make_transport`` only establishes that the
            # peer is paired, has a pinned fingerprint and has an
            # address — it cannot know whether they GRANTED us, because
            # only they hold that bit. Without this probe the window
            # opened for a device that had allowed nothing, and every
            # control inside it failed one by one.
            #
            # Kent 2026-07-29: *"click on check settings, and it opens up
            # the box — never having set allowed."* A window that opens
            # and then refuses everything is worse than a refusal: it
            # looks like the feature works and the device is broken.
            #
            # One real admin call is the only honest test. ``/v1/health``
            # needs no auth locally but still traverses all three gates
            # here, so a 403 means exactly "not granted".
            try:
                # Kept under the launcher button's wait (0.55.121) so a
                # refusal becomes a message on the button rather than the
                # button reporting success just before we exit.
                tr.call('GET', '/v1/health', None, timeout=8)
            except Exception as ex:
                msg = str(ex)
                if '403' in msg or 'no admin grant' in msg:
                    print(f'[ui] {tr.device_name or peer} has not allowed '
                          f'this device to change its settings. On THAT '
                          f'device: Settings → its peer list → this '
                          f'device → Manage → "Let this device change my '
                          f'settings".', file=sys.stderr, flush=True)
                else:
                    print(f'[ui] cannot reach {tr.device_name or peer} for '
                          f'remote settings: {msg}',
                          file=sys.stderr, flush=True)
                sys.exit(2)
            _t.target_peer(tr)
            print(f'[ui] REMOTE MODE — every setting shown belongs to '
                  f'{tr.device_name or peer}, not this machine',
                  file=sys.stderr, flush=True)
        from .ui.app import main as ui_main
        ui_main()
    elif args and args[0] == 'projects':
        from .ui.picker_app import main as picker_main
        picker_main()
    elif args and args[0] == 'fingerprint':
        from ._fingerprint import source_fingerprint, module_fingerprints
        if len(args) >= 2 and args[1] == '--modules':
            # Per-module breakdown for stale-file diagnosis.
            # Combined fingerprint can shift from a single
            # one-line edit (e.g., ``__version__`` bump); the
            # per-module dict reveals which files actually
            # changed between two checkouts (or between source
            # and deployed). Print one line per module so output
            # is grep-able; line format matches the daemon's
            # ``/v1/health.modules`` keys exactly.
            import os as _os
            here = _os.path.dirname(_os.path.abspath(__file__))
            root = _os.path.dirname(here)
            dirs = [
                _os.path.join(root, 'azt_collabd'),
                _os.path.join(root, 'azt_collab_client'),
            ]
            modules = module_fingerprints(dirs)
            for name in sorted(modules):
                print(f'{modules[name]}  {name}')
        else:
            print(source_fingerprint())
    elif not args or args[0] == 'server':
        from .server import run
        run()
    else:
        print(f'unknown command: {args[0]}', file=sys.stderr)
        _print_help()
        sys.exit(2)
