import hashlib
import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPLASH_DIR = ROOT / 'deploy' / 'splash'
SOURCE_IMAGE = SPLASH_DIR / 'bart-splash.png'
SPLASH_SCRIPT = SPLASH_DIR / 'show_splash.py'
SOURCE_IMAGE_SHA256 = (
    '9293efb5aeea976487e528ed144207f7c908c97e00764cf42df99ff71b222cfb'
)

spec = importlib.util.spec_from_file_location('show_splash', SPLASH_SCRIPT)
show_splash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(show_splash)


class SplashAssetTests(unittest.TestCase):
    def test_repository_asset_is_the_exact_supplied_png(self):
        contents = SOURCE_IMAGE.read_bytes()

        self.assertEqual(
            hashlib.sha256(contents).hexdigest(),
            SOURCE_IMAGE_SHA256,
        )
        self.assertEqual(contents[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(contents[12:16], b'IHDR')
        self.assertEqual(struct.unpack('>II', contents[16:24]), (1280, 780))

    def test_no_prerendered_derivative_replaces_the_source(self):
        self.assertFalse((SPLASH_DIR / 'bart-splash.rgb565').exists())


class SplashRendererTests(unittest.TestCase):
    def test_supplied_image_fits_display_without_changing_aspect_ratio(self):
        self.assertEqual(
            show_splash._scale_to_fit(1280, 780),
            (480, 293),
        )

    def test_portrait_image_is_limited_by_display_height(self):
        self.assertEqual(
            show_splash._scale_to_fit(100, 200),
            (160, 320),
        )

    def test_invalid_dimensions_are_rejected(self):
        with self.assertRaises(ValueError):
            show_splash._scale_to_fit(0, 780)

    def test_write_all_writes_the_entire_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'framebuffer'
            target.write_bytes(b'\x00' * 6)

            show_splash._write_all(target, b'BART!!')

            self.assertEqual(target.read_bytes(), b'BART!!')


class SplashServiceTests(unittest.TestCase):
    def test_splash_runs_once_before_the_display_service(self):
        unit = (
            ROOT / 'bart-platform-display-splash.service'
        ).read_text(encoding='utf-8')

        self.assertIn('Type=oneshot', unit)
        self.assertIn('User=kevinchan', unit)
        self.assertIn('RemainAfterExit=yes', unit)
        self.assertIn('Before=bart-platform-display.service', unit)
        self.assertIn('WantedBy=multi-user.target', unit)
        self.assertIn(
            'deploy/splash/show_splash.py --image '
            '/home/kevinchan/bart-platform-display/deploy/splash/'
            'bart-splash.png',
            unit,
        )

    def test_display_service_waits_for_boot_splash_attempt(self):
        unit = (
            ROOT / 'bart-platform-display.service'
        ).read_text(encoding='utf-8')

        self.assertIn(
            'After=network-online.target '
            'bart-platform-display-splash.service',
            unit,
        )
        self.assertIn('Wants=network-online.target', unit)
        self.assertNotIn(
            'Wants=network-online.target '
            'bart-platform-display-splash.service',
            unit,
        )
        self.assertNotIn(
            'Requires=bart-platform-display-splash.service',
            unit,
        )


if __name__ == '__main__':
    unittest.main()
