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
    settings_module = sys.modules[SettingsPanel.__module__]


class WifiUiStateTests(unittest.TestCase):
    def test_wrapped_detail_status_stays_above_action_buttons(self):
        panel = SettingsPanel()
        font = types.SimpleNamespace(
            get_height=lambda: 17,
            size=lambda text: (len(text) * 8, 17),
        )
        max_width = 120
        status = 'Previous connection cleanup is still pending'

        with mock.patch.object(
                settings_module.display,
                'font_xs',
                font,
                create=True,
        ), mock.patch.object(
                settings_module.display,
                'truncate',
                lambda text, _font, _width: text,
                create=True,
        ):
            lines = panel._status_lines(status, font, max_width)
            top = panel._status_top_before(
                status,
                settings_module._DETAIL_ACTION_TOP,
                max_width,
            )

        line_h = font.get_height() + 2
        rendered_bottom = (
            top
            + (len(lines) - 1) * line_h
            + font.get_height()
        )
        self.assertEqual(len(lines), 2)
        self.assertLessEqual(
            rendered_bottom,
            settings_module._DETAIL_ACTION_TOP
            - settings_module._STATUS_ACTION_GAP,
        )

    def test_entering_wifi_uses_cached_refresh_without_hardware_rescan(self):
        panel = SettingsPanel()
        panel.view = 'menu'
        job = wifi.WifiJob('scan')

        with mock.patch.object(wifi, 'scan_async', return_value=job) as scan:
            panel._goto('wifi')

        scan.assert_called_once_with(rescan=False)
        self.assertIs(panel.scan_job, job)
        self.assertEqual(panel.status, 'Refreshing...')

    def test_returning_from_detail_preserves_list_and_scroll_without_refresh(self):
        panel = SettingsPanel()
        panel.view = 'wifi_detail'
        panel.scroll = -80

        with mock.patch.object(wifi, 'scan_async') as scan:
            panel._goto('wifi')

        scan.assert_not_called()
        self.assertEqual(panel.view, 'wifi')
        self.assertEqual(panel.scroll, -80)

    def test_explicit_scan_requests_hardware_rescan(self):
        panel = SettingsPanel()
        job = wifi.WifiJob('scan')

        with mock.patch.object(wifi, 'scan_async', return_value=job) as scan:
            panel._start_scan()

        scan.assert_called_once_with(rescan=True)
        self.assertEqual(panel.status, 'Scanning...')

    def test_detail_worker_does_not_overwrite_newer_ui_state(self):
        panel = SettingsPanel()
        original = wifi.Network('Original', active=True)
        replacement = wifi.Network('Replacement')
        stale_info = [('SSID', original.ssid), ('IP', '192.0.2.10')]

        with mock.patch.object(wifi, 'info', return_value=stale_info), \
                mock.patch.object(
                    settings_module.threading,
                    'Thread',
                ) as thread_class:
            panel._select(original)
            worker = thread_class.call_args.kwargs['target']

            panel._select(replacement)
            replacement_info = panel.detail_info
            worker()

            self.assertIs(panel.selected, replacement)
            self.assertEqual(panel.detail_info, replacement_info)

            panel.selected = original
            panel.view = 'wifi'
            panel.detail_info = [('sentinel', 'list view')]
            worker()

            self.assertEqual(panel.detail_info, [('sentinel', 'list view')])

    def test_older_detail_worker_cannot_overwrite_reopened_same_network(self):
        panel = SettingsPanel()
        network = wifi.Network('Home', active=True)
        older_info = [('SSID', network.ssid), ('IP', '192.0.2.10')]
        newer_info = [('SSID', network.ssid), ('IP', '192.0.2.11')]

        with mock.patch.object(
                settings_module.threading,
                'Thread',
        ) as thread_class:
            panel._select(network)
            older_worker = thread_class.call_args.kwargs['target']

            panel.view = 'wifi'
            panel._select(network)
            newer_worker = thread_class.call_args.kwargs['target']

        with mock.patch.object(wifi, 'info', return_value=newer_info):
            newer_worker()
        with mock.patch.object(wifi, 'info', return_value=older_info):
            older_worker()

        self.assertEqual(panel.detail_info, newer_info)

    def test_protected_unsaved_network_opens_password_keyboard(self):
        panel = SettingsPanel()
        network = wifi.Network('Cafe', security='WPA2', saved=False)
        panel.selected = network
        panel.view = 'wifi_detail'

        panel._do_connect()

        self.assertIsInstance(panel.keyboard, FakeKeyboard)
        self.assertEqual(panel.keyboard.title, 'Password: Cafe')
        self.assertEqual(panel.keyboard.initial, '')
        self.assertTrue(panel.keyboard.password)
        self.assertIsNone(panel.action_job)

    def test_unknown_saved_state_is_resolved_before_password_prompt(self):
        panel = SettingsPanel()
        network = wifi.Network(
            'Cafe',
            security='WPA2',
            saved=False,
            profile_known=False,
        )
        panel.selected = network
        panel.view = 'wifi_detail'
        job = wifi.WifiJob('connect', network.ssid)

        with mock.patch.object(wifi, 'connect_async', return_value=job) as connect:
            panel._do_connect()

        self.assertIsNone(panel.keyboard)
        self.assertIs(panel.action_job, job)
        self.assertEqual(panel.status, 'Checking saved network...')
        connect.assert_called_once_with(network)

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

    def test_disconnect_success_returns_to_list_and_refreshes_cached_state(self):
        panel = SettingsPanel()
        network = wifi.Network('Home', active=True)
        panel.networks = [network]
        panel.selected = network
        panel.view = 'wifi_detail'
        action = wifi.WifiJob('disconnect', network.ssid)
        action.finish(True, 'Disconnected from Home', code='disconnected')
        panel.action_job = action
        scan = wifi.WifiJob('scan')

        with mock.patch.object(
                wifi,
                'scan_async',
                return_value=scan,
        ) as scan_async:
            panel._update_wifi_jobs()

        scan_async.assert_called_once_with(rescan=False)
        self.assertEqual(panel.view, 'wifi')
        self.assertIsNone(panel.selected)
        self.assertIs(panel.scan_job, scan)
        self.assertEqual(panel._after_scan_message, 'Disconnected from Home')
        self.assertEqual(panel.status, 'Refreshing...')
        self.assertFalse(network.active)

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

    def test_connect_success_returns_to_list_without_hardware_rescan(self):
        panel = SettingsPanel()
        network = wifi.Network('Home', saved=True)
        panel.networks = [network]
        panel.selected = network
        panel.view = 'wifi_detail'
        action = wifi.WifiJob('connect', network.ssid)
        action.finish(True, 'Connected to Home', code='connected')
        panel.action_job = action
        scan = wifi.WifiJob('scan')

        with mock.patch.object(
                wifi,
                'scan_async',
                return_value=scan,
        ) as scan_async:
            panel._update_wifi_jobs()

        scan_async.assert_called_once_with(rescan=False)
        self.assertEqual(panel.view, 'wifi')
        self.assertIsNone(panel.selected)
        self.assertEqual(panel._after_scan_message, 'Connected to Home')
        self.assertEqual(panel.status, 'Refreshing...')

    def test_failed_follow_up_scan_keeps_disconnect_confirmation(self):
        panel = SettingsPanel()
        network = wifi.Network('Home', active=True)
        panel.networks = [network]
        panel.selected = network
        panel.view = 'wifi_detail'
        action = wifi.WifiJob('disconnect', network.ssid)
        action.finish(True, 'Disconnected from Home', code='disconnected')
        panel.action_job = action
        scan = wifi.WifiJob('scan')

        with mock.patch.object(wifi, 'scan_async', return_value=scan):
            panel._update_wifi_jobs()

        scan.finish(False, 'Scan failed', code='scan_failed', networks=[])
        panel._update_wifi_jobs()

        self.assertEqual(panel.networks, [network])
        self.assertFalse(network.active)
        self.assertEqual(
            panel.status,
            'Disconnected from Home - Scan failed',
        )
        self.assertEqual(panel.status_kind, 'error')

    def test_empty_follow_up_scan_keeps_disconnect_confirmation(self):
        panel = SettingsPanel()
        network = wifi.Network('Home', active=True)
        panel.networks = [network]
        panel.selected = network
        panel.view = 'wifi_detail'
        action = wifi.WifiJob('disconnect', network.ssid)
        action.finish(True, 'Disconnected from Home', code='disconnected')
        panel.action_job = action
        scan = wifi.WifiJob('scan')

        with mock.patch.object(wifi, 'scan_async', return_value=scan):
            panel._update_wifi_jobs()

        scan.finish(True, '', networks=[])
        panel._update_wifi_jobs()

        self.assertEqual(panel.networks, [])
        self.assertEqual(panel.status, 'Disconnected from Home')
        self.assertEqual(panel.status_kind, 'success')

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

    def test_scroll_buttons_move_one_row_and_clamp_at_each_end(self):
        panel = SettingsPanel()
        panel.networks = [wifi.Network(str(i)) for i in range(10)]

        panel._scroll_rows(-1)
        self.assertEqual(panel.scroll, -40)

        for _ in range(20):
            panel._scroll_rows(-1)
        self.assertEqual(panel.scroll, panel._min_scroll())

        for _ in range(20):
            panel._scroll_rows(1)
        self.assertEqual(panel.scroll, 0)

    def test_scrollbar_thumb_drag_maps_to_full_scroll_range(self):
        panel = SettingsPanel()
        panel.state = 'OPEN'
        panel.view = 'wifi'
        panel.networks = [wifi.Network(str(i)) for i in range(12)]
        thumb = panel._scrollbar_thumb_rect()
        x = thumb[0] + 1
        y = thumb[1] + 4

        panel.handle({'kind': 'press', 'x': x, 'y': y})
        panel.handle({
            'kind': 'drag',
            'x': x,
            'y': y,
            'dx': 0,
            'dy': 0,
        })
        track_top, track_h, _, thumb_h = panel._scrollbar_metrics()
        panel.handle({
            'kind': 'drag',
            'x': x,
            'y': track_top + track_h - thumb_h + 4,
            'dx': 0,
            'dy': track_h,
        })

        self.assertEqual(panel.scroll, panel._min_scroll())

    def test_scrollbar_drag_can_start_from_stable_first_move_sample(self):
        panel = SettingsPanel()
        panel.state = 'OPEN'
        panel.view = 'wifi'
        panel.networks = [wifi.Network(str(i)) for i in range(12)]
        thumb = panel._scrollbar_thumb_rect()
        x = thumb[0] + 1
        y = thumb[1] + 4

        panel.handle({'kind': 'press', 'x': 0, 'y': 0})
        panel.handle({
            'kind': 'drag',
            'x': x,
            'y': y,
            'dx': 0,
            'dy': 0,
        })

        self.assertTrue(panel._scrollbar_dragging)
        self.assertEqual(panel._scrollbar_drag_offset, 4)

    def test_noisy_press_on_thumb_does_not_start_scrollbar_drag(self):
        panel = SettingsPanel()
        panel.state = 'OPEN'
        panel.view = 'wifi'
        panel.networks = [wifi.Network(str(i)) for i in range(12)]
        thumb = panel._scrollbar_thumb_rect()

        panel.handle({
            'kind': 'press',
            'x': thumb[0] + 1,
            'y': thumb[1] + 4,
        })
        panel.handle({
            'kind': 'drag',
            'x': 100,
            'y': 180,
            'dx': 0,
            'dy': 0,
        })

        self.assertFalse(panel._scrollbar_dragging)
        self.assertEqual(panel.scroll, 0)

    def test_dragging_list_body_no_longer_scrolls_rows(self):
        panel = SettingsPanel()
        panel.state = 'OPEN'
        panel.view = 'wifi'
        panel.networks = [wifi.Network(str(i)) for i in range(12)]

        panel.handle({
            'kind': 'drag',
            'x': 100,
            'y': 180,
            'dx': 0,
            'dy': -80,
        })

        self.assertEqual(panel.scroll, 0)


if __name__ == '__main__':
    unittest.main()
