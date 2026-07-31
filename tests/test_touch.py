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


if __name__ == '__main__':
    unittest.main()
