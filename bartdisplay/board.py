"""Render the main BART departure board."""

from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, display
from .display import C, PAD, W, H, blit_left, blit_right, divider, truncate

LA_TZ = ZoneInfo('America/Los_Angeles')

ROW_H = 50  # height of each departure row (px)

_metrics = {}


def _time_min_widths():
    """Column widths that depend on fonts (only available after display.init())."""
    if not _metrics:
        _metrics['time'] = display.font_sm.size('NOW')[0] + 10
        _metrics['min'] = display.font_xs.size('MIN')[0] + 10
    return _metrics['time'], _metrics['min']


def render(rows, loading, blink):
    """Draw one departure-board frame onto display.screen."""
    time_w, min_w = _time_min_widths()
    fx, fs = display.font_xs, display.font_sm
    screen = display.screen
    screen.fill(C['bg'])
    y = 10

    # Station name — top left, clock — top right, both dim orange.
    clock_str = datetime.now(LA_TZ).strftime('%I:%M:%S %p')
    blit_left(config.get_station_name(), fx, C['dim'], PAD, y)
    _, ch = blit_right(clock_str, fx, C['dim'], W - PAD, y)
    y += ch + 8

    divider(y); y += 5

    # Column headers
    _, hh = blit_left('DESTINATION', fx, C['ghost'], PAD, y)
    blit_right('DEPARTS', fx, C['ghost'], W - PAD, y)
    y += hh + 4

    divider(y); y += 5

    if loading:
        blit_left('LOADING...', fs, C['dim'], PAD, y)
    elif not rows:
        blit_left('NO SERVICE', fs, C['ghost'], PAD, y)
    else:
        for row in rows[:4]:
            mins = row['minutes']
            is_now = mins[0] in ('Leaving', '0')
            dest_y = y + (ROW_H - fx.get_height()) // 2
            time_y = y + (ROW_H - fs.get_height()) // 2
            min_y = y + (ROW_H - fx.get_height()) // 2

            # Destination — flashes yellow when now arriving, else orange.
            dest_color = C['arrive'] if (is_now and blink) else C['on']
            max_dest_w = W - PAD * 2 - time_w * 2 - min_w - 8
            dest = truncate(row['destination'], fx, max_dest_w)
            blit_left(dest, fx, dest_color, PAD, dest_y)

            # Two time columns, then a single shared MIN label at far right.
            for i, m in enumerate(mins):
                slot_right = W - PAD - min_w - (1 - i) * time_w
                if m in ('Leaving', '0'):
                    color = C['arrive'] if blink else C['dim']
                    blit_right('NOW', fs, color, slot_right, time_y)
                else:
                    blit_right(m, fs, C['on'], slot_right, time_y)

            blit_right('MIN', fx, C['dim'], W - PAD, min_y)
            y += ROW_H

    # Footer
    footer_y = H - 26
    divider(footer_y - 4)
    blit_left('BART', fx, C['ghost'], PAD, footer_y)
    blit_right(f'PLATFORM {config.get_platform()}', fx, C['white'], W - PAD, footer_y)
