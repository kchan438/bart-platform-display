"""Config loading/saving for config.json.

Thread-safe because the departures fetch thread reads the API key live while the
UI thread may write a new one from the settings panel. All access goes through
the getters/setters here so writes preserve every other field on disk.
"""

import json
import os
import threading

# config.json lives at the project root, one level above this package.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, 'config.json')

_lock = threading.Lock()
_cfg = {}


def load():
    """(Re)load config.json into memory. Call once at startup."""
    global _cfg
    with _lock:
        with open(CONFIG_PATH) as f:
            _cfg = json.load(f)
    return dict(_cfg)


def _get(key, default=None):
    with _lock:
        return _cfg.get(key, default)


# --- Departure / display settings (read once at startup is fine) --------------
def get_station():
    return _get('station')


def get_station_name():
    return (_get('station_name') or get_station() or '').upper()


def get_platform():
    return str(_get('platform'))


def get_refresh_interval():
    return int(_get('refresh_interval', 30))


# --- API key (read live on every fetch so a saved key applies immediately) ----
def get_api_key():
    return _get('api_key', '')


def set_api_key(new_key):
    """Write a new API key to config.json, preserving all other fields."""
    _write_field('api_key', new_key)


# --- Touch (optional overrides) ----------------------------------------------
def get_touch_device():
    """Explicit evdev path for the touchscreen, or None to auto-detect."""
    return _get('touch_device')


def get_touch_calibration():
    """Optional dict: {x_min, x_max, y_min, y_max, swap_xy, invert_x, invert_y}."""
    return _get('touch_calibration')


def get_touch_tuning():
    """Optional dict tuning tap/swipe sensitivity:
    {tap_max_move, tap_max_time, flick_min_dist, panel_open_start_max_y}.
    Larger tap_max_move = more forgiving finger taps."""
    return _get('touch_tuning')


def get_show_touch_cursor():
    """Show a crosshair where the screen is being touched. Default on (debug aid)."""
    v = _get('show_touch_cursor')
    return True if v is None else bool(v)


def set_show_touch_cursor(enabled):
    """Persist the touch-cursor toggle to config.json."""
    _write_field('show_touch_cursor', bool(enabled))


def _write_field(key, value):
    """Read-modify-write config.json so unrelated fields are never dropped."""
    with _lock:
        # Re-read from disk to avoid clobbering external edits, then update.
        with open(CONFIG_PATH) as f:
            disk = json.load(f)
        disk[key] = value
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(disk, f, indent=2)
            f.write('\n')
        os.replace(tmp, CONFIG_PATH)
        _cfg[key] = value
