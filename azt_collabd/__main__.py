"""Entrypoints:

    python -m azt_collabd            — start the loopback server (daemon)
    python -m azt_collabd ui         — start the Kivy settings UI
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
