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
_LIST_TOP = 64
_LIST_BOTTOM = H - 8


class SettingsPanel:
    def __init__(self):
        self.state = 'CLOSED'          # CLOSED | OPENING | OPEN | CLOSING
        self.y_off = -H                # -H (hidden) .. 0 (fully open)
        self.view = 'menu'             # menu | wifi | wifi_detail | apikey
        self.keyboard = None
        self.request_exit = False      # main loop watches this for RESTART

        self.networks = []
        self.scanning = False
        self.selected = None           # Network in detail view
        self.detail_info = []          # cached info rows for the detail view
        self.connect_job = None
        self.status = ''
        self.scroll = 0

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

    def _goto(self, view):
        self.view = view
        self.status = ''
        self.scroll = 0
        if view == 'wifi':
            self._start_scan()
        if view == 'apikey':
            self.api_saved = False

    # -- Wi-Fi scanning (threaded so nmcli doesn't stall the render loop) ----
    def _start_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.status = 'Scanning...'

        def work():
            nets = wifi.scan(rescan=True)
            self.networks = nets
            self.scanning = False
            self.status = ''
        threading.Thread(target=work, daemon=True).start()

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

        if self.connect_job is not None:
            self.status = self.connect_job.status
            if self.connect_job.done:
                if self.connect_job.ok:
                    self._start_scan()          # refresh saved/active flags
                    self.selected = None
                    self.view = 'wifi'
                self.connect_job = None

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

        # Swipe up returns to the board (from the menu, or grabbing the top edge).
        if kind == 'release' and event.get('swipe') == 'up' and (
                self.view == 'menu' or event.get('start_y', 999) < 70):
            self.close()
            return

        # Drag scrolls the Wi-Fi list.
        if kind == 'drag' and self.view == 'wifi':
            self.scroll = max(self._min_scroll(), min(0, self.scroll + event['dy']))
            return

        if kind == 'release' and event.get('tap'):
            for btn in self._buttons:
                if btn.hit(event['x'], event['y']):
                    if btn.on_tap:
                        btn.on_tap(btn)
                    return

    def _min_scroll(self):
        content_h = len(self.networks) * _ROW_H
        visible_h = _LIST_BOTTOM - _LIST_TOP
        return min(0, visible_h - content_h)

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
                       on_tap=lambda _b: self._goto(back_to), font=display.font_xs)
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
                        on_tap=lambda _b: self._start_scan(), font=display.font_xs)
        rescan.draw()
        btns.append(rescan)

        if self.status:
            display.blit_center(self.status, display.font_xs, C['dim'], W // 2, 44)

        clip = pygame.Rect(0, _LIST_TOP, W, _LIST_BOTTOM - _LIST_TOP)
        self._surf.set_clip(clip)
        y = _LIST_TOP + self.scroll
        for net in self.networks:
            if y + _ROW_H > _LIST_TOP and y < _LIST_BOTTOM:
                b = self._network_row(net, y)
                btns.append(b)
            y += _ROW_H
        self._surf.set_clip(None)

        if not self.networks and not self.scanning:
            display.blit_center('No networks found', display.font_xs, C['ghost'], W // 2, 120)
        self._buttons = btns

    def _network_row(self, net, y):
        row = pygame.Rect(0, y, W, _ROW_H)
        if net.active:
            pygame.draw.rect(self._surf, C['panel_hi'], row)
        display.divider(y + _ROW_H - 1, x0=PAD, x1=W - PAD, color=C['ghost'])

        name_color = C['arrive'] if net.active else C['on']
        # right-side cluster: signal bars, lock, saved/active tag
        right = W - PAD
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
        hit = pygame.Rect(0, top, W, max(0, bottom - top))
        return Button(hit, '', on_tap=lambda _b, n=net: self._select(n),
                      bg=C['panel'], border=None)

    def _select(self, net):
        self.selected = net
        self.status = ''
        self.view = 'wifi_detail'
        # Show the cheap rows immediately; fetch IP/gateway (nmcli) off-thread
        # so the detail render never spawns subprocesses per frame.
        self.detail_info = wifi.info_basic(net)
        if net.active:
            def work():
                if self.selected is net:
                    self.detail_info = wifi.info(net)
            threading.Thread(target=work, daemon=True).start()

    # -- Wi-Fi detail -------------------------------------------------------
    def _render_wifi_detail(self):
        net = self.selected
        if net is None:
            self._buttons = self._header('NETWORK', back_to='wifi')
            return
        btns = self._header('NETWORK', back_to='wifi')
        y = _HEADER_H + 8
        for label, value in self.detail_info:
            display.blit_left(label, display.font_xs, C['dim'], PAD, y)
            val = display.truncate(str(value), display.font_xs, W - PAD - 130)
            display.blit_right(val, display.font_xs, C['white'], W - PAD, y)
            y += 22

        if self.status:
            display.blit_center(self.status, display.font_xs,
                                C['ok'] if self.status == 'Connected' else C['arrive'],
                                W // 2, H - 62)

        bw = (W - 2 * PAD - 10) // 2
        by = H - 40
        connecting = self.connect_job is not None and not self.connect_job.done
        if net.active:
            act = Button((PAD, by, bw, 30), 'DISCONNECT',
                         on_tap=lambda _b: self._do_disconnect(), font=display.font_xs)
        else:
            act = Button((PAD, by, bw, 30),
                         'CONNECTING...' if connecting else 'CONNECT',
                         on_tap=lambda _b: self._do_connect(), font=display.font_xs,
                         enabled=not connecting)
        back = Button((PAD + bw + 10, by, bw, 30), 'BACK',
                      on_tap=lambda _b: self._goto('wifi'), font=display.font_xs)
        act.draw()
        back.draw()
        btns += [act, back]
        self._buttons = btns

    def _do_connect(self):
        net = self.selected
        if net.protected and not net.saved:
            self._open_keyboard('Password: ' + net.ssid, '', password=True,
                                on_submit=self._connect_with_password)
        else:
            self.connect_job = wifi.connect_async(net)

    def _connect_with_password(self, password):
        self.keyboard = None
        self.connect_job = wifi.connect_async(self.selected, password=password)

    def _do_disconnect(self):
        # Run the blocking nmcli call off the render thread, then return to the
        # list (a rescan reflects the new state).
        net = self.selected

        def work():
            wifi.disconnect(net)
            self._start_scan()
        threading.Thread(target=work, daemon=True).start()
        self.selected = None
        self.status = ''
        self.scroll = 0
        self.view = 'wifi'

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
