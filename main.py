import os
import sys

# Must be set before pygame is imported so SDL targets the TFT framebuffer.
# On the Pi Zero W the SPI TFT display is mapped to /dev/fb1 by the fbtft driver.
# When running on a desktop for development, unset these or override via env.
os.environ.setdefault('SDL_VIDEODRIVER', 'fbcon')
os.environ.setdefault('SDL_FBDEV',       '/dev/fb0')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')   # suppress audio errors

import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pygame
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(_HERE, 'config.json')) as _f:
    _cfg = json.load(_f)

STATION      = _cfg['station']
PLATFORM     = str(_cfg['platform'])
API_KEY      = _cfg['api_key']
REFRESH_SEC  = int(_cfg.get('refresh_interval', 30))
LA_TZ        = ZoneInfo('America/Los_Angeles')

# ---------------------------------------------------------------------------
# Color palette — matches the web component's orange-red LED aesthetic
# ---------------------------------------------------------------------------
C = {
    'on':       (255,  85,   0),
    'dim':      (122,  40,   0),
    'ghost':    ( 58,  18,   0),
    'arrive':   (255, 204,   0),
    'arrive_bg':( 13,   8,   0),
    'bg':       (  5,   2,   0),
    'white':    (255, 255, 255),
}

# ---------------------------------------------------------------------------
# BART Real-Time Departures API
# ---------------------------------------------------------------------------
_BART_URL = 'https://api.bart.gov/api/etd.aspx'


