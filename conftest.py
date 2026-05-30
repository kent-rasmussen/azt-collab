"""Project-root conftest.

Puts the repo root on ``sys.path`` so ``from azt_collabd import …``
and ``from azt_collab_client import …`` resolve when pytest is
invoked as ``pytest tests/…`` without a ``pip install -e .`` step.

There's no setup.py / pyproject.toml at the repo root — both
packages ship via symlink into peer apps, not via PyPI — so
pytest can't infer the project root from packaging metadata. This
file is the explicit hook.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
