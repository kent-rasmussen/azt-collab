"""Entrypoints:

    python -m azt_collabd            — start the loopback server (daemon)
    python -m azt_collabd ui         — start the Kivy settings UI
    python -m azt_collabd projects   — start the Kivy project picker
                                       (helper subprocess; sister apps
                                       call azt_collab_client.pick_project()
                                       to spawn this and read the chosen
                                       path from stdout)
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
    elif not args or args[0] == 'server':
        from .server import run
        run()
    else:
        print(f'unknown command: {args[0]}', file=sys.stderr)
        _print_help()
        sys.exit(2)
