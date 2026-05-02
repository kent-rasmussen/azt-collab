"""
Colour themes for A-Z+T Recorder.

Every colour used in the UI is defined here so that theming is a single-file
change.  Names follow a role-based convention (what the colour *does*, not
what it *looks like*).

Kivy KV files import this module with ``#:import T theme`` and reference
colours as e.g. ``T.BG``.  Python code uses ``import theme`` and
``theme.BG`` etc.

Call ``set_theme(name)`` before the KV is built to switch palettes.
"""

import sys

# ── Role names that every palette must define ────────────────────────────────
_ROLE_KEYS = [
    'BG', 'SURFACE', 'SURFACE_ALT',
    'TEXT', 'TEXT_DIM', 'TEXT_MID', 'TEXT_FAINT', 'HINT',
    'ACCENT', 'GREEN', 'GREEN_BRIGHT', 'GREEN_DARK',
    'RED', 'TEAL', 'BLUE',
    'BTN_INACTIVE',
    'TRANSPARENT', 'OVERLAY_DARK', 'OVERLAY',
]

# ── Palette definitions ─────────────────────────────────────────────────────

_EARTH = {
    'BG':           (0.102,  0.0863, 0.0706, 1),      # #1a1612  dark earth
    'SURFACE':      (0.1647, 0.1373, 0.1255, 1),      # #2a2320  raised surface
    'SURFACE_ALT':  (0.2353, 0.1961, 0.1647, 1),      # #3c3229  lighter variant
    'TEXT':         (0.9412, 0.9098, 0.8627, 1),       # #f0e8dc  warm off-white
    'TEXT_DIM':     (0.5412, 0.4784, 0.4157, 1),       # #8a7a6a  muted taupe
    'TEXT_MID':     (0.7,    0.62,   0.55,   1),       # #b39e8c  mid-brightness
    'TEXT_FAINT':   (0.2902, 0.2275, 0.1647, 1),       # #4a3a2a  barely visible
    'HINT':         (0.5412, 0.4784, 0.4157, 0.6),    # TEXT_DIM at 60 %
    'ACCENT':       (0.7882, 0.4824, 0.2275, 1),       # #c97b3a  warm amber
    'GREEN':        (0.1529, 0.6824, 0.3765, 1),       # #27ae60  success
    'GREEN_BRIGHT': (0.1529, 0.9,    0.3765, 1),       # #27e660  highlight
    'GREEN_DARK':   (0.15,   0.35,   0.22,   1),       # #265939  toggle bg
    'RED':          (0.7529, 0.2235, 0.1686, 1),       # #c0392b  recording
    'TEAL':         (0.1529, 0.5824, 0.5765, 1),       # #279593  teal
    'BLUE':         (0.2,    0.4,    0.7,    1),       # #3366b3  blue
    'BTN_INACTIVE': (0.3922, 0.3137, 0.2353, 1),       # #64503c  dim button
    'TRANSPARENT':  (0, 0, 0, 0),
    'OVERLAY_DARK': (0, 0, 0, 0.85),
    'OVERLAY':      (0, 0, 0, 0.7),
}

