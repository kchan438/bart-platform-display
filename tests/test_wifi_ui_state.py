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

    def handle(self, event):
        pass


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
    def test_upward_swipe_closes_panel_from_subview(self):
        panel = SettingsPanel()
        panel.state = 'OPEN'
        panel.view = 'wifi_detail'

        panel.handle({
            'kind': 'release',
            'swipe': 'up',
            'start_y': 200,
        })

        self.assertEqual(panel.state, 'CLOSING')

    def test_upward_swipe_does_not_close_while_keyboard_is_open(self):
        panel = SettingsPanel()
        panel.state = 'OPEN'
        panel.view = 'apikey'
        panel.keyboard = FakeKeyboard('API key')

        panel.handle({
            'kind': 'release',
            'swipe': 'up',
            'start_y': 200,
        })

        self.assertEqual(panel.state, 'OPEN')

    def test_service_restart_requires_confirmation_and_feedback_delay(self):
        panel = SettingsPanel()

        panel._begin_system_confirmation('service')

        self.assertEqual(panel.system_state, 'confirming')
        self.assertEqual(panel.system_confirm_target, 'service')
        self.assertFalse(panel.request_service_restart)

        panel._confirm_system_action()

        self.assertEqual(panel.system_state, 'dispatching')
        self.assertEqual(panel.system_active_target, 'service')
        self.assertFalse(panel.request_service_restart)
        panel._update_system_action(10_000)
        self.assertTrue(panel.system_feedback_rendered)
        self.assertFalse(panel.request_service_restart)
        panel._update_system_action(settings_module._SYSTEM_FEEDBACK_MS - 1)
        self.assertFalse(panel.request_service_restart)
        panel._update_system_action(1)
        self.assertTrue(panel.request_service_restart)

    def test_system_confirmation_can_be_cancelled(self):
        panel = SettingsPanel()
        panel._begin_system_confirmation('device')

        panel._cancel_system_confirmation()

        self.assertEqual(panel.system_state, 'idle')
        self.assertEqual(panel.system_confirm_target, '')
        self.assertFalse(panel.request_service_restart)
        self.assertIsNone(panel.system_job)

    def test_device_restart_dispatches_once_after_feedback_delay(self):
        panel = SettingsPanel()
        job = types.SimpleNamespace(
            status='Requesting Raspberry Pi restart...',
            message='',
            done=False,
            ok=False,
        )
        panel._begin_system_confirmation('device')
        panel._confirm_system_action()

        with mock.patch.object(
                settings_module.system_control,
                'reboot_async',
                return_value=job) as reboot:
            panel._update_system_action(10_000)
            reboot.assert_not_called()
            panel._update_system_action(settings_module._SYSTEM_FEEDBACK_MS)
            panel._update_system_action(16)

        reboot.assert_called_once_with()
        self.assertIs(panel.system_job, job)
        self.assertEqual(panel.system_active_target, 'device')
        self.assertEqual(
            panel.system_status,
            'Requesting Raspberry Pi restart...',
        )

    def test_device_restart_failure_restores_usable_system_view(self):
        panel = SettingsPanel()
        panel.system_state = 'dispatching'
        panel.system_active_target = 'device'
        panel.system_job = types.SimpleNamespace(
            status='Device restart is not authorized',
            message='Device restart is not authorized',
            done=True,
            ok=False,
        )

        panel._update_system_action(16)

        self.assertEqual(panel.system_state, 'failed')
        self.assertFalse(panel._system_busy())
        self.assertEqual(panel.system_active_target, '')
        self.assertIsNone(panel.system_job)
        self.assertEqual(panel.system_status_kind, 'error')
        self.assertEqual(
            panel.system_status,
            'Device restart is not authorized',
        )

    def test_accepted_device_restart_stays_locked_for_shutdown(self):
        panel = SettingsPanel()
        panel.system_state = 'dispatching'
        panel.system_active_target = 'device'
        panel.system_job = types.SimpleNamespace(
            status='Restart accepted. Waiting for Raspberry Pi...',
            message='Restart accepted. Waiting for Raspberry Pi...',
            done=True,
            ok=True,
        )

        panel._update_system_action(16)

        self.assertEqual(panel.system_state, 'dispatching')
        self.assertTrue(panel._system_busy())
        self.assertEqual(panel.system_active_target, 'device')
        self.assertIsNone(panel.system_job)
        self.assertEqual(panel.system_status_kind, 'success')

    def test_duplicate_system_action_is_ignored_while_dispatching(self):
        panel = SettingsPanel()
        panel._begin_system_confirmation('service')
        panel._confirm_system_action()
        delay = panel.system_dispatch_delay_ms

        panel._begin_system_confirmation('device')
        panel._confirm_system_action()

        self.assertEqual(panel.system_state, 'dispatching')
        self.assertEqual(panel.system_active_target, 'service')
        self.assertEqual(panel.system_dispatch_target, 'service')
        self.assertEqual(panel.system_dispatch_delay_ms, delay)

    def test_upward_swipe_cannot_close_during_system_action(self):
        panel = SettingsPanel()
        panel.state = 'OPEN'
        panel.system_state = 'dispatching'
        panel.system_active_target = 'device'

        panel.handle({
            'kind': 'release',
            'swipe': 'up',
            'start_y': 200,
        })

        self.assertEqual(panel.state, 'OPEN')

    def test_system_view_renders_two_distinct_restart_buttons(self):
        panel = SettingsPanel()
        buttons = []

        class RecordingButton:
            def __init__(self, rect, label, **kwargs):
                self.rect = rect
                self.label = label
                buttons.append(self)

            def draw(self):
                pass

        with (
                mock.patch.object(
                    settings_module,
                    'Button',
                    RecordingButton,
                ),
                mock.patch.object(
                    panel,
                    '_header',
                    return_value=[],
                ),
                mock.patch.object(
                    settings_module.display,
                    'blit_center',
                    return_value=None,
                    create=True,
                ),
                mock.patch.object(
                    settings_module.display,
                    'font_xs',
                    object(),
                    create=True,
                )):
            panel._render_system()

        self.assertEqual(
            [button.label for button in buttons],
            [
                'RESTART DISPLAY SERVICE',
                'RESTART RASPBERRY PI',
            ],
        )
        service_rect, device_rect = [button.rect for button in buttons]
        self.assertLess(
            service_rect[1] + service_rect[3],
            device_rect[1],
        )

    def test_scrollbar_drag_release_does_not_close_panel(self):
        panel = SettingsPanel()
        panel.state = 'OPEN'
        panel.view = 'wifi'
        panel._scrollbar_dragging = True

        panel.handle({
            'kind': 'release',
            'swipe': 'up',
            'start_y': 200,
        })

        self.assertEqual(panel.state, 'OPEN')
        self.assertFalse(panel._scrollbar_dragging)

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

    def test_entering_wifi_does_not_scan_and_prompts_for_rescan(self):
        panel = SettingsPanel()
        panel.view = 'menu'

        with mock.patch.object(wifi, 'scan_async') as scan:
            panel._goto('wifi')

        scan.assert_not_called()
        self.assertIsNone(panel.scan_job)
        self.assertEqual(panel.status, 'Press Rescan to find nearby networks')

    def test_idle_updates_never_start_a_scan(self):
        panel = SettingsPanel()
        panel.state = 'OPEN'
        panel.view = 'wifi'

        with mock.patch.object(wifi, 'scan_async') as scan:
            for _ in range(10):
                panel.update(16)

        scan.assert_not_called()
        self.assertIsNone(panel.scan_job)

    def test_returning_from_detail_preserves_list_and_scroll_without_refresh(self):
        panel = SettingsPanel()
        panel.view = 'wifi_detail'
        panel.scroll = -80
        panel.scan_summary = '6 detected, 1 recent'

        with mock.patch.object(wifi, 'scan_async') as scan:
            panel._goto('wifi')

        scan.assert_not_called()
        self.assertEqual(panel.view, 'wifi')
        self.assertEqual(panel.scroll, -80)
        self.assertEqual(panel.status, '6 detected, 1 recent')

    def test_explicit_scan_requests_hardware_rescan(self):
        panel = SettingsPanel()
        job = wifi.WifiJob('scan')

        with mock.patch.object(wifi, 'scan_async', return_value=job) as scan:
            panel._start_scan()

        scan.assert_called_once_with(rescan=True)
        self.assertEqual(panel.status, 'Scanning...')

    def test_empty_message_is_hidden_until_a_scan_finishes(self):
        panel = SettingsPanel()

        self.assertFalse(panel._show_empty_scan_result())

        panel.scan_summary = '0 detected; No networks found'
        self.assertFalse(panel._show_empty_scan_result())

        panel.last_scan_completed_at = 1234.5
        self.assertTrue(panel._show_empty_scan_result())

    def test_supervisor_recovery_disables_wifi_controls(self):
        panel = SettingsPanel()

        with mock.patch.object(
                wifi,
                'connection_supervisor_recovery_busy',
                return_value=True,
        ):
            self.assertTrue(panel._wifi_mutation_busy())
            self.assertFalse(panel._wifi_busy())

    def test_foreground_job_keeps_supervisor_notice_pending(self):
        panel = SettingsPanel()
        panel.view = 'wifi'
        panel.status = 'Connecting...'
        panel.action_job = wifi.WifiJob('connect', 'Home')
        snapshot = wifi.ConnectionStatus(
            sequence=1,
            phase='failed',
            code='ssid_not_found',
            message='Saved network is not currently visible',
            profile_uuid='uuid-home',
        )

        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=snapshot,
        ):
            panel._update_connection_supervisor()

        self.assertEqual(panel._supervisor_sequence, 0)
        self.assertEqual(panel.status, 'Connecting...')

    def test_supervisor_reconnect_updates_active_row_and_notice(self):
        panel = SettingsPanel()
        panel.view = 'wifi'
        previous = wifi.Network(
            'Cafe',
            saved=True,
            active=True,
            profile_uuid='uuid-cafe',
        )
        restored = wifi.Network(
            'Holmes Guest',
            saved=True,
            profile_uuid='uuid-holmes',
        )
        panel.networks = [previous, restored]
        snapshot = wifi.ConnectionStatus(
            sequence=2,
            phase='reconnected',
            code='reconnected',
            message='Reconnected to Holmes Guest',
            ssid='Holmes Guest',
            profile_uuid='uuid-holmes',
            active_uuid='uuid-holmes',
            reason_code=0,
        )

        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=snapshot,
        ):
            panel._update_connection_supervisor()

        self.assertFalse(previous.active)
        self.assertTrue(restored.active)
        self.assertEqual(panel.status, 'Reconnected to Holmes Guest')
        self.assertEqual(panel.status_kind, 'success')
        self.assertEqual(panel._supervisor_sequence, 2)

    def test_supervisor_reconnect_falls_back_to_matching_ssid(self):
        panel = SettingsPanel()
        previous = wifi.Network(
            'Cafe',
            saved=True,
            active=True,
            profile_uuid='uuid-cafe',
        )
        restored = wifi.Network(
            'Holmes Guest',
            saved=True,
            profile_known=False,
            profile_uuid='',
        )
        panel.networks = [previous, restored]
        snapshot = wifi.ConnectionStatus(
            sequence=1,
            phase='reconnected',
            code='reconnected',
            message='Reconnected to Holmes Guest',
            ssid='Holmes Guest',
            profile_uuid='uuid-holmes',
            active_uuid='uuid-holmes',
        )

        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=snapshot,
        ):
            panel._update_connection_supervisor()

        self.assertFalse(previous.active)
        self.assertTrue(restored.active)
        self.assertTrue(restored.saved)

    def test_unresolved_supervisor_identity_does_not_clear_active_row(self):
        panel = SettingsPanel()
        existing = wifi.Network(
            'Cafe',
            saved=True,
            active=True,
            profile_uuid='uuid-cafe',
        )
        panel.networks = [existing]
        snapshot = wifi.ConnectionStatus(
            sequence=1,
            phase='connected',
            code='connected',
            message='Connected to another network',
            ssid='Unknown network',
            active_uuid='uuid-unknown',
        )

        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=snapshot,
        ):
            panel._update_connection_supervisor()

        self.assertTrue(existing.active)

    def test_supervisor_ssid_fallback_rejects_conflicting_known_uuid(self):
        panel = SettingsPanel()
        conflicting = wifi.Network(
            'Holmes Guest',
            saved=True,
            active=True,
            profile_uuid='uuid-old',
        )
        panel.networks = [conflicting]
        connected = wifi.ConnectionStatus(
            sequence=1,
            phase='connected',
            code='connected',
            message='Connected to Holmes Guest',
            ssid='Holmes Guest',
            profile_uuid='uuid-new',
            active_uuid='uuid-new',
        )

        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=connected,
        ):
            panel._update_connection_supervisor()

        self.assertTrue(conflicting.active)

        dropped = wifi.ConnectionStatus(
            sequence=2,
            phase='grace',
            code='reconnecting',
            message='Connection lost - NetworkManager is reconnecting',
            ssid='Holmes Guest',
            profile_uuid='uuid-new',
        )
        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=dropped,
        ):
            panel._update_connection_supervisor()

        self.assertTrue(conflicting.active)

    def test_supervisor_drop_refreshes_selected_detail_state(self):
        panel = SettingsPanel()
        network = wifi.Network(
            'Holmes Guest',
            saved=True,
            active=True,
            profile_uuid='uuid-holmes',
        )
        panel.networks = [network]
        panel.selected = network
        panel.view = 'wifi_detail'
        panel.detail_info = [
            ('SSID', network.ssid),
            ('STATUS', 'Connected'),
            ('IP', '192.0.2.10'),
        ]
        snapshot = wifi.ConnectionStatus(
            sequence=1,
            phase='grace',
            code='reconnecting',
            message='Connection lost - NetworkManager is reconnecting',
            ssid=network.ssid,
            profile_uuid=network.profile_uuid,
        )

        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=snapshot,
        ):
            panel._update_connection_supervisor()

        detail = dict(panel.detail_info)
        self.assertFalse(network.active)
        self.assertEqual(detail['STATUS'], 'Not connected')
        self.assertNotIn('IP', detail)

    def test_reconnected_snapshot_refreshes_already_active_detail(self):
        panel = SettingsPanel()
        network = wifi.Network(
            'Holmes Guest',
            saved=True,
            active=True,
            profile_uuid='uuid-holmes',
        )
        panel.networks = [network]
        panel.selected = network
        panel.view = 'wifi_detail'
        snapshot = wifi.ConnectionStatus(
            sequence=1,
            phase='reconnected',
            code='reconnected',
            message='Reconnected to Holmes Guest',
            ssid=network.ssid,
            profile_uuid=network.profile_uuid,
            active_uuid=network.profile_uuid,
        )

        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=snapshot,
        ), mock.patch.object(
                panel,
                '_refresh_detail_info',
        ) as refresh:
            panel._update_connection_supervisor()

        self.assertTrue(network.active)
        refresh.assert_called_once_with(network)

    def test_supervisor_failure_is_retained_when_wifi_view_opens(self):
        panel = SettingsPanel()
        snapshot = wifi.ConnectionStatus(
            sequence=3,
            phase='attention',
            code='authentication_failed',
            message='Authentication failed - password required',
            profile_uuid='uuid-home',
            reason_code=7,
        )

        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=snapshot,
        ):
            panel._update_connection_supervisor()
        panel._goto('wifi')

        self.assertEqual(
            panel.status,
            'Authentication failed - password required',
        )
        self.assertEqual(panel.status_kind, 'error')

    def test_older_supervisor_snapshot_does_not_replace_newer_notice(self):
        panel = SettingsPanel()
        panel.view = 'wifi'
        panel._supervisor_sequence = 4
        panel.status = 'Reconnected to Home'
        snapshot = wifi.ConnectionStatus(
            sequence=3,
            phase='failed',
            code='ssid_not_found',
            message='Saved network is not currently visible',
        )

        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=snapshot,
        ):
            panel._update_connection_supervisor()

        self.assertEqual(panel.status, 'Reconnected to Home')
        self.assertEqual(panel._supervisor_sequence, 4)

    def test_connected_snapshot_clears_visible_supervisor_error(self):
        panel = SettingsPanel()
        panel.view = 'wifi'
        panel._supervisor_sequence = 1
        panel._supervisor_notice = 'Could not read Wi-Fi state'
        panel._supervisor_notice_kind = 'error'
        panel.status = panel._supervisor_notice
        panel.status_kind = 'error'
        snapshot = wifi.ConnectionStatus(
            sequence=2,
            phase='connected',
            code='connected',
            message='Connected to Home',
            ssid='Home',
            active_uuid='uuid-home',
        )

        with mock.patch.object(
                wifi,
                'connection_supervisor_snapshot',
                return_value=snapshot,
        ):
            panel._update_connection_supervisor()

        self.assertEqual(panel._supervisor_notice, '')
        self.assertEqual(panel.status, 'Connected to Home')
        self.assertEqual(panel.status_kind, 'success')

    def test_selecting_target_preserves_supervisor_failure_notice(self):
        panel = SettingsPanel()
        network = wifi.Network(
            'Holmes Guest',
            saved=True,
            profile_uuid='uuid-holmes',
        )
        panel._supervisor_notice = 'Authentication failed - password required'
        panel._supervisor_notice_kind = 'error'
        panel._supervisor_notice_profile_uuid = 'uuid-holmes'
        panel._supervisor_notice_ssid = network.ssid

        panel._select(network)

        self.assertEqual(
            panel.status,
            'Authentication failed - password required',
        )
        self.assertEqual(panel.status_kind, 'error')

    def test_identityless_disconnect_notice_does_not_leak_into_detail(self):
        panel = SettingsPanel()
        network = wifi.Network('ASUS_C')
        panel._supervisor_notice = 'Disconnected from Holmes Guest'
        panel._supervisor_notice_kind = 'info'
        panel._supervisor_notice_phase = 'disconnected'

        panel._select(network)

        self.assertEqual(panel.status, '')
        self.assertEqual(panel.status_kind, 'info')

    def test_successful_scan_keeps_summary_when_backend_message_is_empty(self):
        panel = SettingsPanel()
        job = wifi.WifiJob('scan')
        job.finish(
            True,
            '',
            networks=[
                wifi.Network('Home'),
                wifi.Network('Cafe', stale=True),
            ],
        )
        panel.scan_job = job

        panel._update_wifi_jobs()

        self.assertEqual(panel.status, '1 detected, 1 recent')
        self.assertEqual(panel.scan_summary, '1 detected, 1 recent')
        self.assertEqual(panel.status_kind, 'info')

    def test_scan_summary_retains_session_completion_time(self):
        panel = SettingsPanel()
        job = wifi.WifiJob('scan')
        job.finish(
            True,
            '',
            networks=[wifi.Network('Home')],
            completed_at=1234.5,
        )
        panel.scan_job = job

        with mock.patch.object(
                SettingsPanel,
                '_scan_time_label',
                return_value='2:34 PM',
        ):
            panel._update_wifi_jobs()

        self.assertEqual(panel.last_scan_completed_at, 1234.5)
        self.assertEqual(panel.scan_summary, '1 detected @ 2:34 PM')

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

    def test_saved_network_not_detected_has_no_current_signal_state(self):
        network = types.SimpleNamespace(
            ssid='Holmes Guest',
            signal=84,
            security='WPA2',
            protected=True,
            saved=True,
            active=False,
            profile_known=True,
            supported=True,
            detected=False,
            stale=False,
        )

        tag, show_signal = SettingsPanel._network_row_state(network)
        detail = SettingsPanel._basic_network_info(network)

        self.assertEqual(tag, 'NOT DETECTED')
        self.assertFalse(show_signal)
        self.assertNotIn('SIGNAL', [label for label, _value in detail])
        self.assertIn(
            ('AVAILABILITY', 'Not detected'),
            detail,
        )

    def test_active_saved_only_row_does_not_invent_signal_or_security(self):
        network = wifi.Network(
            'Holmes Guest',
            saved=True,
            active=True,
            profile_uuid='uuid-holmes',
            detected=False,
        )

        tag, show_signal = SettingsPanel._network_row_state(network)
        detail = dict(SettingsPanel._basic_network_info(network))
        summary = SettingsPanel._scan_result_summary([network])

        self.assertEqual(tag, 'ONLINE')
        self.assertFalse(show_signal)
        self.assertEqual(detail['AVAILABILITY'], 'Not detected')
        self.assertEqual(detail['SECURITY'], 'Unknown')
        self.assertEqual(detail['PROTECTED'], 'Unknown')
        self.assertEqual(summary, '0 detected')

    def test_active_saved_only_detail_worker_keeps_signal_sanitized(self):
        panel = SettingsPanel()
        network = wifi.Network(
            'Holmes Guest',
            saved=True,
            active=True,
            profile_uuid='uuid-holmes',
            detected=False,
        )
        full_info = [
            ('SSID', network.ssid),
            ('SIGNAL', '0%'),
            ('STATUS', 'Connected'),
            ('IP', '192.0.2.10'),
        ]

        with mock.patch.object(wifi, 'info', return_value=full_info), \
                mock.patch.object(
                    settings_module.threading,
                    'Thread',
                ) as thread_class:
            panel._select(network)
            worker = thread_class.call_args.kwargs['target']
            worker()

        detail = dict(panel.detail_info)
        self.assertNotIn('SIGNAL', detail)
        self.assertEqual(detail['AVAILABILITY'], 'Not detected')
        self.assertEqual(detail['IP'], '192.0.2.10')

    def test_recent_cached_network_is_distinct_and_has_no_current_signal(self):
        network = wifi.Network('Cafe', signal=72, stale=True)

        tag, show_signal = SettingsPanel._network_row_state(network)
        detail = SettingsPanel._basic_network_info(network)

        self.assertEqual(tag, 'RECENT')
        self.assertFalse(show_signal)
        self.assertNotIn('SIGNAL', [label for label, _value in detail])
        self.assertIn(('AVAILABILITY', 'Recent result'), detail)

    def test_saved_network_not_detected_cannot_connect_until_rescan(self):
        panel = SettingsPanel()
        network = types.SimpleNamespace(
            ssid='Holmes Guest',
            signal=0,
            security='WPA2',
            protected=True,
            saved=True,
            active=False,
            profile_known=True,
            supported=True,
            detected=False,
            stale=False,
        )
        panel.selected = network
        panel.view = 'wifi_detail'

        with mock.patch.object(wifi, 'connect_async') as connect:
            panel._do_connect()

        connect.assert_not_called()
        self.assertIsNone(panel.action_job)
        self.assertEqual(panel.status, 'Not detected. Press Rescan.')
        self.assertEqual(panel.status_kind, 'error')

    def test_password_submit_waits_for_supervisor_recovery(self):
        panel = SettingsPanel()
        network = wifi.Network('Cafe', security='WPA2')
        keyboard = FakeKeyboard('Password: Cafe')
        panel.selected = network
        panel.keyboard = keyboard

        with mock.patch.object(
                wifi,
                'connection_supervisor_recovery_busy',
                return_value=True,
        ), mock.patch.object(wifi, 'connect_async') as connect:
            panel._connect_with_password('correct password')

        connect.assert_not_called()
        self.assertIs(panel.keyboard, keyboard)
        self.assertEqual(
            panel.status,
            'Wi-Fi recovery in progress. Try again shortly.',
        )

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

    def test_disconnect_success_updates_local_state_without_scanning(self):
        panel = SettingsPanel()
        network = wifi.Network('Home', saved=True, active=True)
        panel.networks = [network]
        panel.selected = network
        panel.view = 'wifi_detail'
        panel.scroll = -80
        action = wifi.WifiJob('disconnect', network.ssid)
        action.finish(True, 'Disconnected from Home', code='disconnected')
        panel.action_job = action

        with mock.patch.object(wifi, 'scan_async') as scan_async:
            panel._update_wifi_jobs()

        scan_async.assert_not_called()
        self.assertEqual(panel.view, 'wifi')
        self.assertIsNone(panel.selected)
        self.assertIsNone(panel.scan_job)
        self.assertEqual(panel.status, 'Disconnected from Home')
        self.assertEqual(panel.status_kind, 'success')
        self.assertEqual(panel.scroll, -80)
        self.assertFalse(network.active)
        self.assertTrue(network.saved)

    def test_connect_success_updates_local_state_without_scanning(self):
        panel = SettingsPanel()
        network = wifi.Network('Home', saved=True)
        previous = wifi.Network('Cafe', saved=True, active=True)
        panel.networks = [previous, network]
        panel.selected = network
        panel.view = 'wifi_detail'
        panel.scroll = -40
        action = wifi.WifiJob('connect', network.ssid)
        action.finish(True, 'Connected to Home', code='connected')
        panel.action_job = action

        with mock.patch.object(wifi, 'scan_async') as scan_async:
            panel._update_wifi_jobs()

        scan_async.assert_not_called()
        self.assertEqual(panel.view, 'wifi')
        self.assertIsNone(panel.selected)
        self.assertEqual(panel.status, 'Connected to Home')
        self.assertEqual(panel.status_kind, 'success')
        self.assertEqual(panel.scroll, -40)
        self.assertTrue(network.active)
        self.assertTrue(network.saved)
        self.assertFalse(previous.active)

        # Polling again must not clear the retained confirmation.
        panel._update_wifi_jobs()
        self.assertEqual(panel.status, 'Connected to Home')
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

    def test_empty_failed_scan_expires_previous_detected_rows(self):
        panel = SettingsPanel()
        existing = wifi.Network('Old network', signal=91)
        panel.networks = [existing]
        job = wifi.WifiJob('scan')
        job.finish(
            False,
            'Scan failed',
            code='scan_failed',
            networks=[],
            completed_at=1234.5,
        )
        panel.scan_job = job

        with mock.patch.object(
                SettingsPanel,
                '_scan_time_label',
                return_value='2:34 PM',
        ):
            panel._update_wifi_jobs()

        self.assertEqual(panel.networks, [])
        self.assertEqual(
            panel.status,
            '0 detected @ 2:34 PM; Scan failed',
        )
        self.assertEqual(panel.status_kind, 'error')
        self.assertEqual(panel.scan_summary_kind, 'error')
        self.assertIsNone(panel.scan_job)

        panel.view = 'wifi_detail'
        panel._goto('wifi')
        self.assertEqual(
            panel.status,
            '0 detected @ 2:34 PM; Scan failed',
        )
        self.assertEqual(panel.status_kind, 'error')

    def test_empty_failed_scan_safely_retains_saved_and_active_rows(self):
        panel = SettingsPanel()
        saved = wifi.Network(
            'Holmes Guest',
            signal=84,
            security='WPA2',
            saved=True,
            bssid='00:11:22:33:44:55',
            iface='wlan0',
            profile_name='holmes-profile',
            profile_uuid='uuid-holmes',
            last_seen=100.0,
            in_use=False,
        )
        active = wifi.Network(
            'ASUS_C',
            signal=72,
            security='WPA2',
            saved=True,
            active=True,
            bssid='00:11:22:33:44:66',
            iface='wlan0',
            profile_name='asus-profile',
            profile_uuid='uuid-asus',
            last_seen=100.0,
            in_use=True,
        )
        expired = wifi.Network('Neighbor', signal=90)
        panel.networks = [saved, active, expired]
        job = wifi.WifiJob('scan')
        job.finish(
            False,
            'Scan failed',
            code='scan_failed',
            networks=[],
            completed_at=1234.5,
        )
        panel.scan_job = job

        with mock.patch.object(
                SettingsPanel,
                '_scan_time_label',
                return_value='',
        ):
            panel._update_wifi_jobs()

        self.assertEqual(panel.networks, [saved, active])
        self.assertEqual(saved.profile_uuid, 'uuid-holmes')
        self.assertEqual(saved.profile_name, 'holmes-profile')
        self.assertEqual(active.profile_uuid, 'uuid-asus')
        self.assertTrue(active.active)
        for network in panel.networks:
            self.assertFalse(network.detected)
            self.assertFalse(network.stale)
            self.assertEqual(network.signal, 0)
            self.assertEqual(network.security, '')
            self.assertFalse(network.protected)
            self.assertEqual(network.bssid, '')
            self.assertEqual(network.last_seen, 0.0)
            self.assertFalse(network.in_use)
        self.assertEqual(
            panel.status,
            '0 detected, 1 saved not detected; Scan failed',
        )

    def test_preflight_scan_failure_preserves_list_and_scroll_unchanged(self):
        panel = SettingsPanel()
        saved = wifi.Network(
            'Holmes Guest',
            signal=84,
            security='WPA2',
            saved=True,
            profile_uuid='uuid-holmes',
        )
        nearby = wifi.Network('Neighbor', signal=71)
        original = [saved, nearby]
        panel.networks = original
        panel.scroll = -40
        job = wifi.WifiJob('scan')
        job.finish(
            False,
            'Wi-Fi is busy',
            code='busy',
            networks=[],
            completed_at=0.0,
        )
        panel.scan_job = job

        panel._update_wifi_jobs()

        self.assertIs(panel.networks, original)
        self.assertEqual(panel.networks, [saved, nearby])
        self.assertEqual(panel.scroll, -40)
        self.assertTrue(saved.detected)
        self.assertEqual(saved.signal, 84)
        self.assertEqual(saved.security, 'WPA2')
        self.assertTrue(nearby.detected)
        self.assertEqual(nearby.signal, 71)
        self.assertEqual(panel.status, '2 detected; Wi-Fi is busy')
        self.assertFalse(panel._show_empty_scan_result())

    def test_zero_time_unexpected_scan_failure_expires_detected_rows(self):
        panel = SettingsPanel()
        saved = wifi.Network(
            'Holmes Guest',
            signal=84,
            security='WPA2',
            saved=True,
            profile_uuid='uuid-holmes',
        )
        expired = wifi.Network('Neighbor', signal=71)
        panel.networks = [saved, expired]
        job = wifi.WifiJob('scan')
        job.finish(
            False,
            'Scan failed',
            code='unexpected',
            networks=[],
            completed_at=0.0,
        )
        panel.scan_job = job

        panel._update_wifi_jobs()

        self.assertEqual(panel.networks, [saved])
        self.assertFalse(saved.detected)
        self.assertEqual(saved.signal, 0)
        self.assertEqual(saved.security, '')
        self.assertEqual(
            panel.status,
            '0 detected, 1 saved not detected; Scan failed',
        )

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
        self.assertEqual(
            panel.status,
            '0 detected, 1 recent; Showing recent results',
        )
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
