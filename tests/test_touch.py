import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import bartdisplay


fake_config = types.ModuleType('bartdisplay.config')
fake_config.get_touch_calibration = lambda: {}
fake_config.get_touch_device = lambda: None
fake_config.get_touch_tuning = lambda: {}

fake_display = types.ModuleType('bartdisplay.display')
fake_display.W = 480
fake_display.H = 320

touch_path = (
    Path(__file__).resolve().parents[1]
    / 'bartdisplay'
    / 'touch.py'
)
touch_spec = importlib.util.spec_from_file_location(
    'bartdisplay._touch_under_test',
    touch_path,
)
touch_module = importlib.util.module_from_spec(touch_spec)
with mock.patch.dict(
        sys.modules,
        {
            'bartdisplay.config': fake_config,
            'bartdisplay.display': fake_display,
        },
), mock.patch.object(
        bartdisplay,
        'config',
        fake_config,
        create=True,
), mock.patch.object(
        bartdisplay,
        'display',
        fake_display,
        create=True,
):
    touch_spec.loader.exec_module(touch_module)


class TouchMappingTests(unittest.TestCase):
    def test_swapped_axes_use_their_original_calibration_ranges(self):
        reader = touch_module.TouchReader()
        reader._cal = {
            'x_min': 0,
            'x_max': 1000,
            'y_min': 0,
            'y_max': 2000,
            'swap_xy': True,
        }

        mapper = reader._make_mapper(object())

        self.assertEqual(mapper(250, 1500), (359, 79))


class PanelGestureTests(unittest.TestCase):
    def tearDown(self):
        fake_config.get_touch_tuning = lambda: {}

    def test_downward_swipe_must_start_in_default_opening_zone(self):
        event = {
            'kind': 'release',
            'swipe': 'down',
            'start_y': 119,
        }

        self.assertTrue(touch_module.should_open_panel(event))

        event['start_y'] = 120
        self.assertFalse(touch_module.should_open_panel(event))

    def test_opening_zone_can_be_overridden_per_device(self):
        fake_config.get_touch_tuning = lambda: {
            'panel_open_start_max_y': 180,
        }

        self.assertTrue(touch_module.should_open_panel({
            'kind': 'release',
            'swipe': 'down',
            'start_y': 179,
        }))

    def test_only_downward_release_swipes_open_panel(self):
        for event in (
                {'kind': 'press', 'swipe': 'down', 'start_y': 0},
                {'kind': 'release', 'swipe': 'up', 'start_y': 0},
                {'kind': 'release', 'swipe': None, 'start_y': 0}):
            with self.subTest(event=event):
                self.assertFalse(touch_module.should_open_panel(event))


if __name__ == '__main__':
    unittest.main()
