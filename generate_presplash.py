"""
Generate presplash.png with icon, app name, tagline, and version.

Usage: python generate_presplash.py
"""

import re
from PIL import Image, ImageDraw, ImageFont

from appinfo import APP_NAME, APP_TAGLINE, APP_ICON, FILE_W_VERSION
from azt_collab_client.ui.theme import BG_RGB, GREEN_RGB, TEXT_RGB

WIDTH, HEIGHT = 480, 800


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
        pass
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
        pass
    return BG_RGB

def generate():
    """Generate presplash.png. Returns version string."""
    version = read_version()

    img = Image.new('RGBA', (WIDTH, HEIGHT), read_bg_rgb())
    draw = ImageDraw.Draw(img)

    # Load icon and paste centered
    try:
        icon = Image.open(APP_ICON).convert('RGBA')
        icon_size = 512
        icon = icon.resize((icon_size, icon_size), Image.LANCZOS)
        icon_x = (WIDTH - icon_size) // 2
        icon_y = 200 - (icon_size - 180 ) //2
        img.paste(icon, (icon_x, icon_y), icon)
    except Exception as e:
        print(f'Warning: could not load icon: {e}')
        icon_y = 200
        icon_size = 180

    # Load fonts
    def load_font(size, bold=False):
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

    name_font = load_font(36, bold=True)
    tagline_font = load_font(22)
    version_font = load_font(14)

    # App name
    name_y = icon_y + icon_size + 30
    bbox = draw.textbbox((0, 0), APP_NAME, font=name_font)
    tw = bbox[2] - bbox[0]
    draw.text(((WIDTH - tw) // 2, name_y), APP_NAME, fill=GREEN_RGB, font=name_font)

    # Tagline
    tag_y = name_y + 55
    bbox = draw.textbbox((0, 0), APP_TAGLINE, font=tagline_font)
    tw = bbox[2] - bbox[0]
    draw.text(((WIDTH - tw) // 2, tag_y), APP_TAGLINE, fill=TEXT_RGB, font=tagline_font)

    # Version
    ver_display = 'v' + version
    version_y = HEIGHT - 60
    bbox = draw.textbbox((0, 0), ver_display, font=version_font)
    vw = bbox[2] - bbox[0]
    draw.text(((WIDTH - vw) // 2, version_y), ver_display,
              fill=TEXT_RGB[:3] + (102,),  # off-white at ~40 % alpha
              font=version_font)

    img.save('server_apk/presplash.png')
    print(f'Generated presplash.png for v{version}')
    return version


if __name__ == '__main__':
    generate()
