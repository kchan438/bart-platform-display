"""On-screen keyboard for 480x320 touch input.

Used for Wi-Fi passwords and the BART API key. Supports uppercase (shift), a
symbols page, backspace, space, a Done action, and a dedicated Hide button that
dismisses the keyboard without submitting. Password fields get a show/hide eye.
"""

import pygame

from .. import display
from ..display import C, W, H, PAD

# Row layouts: each token is a character or a special action key.
_LETTER_ROWS = [
    list('1234567890'),
    list('qwertyuiop'),
    list('asdfghjkl'),
    ['SHIFT'] + list('zxcvbnm') + ['BKSP'],
    ['SYM', 'SPACE', 'HIDE', 'DONE'],
]
_SYMBOL_ROWS = [
    list('1234567890'),
    list('-/:;()$&@['),
    list('.,?!\'"*+=]'),
    list('_#%~<>|\\{}^`') + ['BKSP'],
    ['ABC', 'SPACE', 'HIDE', 'DONE'],
]

_WEIGHTS = {'SHIFT': 1.6, 'BKSP': 1.6, 'SYM': 1.6, 'ABC': 1.6,
            'HIDE': 2.0, 'DONE': 2.0, 'SPACE': 4.4}
_LABELS = {'SHIFT': 'aA', 'BKSP': 'DEL', 'SYM': '?#', 'ABC': 'ABC',
           'HIDE': 'HIDE', 'DONE': 'DONE', 'SPACE': 'SPACE'}

_KEYS_TOP = 74
_ROW_H = 46
_ROW_GAP = 2
_KEY_GAP = 2


class Keyboard:
    def __init__(self, title, initial='', password=False,
                 on_submit=None, on_cancel=None):
        self.title = title
        self.text = initial
        self.password = password
        self.reveal = False
        self.on_submit = on_submit
        self.on_cancel = on_cancel
        self._shift = False
        self._symbols = False
        self._keys = []       # list of (token, pygame.Rect)
        self._eye_rect = None
        self._layout()

    # -- layout -------------------------------------------------------------
    def _layout(self):
        self._keys = []
        rows = _SYMBOL_ROWS if self._symbols else _LETTER_ROWS
        y = _KEYS_TOP
        for row in rows:
            weights = [_WEIGHTS.get(tok, 1.0) for tok in row]
            avail = W - 2 * PAD
            unit = (avail - _KEY_GAP * (len(row) - 1)) / sum(weights)
            x = PAD
            for tok, wgt in zip(row, weights):
                w = int(unit * wgt)
                self._keys.append((tok, pygame.Rect(x, y, w, _ROW_H)))
                x += w + _KEY_GAP
            y += _ROW_H + _ROW_GAP

    def _key_label(self, tok):
        if tok in _LABELS:
            return _LABELS[tok]
        return tok.upper() if self._shift else tok

    # -- input --------------------------------------------------------------
    def handle(self, event):
        if event.get('kind') != 'release' or not event.get('tap'):
            return
        x, y = event['x'], event['y']
        if self._eye_rect and self._eye_rect.collidepoint(x, y):
            self.reveal = not self.reveal
            return
        for tok, rect in self._keys:
            if rect.collidepoint(x, y):
                self._activate(tok)
                return

    def _activate(self, tok):
        if tok == 'SHIFT':
            self._shift = not self._shift
        elif tok == 'SYM':
            self._symbols = True
            self._shift = False
            self._layout()
        elif tok == 'ABC':
            self._symbols = False
            self._layout()
        elif tok == 'BKSP':
            self.text = self.text[:-1]
        elif tok == 'SPACE':
            self.text += ' '
        elif tok == 'HIDE':
            if self.on_cancel:
                self.on_cancel()
        elif tok == 'DONE':
            if self.on_submit:
                self.on_submit(self.text)
        else:
            self.text += tok.upper() if self._shift else tok

    # -- render -------------------------------------------------------------
    def render(self):
        screen = display.screen
        screen.fill(C['panel'])
        display.blit_left(self.title, display.font_xs, C['on'], PAD, 8)

        # Text field
        field = pygame.Rect(PAD, 34, W - 2 * PAD, 32)
        pygame.draw.rect(screen, C['bg'], field)
        pygame.draw.rect(screen, C['dim'], field, 1)
        shown = self.text if (self.reveal or not self.password) else '*' * len(self.text)
        shown = display.truncate(shown, display.font_sm,
                                 field.width - (34 if self.password else 12))
        display.blit_left(shown or '', display.font_sm, C['white'], field.x + 6, field.y + 7)
        if self.password:
            self._eye_rect = pygame.Rect(field.right - 30, field.y, 30, field.height)
            eye_col = C['on'] if self.reveal else C['dim']
            display.blit_center('o' if self.reveal else '-', display.font_sm, eye_col,
                                self._eye_rect.centerx, self._eye_rect.y + 7)
        else:
            self._eye_rect = None

        # Keys
        for tok, rect in self._keys:
            special = tok in _LABELS
            pygame.draw.rect(screen, C['panel_hi'] if special else C['dim'], rect)
            pygame.draw.rect(screen, C['ghost'], rect, 1)
            label = self._key_label(tok)
            fg = C['arrive'] if (tok == 'DONE') else (
                C['on'] if special else C['white'])
            if tok == 'SHIFT' and self._shift:
                fg = C['arrive']
            surf = display.font_xs.render(label, False, fg)
            screen.blit(surf, (rect.centerx - surf.get_width() // 2,
                               rect.centery - surf.get_height() // 2))
