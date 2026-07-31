"""Swipe-down settings panel: Wi-Fi selector + BART API key editor.

The panel slides down over the board like a phone notification shade. It owns its
own open/close animation, sub-views (menu / Wi-Fi list / Wi-Fi detail / API key),
and an optional on-screen keyboard overlay for password and key entry.

Rendering redirects display.screen to an offscreen surface so all the shared
draw helpers work unchanged, then reveals the top `reveal` pixels of that surface
to create the pull-down effect.
"""

import threading

import pygame

from .. import config, display, wifi
from ..display import C, W, H, PAD
from .keyboard import Keyboard
from .widgets import Button, draw_signal_bars, draw_lock, draw_tag

# Animation: full slide in this many seconds.
_SLIDE_SEC = 0.22
_HEADER_H = 34
_ROW_H = 40
_LIST_STATUS_TOP = 40
# Reserve enough vertical space for both lines of a wrapped Wi-Fi status.
_LIST_TOP = 82
_LIST_BOTTOM = H - 8
_SCROLLBAR_W = 40
_SCROLL_BUTTON_H = 40
_SCROLL_THUMB_MIN_H = 32
_LIST_RIGHT = W - _SCROLLBAR_W
_DETAIL_ACTION_TOP = H - 40
_STATUS_ACTION_GAP = 4


