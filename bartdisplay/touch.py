"""Touch input for the XPT2046 resistive touchscreen.

SDL runs offscreen on the Pi, so it never delivers input events. We read the
touchscreen's evdev device directly on a background thread, map raw ABS_X/ABS_Y
to screen pixels, and expose a stream of raw pointer events (down/move/up).

A GestureRecognizer turns that raw stream into semantic events (press / drag /
release with tap + flick classification) that views consume. In dev mode
(BART_DEV=1) there is no evdev device — main feeds the recognizer with pointer
events synthesised from the mouse, so the same UI code works on a desktop.
"""

import queue
import sys
import threading
import time

from . import config, display

try:
    from evdev import InputDevice, ecodes, list_devices
    _HAVE_EVDEV = True
except Exception:  # evdev missing (e.g. desktop dev machine)
    _HAVE_EVDEV = False


# Gesture thresholds (screen pixels / seconds). Tuned for finger use on the
# resistive panel; each is overridable per-device via config "touch_tuning".
TAP_MAX_MOVE = 32      # max net down->up travel to still count as a tap
TAP_MAX_TIME = 0.6     # max duration to still count as a tap
FLICK_MIN_DIST = 55    # min vertical travel to count as a flick/swipe
FLICK_MAX_TIME = 0.6   # max duration for a flick
TOP_EDGE = 45          # a swipe-down starting above this y opens the shade


class RawEvent:
    __slots__ = ('kind', 'x', 'y', 't')

    def __init__(self, kind, x, y, t=None):
        self.kind = kind          # 'down' | 'move' | 'up'
        self.x = x
        self.y = y
        self.t = t if t is not None else time.monotonic()


class TouchReader:
    """Reads the touchscreen evdev device on a daemon thread."""

    def __init__(self):
        self._q = queue.Queue()
        self._dev = None
        self._cal = config.get_touch_calibration() or {}

    # -- device discovery ---------------------------------------------------
    def _find_device(self):
        explicit = config.get_touch_device()
        if explicit:
            try:
                return InputDevice(explicit)
            except Exception as e:
                print(f'[touch] configured touch_device {explicit!r} failed: {e}',
                      file=sys.stderr)
        for path in list_devices():
            try:
                dev = InputDevice(path)
            except Exception:
                continue
            caps = dev.capabilities()
            abs_codes = {c for c, _ in caps.get(ecodes.EV_ABS, [])}
            name = (dev.name or '').lower()
            if ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes and (
                'ads7846' in name or 'xpt2046' in name or 'touch' in name
            ):
                return dev
        # Fall back to the first device exposing ABS_X/ABS_Y at all.
        for path in list_devices():
            try:
                dev = InputDevice(path)
            except Exception:
                continue
            abs_codes = {c for c, _ in dev.capabilities().get(ecodes.EV_ABS, [])}
            if ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes:
                return dev
        return None

    def _abs_range(self, dev, code, default_max):
        try:
            info = dict(dev.capabilities().get(ecodes.EV_ABS, [])).get(code)
            if info is not None:
                return info.min, info.max
        except Exception:
            pass
        return 0, default_max

    def start(self):
        if not _HAVE_EVDEV:
            print('[touch] evdev not available — touch disabled '
                  '(install python-evdev on the Pi)', file=sys.stderr)
            return self
        self._dev = self._find_device()
        if self._dev is None:
            print('[touch] no touchscreen device found', file=sys.stderr)
            return self
        print(f'[touch] using {self._dev.path} ({self._dev.name})', file=sys.stderr)
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    # -- coordinate mapping -------------------------------------------------
    def _make_mapper(self, dev):
        cal = self._cal
        x_min = cal.get('x_min'); x_max = cal.get('x_max')
        y_min = cal.get('y_min'); y_max = cal.get('y_max')
        if x_min is None or x_max is None:
            x_min, x_max = self._abs_range(dev, ecodes.ABS_X, display.W)
        if y_min is None or y_max is None:
            y_min, y_max = self._abs_range(dev, ecodes.ABS_Y, display.H)
        swap = bool(cal.get('swap_xy'))
        inv_x = bool(cal.get('invert_x'))
        inv_y = bool(cal.get('invert_y'))

        def scale(v, lo, hi, size):
            if hi == lo:
                return 0
            f = (v - lo) / (hi - lo)
            f = min(1.0, max(0.0, f))
            return int(f * (size - 1))

        def mapper(rx, ry):
            if swap:
                rx, ry = ry, rx
            x = scale(rx, x_min, x_max, display.W)
            y = scale(ry, y_min, y_max, display.H)
            if inv_x:
                x = display.W - 1 - x
            if inv_y:
                y = display.H - 1 - y
            return x, y

        return mapper

    def _loop(self):
        dev = self._dev
        mapper = self._make_mapper(dev)
        rx = ry = 0
        have_x = have_y = False
        touching = False
        try:
            for ev in dev.read_loop():
                if ev.type == ecodes.EV_ABS:
                    if ev.code == ecodes.ABS_X:
                        rx, have_x = ev.value, True
                    elif ev.code == ecodes.ABS_Y:
                        ry, have_y = ev.value, True
                elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
                    if ev.value == 1:
                        touching = True
                        if have_x and have_y:
                            x, y = mapper(rx, ry)
                            self._q.put(RawEvent('down', x, y))
                    else:
                        touching = False
                        x, y = mapper(rx, ry)
                        self._q.put(RawEvent('up', x, y))
                elif ev.type == ecodes.EV_SYN and touching and have_x and have_y:
                    x, y = mapper(rx, ry)
                    self._q.put(RawEvent('move', x, y))
        except Exception as e:
            print(f'[touch] reader stopped: {e}', file=sys.stderr)

    def poll(self):
        """Drain and return pending raw events (non-blocking)."""
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out


