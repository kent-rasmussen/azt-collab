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
# Suite-canonical CAWL image set. The recorder hard-coded this
# slug in its pre-1.41.3 ``_CAWLImageResolver`` and relied on it
# any time a project didn't have an explicit image_repo
# configured — i.e. effectively every install, since the
# ConfigScreen TextInput was never widely used. Recorder
# 1.41.3 migrated the resolver to ``cawl_index(langcode)`` and
# removed its own default per the no-daemon-owned-caches rule
# (NOTES_TO_DAEMON.md 2026-05-12), so the daemon-global default
# below is now the *only* source of this slug at runtime.
#
# Per-project overrides via ``Project.cawl_image_repo`` still
# take precedence; this is just the fallback when a project
# doesn't carry an override.
#
# Override via ``azt_collabd.configure(cawl_image_repo=…)`` or
# the ``AZT_CAWL_IMAGE_REPO`` env var. Forks shipping a
# different CAWL set should configure their default at startup.
_CAWL_IMAGE_REPO_DEFAULT = 'kent-rasmussen/images_CAWL'

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
    'cawl_image_repo': os.environ.get('AZT_CAWL_IMAGE_REPO',
                                      _CAWL_IMAGE_REPO_DEFAULT),
}


def configure(*, client_id=None, app_slug=None, collaborator=None,
              template_url=None, update_repo=None,
              cawl_image_repo=None):
    """Override GitHub App identity / default template URL / update
    source repo. Any arg left None keeps the current value. Call once
    at host-app startup (before the first auth/repo op).

    ``update_repo`` is the ``owner/repo`` slug consumed by the
    self-update flow (``azt_collab_client.ui.check_for_update``); each
    sister app may pass its own to point the SettingsScreen's
    "Update this app" button at its release feed.

    ``cawl_image_repo`` is the ``owner/repo`` slug the daemon's
    CAWL image-URL index cache fetches from. The daemon owns the
    fetch + cache so peers don't burn through GitHub's
    unauthenticated rate limit; see ``azt_collabd/cawl.py``."""
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
    if cawl_image_repo is not None:
        _cfg['cawl_image_repo'] = cawl_image_repo


def default_template_url():
    return _cfg['template_url']


def update_repo():
    return _cfg['update_repo']


def cawl_image_repo():
    return _cfg['cawl_image_repo']


def get():
    return dict(_cfg)


def install_url():
    return (f'https://github.com/apps/{_cfg["app_slug"]}/'
            f'installations/new')