class SettingsPanel:
    def __init__(self):
        self.state = 'CLOSED'          # CLOSED | OPENING | OPEN | CLOSING
        self.y_off = -H                # -H (hidden) .. 0 (fully open)
        self.view = 'menu'             # menu | wifi | wifi_detail | apikey
        self.keyboard = None
        self.request_exit = False      # main loop watches this for RESTART

        self.networks = []
        self.selected = None           # Network in detail view
        self.detail_info = []          # cached info rows for the detail view
        self._detail_request_id = 0    # invalidates stale detail worker results
        self.scan_job = None
        self.action_job = None
        self.status = ''
        self.status_kind = 'info'       # info | success | error
        self._after_scan_message = ''
        self.scroll = 0
        self._scrollbar_dragging = False
        self._scrollbar_drag_offset = 0

        self.api_saved = False
        self._buttons = []
        self._surf = pygame.Surface((W, H))

    # -- open/close ---------------------------------------------------------
    def is_active(self):
        return self.state != 'CLOSED'

    def open(self):
        if self.state in ('CLOSED', 'CLOSING'):
            self.state = 'OPENING'
            self.view = 'menu'
            self.status = ''

    def close(self):
        if self.state in ('OPEN', 'OPENING'):
            self.state = 'CLOSING'
            self.keyboard = None
            self._scrollbar_dragging = False

    def _goto(self, view):
        previous = self.view
        self.view = view
        self.status = ''
        self.status_kind = 'info'
        self._scrollbar_dragging = False
        if view == 'wifi' and previous != 'wifi_detail':
            # Entering the list reads NetworkManager's existing AP cache.
            # A hardware rescan happens only when the user taps RESCAN.
            self._start_scan(rescan=False)
        if view == 'apikey':
            self.api_saved = False

    # -- Wi-Fi operations ---------------------------------------------------
    def _wifi_busy(self):
        return (
            (self.scan_job is not None and not self.scan_job.done)
            or (self.action_job is not None and not self.action_job.done)
        )

    def _start_scan(self, after_message='', rescan=True):
        if self._wifi_busy():
            return
        self.scan_job = wifi.scan_async(rescan=rescan)
        self._after_scan_message = after_message
        self.status = 'Scanning...' if rescan else 'Refreshing...'
        self.scan_job.set_status(self.status)
        self.status_kind = 'info'

    def _update_wifi_jobs(self):
        if self.scan_job is not None:
            job = self.scan_job
            self.status = job.status
            self.status_kind = 'info'
            if job.done:
                self.scan_job = None
                if job.networks or job.ok:
                    self.networks = job.networks
                    self._clamp_scroll()
                after_message = self._after_scan_message
                self._after_scan_message = ''
                if not job.ok:
                    self.status = (
                        f'{after_message} - {job.message}'
                        if after_message and job.message
                        else (after_message or job.message)
                    )
                    self.status_kind = 'error'
                elif after_message:
                    self.status = (
                        f'{after_message} - {job.message}'
                        if job.message
                        else after_message
                    )
                    self.status_kind = 'info' if job.partial else 'success'
                elif job.message:
                    self.status = job.message
                    self.status_kind = 'info'
                else:
                    self.status = ''

        if self.action_job is not None:
            job = self.action_job
            self.status = job.status
            self.status_kind = 'info'
            if not job.done:
                return

            self.action_job = None
            if job.ok:
                if job.kind == 'disconnect':
                    for network in self.networks:
                        if network.ssid == job.ssid:
                            network.active = False
                self.selected = None
                self.detail_info = []
                self.view = 'wifi'
                self._start_scan(after_message=job.message, rescan=False)
                return

            self.status = job.message
            self.status_kind = 'error'
            if job.needs_password and self.selected is not None:
                title = (
                    'Wrong password'
                    if job.code == 'authentication_failed'
                    else 'Password: ' + self.selected.ssid
                )
                self._open_keyboard(
                    title,
                    '',
                    password=True,
                    on_submit=self._connect_with_password,
                )

    # -- per-frame update ---------------------------------------------------
    def update(self, dt_ms):
        speed = H / _SLIDE_SEC
        step = speed * dt_ms / 1000.0
        if self.state == 'OPENING':
            self.y_off = min(0, self.y_off + step)
            if self.y_off >= 0:
                self.y_off = 0
                self.state = 'OPEN'
        elif self.state == 'CLOSING':
            self.y_off -= step
            if self.y_off <= -H:
                self.y_off = -H
                self.state = 'CLOSED'

        self._update_wifi_jobs()

    def animating(self):
        return self.state in ('OPENING', 'CLOSING')

    # -- event handling -----------------------------------------------------
    def handle(self, event):
        if not self.is_active():
            return
        kind = event.get('kind')

        if self.keyboard is not None:
            self.keyboard.handle(event)
            return

        # Swipe up returns to the board from every panel view. Finishing a
        # scrollbar-thumb drag must not also dismiss the panel.
        if kind == 'release' and event.get('swipe') == 'up':
            if self._scrollbar_dragging:
                self._scrollbar_dragging = False
                return
            self.close()
            return

        if kind == 'drag' and self.view == 'wifi':
            if (
                    not self._scrollbar_dragging
                    and event.get('dx') == 0
                    and event.get('dy') == 0):
                # The resistive panel's press coordinate can be noisy. Its
                # first drag sample is the recognizer's stable re-anchor.
                thumb = self._scrollbar_thumb_hit_rect()
                if self._point_in_rect(event['x'], event['y'], thumb):
                    self._scrollbar_dragging = True
                    self._scrollbar_drag_offset = event['y'] - thumb[1]
            if self._scrollbar_dragging:
                self._drag_scrollbar(event['y'])
            return

        if kind == 'release' and event.get('tap'):
            was_dragging = self._scrollbar_dragging
            self._scrollbar_dragging = False
            if was_dragging:
                return
            for btn in self._buttons:
                if btn.hit(event['x'], event['y']):
                    if btn.on_tap:
                        btn.on_tap(btn)
                    return
        elif kind == 'release':
            self._scrollbar_dragging = False

    def _min_scroll(self):
        content_h = len(self.networks) * _ROW_H
        visible_h = _LIST_BOTTOM - _LIST_TOP
        return min(0, visible_h - content_h)

    def _clamp_scroll(self):
        self.scroll = max(self._min_scroll(), min(0, self.scroll))

    def _scroll_rows(self, rows):
        self.scroll += rows * _ROW_H
        self._clamp_scroll()

    def _scrollbar_metrics(self):
        track_top = _LIST_TOP + _SCROLL_BUTTON_H
        track_h = (
            _LIST_BOTTOM
            - _LIST_TOP
            - 2 * _SCROLL_BUTTON_H
        )
        visible_h = _LIST_BOTTOM - _LIST_TOP
        content_h = len(self.networks) * _ROW_H
        if content_h <= visible_h or content_h <= 0:
            return track_top, track_h, track_top, track_h

        thumb_h = max(
            _SCROLL_THUMB_MIN_H,
            int(track_h * visible_h / content_h),
        )
        thumb_h = min(track_h, thumb_h)
        travel = track_h - thumb_h
        scroll_range = -self._min_scroll()
        ratio = (-self.scroll / scroll_range) if scroll_range else 0
        thumb_top = track_top + round(travel * ratio)
        return track_top, track_h, thumb_top, thumb_h

    def _scrollbar_thumb_rect(self):
        _, _, thumb_top, thumb_h = self._scrollbar_metrics()
        return (_LIST_RIGHT + 5, thumb_top, _SCROLLBAR_W - 10, thumb_h)

    def _scrollbar_thumb_hit_rect(self):
        _, _, thumb_top, thumb_h = self._scrollbar_metrics()
        return (_LIST_RIGHT, thumb_top, _SCROLLBAR_W, thumb_h)

    @staticmethod
    def _point_in_rect(x, y, rect):
        left, top, width, height = rect
        return left <= x < left + width and top <= y < top + height

    def _drag_scrollbar(self, pointer_y):
        track_top, track_h, _, thumb_h = self._scrollbar_metrics()
        travel = track_h - thumb_h
        scroll_range = -self._min_scroll()
        if travel <= 0 or scroll_range <= 0:
            self.scroll = 0
            return
        thumb_top = pointer_y - self._scrollbar_drag_offset
        thumb_top = max(track_top, min(track_top + travel, thumb_top))
        ratio = (thumb_top - track_top) / travel
        self.scroll = -round(scroll_range * ratio)
        self._clamp_scroll()

    @staticmethod
    def _status_lines(text, font, max_width, max_lines=2):
        words = str(text).split()
        if not words:
            return []
        lines = []
        while words and len(lines) < max_lines:
            if len(lines) == max_lines - 1:
                lines.append(display.truncate(' '.join(words), font, max_width))
                break
            line = words.pop(0)
            while words and font.size(line + ' ' + words[0])[0] <= max_width:
                line += ' ' + words.pop(0)
            lines.append(display.truncate(line, font, max_width))
        return lines

    def _draw_status(self, text, color, top, max_width):
        lines = self._status_lines(text, display.font_xs, max_width)
        line_h = display.font_xs.get_height() + 2
        for index, line in enumerate(lines):
            display.blit_center(
                line,
                display.font_xs,
                color,
                W // 2,
                top + index * line_h,
            )

    def _status_top_before(self, text, bottom, max_width):
        """Place all wrapped status lines above a lower UI boundary."""
        line_count = len(self._status_lines(
            text,
            display.font_xs,
            max_width,
        ))
        line_h = display.font_xs.get_height() + 2
        return bottom - line_count * line_h - _STATUS_ACTION_GAP

    # -- rendering ----------------------------------------------------------
    def render(self):
        if not self.is_active():
            return
        reveal = int(H + self.y_off)
        if reveal <= 0:
            return
        real = display.screen
        display.screen = self._surf
        try:
            if self.keyboard is not None:
                self.keyboard.render()
            else:
                self._render_view()
        finally:
            display.screen = real
        real.blit(self._surf, (0, 0), area=pygame.Rect(0, 0, W, reveal))
        if reveal < H:
            pygame.draw.line(real, C['dim'], (0, reveal - 1), (W, reveal - 1), 1)

    def _header(self, title, back_to=None):
        """Draw a header bar; return the list of header buttons."""
        self._surf.fill(C['panel'])
        display.blit_center(title, display.font_sm, C['on'], W // 2, 8)
        btns = []
        if back_to is not None:
            b = Button((PAD, 5, 82, 26), 'BACK',
                       on_tap=lambda _b: self._goto(back_to), font=display.font_xs,
                       enabled=not (back_to == 'wifi' and self._wifi_busy()))
            b.draw()
            btns.append(b)
        display.divider(_HEADER_H, color=C['dim'])
        return btns

    def _render_view(self):
        if self.view == 'menu':
            self._render_menu()
        elif self.view == 'wifi':
            self._render_wifi()
        elif self.view == 'wifi_detail':
            self._render_wifi_detail()
        elif self.view == 'apikey':
            self._render_apikey()

    # -- menu ---------------------------------------------------------------
    def _render_menu(self):
        self._surf.fill(C['panel'])
        display.blit_center('SETTINGS', display.font_med, C['on'], W // 2, 14)
        # grab-handle affordance
        pygame.draw.rect(self._surf, C['dim'], (W // 2 - 20, 6, 40, 4))

        wifi_btn = Button((PAD, 62, W - 2 * PAD, 50), 'WI-FI',
                          on_tap=lambda _b: self._goto('wifi'), font=display.font_sm)
        api_btn = Button((PAD, 120, W - 2 * PAD, 50), 'BART API KEY',
                         on_tap=lambda _b: self._goto('apikey'), font=display.font_sm)
        wifi_btn.draw()
        api_btn.draw()

        cursor_on = config.get_show_touch_cursor()
        cursor_btn = Button(
            (PAD, 178, W - 2 * PAD, 40),
            'TOUCH CURSOR: ' + ('ON' if cursor_on else 'OFF'),
            on_tap=lambda _b: self._toggle_cursor(), font=display.font_xs,
            fg=(C['arrive'] if cursor_on else C['dim']))
        cursor_btn.draw()

        display.blit_center('Swipe up to close', display.font_xs, C['ghost'], W // 2, H - 24)
        self._buttons = [wifi_btn, api_btn, cursor_btn]

    def _toggle_cursor(self):
        config.set_show_touch_cursor(not config.get_show_touch_cursor())

    # -- Wi-Fi list ---------------------------------------------------------
    def _render_wifi(self):
        btns = self._header('WI-FI', back_to='menu')
        rescan = Button((W - PAD - 116, 5, 116, 26), 'RESCAN',
                        on_tap=lambda _b: self._start_scan(), font=display.font_xs,
                        enabled=not self._wifi_busy())
        rescan.draw()
        btns.append(rescan)

        if self.status:
            status_color = {
                'success': C['ok'],
                'error': C['err'],
            }.get(self.status_kind, C['dim'])
            self._draw_status(
                self.status,
                status_color,
                _LIST_STATUS_TOP,
                W - 2 * PAD,
            )

        clip = pygame.Rect(0, _LIST_TOP, W, _LIST_BOTTOM - _LIST_TOP)
        self._surf.set_clip(clip)
        y = _LIST_TOP + self.scroll
        for net in self.networks:
            if y + _ROW_H > _LIST_TOP and y < _LIST_BOTTOM:
                b = self._network_row(net, y)
                btns.append(b)
            y += _ROW_H
        self._surf.set_clip(None)
        btns += self._render_scrollbar()

        if not self.networks and self.scan_job is None:
            display.blit_center(
                'No networks found',
                display.font_xs,
                C['ghost'],
                _LIST_RIGHT // 2,
                120,
            )
        self._buttons = btns

    def _render_scrollbar(self):
        can_scroll = self._min_scroll() < 0 and not self._wifi_busy()
        up = Button(
            (_LIST_RIGHT, _LIST_TOP, _SCROLLBAR_W, _SCROLL_BUTTON_H),
            'UP',
            on_tap=lambda _b: self._scroll_rows(1),
            font=display.font_xs,
            enabled=can_scroll and self.scroll < 0,
        )
        down = Button(
            (
                _LIST_RIGHT,
                _LIST_BOTTOM - _SCROLL_BUTTON_H,
                _SCROLLBAR_W,
                _SCROLL_BUTTON_H,
            ),
            'DN',
            on_tap=lambda _b: self._scroll_rows(-1),
            font=display.font_xs,
            enabled=can_scroll and self.scroll > self._min_scroll(),
        )
        up.draw()
        down.draw()

        track_top, track_h, _, _ = self._scrollbar_metrics()
        track = pygame.Rect(
            _LIST_RIGHT + 5,
            track_top,
            _SCROLLBAR_W - 10,
            track_h,
        )
        pygame.draw.rect(self._surf, C['panel'], track)
        pygame.draw.rect(self._surf, C['dim'], track, 1)

        thumb = pygame.Rect(self._scrollbar_thumb_rect())
        pygame.draw.rect(
            self._surf,
            C['on'] if can_scroll else C['ghost'],
            thumb,
        )
        return [up, down]

    def _network_row(self, net, y):
        row = pygame.Rect(0, y, _LIST_RIGHT, _ROW_H)
        if net.active:
            pygame.draw.rect(self._surf, C['panel_hi'], row)
        display.divider(
            y + _ROW_H - 1,
            x0=PAD,
            x1=_LIST_RIGHT - PAD,
            color=C['ghost'],
        )

        name_color = C['arrive'] if net.active else C['on']
        # right-side cluster: signal bars, lock, saved/active tag
        right = _LIST_RIGHT - PAD
        bars_w = 4 * 8
        signal_x = right - bars_w
        draw_signal_bars(signal_x, y + _ROW_H // 2 + 8, net.signal, active=net.active)
        right = signal_x - 8
        if net.protected:
            draw_lock(right - 12, y + 10)
            right -= 18
        tag = 'ONLINE' if net.active else ('SAVED' if net.saved else '')
        if tag:
            tw = draw_tag(tag, right, y + 8,
                          bg=(C['ok'] if net.active else C['dim']))
            right -= tw + 6

        name = display.truncate(net.ssid, display.font_xs, right - PAD - 6)
        display.blit_left(name, display.font_xs, name_color, PAD, y + (_ROW_H - 14) // 2)

        # Clamp the tap target to the visible list band so a row scrolled under
        # the header/footer can't be tapped there.
        top = max(y, _LIST_TOP)
        bottom = min(y + _ROW_H, _LIST_BOTTOM)
        hit = pygame.Rect(0, top, _LIST_RIGHT, max(0, bottom - top))
        return Button(hit, '', on_tap=lambda _b, n=net: self._select(n),
                      bg=C['panel'], border=None, enabled=not self._wifi_busy())

    def _select(self, net):
        self.selected = net
        self.status = ''
        self.view = 'wifi_detail'
        self._detail_request_id += 1
        request_id = self._detail_request_id
        # Show the cheap rows immediately; fetch IP/gateway (nmcli) off-thread
        # so the detail render never spawns subprocesses per frame.
        self.detail_info = wifi.info_basic(net)
        if net.active:
            def work():
                info = wifi.info(net)
                if (
                        self._detail_request_id == request_id
                        and self.selected is net
                        and self.view == 'wifi_detail'):
                    self.detail_info = info
            threading.Thread(target=work, daemon=True).start()

    # -- Wi-Fi detail -------------------------------------------------------
    def _render_wifi_detail(self):
        net = self.selected
        if net is None:
            self._buttons = self._header('NETWORK', back_to='wifi')
            return
        btns = self._header('NETWORK', back_to='wifi')

        # Prominent, color-coded SSID title (yellow when connected).
        title_color = C['arrive'] if net.active else C['on']
        display.blit_center(display.truncate(net.ssid, display.font_sm, W - 2 * PAD),
                            display.font_sm, title_color, W // 2, _HEADER_H + 8)

        y = _HEADER_H + 40
        for label, value in self.detail_info:
            if label == 'SSID':
                continue  # shown as the title above
            color = self._detail_color(label, value, net)
            # left accent dot in the value's color, then the dim label
            pygame.draw.rect(self._surf, color, (PAD, y + 3, 6, 12))
            display.blit_left(label, display.font_xs, C['dim'], PAD + 14, y)

            val = display.truncate(str(value), display.font_xs, W - PAD - 170)
            vw = display.font_xs.size(val)[0]
            display.blit_right(val, display.font_xs, color, W - PAD, y)

            # Row-specific accents for extra color / structure.
            if label == 'SIGNAL':
                draw_signal_bars(W - PAD - vw - 8 - 4 * 8, y + 15, net.signal,
                                 color=color)
            elif label == 'SECURITY' and net.protected:
                draw_lock(W - PAD - vw - 22, y, color=color)
            y += 24

        bw = (W - 2 * PAD - 10) // 2
        by = _DETAIL_ACTION_TOP
        if self.status:
            status_color = {
                'success': C['ok'],
                'error': C['err'],
            }.get(self.status_kind, C['arrive'])
            self._draw_status(
                self.status,
                status_color,
                self._status_top_before(
                    self.status,
                    by,
                    W - 2 * PAD,
                ),
                W - 2 * PAD,
            )

        busy = self._wifi_busy()
        action_kind = self.action_job.kind if self.action_job is not None else ''
        if net.active:
            label = 'DISCONNECTING...' if action_kind == 'disconnect' else 'DISCONNECT'
            act = Button((PAD, by, bw, 30), label,
                         on_tap=lambda _b: self._do_disconnect(), font=display.font_xs,
                         enabled=not busy)
        else:
            if not net.supported:
                label = 'UNSUPPORTED'
            elif action_kind == 'connect':
                label = 'CONNECTING...'
            else:
                label = 'CONNECT'
            act = Button((PAD, by, bw, 30),
                         label,
                         on_tap=lambda _b: self._do_connect(), font=display.font_xs,
                         enabled=not busy and net.supported)
        back = Button((PAD + bw + 10, by, bw, 30), 'BACK',
                      on_tap=lambda _b: self._goto('wifi'), font=display.font_xs,
                      enabled=not busy)
        act.draw()
        back.draw()
        btns += [act, back]
        self._buttons = btns

    @staticmethod
    def _detail_color(label, value, net):
        """Color a detail value by meaning: green=good, yellow=warn, red=weak."""
        if label == 'SIGNAL':
            if net.signal >= 66:
                return C['ok']
            if net.signal >= 40:
                return C['arrive']
            return C['err']
        if label == 'SECURITY':
            return C['on'] if net.protected else C['arrive']  # open = caution
        if label == 'PROTECTED':
            return C['ok'] if value == 'Yes' else C['arrive']
        if label == 'SAVED':
            return C['arrive'] if value == 'Yes' else C['dim']
        if label == 'STATUS':
            return C['ok'] if str(value).startswith('Connected') else C['dim']
        if label == 'SUPPORT':
            return C['err']
        if label in ('IP', 'GATEWAY'):
            return C['white']
        return C['on']

    def _do_connect(self):
        if self._wifi_busy():
            return
        net = self.selected
        if net is None:
            return
        if not net.supported:
            self.status = 'Unsupported network security'
            self.status_kind = 'error'
            return
        if net.protected and not net.saved and net.profile_known:
            self._open_keyboard('Password: ' + net.ssid, '', password=True,
                                on_submit=self._connect_with_password)
        else:
            self.action_job = wifi.connect_async(net)
            self.status = (
                'Checking saved network...'
                if not net.profile_known
                else 'Connecting...'
            )
            self.status_kind = 'info'

    def _connect_with_password(self, password):
        self.keyboard = None
        if self.selected is None:
            return
        if not password:
            self.status = 'Password required'
            self.status_kind = 'error'
            return
        self.action_job = wifi.connect_async(self.selected, password=password)
        self.status = 'Authenticating...'
        self.status_kind = 'info'

    def _do_disconnect(self):
        if self._wifi_busy():
            return
        net = self.selected
        if net is None:
            return
        self.action_job = wifi.disconnect_async(net)
        self.status = 'Disconnecting...'
        self.status_kind = 'info'

    # -- API key ------------------------------------------------------------
    def _render_apikey(self):
        btns = self._header('BART API KEY', back_to='menu')
        y = _HEADER_H + 16
        display.blit_left('CURRENT KEY', display.font_xs, C['dim'], PAD, y)
        y += 24
        key = config.get_api_key() or '(none)'
        box = pygame.Rect(PAD, y, W - 2 * PAD, 30)
        pygame.draw.rect(self._surf, C['bg'], box)
        pygame.draw.rect(self._surf, C['dim'], box, 1)
        display.blit_left(display.truncate(key, display.font_sm, box.width - 12),
                          display.font_sm, C['white'], box.x + 6, box.y + 7)
        y += 46

        if self.api_saved:
            display.blit_left('Saved. Applies next poll.',
                              display.font_xs, C['ok'], PAD, y)
            display.blit_left('Restart to apply now.',
                              display.font_xs, C['dim'], PAD, y + 20)
        y += 24

        modify = Button((PAD, H - 78, W - 2 * PAD, 30), 'MODIFY',
                        on_tap=lambda _b: self._modify_api(), font=display.font_xs)
        modify.draw()
        btns.append(modify)

        if self.api_saved:
            restart = Button((PAD, H - 40, W - 2 * PAD, 30), 'RESTART APP',
                             on_tap=lambda _b: self._request_restart(),
                             font=display.font_xs, fg=C['arrive'])
            restart.draw()
            btns.append(restart)
        self._buttons = btns

    def _modify_api(self):
        self._open_keyboard('BART API Key', config.get_api_key() or '',
                            password=False, on_submit=self._save_api)

    def _save_api(self, text):
        config.set_api_key(text.strip())
        self.keyboard = None
        self.api_saved = True

    def _request_restart(self):
        self.request_exit = True

    # -- keyboard helper ----------------------------------------------------
    def _open_keyboard(self, title, initial, password, on_submit):
        self.keyboard = Keyboard(
            title, initial=initial, password=password,
            on_submit=on_submit,
            on_cancel=lambda: setattr(self, 'keyboard', None),
        )
