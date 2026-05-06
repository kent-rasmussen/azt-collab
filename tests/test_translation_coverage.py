"""AST-walk every ``_(...)`` and ``_tr(...)`` call in the code base
and assert the msgid is present in the French catalog.

Catches msgid drift — the failure mode where a UI string is tweaked
in source but the .po isn't updated. The drifted string falls
through to its English form, which is hard to spot from a French
locale dev build.

This is the canonical regression test for test_plan.md §9.3.
"""

import ast
import os
import re

import pytest


REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..'))

# Source dirs to scan. Restrict to the canonical client/server
# packages — the picker subprocess's KV templates are scanned via
# the regex pass below, not AST.
SCAN_DIRS = [
    os.path.join(REPO_ROOT, 'azt_collab_client'),
    os.path.join(REPO_ROOT, 'azt_collabd'),
]

# Function names whose first string-literal argument should be a
# translatable msgid. Both calling conventions are used.
TR_NAMES = {'_', '_tr', 'tr'}

# .po path. Single catalog, single domain.
PO_PATH = os.path.join(
    REPO_ROOT, 'azt_collab_client', 'locales', 'fr',
    'LC_MESSAGES', 'azt_collab_client.po')


def _load_po_msgids(po_path):
    """Lightweight .po parser: collect every ``msgid "..."`` (plus
    multi-line continuations). Doesn't validate — that's gettext's
    job — just builds the set we'll assert against."""
    msgids = set()
    current = None
    in_msgid = False
    with open(po_path, encoding='utf-8') as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith('msgid '):
                if current is not None:
                    msgids.add(current)
                current = _po_unquote(stripped[6:])
                in_msgid = True
            elif stripped.startswith('msgstr '):
                if current is not None:
                    msgids.add(current)
                current = None
                in_msgid = False
            elif in_msgid and stripped.startswith('"'):
                current = (current or '') + _po_unquote(stripped)
    if current is not None:
        msgids.add(current)
    return msgids


_PO_ESCAPES = {'n': '\n', 't': '\t', '"': '"', '\\': '\\'}


def _po_unquote(s):
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            out.append(_PO_ESCAPES.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def _walk_python_callsites():
    """Yield (path, lineno, msgid) for every translatable callsite
    in the scanned dirs. AST-based for Python; regex pass for KV
    inside Python triple-quoted strings (``KV_TEMPLATE = '''…'''``)
    is handled by ``_walk_kv_callsites``."""
    for d in SCAN_DIRS:
        for root, _dirs, files in os.walk(d):
            # Skip locale tree itself and the buildozer cache.
            if 'locales' in root or '.buildozer' in root:
                continue
            for name in files:
                if not name.endswith('.py'):
                    continue
                path = os.path.join(root, name)
                yield from _scan_python_file(path)


def _scan_python_file(path):
    with open(path, encoding='utf-8') as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute):
            name = fn.attr
        else:
            continue
        if name not in TR_NAMES:
            continue
        if not node.args:
            continue
        first = node.args[0]
        # Plain string literal — recoverable via AST. Concatenations
        # (string + ', ' + var) are handled below.
        msgid = _msgid_from_arg(first)
        if msgid:
            yield path, node.lineno, msgid


def _msgid_from_arg(node):
    """Extract a msgid from an AST argument. Handles:

    - ``Constant(str)`` — plain string literal.
    - ``BinOp(Add, ...)`` of two adjacent literals — ``_('foo' + 'bar')``,
      rare but legal in Python.
    - **Implicit concat** like ``_('foo ' 'bar')`` parses as a single
      ``Constant`` automatically (the parser glued them).

    Returns None for anything we can't statically resolve (e.g.,
    f-strings, format-arg substitution); those aren't translatable
    msgids anyway."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _msgid_from_arg(node.left)
        right = _msgid_from_arg(node.right)
        if left is not None and right is not None:
            return left + right
    return None


_KV_TR_RE = re.compile(
    r"\b(?:_|_tr|tr)\s*\(\s*"
    r"(?:'((?:\\.|[^'\\])*)'|\"((?:\\.|[^\"\\])*)\")"
    r"\s*\)"
)


def _walk_kv_callsites():
    """Pull translatable strings out of KV templates embedded as
    triple-quoted Python strings (the patterns we use in
    ``azt_collabd/ui/app.py:KV_TEMPLATE`` and
    ``picker_app.py:_KV_TEMPLATE``). KV's own ``text: _('foo')``
    sites are not Python callsites — the AST walk misses them."""
    for d in SCAN_DIRS:
        for root, _dirs, files in os.walk(d):
            if 'locales' in root or '.buildozer' in root:
                continue
            for name in files:
                if not name.endswith('.py'):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding='utf-8') as f:
                    src = f.read()
                # Restrict regex pass to triple-quoted blocks named
                # KV_TEMPLATE or _KV_TEMPLATE. Cheaper than a full
                # KV parser and avoids re-flagging strings the AST
                # already covered.
                for m in re.finditer(
                        r"_?KV_TEMPLATE\s*=\s*'''(.*?)'''",
                        src, re.DOTALL):
                    block = m.group(1)
                    block_start_line = src.count('\n', 0, m.start()) + 1
                    for hit in _KV_TR_RE.finditer(block):
                        msgid = hit.group(1) or hit.group(2) or ''
                        if not msgid:
                            continue
                        line_in_block = block.count('\n', 0, hit.start())
                        yield (path, block_start_line + line_in_block,
                               msgid)


@pytest.fixture(scope='module')
def msgids():
    return _load_po_msgids(PO_PATH)


def test_po_has_header_entry(msgids):
    """Sanity: empty msgid (catalog header) is present."""
    assert '' in msgids


def test_python_translation_coverage(msgids):
    """Every Python ``_(...)`` / ``_tr(...)`` call's first string
    literal must be in the .po. Drift here is invisible at runtime
    until a French user opens that screen."""
    missing = []
    for path, lineno, msgid in _walk_python_callsites():
        if msgid not in msgids:
            missing.append((path, lineno, msgid))
    if missing:
        sample = '\n'.join(
            f'  {p}:{ln}  {m!r}' for p, ln, m in missing[:20])
        more = (f'\n  ... and {len(missing) - 20} more'
                if len(missing) > 20 else '')
        pytest.fail(
            f'{len(missing)} Python translatable msgid(s) missing '
            f'from {os.path.basename(PO_PATH)}:\n{sample}{more}')


def test_kv_translation_coverage(msgids):
    """KV ``text: _('foo')`` strings inside triple-quoted Python
    blocks (the ``KV_TEMPLATE`` constants in app.py / picker_app.py)
    must also be translated. Drift here is the same hazard."""
    missing = []
    for path, lineno, msgid in _walk_kv_callsites():
        if msgid not in msgids:
            missing.append((path, lineno, msgid))
    if missing:
        sample = '\n'.join(
            f'  {p}:{ln}  {m!r}' for p, ln, m in missing[:20])
        more = (f'\n  ... and {len(missing) - 20} more'
                if len(missing) > 20 else '')
        pytest.fail(
            f'{len(missing)} KV translatable msgid(s) missing '
            f'from {os.path.basename(PO_PATH)}:\n{sample}{more}')
