"""
Generate presplash images with icon, app name, tagline, and version.

Emits one image per Android density bucket under

    server_apk/presplash_variants/drawable-<bucket>/presplash.png

so Android's resource resolver picks the right pixel size at install /
launch time without any runtime rescale. The unqualified
``server_apk/presplash.png`` is also written (at hdpi-sized 480x800) as
the ``presplash.filename`` fallback — Android prefers a bucket match
over the unqualified drawable, so the fallback is only seen on devices
where none of the buckets match (rare).

Multi-density rationale lives in NOTES_TO_DAEMON.md "be eager when you
have room to" §9: rendering at native bucket size in the build keeps
the cost off-device, where it belongs, instead of asking budget phones
to PIL-resize a single oversized asset on first boot.

Usage: python generate_presplash.py
"""

import os
import re
from PIL import Image, ImageDraw, ImageFont

from appinfo import APP_NAME, APP_TAGLINE, APP_ICON, FILE_W_VERSION
from azt_collab_client.ui.theme import BG_RGB, GREEN_RGB, TEXT_RGB

# Mdpi baseline canvas. Other buckets scale from this — matches the
# Android density-scale convention (mdpi = 1.0x). Pick mdpi small
# enough that xxxhdpi (4x) stays under typical resource size limits.
_MDPI_W, _MDPI_H = 320, 533

# (bucket_name, scale_from_mdpi). Ordered low → high so the legacy
# fallback (hdpi-sized) lands in the loop output too.
_BUCKETS = [
    ('ldpi',     0.75),
    ('mdpi',     1.0),
    ('hdpi',     1.5),
    ('xhdpi',    2.0),
    ('xxhdpi',   3.0),
    ('xxxhdpi',  4.0),
]

_VARIANTS_DIR = 'server_apk/presplash_variants'
_FALLBACK_PATH = 'server_apk/presplash.png'
# hdpi is the bucket the legacy presplash.png matches (480x800 == mdpi
# baseline × 1.5). Keep it that way so a device that somehow misses the
# bucketed resources sees the same image at the same physical size.
_FALLBACK_BUCKET = 'hdpi'


def read_version():
    """Read __version__ from main.py."""
    try:
        with open(FILE_W_VERSION) as f:
            for line in f:
                m = re.match(r"^__version__\s*=\s*['\"](.+?)['\"]", line)
                if m:
                    return m.group(1)
    except Exception as e:
        print(f"Exception: {e}")
    return '?'


def read_bg_rgb():
    """Read android.presplash_color from buildozer.spec"""
    try:
        with open('server_apk/buildozer.spec') as f:
            for line in f:
                m = re.match(r"^android.presplash_color\s*=\s*(.+?)\s*$", line)
                if m:
                    return m.group(1)
    except Exception as e:
        print(f"Exception: {e}")
    return BG_RGB


def _load_font(size, bold=False):
    names = [
        'fonts/CharisSIL-Bold.ttf' if bold else 'fonts/CharisSIL-Regular.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
        else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


# Layout, all in mdpi pixel units. Rendered at int(value * scale) per
# bucket. Vertical order top→bottom: icon, name, tagline, …, version.
# Tuned to leave the bottom ~30% of the canvas as breathing room and
# keep every glyph well inside the canvas at every bucket.
_ICON_TOP = 30
_ICON_SIZE = 260                    # ~81% of mdpi width (320)
_ICON_TO_NAME_GAP = 18
_NAME_FONT_PX = 24
_NAME_TO_TAGLINE_GAP = 14
_TAGLINE_FONT_PX = 16
_VERSION_FONT_PX = 10
_VERSION_BOTTOM_MARGIN = 20         # version baseline distance from bottom


def _render(scale, bg, version):
    """Render the presplash at the given scale (1.0 == mdpi baseline)."""
    w, h = int(_MDPI_W * scale), int(_MDPI_H * scale)
    img = Image.new('RGBA', (w, h), bg)
    draw = ImageDraw.Draw(img)

    icon_size = int(_ICON_SIZE * scale)
    icon_x = (w - icon_size) // 2
    icon_y = int(_ICON_TOP * scale)
    try:
        icon = Image.open(APP_ICON).convert('RGBA')
        icon = icon.resize((icon_size, icon_size), Image.LANCZOS)
        img.paste(icon, (icon_x, icon_y), icon)
    except Exception as e:
        print(f'Warning: could not load icon: {e}')

    name_font = _load_font(int(_NAME_FONT_PX * scale), bold=True)
    tagline_font = _load_font(int(_TAGLINE_FONT_PX * scale))
    version_font = _load_font(int(_VERSION_FONT_PX * scale))

    # anchor='lt' pins draw.text(xy, ...) so xy is the top-left of the
    # rendered glyph bbox — no baseline-vs-top ambiguity across PIL
    # versions, and the bbox we measure with textbbox(... anchor='lt')
    # matches exactly what draw.text will produce.
    name_y = icon_y + icon_size + int(_ICON_TO_NAME_GAP * scale)
    bbox = draw.textbbox((0, 0), APP_NAME, font=name_font, anchor='lt')
    tw = bbox[2] - bbox[0]
    name_h = bbox[3] - bbox[1]
    draw.text((w // 2, name_y), APP_NAME, fill=GREEN_RGB,
              font=name_font, anchor='mt')

    text_dark = '#000000'  # tagline + version on green-tinted background
    tag_y = name_y + name_h + int(_NAME_TO_TAGLINE_GAP * scale)
    bbox = draw.textbbox((0, 0), APP_TAGLINE, font=tagline_font, anchor='lt')
    tag_h = bbox[3] - bbox[1]
    draw.text((w // 2, tag_y), APP_TAGLINE, fill=text_dark,
              font=tagline_font, anchor='mt')

    ver_display = 'v' + version
    bbox = draw.textbbox((0, 0), ver_display, font=version_font, anchor='lt')
    vh = bbox[3] - bbox[1]
    version_y = h - int(_VERSION_BOTTOM_MARGIN * scale) - vh
    draw.text((w // 2, version_y), ver_display, fill=text_dark,
              font=version_font, anchor='mt')
    return img


def generate():
    """Generate every density bucket plus the legacy fallback."""
    version = read_version()
    bg = read_bg_rgb()

    for bucket, scale in _BUCKETS:
        out_dir = os.path.join(_VARIANTS_DIR, f'drawable-{bucket}')
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, 'presplash.png')
        img = _render(scale, bg, version)
        img.save(out_path)
        print(f'wrote {out_path} ({img.size[0]}x{img.size[1]})')
        if bucket == _FALLBACK_BUCKET:
            img.save(_FALLBACK_PATH)
            print(f'wrote {_FALLBACK_PATH} ({img.size[0]}x{img.size[1]}) '
                  f'[unqualified fallback]')
    return version


if __name__ == '__main__':
    generate()