_OCEAN = {
    'BG':           (0.067,  0.09,   0.122,  1),       # #111722  deep navy
    'SURFACE':      (0.102,  0.133,  0.18,   1),       # #1a222e  raised surface
    'SURFACE_ALT':  (0.149,  0.192,  0.251,  1),       # #263140  lighter variant
    'TEXT':         (0.878,  0.925,  0.969,  1),       # #e0ecf7  cool white
    'TEXT_DIM':     (0.431,  0.506,  0.584,  1),       # #6e8195  muted blue-grey
    'TEXT_MID':     (0.6,    0.68,   0.76,   1),       # #99adc2  mid-brightness
    'TEXT_FAINT':   (0.18,   0.22,   0.28,   1),       # #2e3847  barely visible
    'HINT':         (0.431,  0.506,  0.584,  0.6),    # TEXT_DIM at 60 %
    'ACCENT':       (0.259,  0.522,  0.957,  1),       # #4285f4  ocean blue
    'GREEN':        (0.1529, 0.6824, 0.3765, 1),       # #27ae60  success
    'GREEN_BRIGHT': (0.1529, 0.9,    0.3765, 1),       # #27e660  highlight
    'GREEN_DARK':   (0.1,    0.25,   0.2,    1),       # #1a4033  toggle bg
    'RED':          (0.839,  0.278,  0.227,  1),       # #d6473a  recording
    'TEAL':         (0.153,  0.584,  0.671,  1),       # #2795ab  teal
    'BLUE':         (0.259,  0.522,  0.957,  1),       # #4285f4  blue
    'BTN_INACTIVE': (0.22,   0.275,  0.345,  1),       # #384658  dim button
    'TRANSPARENT':  (0, 0, 0, 0),
    'OVERLAY_DARK': (0, 0, 0, 0.85),
    'OVERLAY':      (0, 0, 0, 0.7),
}

_FOREST = {
    'BG':           (0.075,  0.106,  0.078,  1),       # #131b14  deep forest
    'SURFACE':      (0.114,  0.157,  0.118,  1),       # #1d281e  raised surface
    'SURFACE_ALT':  (0.165,  0.22,   0.169,  1),       # #2a382b  lighter variant
    'TEXT':         (0.898,  0.941,  0.882,  1),       # #e5f0e1  leaf white
    'TEXT_DIM':     (0.459,  0.541,  0.435,  1),       # #758a6f  muted sage
    'TEXT_MID':     (0.62,   0.71,   0.6,    1),       # #9eb599  mid-brightness
    'TEXT_FAINT':   (0.2,    0.255,  0.192,  1),       # #334131  barely visible
    'HINT':         (0.459,  0.541,  0.435,  0.6),    # TEXT_DIM at 60 %
    'ACCENT':       (0.757,  0.608,  0.227,  1),       # #c19b3a  golden amber
    'GREEN':        (0.235,  0.702,  0.357,  1),       # #3cb35b  success
    'GREEN_BRIGHT': (0.235,  0.9,    0.357,  1),       # #3ce65b  highlight
    'GREEN_DARK':   (0.12,   0.3,    0.15,   1),       # #1f4d26  toggle bg
    'RED':          (0.753,  0.224,  0.169,  1),       # #c0392b  recording
    'TEAL':         (0.153,  0.584,  0.506,  1),       # #279581  teal
    'BLUE':         (0.263,  0.447,  0.639,  1),       # #4372a3  blue
    'BTN_INACTIVE': (0.25,   0.318,  0.243,  1),       # #40513e  dim button
    'TRANSPARENT':  (0, 0, 0, 0),
    'OVERLAY_DARK': (0, 0, 0, 0.85),
    'OVERLAY':      (0, 0, 0, 0.7),
}

_SLATE = {
    'BG':           (0.102,  0.106,  0.114,  1),       # #1a1b1d  cool charcoal
    'SURFACE':      (0.153,  0.157,  0.169,  1),       # #27282b  raised surface
    'SURFACE_ALT':  (0.216,  0.224,  0.239,  1),       # #37393d  lighter variant
    'TEXT':         (0.91,   0.918,  0.929,  1),       # #e8eaed  cool white
    'TEXT_DIM':     (0.494,  0.506,  0.529,  1),       # #7e8187  muted grey
    'TEXT_MID':     (0.66,   0.67,   0.69,   1),       # #a8abb0  mid-brightness
    'TEXT_FAINT':   (0.24,   0.247,  0.259,  1),       # #3d3f42  barely visible
    'HINT':         (0.494,  0.506,  0.529,  0.6),    # TEXT_DIM at 60 %
    'ACCENT':       (0.608,  0.459,  0.839,  1),       # #9b75d6  soft purple
    'GREEN':        (0.1529, 0.6824, 0.3765, 1),       # #27ae60  success
    'GREEN_BRIGHT': (0.1529, 0.9,    0.3765, 1),       # #27e660  highlight
    'GREEN_DARK':   (0.12,   0.28,   0.18,   1),       # #1f472e  toggle bg
    'RED':          (0.839,  0.278,  0.278,  1),       # #d64747  recording
    'TEAL':         (0.306,  0.596,  0.624,  1),       # #4e989f  teal
    'BLUE':         (0.357,  0.51,   0.741,  1),       # #5b82bd  blue
    'BTN_INACTIVE': (0.298,  0.306,  0.325,  1),       # #4c4e53  dim button
    'TRANSPARENT':  (0, 0, 0, 0),
    'OVERLAY_DARK': (0, 0, 0, 0.85),
    'OVERLAY':      (0, 0, 0, 0.7),
}

