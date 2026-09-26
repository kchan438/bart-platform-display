"""Touch station/platform selection with background metadata requests."""

import os
import queue
import threading

import pygame

from .. import board, config, departures, display, stations
from ..display import C
from .widgets import Button

LIST_TOP = 62
ROW_H = 42
VISIBLE_ROWS = 4
POPUP = pygame.Rect(14, 34, 452, 250)
SCROLL_TRACK = pygame.Rect(422, 104, 36, 84)


class StationPicker:
    def __init__(self):
        self.active = False
        self.view = 'stations'
        self.entries = []
        self.platforms = []
        self.selected = None
        self.platform = None
        self.offset = 0
        self.busy = False
        self.error = ''
        self._results = queue.Queue()
        self._request_id = 0
        self._buttons = []
        self._drag_distance = 0
        self._dragging_list = False
        self._scrolled = False
        self._font = None
        self._dragging_scroll = False
        self._thumb_grab = 0
        self._return_to_stations = False

    @property
    def font(self):
        if self._font is None:
            self._font = pygame.font.Font(os.path.join(
                config.PROJECT_ROOT, 'fonts', 'PressStart2P-Regular.ttf'), 12)
        return self._font

    def _begin(self):
        self.active = True
        self.offset = 0
        self.error = ''
        self.busy = False
        self._request_id += 1
        self._buttons = []
        self._dragging_list = False
        self._dragging_scroll = False
        self._scrolled = False

    def open(self):
        self._begin()
        self.view = 'stations'
        self.selected = None
        self.platform = None
        self._return_to_stations = True
        if not self.entries:
            self._load()

    def open_platforms(self):
        self._begin()
        self.view = 'platforms'
        self.selected = (config.get_station(), config.get_station_name())
        self.platforms = []
        self.platform = None
        self._return_to_stations = False
        self._load()

    def close(self):
        self.active = False
        self._request_id += 1
        self.busy = False
        self._buttons = []

    def _load(self):
        self._request_id += 1
        token = self._request_id
        self.busy = True
        self.error = ''
        self._buttons = []
        view = self.view
        code = self.selected[0] if self.selected else None

        def worker():
            try:
                result = (stations.fetch_stations() if view == 'stations'
                          else stations.fetch_platforms(code))
                self._results.put((token, result, ''))
            except Exception:
                self._results.put((token, None, 'Could not load ' + view + '.'))

        threading.Thread(target=worker, daemon=True).start()

    def update(self):
        while True:
            try:
                token, result, error = self._results.get_nowait()
            except queue.Empty:
                return
            if not self.active or token != self._request_id:
                continue
            self.busy = False
            self.error = error
            if error:
                continue
            if self.view == 'stations':
                self.entries = result
            else:
                self.platforms = result
                current_station, current_platform = config.get_selection()
                self.platform = (current_platform if self.selected[0] == current_station
                                 and current_platform in result else None)

    def _choose_station(self, entry):
        self.selected = entry
        self.view = 'platforms'
        self.platforms = []
        self.platform = None
        self.offset = 0
        self._return_to_stations = True
        self._load()

    def _back(self):
        self._request_id += 1
        self.busy = False
        self.error = ''
        self.view = 'stations'
        self.offset = 0
        self._buttons = []

    def _choose_platform(self, platform):
        self.platform = platform
        self.error = ''

    def _apply(self):
        if self.busy or not self.selected or self.platform not in self.platforms:
            return
        try:
            config.set_selection(*self.selected, self.platform)
        except (OSError, ValueError):
            self.error = 'Could not save. Try Apply again.'
            return
        departures.refresh()
        self.close()

    def _items(self):
        return self.entries if self.view == 'stations' else self.platforms

    def _scroll(self, rows):
        self.offset = max(0, min(max(0, len(self._items()) - VISIBLE_ROWS),
                                 self.offset + rows))
        self._buttons = []

    def _thumb(self):
        count = len(self._items())
        height = max(24, round(SCROLL_TRACK.height * min(1, VISIBLE_ROWS / max(1, count))))
        travel = SCROLL_TRACK.height - height
        fraction = self.offset / max(1, count - VISIBLE_ROWS)
        return pygame.Rect(SCROLL_TRACK.x, SCROLL_TRACK.y + round(travel * fraction),
                           SCROLL_TRACK.width, height)

    def _move_thumb(self, y):
        thumb = self._thumb()
        travel = SCROLL_TRACK.height - thumb.height
        fraction = max(0, min(1, (y - self._thumb_grab - SCROLL_TRACK.y) / max(1, travel)))
        self.offset = round(fraction * max(0, len(self._items()) - VISIBLE_ROWS))
        self._buttons = []

    def handle(self, event):
        kind = event.get('kind')
        x, y = event['x'], event['y']
        if kind == 'press':
            self._drag_distance = 0
            self._scrolled = False
            self._dragging_scroll = False
            self._dragging_list = (not self.busy and x < 416
                                   and LIST_TOP <= y < LIST_TOP + ROW_H * VISIBLE_ROWS)
            if (not self.busy and len(self._items()) > VISIBLE_ROWS
                    and SCROLL_TRACK.collidepoint(x, y)):
                thumb = self._thumb()
                self._dragging_list = False
                self._dragging_scroll = True
                self._scrolled = True
                self._thumb_grab = y - thumb.y if thumb.collidepoint(x, y) else thumb.height / 2
                self._move_thumb(y)
        elif kind == 'drag':
            if self._dragging_scroll:
                self._move_thumb(y)
            elif self._dragging_list:
                self._drag_distance += event.get('dy', 0)
                if abs(self._drag_distance) >= ROW_H:
                    rows = int(-self._drag_distance / ROW_H)
                    self._scroll(rows)
                    self._scrolled = True
                    self._drag_distance += rows * ROW_H
        elif kind == 'release':
            self._dragging_list = False
            self._dragging_scroll = False
            if event.get('tap') and not self._scrolled:
                # Switching header controls is possible without visiting settings.
                if board.station_tapped(event):
                    if self.view == 'stations':
                        self.close()
                    else:
                        self.open()
                elif board.platform_tapped(event):
                    if self.view == 'platforms':
                        self.close()
                    else:
                        self.open_platforms()
                elif not POPUP.collidepoint(x, y):
                    self.close()
                else:
                    for button in self._buttons:
                        if button.hit(x, y):
                            button.on_tap(button)
                            break

    def _text(self, text, x, y, color=None):
        display.blit_left(text, self.font, color or C['on'], x, y)

    def _wrapped(self, text, x, y, width, color=None):
        # Station names can exceed a single line on the small display.
        words = text.upper().split()
        lines = ['']
        for word in words:
            candidate = (lines[-1] + ' ' + word).strip()
            if self.font.size(candidate)[0] > width and lines[-1]:
                lines.append(word)
            else:
                lines[-1] = candidate
        for index, line in enumerate(lines[:2]):
            if index == 1 and len(lines) > 2:
                line += '...'
            self._text(display.truncate(line, self.font, width), x, y + index * 17, color)

    def _button(self, rect, label, action, enabled=True, selected=False):
        button = Button(rect, label, lambda _b: action(), font=self.font,
                        enabled=enabled, fg=C['arrive'] if selected else C['on'],
                        border=C['arrive'] if selected else C['dim'])
        button.draw()
        self._buttons.append(button)

    def render(self):
        if not self.active:
            return
        pygame.draw.rect(display.screen, C['panel'], POPUP)
        pygame.draw.rect(display.screen, C['dim'], POPUP, 1)
        self._buttons = []
        title = 'CHOOSE STATION' if self.view == 'stations' else self.selected[1].upper() + ' / PLATFORM'
        self._text(display.truncate(title, self.font, 430), 24, 42)
        items = self._items()
        scrollable = len(items) > VISIBLE_ROWS
        # Save failures leave choices available so Apply can be retried.
        if not self.busy and (not self.error or (self.view == 'platforms' and items)):
            for index, item in enumerate(items[self.offset:self.offset + VISIBLE_ROWS]):
                y = LIST_TOP + index * ROW_H
                if self.view == 'stations':
                    selected = item[0] == config.get_station()
                    action = lambda entry=item: self._choose_station(entry)
                    label = item[1]
                else:
                    selected = item == self.platform
                    action = lambda platform=item: self._choose_platform(platform)
                    label = 'PLATFORM ' + item
                width = 392 if scrollable else 436
                self._button((22, y, width, ROW_H - 2), '', action, selected=selected)
                self._wrapped(label, 30, y + 5, width - 16,
                              C['arrive'] if selected else C['on'])
            if scrollable:
                self._button((422, 62, 36, 40), '^', lambda: self._scroll(-1), self.offset > 0)
                pygame.draw.rect(display.screen, C['ghost'], SCROLL_TRACK)
                pygame.draw.rect(display.screen, C['on'], self._thumb())
                self._button((422, 190, 36, 40), 'v', lambda: self._scroll(1),
                             self.offset + VISIBLE_ROWS < len(items))
        self._button((22, 234, 136, 42), 'CANCEL', self.close)
        if self.view == 'platforms':
            if self._return_to_stations and not self.error:
                self._button((170, 234, 136, 42), 'BACK', self._back)
            self._button((322, 234, 136, 42), 'APPLY', self._apply,
                         not self.busy and self.platform in self.platforms)
        elif not self.busy and not self.error:
            self._button((170, 234, 136, 42), 'PREV 4',
                         lambda: self._scroll(-VISIBLE_ROWS), self.offset > 0)
            self._button((322, 234, 136, 42), 'NEXT 4',
                         lambda: self._scroll(VISIBLE_ROWS),
                         self.offset + VISIBLE_ROWS < len(items))
            count = (f'{self.offset + 1}-{min(self.offset + VISIBLE_ROWS, len(items))}'
                     f' / {len(items)}')
            display.blit_right(count, self.font, C['dim'], 454, 42)
        if self.busy:
            self._text('LOADING...', 24, 126, C['dim'])
        elif self.error:
            if self.view == 'stations' or not self.platforms:
                self._wrapped(self.error, 24, 126, 426, C['err'])
                self._button((170, 234, 136, 42), 'RETRY', self._load)
            else:
                # Keep Apply available and show the save error in the footer.
                self._text('SAVE FAILED', 170, 248, C['err'])
