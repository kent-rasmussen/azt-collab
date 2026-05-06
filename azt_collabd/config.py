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
_SLUG_DEFAULT = 'azt-collaboration'
_COLLAB_DEFAULT = 'kent-rasmussen'
_TEMPLATE_URL_DEFAULT = (
    'https://raw.githubusercontent.com/'
    'kent-rasmussen/lift_templates/main/SILCAWL.lift')
_UPDATE_REPO_DEFAULT = 'kent-rasmussen/azt-collab'

_cfg = {
    'client_id': os.environ.get('AZT_GITHUB_APP_CLIENT_ID',
                                _CLIENT_ID_DEFAULT),
    'app_slug': os.environ.get('AZT_GITHUB_APP_SLUG', _SLUG_DEFAULT),
    'collaborator': os.environ.get('AZT_GITHUB_COLLABORATOR',
                                   _COLLAB_DEFAULT),
    'template_url': os.environ.get('AZT_DEFAULT_TEMPLATE_URL',
                                   _TEMPLATE_URL_DEFAULT),
    'update_repo': os.environ.get('AZT_UPDATE_REPO',
                                  _UPDATE_REPO_DEFAULT),
}


def configure(*, client_id=None, app_slug=None, collaborator=None,
              template_url=None, update_repo=None):
    """Override GitHub App identity / default template URL / update
    source repo. Any arg left None keeps the current value. Call once
    at host-app startup (before the first auth/repo op).

    ``update_repo`` is the ``owner/repo`` slug consumed by the
    self-update flow (``azt_collab_client.ui.check_for_update``); each
    sister app may pass its own to point the SettingsScreen's
    "Update this app" button at its release feed."""
    if client_id is not None:
        _cfg['client_id'] = client_id
    if app_slug is not None:
        _cfg['app_slug'] = app_slug
    if collaborator is not None:
        _cfg['collaborator'] = collaborator
    if template_url is not None:
        _cfg['template_url'] = template_url
    if update_repo is not None:
        _cfg['update_repo'] = update_repo


def default_template_url():
    return _cfg['template_url']


def update_repo():
    return _cfg['update_repo']


def get():
    return dict(_cfg)


def install_url():
    return (f'https://github.com/apps/{_cfg["app_slug"]}/'
            f'installations/new')