_LIGHT = {
    'BG':           (0.949,  0.941,  0.929,  1),       # #f2f0ed  warm paper
    'SURFACE':      (1.0,    1.0,    1.0,    1),       # #ffffff  white
    'SURFACE_ALT':  (0.918,  0.906,  0.886,  1),       # #eae7e2  light tan
    'TEXT':         (0.133,  0.122,  0.106,  1),       # #221f1b  near-black
    'TEXT_DIM':     (0.435,  0.412,  0.38,   1),       # #6f6961  muted brown
    'TEXT_MID':     (0.33,   0.31,   0.29,   1),       # #544f4a  mid-brightness
    'TEXT_FAINT':   (0.78,   0.76,   0.73,   1),       # #c7c2ba  barely visible
    'HINT':         (0.435,  0.412,  0.38,   0.6),    # TEXT_DIM at 60 %
    'ACCENT':       (0.698,  0.404,  0.118,  1),       # #b2671e  warm amber
    'GREEN':        (0.133,  0.545,  0.314,  1),       # #228b50  success
    'GREEN_BRIGHT': (0.133,  0.7,    0.314,  1),       # #22b350  highlight
    'GREEN_DARK':   (0.82,   0.92,   0.85,   1),       # #d1ebd9  toggle bg
    'RED':          (0.753,  0.224,  0.169,  1),       # #c0392b  recording
    'TEAL':         (0.133,  0.467,  0.463,  1),       # #227776  teal
    'BLUE':         (0.157,  0.353,  0.616,  1),       # #285a9d  blue
    'BTN_INACTIVE': (0.78,   0.76,   0.73,   1),       # #c7c2ba  dim button
    'TRANSPARENT':  (0, 0, 0, 0),
    'OVERLAY_DARK': (0, 0, 0, 0.55),
    'OVERLAY':      (0, 0, 0, 0.4),
}

# ── Public API ───────────────────────────────────────────────────────────────

THEMES = {
    'Earth':  _EARTH,
    'Ocean':  _OCEAN,
    'Forest': _FOREST,
    'Slate':  _SLATE,
    'Light':  _LIGHT,
}

THEME_NAMES = list(THEMES.keys())

current_theme = 'Ocean'


def _to_rgb255(rgba):
    """Convert (r,g,b,a) 0-1 tuple to (R,G,B) 0-255 for Pillow."""
    return tuple(int(c * 255) for c in rgba[:3])


def _to_rgba255(rgba):
    """Convert (r,g,b,a) 0-1 tuple to (R,G,B,A) 0-255 for Pillow."""
    return tuple(int(c * 255) for c in rgba)


def set_theme(name):
    """Apply a named palette, updating all module-level colour globals."""
    global current_theme
    if name not in THEMES:
        name = 'Ocean'
    palette = THEMES[name]
    current_theme = name
    mod = sys.modules[__name__]
    for key in _ROLE_KEYS:
        setattr(mod, key, palette[key])
    # Regenerate Pillow helpers
    setattr(mod, 'BG_RGB', _to_rgba255(palette['BG']))
    setattr(mod, 'GREEN_RGB', _to_rgb255(palette['GREEN']))
    setattr(mod, 'TEXT_RGB', _to_rgb255(palette['TEXT']))


# ── Apply default palette on import ─────────────────────────────────────────
set_theme('Ocean')
