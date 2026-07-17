"""Reusable UI widgets: tappable buttons plus small icon/draw helpers.

Built on the display drawing primitives so everything shares the board's
orange-red LED aesthetic and the Press Start 2P font.
"""

import pygame

from .. import display
from ..display import C


class Button:
    """A tappable rectangle with a centered label.

    `on_tap(button)` is called when a tap-release lands inside the button.
    Track presses with set_pressed() so the button can render a highlight.
    """

    def __init__(self, rect, label, on_tap=None, font=None,
                 fg=None, bg=None, border=None, enabled=True):
        self.rect = pygame.Rect(rect)
        self.label = label
        self.on_tap = on_tap
        self.font = font
        self.fg = fg or C['on']
        self.bg = bg or C['panel_hi']
        self.border = border if border is not None else C['dim']
        self.enabled = enabled
        self.pressed = False

    def hit(self, x, y):
        return self.enabled and self.rect.collidepoint(x, y)

    def draw(self):
        font = self.font or display.font_xs
        fill = self.bg
        if not self.enabled:
            fill = C['panel']
        if self.pressed and self.enabled:
            fill = C['dim']
        pygame.draw.rect(display.screen, fill, self.rect)
        if self.border is not None:
            pygame.draw.rect(display.screen, self.border, self.rect, 1)
        fg = self.fg if self.enabled else C['ghost']
        label = display.truncate(self.label, font, self.rect.width - 10)
        surf = font.render(label, False, fg)
        display.screen.blit(surf, (
            self.rect.centerx - surf.get_width() // 2,
            self.rect.centery - surf.get_height() // 2,
        ))


def draw_signal_bars(x, y, signal_pct, color=None, active=False):
    """Draw 4 signal bars filled proportionally to signal_pct (0-100)."""
    bars = 4
    filled = 0 if signal_pct is None else max(0, min(bars, round(signal_pct / 25.0)))
    bar_w, gap, base_h = 5, 3, 4
    on_col = color or (C['arrive'] if active else C['on'])
    for i in range(bars):
        h = base_h + i * 4
        bx = x + i * (bar_w + gap)
        by = y - h
        col = on_col if i < filled else C['ghost']
        pygame.draw.rect(display.screen, col, (bx, by, bar_w, h))
    return bars * (bar_w + gap)


def draw_lock(x, y, color=None):
    """Draw a small padlock glyph (indicates a password-protected network)."""
    col = color or C['dim']
    body = pygame.Rect(x, y + 5, 11, 8)
    pygame.draw.rect(display.screen, col, body)
    # shackle
    pygame.draw.arc(display.screen, col, (x + 2, y, 7, 10), 0, 3.14159, 2)
    return 11


def draw_tag(text, right_x, y, fg=None, bg=None):
    """Draw a small pill-style tag (e.g. 'SAVED') ending at right_x."""
    font = display.font_xs
    fg = fg or C['bg']
    bg = bg or C['dim']
    tw = font.size(text)[0]
    rect = pygame.Rect(right_x - tw - 8, y, tw + 8, font.get_height() + 4)
    pygame.draw.rect(display.screen, bg, rect)
    surf = font.render(text, False, fg)
    display.screen.blit(surf, (rect.x + 4, rect.y + 2))
    return rect.width
