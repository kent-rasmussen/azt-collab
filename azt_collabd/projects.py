"""
Project registry, backed by ``$AZT_HOME/projects.json``.

A "project" is a working tree containing one .lift file plus its audio/
and images/ directories. The recorder registers the path it already has
(register-in-place); the backend remembers (langcode → path) so clients
can request ops by langcode instead of passing working_dir each time.

Schema (``$AZT_HOME/projects.json``):
    {
      "<langcode>": {
        "working_dir": "/abs/path/to/tree",
        "lift_path":   "/abs/path/to/tree/langcode.lift",
        "remote_url":  "https://github.com/owner/langcode.git",
        "last_commit": 1712345600.0,
        "last_sync":   1712345678.0,
        "created_at":  1700000000.0
      },
      ...
    }

``last_commit`` and ``last_sync`` are deliberately separate. The
former stamps any "the daemon committed work locally" outcome
(``COMMITTED_LOCAL``, ``COMMITTED_NO_REMOTE``,
``COMMITTED_AND_PUSHED``). The latter only stamps when the daemon
successfully reached the remote (``PUSHED``, ``PULLED``,
``COMMITTED_AND_PUSHED``). Peers can render the more recent of the
two with a marker so the user sees "13:45* committed but not yet
pushed" vs. "13:45 backed up". Filed by azt_recorder 1.37.3 in
``azt_collab_client/NOTES_TO_DAEMON.md``.
"""

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field

from .paths import azt_home


_PROJECTS_FILENAME = 'projects.json'


def projects_path():
    return os.path.join(azt_home(), _PROJECTS_FILENAME)


@dataclass
class Project:
    langcode: str
    working_dir: str
    lift_path: str = ''
    remote_url: str = ''
    last_commit: float = 0.0
    last_sync: float = 0.0
    created_at: float = 0.0
    # Per-project CAWL image source. Empty → fall back to the daemon's
    # global ``config.cawl_image_repo()`` value (smoothes the recorder
    # migration so unmigrated projects don't have to be touched). When
    # set, the daemon serves CAWL index / image bytes for this project
    # from ``$AZT_HOME/cawl/<owner>/<repo>/...`` — multiple projects
    # pointing at the same repo share that one cache directory.
    cawl_image_repo: str = ''
    # Per-project override for the GitHub repo *name* (last segment of
    # the remote URL) used by the publish path. Empty → callers treat
    # as equal to ``langcode`` (no override; the typical case).
    repo_slug: str = ''
    # The linguistic vernacular-language code (BCP-47) for entries
    # being *analyzed* in this project — the value LIFT writers stamp
    # as ``<form lang="…">`` for new entries. Distinct from
    # ``langcode``, which is the project *key* / human-readable
    # project name (``MyEnglishProject``, ``baf-test``, …) and may
    # have nothing to do with the linguistic code. The two are equal
    # in single-language projects (the common case); they diverge
    # in multilingual dictionaries and in projects whose name was
    # chosen for organizational reasons rather than linguistic ones.
    #
    # Empty → callers fall back to ``langcode`` for back-compat
    # with every project registered before 0.45.0 (the field
    # didn't exist; the old conflation IS the implicit value). New
    # projects and LAN clones explicitly populate it from the
    # handshake / template input.
    vernlang: str = ''
    # Additional Internet-hosted remotes for this project. Populated
    # when the user picks "Use both" on a KIND_REMOTE_CONFLICT
    # popup (LAN sync surfaces two divergent ``remote_url``\ s
    # from paired devices). ``remote_url`` stays as primary; the
    # push path attempts each ``extra_remotes`` entry after primary
    # as best-effort secondaries. Empty list is the common case.
    # 0.47.7+.
    extra_remotes: list = field(default_factory=list)

    def to_dict(self):
        return {
            'langcode': self.langcode,
            'working_dir': self.working_dir,
            'lift_path': self.lift_path,
            'remote_url': self.remote_url,
            'last_commit': self.last_commit,
            'last_sync': self.last_sync,
            'created_at': self.created_at,
            'cawl_image_repo': self.cawl_image_repo,
            'repo_slug': self.repo_slug,
            'vernlang': self.vernlang,
            'extra_remotes': list(self.extra_remotes or []),
        }

    @classmethod
    def from_entry(cls, langcode, d):
        raw_extra = d.get('extra_remotes') or []
        if not isinstance(raw_extra, list):
            raw_extra = []
        extra = [str(u) for u in raw_extra if isinstance(u, str) and u]
        return cls(
            langcode=langcode,
            working_dir=d.get('working_dir', ''),
            lift_path=d.get('lift_path', ''),
            remote_url=d.get('remote_url', ''),
            last_commit=float(d.get('last_commit', 0.0)),
            last_sync=float(d.get('last_sync', 0.0)),
            created_at=float(d.get('created_at', 0.0)),
            cawl_image_repo=d.get('cawl_image_repo', ''),
            repo_slug=d.get('repo_slug', ''),
            vernlang=d.get('vernlang', ''),
            extra_remotes=extra,
        )

    def effective_vernlang(self):
        """The vernlang to feed LIFT writers with. Returns
        ``self.vernlang`` if explicitly set, else ``self.langcode``
        (the pre-0.45.0 implicit value). Use this everywhere a
        LIFT ``<form lang="…">`` is written so the conflation
        fallback stays in one place."""
        return self.vernlang or self.langcode


