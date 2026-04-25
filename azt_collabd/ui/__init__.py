"""
Standalone Kivy UI for the A-Z+T collab server.

Launch with::

    python -m azt_collabd ui

The UI runs in its own process and talks to the daemon via
azt_collab_client (same loopback transport as any other client). The
daemon is auto-spawned on first call if it isn't already running.
"""
