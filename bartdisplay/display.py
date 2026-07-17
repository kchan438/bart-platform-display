"""Pygame display setup, fonts, palette, and framebuffer output.

Production (on the Pi): SDL renders offscreen and each frame is converted to
RGB565 and written directly to /dev/fb0 via mmap (SDL2 on Raspbian trixie has no
fbcon backend). Dev mode (BART_DEV=1): a normal SDL window is used and frames are
flipped to it, so the UI can be built on a desktop without the hardware.
"""

import os
import sys

# DEV mode must be resolved before pygame is imported so we pick the right SDL
# video driver. In production the systemd unit sets SDL_VIDEODRIVER=offscreen;
# in dev we let SDL choose a real windowed driver.
DEV = os.environ.get('BART_DEV') == '1'
if DEV:
    os.environ.pop('SDL_VIDEODRIVER', None)
else:
    os.environ.setdefault('SDL_VIDEODRIVER', 'offscreen')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')  # suppress audio errors

import mmap

import numpy as np
import pygame

from . import config

W, H = 480, 320
PAD = 14  # horizontal padding (px)

# Color palette — orange-red LED aesthetic matching the departure board.
C = {
    'on':        (255,  85,   0),
    'dim':       (122,  40,   0),
    'ghost':     ( 58,  18,   0),
    'arrive':    (255, 204,   0),
    'arrive_bg': ( 13,   8,   0),
    'bg':        (  5,   2,   0),
    'panel':     ( 12,   6,   0),   # settings shade background
    'panel_hi':  ( 30,  14,   0),   # selected/pressed row
    'white':     (255, 255, 255),
    'ok':        ( 60, 200,  60),
    'err':       (220,  60,  40),
}

# Populated by init().
screen = None
font_xs = None
font_sm = None
font_med = None

_fb_mmap = None
_fb_file = None


def init():
    """Initialise pygame, the screen surface, fonts, and framebuffer output."""
    global screen, font_xs, font_sm, font_med, _fb_mmap, _fb_file

    pygame.init()
    if DEV:
        screen = pygame.display.set_mode((W, H))
        pygame.display.set_caption('BART Platform Display (dev)')
    else:
        screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    pygame.mouse.set_visible(DEV)

    if not DEV:
        try:
            _fb_file = open('/dev/fb0', 'rb+')
            _fb_mmap = mmap.mmap(_fb_file.fileno(), W * H * 2)
        except OSError as e:
            print(f'[display] /dev/fb0 not available ({e}) — running headless',
                  file=sys.stderr)

    font_path = os.path.join(config.PROJECT_ROOT, 'fonts', 'PressStart2P-Regular.ttf')
    if not os.path.exists(font_path):
        print(
            f'Font not found: {font_path}\n'
            'Download PressStart2P-Regular.ttf from Google Fonts and place it in fonts/.',
            file=sys.stderr,
        )
        pygame.quit()
        sys.exit(1)

    font_xs = pygame.font.Font(font_path, 17)
    font_sm = pygame.font.Font(font_path, 20)
    font_med = pygame.font.Font(font_path, 24)


def present():
    """Push the current screen contents to the display (framebuffer or window)."""
    if DEV:
        pygame.display.flip()
        return
    if _fb_mmap is None:
        return
    arr = pygame.surfarray.array3d(screen)   # (W, H, 3) uint8, column-major
    arr = arr.transpose(1, 0, 2)             # (H, W, 3) row-major
    r = arr[:, :, 0].astype(np.uint16)
    g = arr[:, :, 1].astype(np.uint16)
    b = arr[:, :, 2].astype(np.uint16)
    rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    _fb_mmap.seek(0)
    _fb_mmap.write(rgb565.tobytes())


# ---------------------------------------------------------------------------
# Drawing primitives (operate on the active screen). Reused across board + UI.
# ---------------------------------------------------------------------------

def blit_left(text, font, color, x, y):
    surf = font.render(text, False, color)
    screen.blit(surf, (x, y))
    return surf.get_width(), surf.get_height()


def blit_right(text, font, color, right_x, y):
    surf = font.render(text, False, color)
    screen.blit(surf, (right_x - surf.get_width(), y))
    return surf.get_width(), surf.get_height()


def blit_center(text, font, color, cx, y):
    surf = font.render(text, False, color)
    screen.blit(surf, (cx - surf.get_width() // 2, y))
    return surf.get_width(), surf.get_height()


def divider(y, x0=PAD, x1=W - PAD, color=None):
    pygame.draw.line(screen, color or C['ghost'], (x0, y), (x1, y), 1)


def truncate(text, font, max_px):
    """Truncate text with '...' so it fits within max_px width."""
    if font.size(text)[0] <= max_px:
        return text
    while text and font.size(text + '...')[0] > max_px:
        text = text[:-1]
    return text + '...'