# ── load / save ─────────────────────────────────────────────────────────────

def _load_raw():
    try:
        with open(projects_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as ex:
        print(f'[collab.projects] load failed: {ex}')
        # Sentinel: distinguishes "load failed" from "file legitimately
        # empty / missing." ``_update`` refuses to save when this comes
        # back so a transient parse failure can't clobber the on-disk
        # registry with an empty dict. Callers that just *read*
        # (``get``, ``list_all``) get an empty dict view as before — a
        # ``KeyError``-free missing-project lookup degrades the same
        # way regardless of the underlying reason.
        return _LoadFailed()


class _LoadFailed(dict):
    """Empty-dict sentinel returned when ``projects.json`` couldn't
    be parsed. ``isinstance(d, _LoadFailed)`` flags the case in
    ``_update`` so the mutator's write step is skipped. Inherits
    from ``dict`` so existing read-path callers (``get``,
    ``list_all``) see an empty mapping without special-casing."""
    pass


def _save_raw(data):
    path = projects_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.projects.', suffix='.tmp',
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _update(mutator):
    d = _load_raw()
    if isinstance(d, _LoadFailed):
        # Parse failure: leave the on-disk file alone. Saving would
        # overwrite the (presumably) recoverable corrupt file with
        # an empty registry and silently destroy every project entry.
        print('[collab.projects] _update aborted — load failed; '
              'on-disk projects.json left untouched',
              file=sys.stderr, flush=True)
        return
    mutator(d)
    _save_raw(d)


# ── public API ──────────────────────────────────────────────────────────────

def register(langcode, working_dir, lift_path='', remote_url='',
             cawl_image_repo=None, repo_slug=None):
    """Register or update a project. Returns the resulting Project.

    ``cawl_image_repo`` / ``repo_slug`` accept None (don't touch
    the field; preserves any previously-set value across
    re-registration), empty string (explicitly clear; the
    project falls back to default behaviour — daemon-global
    CAWL repo for ``cawl_image_repo``, ``langcode`` itself for
    ``repo_slug``), or a non-empty string."""
    if not langcode:
        raise ValueError('langcode required')
    if not working_dir:
        raise ValueError('working_dir required')
    data = _load_raw()
    if isinstance(data, _LoadFailed):
        # Same guard as ``_update``: refuse to clobber a corrupt
        # ``projects.json`` with a registry that contains *only* the
        # newly-registered project. Caller is expected to surface
        # the failure and let the user / a future recovery pass
        # restore the file.
        raise RuntimeError(
            'projects.json could not be parsed; refusing to '
            'register over a corrupt registry — inspect and '
            'recover the on-disk file first')
    entry = dict(data.get(langcode, {}))
    entry['working_dir'] = working_dir
    if lift_path:
        entry['lift_path'] = lift_path
    if remote_url:
        entry['remote_url'] = remote_url
    if cawl_image_repo is not None:
        entry['cawl_image_repo'] = cawl_image_repo
    if repo_slug is not None:
        entry['repo_slug'] = repo_slug
    entry.setdefault('last_sync', 0.0)
    entry.setdefault('created_at', time.time())
    data[langcode] = entry
    _save_raw(data)
    return Project.from_entry(langcode, entry)


def set_cawl_image_repo(langcode, repo):
    """Persist a per-project CAWL image repo slug. Empty string is a
    valid value — clears the override so the project falls back to the
    daemon-global default."""
    def mut(d):
        if langcode in d:
            d[langcode]['cawl_image_repo'] = repo
    _update(mut)


def set_repo_slug(langcode, slug):
    """Persist a per-project GitHub-repo-name override for the
    publish path. Empty string is a valid value — clears the
    override so callers fall back to ``langcode`` (the typical
    case)."""
    def mut(d):
        if langcode in d:
            d[langcode]['repo_slug'] = slug
    _update(mut)


def unregister(langcode):
    def mut(d):
        d.pop(langcode, None)
    _update(mut)


def rename(old_langcode, new_langcode):
    """Rename a project's key in ``projects.json`` while preserving
    its working_dir / lift_path / remote_url / created_at /
    last_sync. Returns the resulting Project under the new key, or
    None if ``old_langcode`` isn't registered. Raises ``ValueError``
    if ``new_langcode`` is empty or already names a different
    project.

    Used by the picker's "confirm langcode" flow: the daemon
    auto-derives a langcode from the LIFT filename / URL on clone
    or open-file, but the user may want to override it before the
    project is handed back to the recorder. Same-name rename is a
    no-op."""
    if not new_langcode:
        raise ValueError('new_langcode required')
    if old_langcode == new_langcode:
        return get(old_langcode)
    data = _load_raw()
    entry = data.get(old_langcode)
    if entry is None:
        return None
    if new_langcode in data:
        raise ValueError(
            f'{new_langcode!r} is already registered to a different '
            f'working_dir; pick a different langcode')
    def mut(d):
        d[new_langcode] = dict(entry)
        d.pop(old_langcode, None)
    _update(mut)
    return Project.from_entry(new_langcode, entry)


def get(langcode):
    entry = _load_raw().get(langcode)
    if entry is None:
        return None
    return Project.from_entry(langcode, entry)


def list_all():
    return [Project.from_entry(code, entry)
            for code, entry in _load_raw().items()]


def find_langcode_by_working_dir(working_dir):
    """Return the registered langcode whose ``working_dir`` matches
    ``working_dir``, or '' if none is registered. Used by helpers
    that operate on ``project_dir`` (sync, commit-audio-and-sync,
    init) but need the langcode to update langcode-keyed state
    (e.g. ``commit_failure_count``)."""
    if not working_dir:
        return ''
    try:
        target = os.path.abspath(working_dir)
    except Exception:
        target = working_dir
    for code, entry in _load_raw().items():
        wd = entry.get('working_dir', '')
        if not wd:
            continue
        try:
            if os.path.abspath(wd) == target:
                return code
        except Exception:
            if wd == working_dir:
                return code
    return ''


def set_last_sync(langcode, ts=None):
    if ts is None:
        ts = time.time()
    def mut(d):
        if langcode in d:
            d[langcode]['last_sync'] = float(ts)
    _update(mut)


def set_last_commit(langcode, ts=None):
    """Stamp the timestamp of the most recent local commit. Set on
    ``COMMITTED_LOCAL`` / ``COMMITTED_NO_REMOTE`` /
    ``COMMITTED_AND_PUSHED`` outcomes — any path where the daemon
    actually wrote a commit object to the working tree, push or no
    push. Peers render this alongside ``last_sync`` so the
    "committed but not yet pushed" state has a real timestamp."""
    if ts is None:
        ts = time.time()
    def mut(d):
        if langcode in d:
            d[langcode]['last_commit'] = float(ts)
    _update(mut)


def set_remote_url(langcode, url):
    def mut(d):
        if langcode in d:
            d[langcode]['remote_url'] = url
    _update(mut)


def set_last_sync_error(langcode, code, ts=None):
    """Persist the access-class reason the last WAN sync failed
    (``AUTH_REQUIRED`` / ``REPO_NO_ACCESS`` / ``REPO_NOT_AUTHORIZED`` /
    ``APP_SUSPENDED`` / …) so ``project_status`` can surface WHY sync is
    stuck instead of silently backing off (0.52.24, requirement 1.1).
    Cleared by ``clear_last_sync_error`` on the next successful sync."""
    if ts is None:
        ts = time.time()
    def mut(d):
        if langcode in d:
            d[langcode]['last_sync_error'] = str(code)
            d[langcode]['last_sync_error_at'] = float(ts)
    _update(mut)


def clear_last_sync_error(langcode):
    """Drop any persisted sync-error reason. Called on a successful sync
    and after an auto-accepted invite clears the access problem."""
    def mut(d):
        entry = d.get(langcode)
        if isinstance(entry, dict):
            entry.pop('last_sync_error', None)
            entry.pop('last_sync_error_at', None)
    _update(mut)


def add_extra_remote(langcode, url):
    """Append *url* to this project's ``extra_remotes`` list,
    preserving order and deduping. No-op if *url* equals the
    project's primary ``remote_url`` (the user picked
    ``dual_publish`` but the two URLs are identical — possibly
    a re-pair race) or if *url* is already in the list. Used by
    KIND_REMOTE_CONFLICT mode ``dual_publish``."""
    def mut(d):
        if langcode not in d:
            return
        entry = d[langcode]
        if str(entry.get('remote_url', '') or '') == url:
            return
        extras = list(entry.get('extra_remotes') or [])
        if url in extras:
            return
        extras.append(url)
        entry['extra_remotes'] = extras
    _update(mut)


def remove_extra_remote(langcode, url):
    """Remove *url* from ``extra_remotes`` (no-op if absent).
    Used when the user changes their mind via the settings UI."""
    def mut(d):
        if langcode not in d:
            return
        extras = [u for u in (d[langcode].get('extra_remotes') or [])
                  if u != url]
        d[langcode]['extra_remotes'] = extras
    _update(mut)


def set_last_lan_pushed_sha(langcode, sha):
    """Record the most recent commit we've successfully LAN-delivered
    to *any* paired peer for *langcode*. Used by the sync-indicator
    "OK" branch — a local commit is "shared somewhere" if it's an
    ancestor of either ``refs/remotes/origin/main`` (github) or this
    SHA (LAN). Empty string clears."""
    def mut(d):
        if langcode in d:
            d[langcode]['last_lan_pushed_sha'] = sha
    _update(mut)


def get_last_lan_pushed_sha(langcode):
    entry = _load_raw().get(langcode) or {}
    return entry.get('last_lan_pushed_sha', '') or ''


def set_vernlang(langcode, vernlang):
    """Persist the per-project linguistic vernacular code (the
    LIFT ``<form lang="…">`` value for newly-written entries).
    Distinct from ``langcode`` (the project key); see
    ``Project.vernlang`` and ``Project.effective_vernlang()``.
    Empty string clears the field — callers then fall back to
    ``langcode`` per the back-compat rule."""
    def mut(d):
        if langcode in d:
            d[langcode]['vernlang'] = vernlang
    _update(mut)


# ── derivation helpers (used for auto-registration) ─────────────────────────

def derive_remote_url(working_dir):
    """Return the origin URL from the git config, or ''."""
    try:
        from dulwich.repo import Repo
        repo = Repo(working_dir)
        try:
            return repo.get_config().get(
                (b'remote', b'origin'), b'url').decode('utf-8')
        except KeyError:
            return ''
    except Exception:
        return ''


def _mint_fresh_guids(xml_bytes):
    """Return a copy of *xml_bytes* (a LIFT document) where every
    ``<entry guid="...">`` carries a fresh UUID-4 and every
    ``ref="..."`` attribute that pointed at one of those old guids
    is rewritten to the new value.

    Used by ``create_from_template`` so two projects derived from
    the same template don't share entry GUIDs entry-for-entry.
    LIFT only requires ``<entry guid="...">`` to be unique within a
    single file, but several peer-side features key state off guid
    alone (caches, retry queues, future shared-clipboard work);
    those features misbehave silently when two distinct projects
    in the same daemon share 1700+ identical guids out of a SILCAWL
    template.

    Conservative-by-design:
      - Only rewrites ``<entry guid="...">`` — leaves
        ``<sense id="...">`` and other identifier slots alone.
      - Only rewrites ``ref="..."`` attributes whose value matches
        one of OUR rewritten entry guids. A ``ref`` on some other
        LIFT element with a non-guid value (sense ids, etc.) is
        left alone.
      - Returns the input bytes unchanged on parse failure or if
        no ``<entry guid="...">`` is found (so a non-LIFT template
        flows through untouched and the downstream consumer sees
        the same content it would have seen before this transform
        existed).

    Note on serialization: ``ET.tostring`` writes the document
    without preserving the original ``<?xml ... ?>`` declaration
    verbatim. We emit ``<?xml version='1.0' encoding='utf-8'?>``,
    which matches the convention every other write site in the
    daemon uses (``lift_merge`` and ``atomic_recovery`` both
    serialize via ET).
    """
    import uuid
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return xml_bytes

    mapping = {}
    for entry in root.iter('entry'):
        old = entry.get('guid')
        if not old:
            continue
        new = str(uuid.uuid4())
        mapping[old] = new
        entry.set('guid', new)

    if not mapping:
        return xml_bytes

    for elem in root.iter():
        ref = elem.get('ref')
        if ref and ref in mapping:
            elem.set('ref', mapping[ref])

    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _clean_template(xml_bytes, vernlang):
    """Prune a freshly-downloaded SILCAWL template down to the target
    vernacular language — once, server-side, so no peer needs its own
    cleaner and the rules can't drift across peers.

    Host-decided rules (2026-07-04; full rationale in
    ``azt_collab_client/NOTES_TO_DAEMON.md``):

      1. **lexical-unit** — keep only ``<form lang=vernlang>``. Drop
         every other-language form. *No-loss guard:* before dropping a
         populated other-language form, if its language has no non-empty
         ``<gloss>`` in the entry, move the form's text into a
         ``<gloss>`` first (reusing an empty gloss for that lang if one
         exists, else creating one). If no vernlang form exists, add an
         empty ``<form lang=vernlang><text/></form>`` so the headword
         slot exists and vernlang stays auto-detectable by peers.
      2. **glosses** — drop ``<gloss>`` whose text is empty/whitespace;
         keep every populated one. Runs after rule 1 so a just-moved
         gloss is never pruned.
      3. **definition** — drop empty ``<form>`` children; keep every
         populated one and keep the ``<definition>`` parent even when
         it ends up formless (user familiarity).
      4. **citation** — mirror rule 1: keep only ``<form lang=vernlang>``,
         drop every other-language form (empty or populated); keep the
         ``<citation>`` parent even when it ends up formless
         (``set_audio`` tolerates its presence or absence).
      5. **sense** — left as-is (never empty in practice).

    ``vernlang`` is matched as the full assembled BCP-47 tag, compared
    exactly (``nml``, ``ba-x-dialect``, ``en-US-x-Kent``) — never on a
    bare language subtag.

    Bytes -> bytes, same contract as ``_mint_fresh_guids``: returns the
    input unchanged on parse failure (or when nothing needed changing),
    so an almost-but-not-quite-LIFT template flows through untouched.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return xml_bytes

    def _text_of(holder):
        # <form>/<gloss> carry their text in a child <text> element.
        t = holder.find('text')
        return None if t is None else t.text

    def _is_empty(s):
        return s is None or not s.strip()

    changed = False

    for entry in root.iter('entry'):
        lu = entry.find('lexical-unit')
        if lu is not None:
            forms = lu.findall('form')
            has_vern = any(f.get('lang') == vernlang for f in forms)

            for f in forms:
                if f.get('lang') == vernlang:
                    continue
                lang = f.get('lang')
                txt = _text_of(f)
                if lang and not _is_empty(txt):
                    # No-loss: ensure this source word survives as a gloss.
                    sense = entry.find('sense')
                    if sense is None:
                        sense = ET.SubElement(entry, 'sense')
                        changed = True
                    have_populated = any(
                        g.get('lang') == lang and not _is_empty(_text_of(g))
                        for g in sense.findall('gloss'))
                    if not have_populated:
                        target = next(
                            (g for g in sense.findall('gloss')
                             if g.get('lang') == lang), None)
                        if target is None:
                            target = ET.SubElement(sense, 'gloss')
                            target.set('lang', lang)
                        gt = target.find('text')
                        if gt is None:
                            gt = ET.SubElement(target, 'text')
                        gt.text = txt
                        changed = True
                lu.remove(f)
                changed = True

            if not has_vern:
                vf = ET.SubElement(lu, 'form')
                vf.set('lang', vernlang)
                ET.SubElement(vf, 'text')
                changed = True

        # Rule 2: drop empty glosses (after the rule-1 move above).
        for sense in entry.findall('sense'):
            for g in list(sense.findall('gloss')):
                if _is_empty(_text_of(g)):
                    sense.remove(g)
                    changed = True

        # Rule 3: drop empty <form> children of <definition>; keep the
        # <definition> parent even if it ends up formless (user
        # familiarity). Nested under <sense>, so iter over all of them.
        for definition in entry.iter('definition'):
            for f in list(definition.findall('form')):
                if _is_empty(_text_of(f)):
                    definition.remove(f)
                    changed = True

        # Rule 4: citation mirrors lexical-unit (rule 1) — keep only
        # <form lang=vernlang>, drop every other-language form (empty or
        # populated). Keep the <citation> parent even if it ends up
        # formless (set_audio tolerates its presence or absence).
        for citation in entry.findall('citation'):
            for f in list(citation.findall('form')):
                if f.get('lang') != vernlang:
                    citation.remove(f)
                    changed = True

    if not changed:
        return xml_bytes

    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def create_from_template(template_url, vernlang, dest_dir,
                         timeout=60, size_cap=10 * 1024 * 1024):
    """Download a LIFT template and register it as a project.

    Returns the resulting Project. ``size_cap`` (default 10 MiB) defends
    against accidentally pulling a giant repo via a misconfigured URL —
    the SILCAWL template is ~200 KB, so this is plenty of head-room.

    Mints fresh ``<entry guid="...">`` values on import (since 0.50.8;
    see ``_mint_fresh_guids``) so two projects derived from the same
    template don't collide on guid-keyed state.

    Raises ``ValueError`` for missing args, ``RuntimeError`` for download
    failures.
    """
    import urllib.request
    from .net import _ensure_ssl

    if not template_url:
        raise ValueError('template_url required')
    if not vernlang:
        raise ValueError('vernlang required')
    if not dest_dir:
        raise ValueError('dest_dir required')

    project_dir = os.path.abspath(dest_dir)
    os.makedirs(project_dir, exist_ok=True)
    lift_path = os.path.join(project_dir, f'{vernlang}.lift')

    # On Android p4a doesn't ship system CA certs; without this patch
    # urlopen fails with SSL: CERTIFICATE_VERIFY_FAILED. Every other
    # network-touching function in azt_collabd calls this first; this
    # site was missed.
    _ensure_ssl()
    req = urllib.request.Request(template_url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = resp.read(size_cap + 1)
    if len(content) > size_cap:
        raise RuntimeError(
            f'template exceeds size cap ({size_cap} bytes)')
    if len(content) < 50:
        raise RuntimeError(
            f'template download too small ({len(content)} bytes)')

    # Mint fresh entry GUIDs before settling the file in place.
    # Defensive: any failure during the transform falls back to the
    # original bytes so a template that's almost-but-not-quite-LIFT
    # (e.g. served via a 200 OK error page) doesn't break the
    # download path entirely — it'll fail more specifically later
    # when LIFT readers try to parse it.
    try:
        content = _mint_fresh_guids(content)
    except Exception as ex:
        print(f'[create_from_template] _mint_fresh_guids failed '
              f'(template GUIDs unchanged): {ex!r}',
              file=sys.stderr, flush=True)

    fd, tmp = tempfile.mkstemp(prefix='.template.', suffix='.lift',
                               dir=project_dir)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
        os.replace(tmp, lift_path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

    # langcode is the project key (== the slug used in the filename
    # and the registry); vernlang is the linguistic code for LIFT
    # writes. For template-created projects the two are equal by
    # design (the BCP-47 picker collected one value before this
    # function ran), so we stamp them both. Distinct from the
    # clone path where ``langcode`` is the repo slug and ``vernlang``
    # may differ.
    p = register(vernlang, project_dir, lift_path=lift_path)
    try:
        set_vernlang(vernlang, vernlang)
    except Exception:
        pass
    # Initialize git immediately so every project has a usable .git/
    # from day one. Pre-0.45.42 this step was deferred to the user's
    # eventual Publish gesture (init_repo, which needs a remote_url),
    # which meant projects that lived their whole life without github
    # publishing accumulated audio + LIFT writes on disk while every
    # commit_project call NOT_A_REPO'd silently — no git history, no
    # LAN sharing (listener returns 404 with no .git/), no recovery
    # path for crash protection. Initializing here gives the project
    # a HEAD before the user's first record fires. Best-effort: a
    # failure here is logged but doesn't fail the create — the
    # auto-init recovery branch in ``repo._commit_repo_locked`` is
    # the safety net for any case this misses.
    try:
        from . import repo as _repo
        _repo.ensure_initial_commit(
            project_dir, contributor_name='AZT')
    except Exception as ex:
        print(f'[create_from_template] initial-commit on '
              f'{project_dir!r} failed (project still usable; '
              f'next commit_project will retry): {ex!r}',
              file=sys.stderr, flush=True)
    return p


def derive_langcode(working_dir, lift_path=''):
    """Pick a langcode for a working_dir by this priority:
        1. git remote repo name (last path segment, .git stripped)
        2. .lift filename stem
        3. working_dir basename
    """
    url = derive_remote_url(working_dir)
    if url:
        name = url.rstrip('/').rsplit('/', 1)[-1]
        if name.endswith('.git'):
            name = name[:-4]
        if name:
            return name
    if lift_path:
        base = os.path.basename(lift_path)
        if base.endswith('.lift'):
            base = base[:-5]
        if base:
            return base
    base = os.path.basename(os.path.normpath(working_dir))
    return base or 'project'
