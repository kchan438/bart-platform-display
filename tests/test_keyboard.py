import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import bartdisplay


class FakeRect:
    def __init__(self, *args):
        if len(args) == 1:
            args = tuple(args[0])
        self.x, self.y, self.width, self.height = map(int, args)

    @property
    def right(self):
        return self.x + self.width

    @property
    def centerx(self):
        return self.x + self.width // 2

    @property
    def centery(self):
        return self.y + self.height // 2

    def collidepoint(self, x, y):
        return (
            self.x <= x < self.right
            and self.y <= y < self.y + self.height
        )


class FakeSurface:
    def __init__(self, width=1, height=1):
        self._width = width
        self._height = height

    def get_width(self):
        return self._width

    def get_height(self):
        return self._height


class FakeFont:
    def render(self, text, antialias, color):
        return FakeSurface(len(text) * 8, 12)

    def size(self, text):
        return len(text) * 8, 12


class FakeScreen:
    def fill(self, color):
        pass

    def blit(self, surface, position):
        pass


fake_pygame = types.ModuleType('pygame')
fake_pygame.Rect = FakeRect
fake_pygame.draw = types.SimpleNamespace(rect=lambda *args, **kwargs: None)

fake_display = types.ModuleType('bartdisplay.display')
fake_display.C = {
    'on': (0, 0, 0),
    'dim': (0, 0, 0),
    'ghost': (0, 0, 0),
    'arrive': (0, 0, 0),
    'bg': (0, 0, 0),
    'panel': (0, 0, 0),
    'panel_hi': (0, 0, 0),
    'white': (0, 0, 0),
}
fake_display.W = 480
fake_display.H = 320
fake_display.PAD = 14
fake_display.font_xs = FakeFont()
fake_display.font_sm = FakeFont()
fake_display.screen = FakeScreen()
fake_display.left_calls = []
fake_display.center_calls = []
fake_display.truncate_calls = []


def fake_blit_left(text, font, color, x, y):
    fake_display.left_calls.append((text, x, y))


def fake_blit_center(text, font, color, center_x, y):
    fake_display.center_calls.append((text, center_x, y))


def fake_truncate(text, font, max_width):
    fake_display.truncate_calls.append((text, max_width))
    return text


fake_display.blit_left = fake_blit_left
fake_display.blit_center = fake_blit_center
fake_display.truncate = fake_truncate

keyboard_path = (
    Path(__file__).resolve().parents[1]
    / 'bartdisplay'
    / 'ui'
    / 'keyboard.py'
)
keyboard_spec = importlib.util.spec_from_file_location(
    'bartdisplay.ui._keyboard_under_test',
    keyboard_path,
)
keyboard_module = importlib.util.module_from_spec(keyboard_spec)
with mock.patch.dict(
        sys.modules,
        {
            'pygame': fake_pygame,
            'bartdisplay.display': fake_display,
        }), mock.patch.object(bartdisplay, 'display', fake_display, create=True):
    keyboard_spec.loader.exec_module(keyboard_module)

Keyboard = keyboard_module.Keyboard


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        fake_display.left_calls.clear()
        fake_display.center_calls.clear()
        fake_display.truncate_calls.clear()

    def test_password_toggle_exists_before_render_and_starts_masked(self):
        keyboard = Keyboard('Password', initial='secret', password=True)

        self.assertFalse(keyboard.reveal)
        self.assertIsNotNone(keyboard._password_toggle_rect)
        self.assertGreaterEqual(keyboard._password_toggle_rect.width, 100)
        self.assertGreaterEqual(keyboard._password_toggle_rect.height, 38)

    def test_password_toggle_reveals_and_rehides_without_side_effects(self):
        submitted = []
        cancelled = []
        keyboard = Keyboard(
            'Password',
            initial='secret',
            password=True,
            on_submit=submitted.append,
            on_cancel=lambda: cancelled.append(True),
        )
        toggle = keyboard._password_toggle_rect
        tap = {
            'kind': 'release',
            'tap': True,
            'x': toggle.centerx,
            'y': toggle.centery,
        }

        keyboard.handle(tap)
        self.assertTrue(keyboard.reveal)
        self.assertEqual(keyboard.text, 'secret')
        self.assertEqual(submitted, [])
        self.assertEqual(cancelled, [])

        keyboard.handle(tap)
        self.assertFalse(keyboard.reveal)
        self.assertEqual(keyboard.text, 'secret')
        self.assertEqual(submitted, [])
        self.assertEqual(cancelled, [])

    def test_non_tap_and_outside_events_do_not_reveal_password(self):
        keyboard = Keyboard('Password', initial='secret', password=True)
        toggle = keyboard._password_toggle_rect

        keyboard.handle({
            'kind': 'drag',
            'tap': True,
            'x': toggle.centerx,
            'y': toggle.centery,
        })
        keyboard.handle({
            'kind': 'release',
            'tap': True,
            'x': 0,
            'y': 0,
        })

        self.assertFalse(keyboard.reveal)

    def test_non_password_keyboard_has_no_password_toggle(self):
        keyboard = Keyboard('API key', initial='visible', password=False)

        self.assertIsNone(keyboard._password_toggle_rect)

    def test_render_masks_and_reserves_toggle_width_then_reveals(self):
        keyboard = Keyboard('Password', initial='secret', password=True)

        keyboard.render()

        self.assertEqual(fake_display.truncate_calls[-1][0], '******')
        self.assertEqual(
            fake_display.truncate_calls[-1][1],
            480 - 2 * 14 - keyboard._password_toggle_rect.width - 12,
        )
        self.assertEqual(fake_display.center_calls[-1][0], 'SHOW')

        fake_display.center_calls.clear()
        keyboard.reveal = True
        keyboard.render()

        self.assertEqual(fake_display.truncate_calls[-1][0], 'secret')
        self.assertEqual(fake_display.center_calls[-1][0], 'HIDE')

    def test_close_label_keeps_keyboard_cancel_behavior(self):
        cancelled = []
        keyboard = Keyboard(
            'Password',
            initial='secret',
            password=True,
            on_cancel=lambda: cancelled.append(True),
        )

        self.assertEqual(keyboard._key_label('HIDE'), 'CLOSE')
        keyboard._activate('HIDE')

        self.assertEqual(cancelled, [True])
        self.assertEqual(keyboard.text, 'secret')


if __name__ == '__main__':
    unittest.main()