def _fetch():
    """Call BART ETD API and return sorted departure rows for the configured platform."""
    resp = requests.get(_BART_URL, params={
        'cmd':  'etd',
        'orig': STATION,
        'key':  API_KEY,
        'json': 'y',
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    station_list = data.get('root', {}).get('station', [])
    if isinstance(station_list, dict):
        station_list = [station_list]
    raw_etd = station_list[0].get('etd', []) if station_list else []
    if isinstance(raw_etd, dict):
        raw_etd = [raw_etd]

    rows = []
    for etd in raw_etd:
        estimates_raw = etd.get('estimate', [])
        if isinstance(estimates_raw, dict):
            estimates_raw = [estimates_raw]

        pool = [
            e for e in estimates_raw
            if str(e.get('platform', '')) == PLATFORM
        ]
        # Prefer non-cancelled; fall back to first if all cancelled
        first = next((e for e in pool if e.get('cancelflag', '0') != '1'), None)
        if first is None:
            first = pool[0] if pool else None
        if first is None:
            continue

        rows.append({
            'destination': etd.get('destination', '').upper(),
            'minutes':     first.get('minutes', ''),
        })

    rows.sort(key=lambda r: (
        0 if r['minutes'] in ('Leaving', '0')
        else (int(r['minutes']) if r['minutes'].isdigit() else 999)
    ))
    return rows


# ---------------------------------------------------------------------------
# Shared state — updated by background fetch thread
# ---------------------------------------------------------------------------
_lock    = threading.Lock()
_rows    = []
_loading = True


def _fetch_loop():
    global _rows, _loading
    while True:
        try:
            new_rows = _fetch()
            with _lock:
                _rows    = new_rows
                _loading = False
        except Exception as exc:
            # Keep previous rows; stop spinner so screen isn't stuck on LOADING
            with _lock:
                _loading = False
            print(f'[fetch error] {exc}', file=sys.stderr)
        time.sleep(REFRESH_SEC)


threading.Thread(target=_fetch_loop, daemon=True).start()

# ---------------------------------------------------------------------------
# Pygame / display setup
# ---------------------------------------------------------------------------
pygame.init()

W, H   = 480, 320
screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
pygame.mouse.set_visible(False)

_FONT_PATH = os.path.join(_HERE, 'fonts', 'PressStart2P-Regular.ttf')
if not os.path.exists(_FONT_PATH):
    print(
        f'Font not found: {_FONT_PATH}\n'
        'Download PressStart2P-Regular.ttf from Google Fonts and place it in fonts/.',
        file=sys.stderr,
    )
    pygame.quit()
    sys.exit(1)

font_xs  = pygame.font.Font(_FONT_PATH,  7)
font_sm  = pygame.font.Font(_FONT_PATH,  9)
font_med = pygame.font.Font(_FONT_PATH, 11)

PAD   = 14   # horizontal padding (px)
ROW_H = 38   # height of each departure row (px)
FPS   = 10   # render loop rate — low enough to spare the Pi Zero W's CPU

# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _blit_left(text, font, color, x, y):
    surf = font.render(text, False, color)
    screen.blit(surf, (x, y))
    return surf.get_width(), surf.get_height()


def _blit_right(text, font, color, right_x, y):
    surf = font.render(text, False, color)
    screen.blit(surf, (right_x - surf.get_width(), y))
    return surf.get_width(), surf.get_height()


def _divider(y):
    pygame.draw.line(screen, C['ghost'], (PAD, y), (W - PAD, y), 1)


def _truncate(text, font, max_px):
    """Truncate text with '...' so it fits within max_px width."""
    if font.size(text)[0] <= max_px:
        return text
    while text and font.size(text + '...')[0] > max_px:
        text = text[:-1]
    return text + '...'


# ---------------------------------------------------------------------------
# Render one frame
# ---------------------------------------------------------------------------

def _render(rows, loading, blink):
    screen.fill(C['bg'])
    y = 10

    # Clock — top right, dim orange
    clock_str = datetime.now(LA_TZ).strftime('%I:%M:%S %p')
    _, ch = _blit_right(clock_str, font_xs, C['dim'], W - PAD, y)
    y += ch + 8

    _divider(y);  y += 5

    # Column headers
    _, hh = _blit_left('DESTINATION', font_xs, C['ghost'], PAD, y)
    _blit_right('DEPARTS', font_xs, C['ghost'], W - PAD, y)
    y += hh + 4

    _divider(y);  y += 5

    if loading:
        _blit_left('LOADING...', font_sm, C['dim'], PAD, y)
    else:
        arriving = [r for r in rows if r['minutes'] in ('Leaving', '0')]
        upcoming = [r for r in rows if r['minutes'] not in ('Leaving', '0')][:5]

        # Blinking "NOW ARRIVING" banners
        for row in arriving:
            if blink:
                pygame.draw.rect(screen, C['arrive_bg'], (0, y, W, ROW_H))
                now_w  = font_med.size('NOW')[0]
                max_dw = W - PAD * 2 - now_w - 20
                dest   = _truncate(f'> {row["destination"]}', font_med, max_dw)
                _blit_left(dest, font_med, C['arrive'], PAD, y + 10)
                _blit_right('NOW', font_med, C['arrive'], W - PAD, y + 10)
            y += ROW_H

        # Upcoming departures
        if not arriving and not upcoming:
            _blit_left('NO SERVICE', font_sm, C['ghost'], PAD, y)
        else:
            for i, row in enumerate(upcoming):
                color   = C['dim'] if i >= 4 else C['on']
                min_txt = f'{row["minutes"]} MIN'
                min_w   = font_med.size(min_txt)[0]
                max_dw  = W - PAD * 2 - min_w - 20
                dest    = _truncate(row['destination'], font_med, max_dw)
                _blit_left(dest,    font_med, color, PAD,       y + 10)
                _blit_right(min_txt, font_med, color, W - PAD,  y + 10)
                y += ROW_H

    # Footer
    footer_y = H - 22
    _divider(footer_y - 4)
    _blit_left('BART',              font_xs, C['ghost'], PAD,    footer_y)
    _blit_right(f'PLATFORM {PLATFORM}', font_xs, C['white'], W - PAD, footer_y)

    pygame.display.flip()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
_pg_clock = pygame.time.Clock()
_blink    = True
_blink_ms = 0

running = True
while running:
    dt = _pg_clock.tick(FPS)

    _blink_ms += dt
    if _blink_ms >= 650:
        _blink    = not _blink
        _blink_ms = 0

    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            running = False

    with _lock:
        current_rows = list(_rows)
        is_loading   = _loading

    _render(current_rows, is_loading, _blink)

pygame.quit()
