"""
GitHub App identity config for azt_collabd.

The recorder sets this via ``azt_collabd.configure(...)`` at startup.
When the server is launched standalone (``python -m azt_collabd``),
values come from env vars:

    AZT_GITHUB_APP_CLIENT_ID   GitHub App client_id (device flow)
    AZT_GITHUB_APP_SLUG        The app slug (used to construct install URL)
    AZT_GITHUB_COLLABORATOR    GitHub user auto-added to new repos

Defaults match the original A-Z+T Recorder values so behavior is
preserved when nobody calls configure().
"""

import os

_CLIENT_ID_DEFAULT = 'Iv23li66Fo9MBReatv6i'
_SLUG_DEFAULT = 'azt-recorder'
_COLLAB_DEFAULT = 'kent-rasmussen'

_cfg = {
    'client_id': os.environ.get('AZT_GITHUB_APP_CLIENT_ID',
                                _CLIENT_ID_DEFAULT),
    'app_slug': os.environ.get('AZT_GITHUB_APP_SLUG', _SLUG_DEFAULT),
    'collaborator': os.environ.get('AZT_GITHUB_COLLABORATOR',
                                   _COLLAB_DEFAULT),
}


def configure(*, client_id=None, app_slug=None, collaborator=None):
    """Override GitHub App identity. Any arg left None keeps the current
    value. Call once at host-app startup (before the first auth/repo op)."""
    if client_id is not None:
        _cfg['client_id'] = client_id
    if app_slug is not None:
        _cfg['app_slug'] = app_slug
    if collaborator is not None:
        _cfg['collaborator'] = collaborator


def get():
    return dict(_cfg)


def install_url():
    return (f'https://github.com/apps/{_cfg["app_slug"]}/'
            f'installations/new')
