import sys
import types
import unittest
from unittest import mock

from bartdisplay import wifi


# The Windows verification interpreter does not include pygame. SettingsPanel's
# operation-state methods do not need rendering, so provide the smallest modules
# needed to import and instantiate it.
fake_pygame = types.ModuleType('pygame')
fake_pygame.Surface = lambda size: object()

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
    'ok': (0, 0, 0),
    'err': (0, 0, 0),
}
fake_display.W = 480
fake_display.H = 320
fake_display.PAD = 14


class FakeKeyboard:
    def __init__(
            self,
            title,
            initial='',
            password=False,
            on_submit=None,
            on_cancel=None):
        self.title = title
        self.initial = initial
        self.password = password
        self.on_submit = on_submit
        self.on_cancel = on_cancel


fake_keyboard = types.ModuleType('bartdisplay.ui.keyboard')
fake_keyboard.Keyboard = FakeKeyboard

fake_widgets = types.ModuleType('bartdisplay.ui.widgets')
fake_widgets.Button = object
fake_widgets.draw_signal_bars = lambda *args, **kwargs: None
fake_widgets.draw_lock = lambda *args, **kwargs: None
fake_widgets.draw_tag = lambda *args, **kwargs: 0

with mock.patch.dict(
        sys.modules,
        {
            'pygame': fake_pygame,
            'bartdisplay.display': fake_display,
            'bartdisplay.ui.keyboard': fake_keyboard,
            'bartdisplay.ui.widgets': fake_widgets,
        }):
    from bartdisplay.ui.settings import SettingsPanel


class WifiUiStateTests(unittest.TestCase):
    def test_disconnect_failure_stays_on_detail_and_shows_error(self):
        panel = SettingsPanel()
        network = wifi.Network('Home', active=True)
        panel.selected = network
        panel.view = 'wifi_detail'
        job = wifi.WifiJob('disconnect', network.ssid)
        job.finish(False, 'Not authorized', code='not_authorized')
        panel.action_job = job

        panel._update_wifi_jobs()

        self.assertEqual(panel.view, 'wifi_detail')
        self.assertIs(panel.selected, network)
        self.assertEqual(panel.status, 'Not authorized')
        self.assertEqual(panel.status_kind, 'error')
        self.assertIsNone(panel.action_job)

    def test_disconnect_success_returns_to_list_and_starts_scan(self):
        panel = SettingsPanel()
        network = wifi.Network('Home', active=True)
        panel.selected = network
        panel.view = 'wifi_detail'
        action = wifi.WifiJob('disconnect', network.ssid)
        action.finish(True, 'Disconnected from Home', code='disconnected')
        panel.action_job = action
        scan = wifi.WifiJob('scan')

        with mock.patch.object(wifi, 'scan_async', return_value=scan):
            panel._update_wifi_jobs()

        self.assertEqual(panel.view, 'wifi')
        self.assertIsNone(panel.selected)
        self.assertIs(panel.scan_job, scan)
        self.assertEqual(panel._after_scan_message, 'Disconnected from Home')
        self.assertEqual(panel.status, 'Scanning...')

        scan.finish(
            True,
            'Showing recent results',
            code='partial',
            networks=[wifi.Network('Home', stale=True)],
            partial=True,
        )
        panel._update_wifi_jobs()
        self.assertEqual(
            panel.status,
            'Disconnected from Home - Showing recent results',
        )

    def test_saved_authentication_failure_reopens_password_keyboard(self):
        panel = SettingsPanel()
        network = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            profile_uuid='uuid-home',
        )
        panel.selected = network
        panel.view = 'wifi_detail'
        job = wifi.WifiJob('connect', network.ssid)
        job.finish(
            False,
            'Wrong password',
            code='authentication_failed',
            needs_password=True,
        )
        panel.action_job = job

        panel._update_wifi_jobs()

        self.assertIsInstance(panel.keyboard, FakeKeyboard)
        self.assertEqual(panel.keyboard.title, 'Wrong password')
        self.assertTrue(panel.keyboard.password)
        self.assertEqual(panel.status, 'Wrong password')
        self.assertEqual(panel.status_kind, 'error')

    def test_failed_scan_preserves_existing_network_list(self):
        panel = SettingsPanel()
        existing = wifi.Network('Home', active=True)
        panel.networks = [existing]
        job = wifi.WifiJob('scan')
        job.finish(False, 'Scan failed', code='scan_failed', networks=[])
        panel.scan_job = job

        panel._update_wifi_jobs()

        self.assertEqual(panel.networks, [existing])
        self.assertEqual(panel.status, 'Scan failed')
        self.assertEqual(panel.status_kind, 'error')
        self.assertIsNone(panel.scan_job)

    def test_partial_scan_updates_list_and_shows_recent_status(self):
        panel = SettingsPanel()
        recent = wifi.Network('Neighbor', stale=True)
        job = wifi.WifiJob('scan')
        job.finish(
            True,
            'Showing recent results',
            code='partial',
            networks=[recent],
            partial=True,
        )
        panel.scan_job = job

        panel._update_wifi_jobs()

        self.assertEqual(panel.networks, [recent])
        self.assertEqual(panel.status, 'Showing recent results')
        self.assertEqual(panel.status_kind, 'info')


if __name__ == '__main__':
    unittest.main()