class GestureRecognizer:
    """Turns raw down/move/up events into semantic events for the UI.

    Emits dicts:
      {'kind': 'press',   'x', 'y'}
      {'kind': 'drag',    'x', 'y', 'dx', 'dy'}     # since last move, while held
      {'kind': 'release', 'x', 'y', 'total_dy', 'dur', 'tap', 'swipe'}
        where 'swipe' is None | 'up' | 'down'
    """

    def __init__(self):
        self._down = None      # (x, y, t)
        self._last = None      # (x, y)
        self._settled = False  # have we seen a post-down sample yet?
        tuning = config.get_touch_tuning() or {}
        self.tap_max_move = tuning.get('tap_max_move', TAP_MAX_MOVE)
        self.tap_max_time = tuning.get('tap_max_time', TAP_MAX_TIME)
        self.flick_min_dist = tuning.get('flick_min_dist', FLICK_MIN_DIST)

    def feed(self, raw):
        if raw.kind == 'down':
            self._down = (raw.x, raw.y, raw.t)
            self._last = (raw.x, raw.y)
            self._settled = False
            return [{'kind': 'press', 'x': raw.x, 'y': raw.y}]
        if raw.kind == 'move':
            if self._down is None:
                return []
            if not self._settled:
                # The resistive panel's touch-down coordinate is noisy; re-anchor
                # to the first stable sample so taps aren't misread as drags.
                self._down = (raw.x, raw.y, self._down[2])
                self._last = (raw.x, raw.y)
                self._settled = True
                return [{'kind': 'drag', 'x': raw.x, 'y': raw.y, 'dx': 0, 'dy': 0}]
            lx, ly = self._last
            dx, dy = raw.x - lx, raw.y - ly
            self._last = (raw.x, raw.y)
            return [{'kind': 'drag', 'x': raw.x, 'y': raw.y, 'dx': dx, 'dy': dy}]
        if raw.kind == 'up':
            if self._down is None:
                return []
            dx0, dy0, t0 = self._down
            total_dy = raw.y - dy0
            dur = raw.t - t0
            # Judge a tap by net down->up travel (not accumulated path), so
            # jitter during the press still registers as a tap.
            net = abs(raw.x - dx0) + abs(raw.y - dy0)
            tap = net <= self.tap_max_move and dur <= self.tap_max_time
            swipe = None
            if dur <= FLICK_MAX_TIME and abs(total_dy) >= self.flick_min_dist \
                    and abs(total_dy) > abs(raw.x - dx0):
                swipe = 'down' if total_dy > 0 else 'up'
            self._down = None
            self._settled = False
            return [{
                'kind': 'release', 'x': raw.x, 'y': raw.y,
                'start_y': dy0, 'total_dy': total_dy, 'dur': dur,
                'tap': tap, 'swipe': swipe,
            }]
        return []
