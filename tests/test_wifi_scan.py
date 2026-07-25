import os
import threading
import time
import unittest
from unittest import mock

from bartdisplay import wifi


def profile(ssid='Home', name='netplan-home', uuid='uuid-home'):
    return wifi.SavedProfile(ssid, name, uuid)


def active(uuid='uuid-home', name='netplan-home'):
    return wifi.ActiveConnection(
        'wlan0',
        state='100 (connected)',
        name=name,
        uuid=uuid,
    )


def discovery(profiles=None, complete=True):
    return wifi.ProfileDiscoveryResult(
        profiles=profiles,
        complete=complete,
        code='ok' if complete else 'profile_query_failed',
        message='' if complete else 'Saved network status unavailable',
    )


class WifiTests(unittest.TestCase):
    def setUp(self):
        wifi._scan_cache.clear()
        wifi._recovery_latch = None
        wifi._autoconnect_restore.clear()
        wifi._reauth_required.clear()

    def test_parse_scan_output_deduplicates_and_unescapes_ssids(self):
        profiles = {'Guest': [profile('Guest', 'guest-profile', 'guest-uuid')]}
        out = '\n'.join([
            r' :45:WPA2:Home\:Lab:AA\:00\:00\:00\:00\:01:wlan0',
            r' :82:WPA2:Home\:Lab:AA\:00\:00\:00\:00\:02:wlan0',
            r' :30:--:Guest:AA\:00\:00\:00\:00\:03:wlan0',
        ])

        nets = wifi._parse_scan_output(
            out,
            profiles,
            wifi.ActiveConnection('wlan0'),
            now=10.0,
        )

        self.assertEqual([net.ssid for net in nets], ['Home:Lab', 'Guest'])
        self.assertEqual(nets[0].signal, 82)
        self.assertEqual(nets[0].bssid, 'AA:00:00:00:00:02')
        self.assertFalse(nets[0].saved)
        self.assertTrue(nets[1].saved)
        self.assertEqual(nets[1].profile_uuid, 'guest-uuid')
        self.assertFalse(nets[1].protected)

    def test_parse_scan_output_prefers_active_bssid_over_stronger_duplicate(self):
        profiles = {'Home': [profile()]}
        out = '\n'.join([
            r'*:30:WPA2:Home:AA\:00\:00\:00\:00\:01:wlan0',
            r' :80:WPA2:Home:AA\:00\:00\:00\:00\:02:wlan0',
        ])

        nets = wifi._parse_scan_output(out, profiles, active(), now=10.0)

        self.assertEqual(len(nets), 1)
        self.assertTrue(nets[0].active)
        self.assertEqual(nets[0].signal, 30)
        self.assertEqual(nets[0].bssid, 'AA:00:00:00:00:01')

    def test_connected_profile_remains_visible_when_scan_omits_it(self):
        profiles = {'Home': [profile()]}

        nets = wifi._parse_scan_output('', profiles, active(), now=10.0)

        self.assertEqual(len(nets), 1)
        self.assertEqual(nets[0].ssid, 'Home')
        self.assertTrue(nets[0].active)
        self.assertTrue(nets[0].saved)
        self.assertEqual(nets[0].profile_uuid, 'uuid-home')

    def test_saved_profiles_use_wireless_ssid_instead_of_profile_name(self):
        def fake_run(args, timeout=20, input_text=None):
            if args == [
                    '-t', '-f', 'NAME,UUID,TYPE,AUTOCONNECT',
                    'connection', 'show']:
                return (
                    0,
                    'netplan-wlan0-ASUS:profile-uuid:802-11-wireless:yes\n',
                    '',
                )
            if args == [
                    '-g', '802-11-wireless.ssid', 'connection', 'show',
                    'uuid', 'profile-uuid']:
                return 0, r'ASUS\:Lab' + '\n', ''
            self.fail(f'unexpected nmcli args: {args}')

        with mock.patch.object(wifi, '_run', side_effect=fake_run):
            profiles = wifi.saved_profiles()

        self.assertEqual(set(profiles), {'ASUS:Lab'})
        self.assertEqual(profiles['ASUS:Lab'][0].name, 'netplan-wlan0-ASUS')
        self.assertEqual(profiles['ASUS:Lab'][0].uuid, 'profile-uuid')

    def test_saved_profile_query_failure_is_not_an_empty_complete_result(self):
        with mock.patch.object(
                wifi,
                '_run',
                return_value=(1, '', 'NetworkManager unavailable'),
        ):
            result = wifi._saved_profiles_detailed()

        self.assertFalse(result.complete)
        self.assertEqual(result.profiles, {})
        self.assertEqual(result.code, 'profile_query_failed')

    def test_incomplete_discovery_preserves_cached_profile_identity(self):
        cached = wifi.Network(
            'Home',
            90,
            'WPA2',
            saved=True,
            profile_name='netplan-home',
            profile_uuid='uuid-home',
            profile_known=True,
            autoconnect=True,
        )
        with mock.patch.object(wifi.time, 'monotonic', return_value=10.0):
            wifi._merge_scan_cache([cached])

        unknown = wifi.Network(
            'Home',
            80,
            'WPA2',
            profile_known=False,
        )
        with mock.patch.object(wifi.time, 'monotonic', return_value=20.0):
            networks, _ = wifi._merge_scan_cache(
                [unknown],
                preserve_profile_identity=True,
            )

        self.assertEqual(networks[0].profile_uuid, 'uuid-home')
        self.assertTrue(networks[0].saved)
        self.assertFalse(networks[0].profile_known)

    def test_incomplete_profile_discovery_blocks_profile_creation(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            profile_known=False,
            iface='wlan0',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery(complete=False),
                ), \
                mock.patch.object(wifi, '_create_profile') as create:
            wifi._connect_worker(job, net, 'password')

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'profile_query_failed')
        create.assert_not_called()

    def test_connect_reresolves_saved_profile_before_creating(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            profile_known=False,
            iface='wlan0',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery(
                        {'Home': [profile()]},
                    ),
                ), \
                mock.patch.object(wifi, '_create_profile') as create, \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(0, '', ''),
                ) as activate, \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-home', ''),
                ):
            wifi._connect_worker(job, net, None)

        self.assertTrue(job.ok)
        self.assertTrue(net.saved)
        self.assertEqual(net.profile_uuid, 'uuid-home')
        create.assert_not_called()
        activate.assert_called_once_with(
            'uuid-home',
            'wlan0',
            password=None,
        )

    def test_stale_cached_profile_is_replaced_only_after_complete_discovery(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-old',
            profile_known=False,
        )
        job = wifi.WifiJob('connect', net.ssid)

        def create_profile(net_arg, iface):
            net_arg.saved = True
            net_arg.profile_uuid = 'uuid-new'
            return 0, '', '', 'uuid-new'

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery(),
                ), \
                mock.patch.object(
                    wifi,
                    '_create_profile',
                    side_effect=create_profile,
                ) as create, \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(0, '', ''),
                ) as activate, \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-new', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_enable_profile_autoconnect',
                    return_value=(0, '', ''),
                ):
            wifi._connect_worker(job, net, 'replacement')

        self.assertTrue(job.ok)
        create.assert_called_once_with(net, 'wlan0')
        activate.assert_called_once_with(
            'uuid-new',
            'wlan0',
            password='replacement',
        )

    def test_profile_selection_prefers_active_then_cached_uuid_deterministically(self):
        candidates = {
            'Home': [
                profile(name='Zulu', uuid='uuid-z'),
                profile(name='Alpha', uuid='uuid-a'),
            ],
        }

        selected = wifi._profile_for_ssid(
            candidates,
            'Home',
            preferred_uuid='uuid-z',
        )
        self.assertEqual(selected.uuid, 'uuid-z')

        selected = wifi._profile_for_ssid(
            candidates,
            'Home',
            active_uuid='uuid-a',
            preferred_uuid='uuid-z',
        )
        self.assertEqual(selected.uuid, 'uuid-a')

        selected = wifi._profile_for_ssid(candidates, 'Home')
        self.assertEqual(selected.uuid, 'uuid-a')

    def test_scan_retains_recent_networks_when_fresh_result_degrades(self):
        first = [
            wifi.Network('Home', 90, 'WPA2', last_seen=10.0),
            wifi.Network('Neighbor', 45, 'WPA2', last_seen=10.0),
        ]
        with mock.patch.object(wifi.time, 'monotonic', return_value=10.0):
            initial, partial = wifi._merge_scan_cache(first)
        with mock.patch.object(wifi.time, 'monotonic', return_value=20.0):
            degraded, partial = wifi._merge_scan_cache([
                wifi.Network('Home', 88, 'WPA2'),
            ])

        self.assertFalse(any(net.stale for net in initial))
        self.assertTrue(partial)
        self.assertEqual([net.ssid for net in degraded], ['Home', 'Neighbor'])
        self.assertFalse(degraded[0].stale)
        self.assertTrue(degraded[1].stale)

    def test_scan_expires_old_cached_networks(self):
        with mock.patch.object(wifi.time, 'monotonic', return_value=10.0):
            wifi._merge_scan_cache([wifi.Network('Old', 50, 'WPA2')])
        expired_at = 10.0 + wifi._SCAN_CACHE_TTL_SEC + 1
        with mock.patch.object(wifi.time, 'monotonic', return_value=expired_at):
            networks, partial = wifi._merge_scan_cache([])

        self.assertEqual(networks, [])
        self.assertFalse(partial)

    def test_scan_reports_disabled_radio_without_enabling_it(self):
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(wifi, '_wifi_radio', return_value='disabled'), \
                mock.patch.object(wifi, '_run') as run:
            result = wifi._scan_detailed_unlocked(rescan=True)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'radio_disabled')
        self.assertEqual(result.message, 'Wi-Fi is disabled')
        run.assert_not_called()

    def test_scan_busy_returns_immediately(self):
        wifi._operation_lock.acquire()
        try:
            result = wifi.scan_detailed()
        finally:
            wifi._operation_lock.release()

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'busy')

    def test_async_job_is_not_done_until_operation_lock_is_released(self):
        job = wifi.WifiJob('scan')
        finished_work = threading.Event()
        allow_return = threading.Event()

        def worker():
            job.finish(True, '', networks=[])
            finished_work.set()
            allow_return.wait(1)

        wifi._launch_job(job, worker)
        self.assertTrue(finished_work.wait(1))
        self.assertFalse(job.done)
        allow_return.set()

        deadline = time.monotonic() + 1
        while not job.done and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(job.done)
        self.assertTrue(wifi._operation_lock.acquire(blocking=False))
        wifi._operation_lock.release()

    def test_pending_cleanup_gates_the_next_async_worker(self):
        wifi._set_recovery_latch(
            'wlan0',
            'uuid-home',
            reason='test cleanup',
        )
        job = wifi.WifiJob('scan')
        worker = mock.Mock()

        with mock.patch.object(
                wifi,
                '_reconcile_recovery',
                return_value=False,
        ):
            wifi._launch_job(job, worker)
            deadline = time.monotonic() + 1
            while not job.done and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertTrue(job.done)
        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'cleanup_pending')
        worker.assert_not_called()

    def test_recovery_latch_clears_only_after_verified_cleanup(self):
        net = wifi.Network(
            'Home',
            saved=True,
            profile_uuid='uuid-home',
        )
        wifi._set_recovery_latch(
            'wlan0',
            'uuid-home',
            delete_profile=True,
            network=net,
            reason='test cleanup',
        )

        with mock.patch.object(
                wifi,
                '_cancel_activation',
                side_effect=[False, True],
        ), mock.patch.object(
                wifi,
                '_discard_new_profile',
                return_value=True,
        ) as discard:
            self.assertFalse(wifi._reconcile_recovery())
            self.assertIsNotNone(wifi._recovery_latch)
            discard.assert_not_called()

            self.assertTrue(wifi._reconcile_recovery())

        self.assertIsNone(wifi._recovery_latch)
        discard.assert_called_once_with(net, 'uuid-home')

    def test_scan_waits_for_last_scan_to_advance(self):
        with mock.patch.object(wifi, '_device_dbus_path', return_value='/device/2'), \
                mock.patch.object(wifi, '_last_scan', side_effect=[100, 100, 101]), \
                mock.patch.object(wifi, '_run', return_value=(0, '', '')) as run, \
                mock.patch.object(wifi.time, 'sleep'):
            result = wifi._request_rescan_and_wait('wlan0')

        self.assertTrue(result[0])
        run.assert_called_once_with(
            ['device', 'wifi', 'rescan', 'ifname', 'wlan0'],
            timeout=15,
        )

    def test_disconnect_uses_active_uuid_not_ssid_or_device_down(self):
        net = wifi.Network(
            'Home',
            saved=True,
            active=True,
            iface='wlan0',
            profile_name='netplan-home',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('disconnect', net.ssid)
        states = [active(), wifi.ActiveConnection('wlan0', state='30 (disconnected)')]

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(wifi, 'active_connection', side_effect=states), \
                mock.patch.object(
                    wifi,
                    'saved_profiles',
                    return_value={'Home': [profile()]},
                ), \
                mock.patch.object(wifi, '_run', return_value=(0, '', '')) as run:
            wifi._disconnect_worker(job, net)

        self.assertTrue(job.ok)
        self.assertEqual(job.code, 'disconnected')
        self.assertEqual(job.message, 'Disconnected from Home')
        run.assert_called_once_with(
            ['connection', 'down', 'uuid', 'uuid-home'],
            timeout=30,
        )

    def test_disconnect_surfaces_not_authorized(self):
        net = wifi.Network(
            'Home',
            saved=True,
            active=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('disconnect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(wifi, 'active_connection', return_value=active()), \
                mock.patch.object(
                    wifi,
                    'saved_profiles',
                    return_value={'Home': [profile()]},
                ), \
                mock.patch.object(
                    wifi,
                    '_run',
                    return_value=(1, '', 'not authorized'),
                ):
            wifi._disconnect_worker(job, net)

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'not_authorized')
        self.assertEqual(job.message, 'Not authorized')
        self.assertTrue(net.active)

    def test_disconnect_does_not_claim_success_when_identity_is_unknown(self):
        net = wifi.Network('Home', active=True, iface='wlan0')
        job = wifi.WifiJob('disconnect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(wifi, 'active_connection', return_value=active()), \
                mock.patch.object(wifi, 'saved_profiles', return_value={}):
            wifi._disconnect_worker(job, net)

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'identity_failed')
        self.assertTrue(net.active)

    def test_disconnect_state_query_failure_is_not_success(self):
        net = wifi.Network(
            'Home',
            active=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('disconnect', net.ssid)
        failed_state = wifi.ActiveConnection(
            'wlan0',
            query_ok=False,
            error_code='state_query_failed',
            error_message='Could not read Wi-Fi state',
        )

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    return_value=failed_state,
                ), \
                mock.patch.object(wifi, '_run') as run:
            wifi._disconnect_worker(job, net)

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'state_query_failed')
        self.assertTrue(net.active)
        run.assert_not_called()

    def test_disconnect_verification_recovers_after_invalid_state_read(self):
        net = wifi.Network(
            'Home',
            active=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('disconnect', net.ssid)
        failed_state = wifi.ActiveConnection(
            'wlan0',
            query_ok=False,
            error_code='state_query_failed',
            error_message='Could not read Wi-Fi state',
        )
        states = [
            active(),
            failed_state,
            wifi.ActiveConnection('wlan0', state='30 (disconnected)'),
        ]

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    side_effect=states,
                ), \
                mock.patch.object(
                    wifi,
                    '_run',
                    return_value=(0, '', ''),
                ), \
                mock.patch.object(wifi.time, 'sleep'):
            wifi._disconnect_worker(job, net)

        self.assertTrue(job.ok)
        self.assertFalse(net.active)

    def test_disconnect_verification_rejects_only_invalid_state_reads(self):
        net = wifi.Network(
            'Home',
            active=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('disconnect', net.ssid)
        failed_state = wifi.ActiveConnection(
            'wlan0',
            query_ok=False,
            error_code='state_query_failed',
            error_message='Could not read Wi-Fi state',
        )

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    side_effect=[active(), failed_state],
                ), \
                mock.patch.object(
                    wifi,
                    '_run',
                    return_value=(0, '', ''),
                ), \
                mock.patch.object(
                    wifi.time,
                    'monotonic',
                    side_effect=[0.0, 0.0, 11.0],
                ), \
                mock.patch.object(wifi.time, 'sleep'):
            wifi._disconnect_worker(job, net)

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'state_query_failed')
        self.assertTrue(net.active)

    def test_saved_connection_uses_profile_uuid(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(0, '', ''),
                ) as activate_profile, \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-home', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_enable_profile_autoconnect',
                    return_value=(0, '', ''),
                ) as enable_autoconnect:
            wifi._connect_worker(job, net, None)

        self.assertTrue(job.ok)
        activate_profile.assert_called_once_with(
            'uuid-home',
            'wlan0',
            password=None,
        )
        enable_autoconnect.assert_not_called()

    def test_saved_authentication_failure_requests_new_password(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(
                        1,
                        '',
                        'Secrets were required, but not provided',
                    ),
                ):
            wifi._connect_worker(job, net, None)

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'authentication_failed')
        self.assertTrue(job.needs_password)

    def test_authentication_detail_wins_over_generic_timeout_text(self):
        code, message = wifi._classify_error(
            3,
            '',
            'Timeout expired: Secrets were required, but not provided',
        )

        self.assertEqual(code, 'authentication_failed')
        self.assertEqual(message, 'Wrong password')

    def test_specific_terminal_errors_win_over_generic_timeout_text(self):
        cases = [
            (
                'IP configuration could not be reserved (timeout)',
                'dhcp_failed',
                'Could not obtain IP',
            ),
            (
                'No network with SSID found before timeout',
                'network_not_found',
                'Network not found',
            ),
        ]
        for detail, expected_code, expected_message in cases:
            with self.subTest(detail=detail):
                code, message = wifi._classify_error(3, '', detail)
                self.assertEqual(code, expected_code)
                self.assertEqual(message, expected_message)

    def test_authentication_text_on_nmcli_timeout_still_cancels_activation(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(
                        3,
                        '',
                        'Timeout expired: Secrets were required, but not provided',
                    ),
                ), \
                mock.patch.object(
                    wifi,
                    '_cancel_activation',
                    return_value=True,
                ) as cancel:
            wifi._connect_worker(job, net, None)

        cancel.assert_called_once_with('uuid-home', 'wlan0')
        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'authentication_failed')
        self.assertTrue(job.needs_password)

    def test_profile_activation_uses_bounded_nmcli_wait(self):
        with mock.patch.object(
                wifi,
                '_run',
                return_value=(0, '', ''),
        ) as run:
            wifi._activate_profile('uuid-home', 'wlan0')

        run.assert_called_once_with(
            [
                '--wait',
                str(wifi._NMCLI_ACTIVATE_WAIT_SEC),
                'connection',
                'up',
                'uuid',
                'uuid-home',
                'ifname',
                'wlan0',
            ],
            timeout=wifi._NMCLI_ACTIVATE_TIMEOUT_SEC,
        )
        self.assertLess(
            wifi._NMCLI_ACTIVATE_WAIT_SEC,
            wifi._NMCLI_ACTIVATE_TIMEOUT_SEC,
        )

    def test_new_profile_is_created_noninteractively_without_password(self):
        net = wifi.Network(
            'Home',
            security='WPA2 WPA3',
            bssid='AA:BB:CC:DD:EE:FF',
            iface='wlan0',
        )
        password = 'special:$ pass'

        def fake_run(args, timeout=20, input_text=None):
            if 'add' in args:
                return 0, '', ''
            self.fail(f'unexpected nmcli args: {args}')

        with mock.patch.object(
                wifi,
                '_new_profile_name',
                return_value='Home (BART test)',
        ), mock.patch.object(
                wifi.uuidlib,
                'uuid4',
                return_value='uuid-new',
        ), mock.patch.object(wifi, '_run', side_effect=fake_run) as run:
            rc, _, _, profile_uuid = wifi._create_profile(net, 'wlan0')

        self.assertEqual(rc, 0)
        self.assertEqual(profile_uuid, 'uuid-new')
        self.assertTrue(net.saved)
        self.assertEqual(net.profile_name, 'Home (BART test)')
        self.assertEqual(net.profile_uuid, 'uuid-new')
        add_args = run.call_args_list[0].args[0]
        self.assertEqual(
            add_args[:3],
            ['--wait', str(wifi._NMCLI_PROFILE_WAIT_SEC), 'connection'],
        )
        self.assertIn('connection.autoconnect', add_args)
        self.assertEqual(
            add_args[add_args.index('connection.uuid') + 1],
            'uuid-new',
        )
        self.assertEqual(
            add_args[add_args.index('connection.autoconnect') + 1],
            'no',
        )
        self.assertIn('wifi-sec.key-mgmt', add_args)
        self.assertEqual(
            add_args[add_args.index('wifi-sec.key-mgmt') + 1],
            'wpa-psk',
        )
        self.assertEqual(
            add_args[add_args.index('wifi-sec.psk-flags') + 1],
            '0',
        )
        self.assertNotIn('--ask', add_args)
        self.assertNotIn('bssid', add_args)
        self.assertNotIn(password, add_args)
        self.assertLess(
            wifi._NMCLI_PROFILE_WAIT_SEC,
            wifi._NMCLI_PROFILE_TIMEOUT_SEC,
        )

    def test_profile_creation_failure_does_not_attempt_activation(self):
        net = wifi.Network('Home', security='WPA2', iface='wlan0')
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery(),
                ), \
                mock.patch.object(
                    wifi,
                    '_create_profile',
                    return_value=(1, '', 'not authorized', ''),
                ), \
                mock.patch.object(wifi, '_activate_profile') as activate:
            wifi._connect_worker(job, net, 'password')

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'not_authorized')
        activate.assert_not_called()

    def test_ambiguous_profile_creation_is_removed_by_caller_uuid(self):
        net = wifi.Network('Home', security='WPA2', iface='wlan0')

        with mock.patch.object(
                wifi,
                '_new_profile_name',
                return_value='Home (BART test)',
        ), mock.patch.object(
                wifi.uuidlib,
                'uuid4',
                return_value='uuid-new',
        ), mock.patch.object(
                wifi,
                '_run',
                return_value=(3, '', 'Timeout expired'),
        ), mock.patch.object(
                wifi,
                '_profile_exists',
                return_value=True,
        ), mock.patch.object(
                wifi,
                '_discard_new_profile',
                return_value=True,
        ) as discard:
            rc, _, _, profile_uuid = wifi._create_profile(net, 'wlan0')

        self.assertNotEqual(rc, 0)
        self.assertEqual(profile_uuid, '')
        self.assertFalse(net.saved)
        discard.assert_called_once_with(None, 'uuid-new')

    def test_ambiguous_profile_creation_blocks_later_operations_when_unknown(self):
        net = wifi.Network('Home', security='WPA2', iface='wlan0')

        with mock.patch.object(
                wifi,
                '_new_profile_name',
                return_value='Home (BART test)',
        ), mock.patch.object(
                wifi.uuidlib,
                'uuid4',
                return_value='uuid-new',
        ), mock.patch.object(
                wifi,
                '_run',
                return_value=(3, '', 'Timeout expired'),
        ), mock.patch.object(
                wifi,
                '_profile_exists',
                return_value=None,
        ):
            rc, _, _, profile_uuid = wifi._create_profile(net, 'wlan0')

        self.assertNotEqual(rc, 0)
        self.assertEqual(profile_uuid, '')
        self.assertIsNotNone(wifi._recovery_latch)
        self.assertEqual(wifi._recovery_latch.profile_uuid, 'uuid-new')
        self.assertTrue(wifi._recovery_latch.delete_profile)

    def test_wpa3_only_profile_uses_sae_but_mixed_uses_wpa_psk(self):
        self.assertEqual(
            wifi._wifi_key_mgmt(wifi.Network('WPA3', security='WPA3')),
            'sae',
        )
        self.assertEqual(
            wifi._wifi_key_mgmt(
                wifi.Network('Mixed', security='WPA2 WPA3')
            ),
            'wpa-psk',
        )
        self.assertEqual(
            wifi._wifi_key_mgmt(wifi.Network('WPA2', security='WPA2')),
            'wpa-psk',
        )

    def test_replacement_password_uses_private_temporary_file(self):
        password = 'special:$ pass'
        observed = {}

        def fake_run(args, timeout=20, input_text=None):
            path = args[args.index('passwd-file') + 1]
            observed['args'] = list(args)
            observed['mode'] = os.stat(path).st_mode & 0o777
            with open(path, encoding='utf-8') as secret_file:
                observed['contents'] = secret_file.read()
            return 0, '', ''

        with mock.patch.object(wifi, '_run', side_effect=fake_run):
            wifi._activate_profile(
                'uuid-home',
                'wlan0',
                password,
            )

        self.assertNotIn(password, observed['args'])
        if os.name == 'posix':
            self.assertEqual(observed['mode'], 0o600)
        self.assertEqual(
            observed['contents'],
            '802-11-wireless-security.psk:' + password + '\n',
        )
        path = observed['args'][observed['args'].index('passwd-file') + 1]
        self.assertFalse(os.path.exists(path))

    def test_failed_new_authentication_discards_profile_before_retry(self):
        net = wifi.Network('Home', security='WPA2', iface='wlan0')
        first_job = wifi.WifiJob('connect', net.ssid)
        retry_job = wifi.WifiJob('connect', net.ssid)
        profile_uuids = iter(['uuid-new-1', 'uuid-new-2'])

        def create_profile(net_arg, iface):
            profile_uuid = next(profile_uuids)
            net_arg.saved = True
            net_arg.profile_name = 'Home (BART test)'
            net_arg.profile_uuid = profile_uuid
            return 0, '', '', profile_uuid

        def discard_profile(net_arg, profile_uuid):
            net_arg.saved = False
            net_arg.profile_name = ''
            net_arg.profile_uuid = ''
            return True

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery(),
                ), \
                mock.patch.object(
                    wifi,
                    '_create_profile',
                    side_effect=create_profile,
                ) as create, \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    side_effect=[
                        (
                            1,
                            '',
                            'Secrets were required, but not provided',
                        ),
                        (0, '', ''),
                    ],
                ) as activate_profile, \
                mock.patch.object(
                    wifi,
                    '_discard_new_profile',
                    side_effect=discard_profile,
                ) as discard, \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-new-2', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_enable_profile_autoconnect',
                    return_value=(0, '', ''),
                ) as enable_autoconnect:
            wifi._connect_worker(first_job, net, 'wrong password')
            wifi._connect_worker(retry_job, net, 'correct password')

        self.assertFalse(first_job.ok)
        self.assertTrue(first_job.needs_password)
        self.assertTrue(retry_job.ok)
        self.assertEqual(net.profile_uuid, 'uuid-new-2')
        self.assertEqual(create.call_count, 2)
        discard.assert_called_once_with(net, 'uuid-new-1')
        self.assertEqual(
            activate_profile.call_args_list,
            [
                mock.call(
                    'uuid-new-1',
                    'wlan0',
                    password='wrong password',
                ),
                mock.call(
                    'uuid-new-2',
                    'wlan0',
                    password='correct password',
                ),
            ],
        )
        enable_autoconnect.assert_called_once_with('uuid-new-2')

    def test_saved_authentication_retry_reuses_profile_uuid(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        first_job = wifi.WifiJob('connect', net.ssid)
        retry_job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(wifi, '_create_profile') as create, \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    side_effect=[
                        (
                            1,
                            '',
                            'Secrets were required, but not provided',
                        ),
                        (0, '', ''),
                    ],
                ) as activate_profile, \
                mock.patch.object(
                    wifi,
                    '_prepare_saved_psk',
                    return_value=(0, '', '', True),
                ) as prepare_saved, \
                mock.patch.object(
                    wifi,
                    '_restore_saved_autoconnect',
                    return_value=(0, '', ''),
                ) as restore_autoconnect, \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-home', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_enable_profile_autoconnect',
                ) as enable_autoconnect:
            wifi._connect_worker(first_job, net, 'wrong password')
            wifi._connect_worker(retry_job, net, 'correct password')

        self.assertFalse(first_job.ok)
        self.assertTrue(first_job.needs_password)
        self.assertTrue(retry_job.ok)
        create.assert_not_called()
        self.assertEqual(
            activate_profile.call_args_list,
            [
                mock.call(
                    'uuid-home',
                    'wlan0',
                    password='wrong password',
                ),
                mock.call(
                    'uuid-home',
                    'wlan0',
                    password='correct password',
                ),
            ],
        )
        self.assertEqual(
            prepare_saved.call_args_list,
            [
                mock.call('uuid-home'),
                mock.call('uuid-home'),
            ],
        )
        restore_autoconnect.assert_called_once_with('uuid-home')
        enable_autoconnect.assert_not_called()

    def test_saved_replacement_blocks_autoconnect_without_exposing_secret(self):
        with mock.patch.object(
                wifi,
                '_profile_autoconnect',
                return_value=(0, 'yes\n', '', True),
        ), mock.patch.object(
                wifi,
                '_profile_autoconnect_marker',
                return_value=(0, '', '', None),
        ), mock.patch.object(
                wifi,
                '_run',
                return_value=(0, '', ''),
        ) as run:
            result = wifi._prepare_saved_psk('uuid-home')

        self.assertEqual(result, (0, '', '', True))
        run.assert_called_once_with(
            [
                '--wait',
                str(wifi._NMCLI_PROFILE_WAIT_SEC),
                'connection',
                'modify',
                'uuid',
                'uuid-home',
                'connection.autoconnect',
                'no',
                'wifi-sec.psk-flags',
                '0',
                'wifi-sec.psk',
                '',
                '+user.data',
                'org.bartdisplay.autoconnect-original=yes',
            ],
            timeout=wifi._NMCLI_PROFILE_TIMEOUT_SEC,
        )

    def test_saved_replacement_restores_original_autoconnect_after_verification(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
            autoconnect=True,
        )
        job = wifi.WifiJob('connect', net.ssid)
        events = []

        def prepare(*args):
            events.append('prepare')
            return 0, '', '', True

        def activate(*args, **kwargs):
            events.append('activate')
            return 0, '', ''

        def verify(*args, **kwargs):
            events.append('verify')
            return True, 'uuid-home', ''

        def restore(*args):
            events.append('restore')
            return 0, '', ''

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    '_prepare_saved_psk',
                    side_effect=prepare,
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    side_effect=activate,
                ), \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    side_effect=verify,
                ), \
                mock.patch.object(
                    wifi,
                    '_restore_saved_autoconnect',
                    side_effect=restore,
                ):
            wifi._connect_worker(job, net, 'replacement')

        self.assertTrue(job.ok)
        self.assertEqual(events, ['prepare', 'activate', 'verify', 'restore'])
        self.assertTrue(net.autoconnect)

    def test_saved_replacement_retry_retains_original_autoconnect_preference(self):
        with mock.patch.object(
                wifi,
                '_profile_autoconnect',
                side_effect=[
                    (0, 'yes\n', '', True),
                    (0, 'no\n', '', False),
                ],
        ), mock.patch.object(
                wifi,
                '_run',
                return_value=(0, '', ''),
        ):
            first = wifi._prepare_saved_psk('uuid-home')
            second = wifi._prepare_saved_psk('uuid-home')

        self.assertTrue(first[3])
        self.assertTrue(second[3])

    def test_autoconnect_recovery_marker_survives_process_restart(self):
        wifi._autoconnect_restore.clear()

        with mock.patch.object(
                wifi,
                '_profile_autoconnect_marker',
                return_value=(0, '', '', True),
        ), mock.patch.object(
                wifi,
                '_run',
                return_value=(0, '', ''),
        ) as run:
            result = wifi._restore_saved_autoconnect('uuid-home')

        self.assertEqual(result, (0, '', ''))
        run.assert_called_once_with(
            [
                '--wait',
                str(wifi._NMCLI_PROFILE_WAIT_SEC),
                'connection',
                'modify',
                'uuid',
                'uuid-home',
                'connection.autoconnect',
                'yes',
                '-user.data',
                wifi._AUTOCONNECT_MARKER,
            ],
            timeout=wifi._NMCLI_PROFILE_TIMEOUT_SEC,
        )
        self.assertNotIn('uuid-home', wifi._autoconnect_restore)

    def test_later_passwordless_success_restores_pending_autoconnect(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
            autoconnect=False,
        )
        job = wifi.WifiJob('connect', net.ssid)
        wifi._autoconnect_restore['uuid-home'] = True

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({
                        'Home': [
                            wifi.SavedProfile(
                                'Home',
                                'netplan-home',
                                'uuid-home',
                                autoconnect=False,
                            ),
                        ],
                    }),
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(0, '', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-home', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_restore_saved_autoconnect',
                    return_value=(0, '', ''),
                ) as restore:
            wifi._connect_worker(job, net, None)

        self.assertTrue(job.ok)
        restore.assert_called_once_with('uuid-home')
        self.assertTrue(net.autoconnect)

    def test_saved_replacement_permission_failure_does_not_activate(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    '_prepare_saved_psk',
                    return_value=(1, '', 'not authorized', None),
                ), \
                mock.patch.object(wifi, '_activate_profile') as activate:
            wifi._connect_worker(job, net, 'replacement')

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'not_authorized')
        activate.assert_not_called()

    def test_profile_autoconnect_is_enabled_only_after_verification(self):
        net = wifi.Network('Home', security='WPA2', iface='wlan0')
        job = wifi.WifiJob('connect', net.ssid)
        events = []

        def create_profile(net_arg, iface):
            events.append('create')
            net_arg.saved = True
            net_arg.profile_name = 'Home (BART test)'
            net_arg.profile_uuid = 'uuid-new'
            return 0, '', '', 'uuid-new'

        def activate_profile(*args, **kwargs):
            events.append('activate')
            return 0, '', ''

        def verify_connected(*args, **kwargs):
            events.append('verify')
            return True, 'uuid-new', ''

        def enable_autoconnect(*args, **kwargs):
            events.append('autoconnect')
            return 0, '', ''

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery(),
                ), \
                mock.patch.object(
                    wifi,
                    '_create_profile',
                    side_effect=create_profile,
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    side_effect=activate_profile,
                ), \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    side_effect=verify_connected,
                ), \
                mock.patch.object(
                    wifi,
                    '_enable_profile_autoconnect',
                    side_effect=enable_autoconnect,
                ):
            wifi._connect_worker(job, net, 'password')

        self.assertTrue(job.ok)
        self.assertEqual(
            events,
            ['create', 'activate', 'verify', 'autoconnect'],
        )

    def test_autoconnect_failure_is_visible_after_successful_connection(self):
        net = wifi.Network('Home', security='WPA2', iface='wlan0')
        job = wifi.WifiJob('connect', net.ssid)

        def create_profile(net_arg, iface):
            net_arg.saved = True
            net_arg.profile_name = 'Home (BART test)'
            net_arg.profile_uuid = 'uuid-new'
            return 0, '', '', 'uuid-new'

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery(),
                ), \
                mock.patch.object(
                    wifi,
                    '_create_profile',
                    side_effect=create_profile,
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(0, '', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-new', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_enable_profile_autoconnect',
                    return_value=(1, '', 'not authorized'),
                ):
            wifi._connect_worker(job, net, 'password')

        self.assertTrue(job.ok)
        self.assertEqual(job.code, 'connected_warning')
        self.assertEqual(
            job.message,
            'Connected to Home; auto-reconnect unavailable',
        )

    def test_cancel_activation_retries_until_device_leaves_profile(self):
        with mock.patch.object(
                wifi,
                '_run',
                return_value=(1, '', 'still activating'),
        ) as run, mock.patch.object(
                wifi,
                '_wait_activation_stopped',
                side_effect=[False, True],
        ) as wait_stopped:
            stopped = wifi._cancel_activation('uuid-home', 'wlan0')

        self.assertTrue(stopped)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            wait_stopped.call_args_list,
            [
                mock.call('uuid-home', 'wlan0'),
                mock.call('uuid-home', 'wlan0'),
            ],
        )

    def test_protected_activation_timeout_is_cancelled_and_reprompts(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    return_value=wifi.ActiveConnection(
                        'wlan0',
                        state='60 (need authentication)',
                        uuid='uuid-home',
                    ),
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(3, '', 'Timeout expired (45 seconds)'),
                ), \
                mock.patch.object(
                    wifi,
                    '_cancel_activation',
                    return_value=True,
                ) as cancel:
            wifi._connect_worker(job, net, None)

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'authentication_timeout')
        self.assertEqual(job.message, 'Authentication timed out')
        self.assertTrue(job.needs_password)
        cancel.assert_called_once_with('uuid-home', 'wlan0')
        self.assertIn('uuid-home', wifi._reauth_required)

        # If the user closes the password keyboard, another Connect tap must
        # reopen it immediately instead of retrying the rejected stored PSK.
        retry = wifi.WifiJob('connect', net.ssid)
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    return_value=wifi.ActiveConnection(
                        'wlan0',
                        state='30 (disconnected)',
                    ),
                ), \
                mock.patch.object(wifi, '_activate_profile') as activate:
            wifi._connect_worker(retry, net, None)

        self.assertFalse(retry.ok)
        self.assertEqual(retry.code, 'authentication_timeout')
        self.assertTrue(retry.needs_password)
        activate.assert_not_called()

    def test_wifi_association_timeout_reprompts_for_password(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    side_effect=[
                        wifi.ActiveConnection('wlan0', state='30 (disconnected)'),
                        wifi.ActiveConnection(
                            'wlan0',
                            state='50 (connecting)',
                            uuid='uuid-home',
                            reason='0 (No reason given)',
                        ),
                    ],
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(3, '', 'Timeout expired (45 seconds)'),
                ), \
                mock.patch.object(
                    wifi,
                    '_cancel_activation',
                    return_value=True,
                ) as cancel:
            wifi._connect_worker(job, net, None)

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'authentication_timeout')
        self.assertTrue(job.needs_password)
        cancel.assert_called_once_with('uuid-home', 'wlan0')

    def test_supplicant_failure_reason_is_authentication_stage(self):
        with mock.patch.object(
                wifi,
                'active_connection',
                return_value=wifi.ActiveConnection(
                    'wlan0',
                    state='30 (disconnected)',
                    uuid='uuid-home',
                    reason='9 (Supplicant configuration failed)',
                ),
        ):
            stage = wifi._activation_timeout_stage(
                'wlan0',
                'uuid-home',
                protected=True,
            )

        self.assertEqual(stage, 'authentication')

    def test_activation_timeout_that_finishes_with_ip_is_salvaged(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    side_effect=[
                        wifi.ActiveConnection('wlan0', state='30 (disconnected)'),
                        wifi.ActiveConnection(
                            'wlan0',
                            state='100 (connected)',
                            uuid='uuid-home',
                            reason='0 (No reason given)',
                        ),
                    ],
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(3, '', 'Timeout expired (45 seconds)'),
                ), \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-home', ''),
                ) as verify, \
                mock.patch.object(
                    wifi,
                    '_cancel_activation',
                ) as cancel:
            wifi._connect_worker(job, net, None)

        self.assertTrue(job.ok)
        self.assertEqual(job.code, 'connected')
        verify.assert_called_once_with(
            'wlan0',
            expected_uuid='uuid-home',
            expected_ssid='Home',
        )
        cancel.assert_not_called()

    def test_ip_configuration_timeout_does_not_reprompt_for_password(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    side_effect=[
                        wifi.ActiveConnection('wlan0', state='30 (disconnected)'),
                        wifi.ActiveConnection(
                            'wlan0',
                            state='70 (connecting)',
                            uuid='uuid-home',
                            reason='0 (No reason given)',
                        ),
                    ],
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(3, '', 'Timeout expired (45 seconds)'),
                ), \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(
                        False,
                        '',
                        'Connection activated without an IPv4 address',
                    ),
                ), \
                mock.patch.object(
                    wifi,
                    '_cancel_activation',
                    return_value=True,
                ):
            wifi._connect_worker(job, net, None)

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'dhcp_failed')
        self.assertEqual(job.message, 'Could not obtain IP')
        self.assertFalse(job.needs_password)

    def test_replacement_password_reaches_ip_and_clears_reauth_requirement(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        wifi._reauth_required.add('uuid-home')
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    side_effect=[
                        wifi.ActiveConnection('wlan0', state='30 (disconnected)'),
                        wifi.ActiveConnection(
                            'wlan0',
                            state='70 (connecting)',
                            uuid='uuid-home',
                            reason='0 (No reason given)',
                        ),
                    ],
                ), \
                mock.patch.object(
                    wifi,
                    '_prepare_saved_psk',
                    return_value=(0, '', '', True),
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(3, '', 'Timeout expired (45 seconds)'),
                ), \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(
                        False,
                        '',
                        'Connection activated without an IPv4 address',
                    ),
                ), \
                mock.patch.object(
                    wifi,
                    '_cancel_activation',
                    return_value=True,
                ):
            wifi._connect_worker(job, net, 'correct-password')

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'dhcp_failed')
        self.assertFalse(job.needs_password)
        self.assertNotIn('uuid-home', wifi._reauth_required)

        retry = wifi.WifiJob('connect', net.ssid)
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    return_value=wifi.ActiveConnection(
                        'wlan0',
                        state='30 (disconnected)',
                    ),
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(1, '', 'Network unavailable'),
                ) as activate:
            wifi._connect_worker(retry, net, None)

        activate.assert_called_once_with('uuid-home', 'wlan0', password=None)
        self.assertEqual(retry.code, 'connect_failed')
        self.assertFalse(retry.needs_password)

    def test_failed_activation_cancellation_blocks_password_retry(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    return_value=wifi.ActiveConnection(
                        'wlan0',
                        state='60 (need authentication)',
                        uuid='uuid-home',
                    ),
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(3, '', 'Timeout expired (45 seconds)'),
                ), \
                mock.patch.object(
                    wifi,
                    '_cancel_activation',
                    return_value=False,
                ):
            wifi._connect_worker(job, net, None)

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'cleanup_failed')
        self.assertEqual(job.message, 'Could not stop connection attempt')
        self.assertFalse(job.needs_password)
        self.assertIsNotNone(wifi._recovery_latch)
        self.assertEqual(wifi._recovery_latch.profile_uuid, 'uuid-home')
        self.assertTrue(wifi._recovery_latch.reauth_required)

    def test_recovered_timeout_prompts_before_retrying_stale_credentials(self):
        net = wifi.Network(
            'Home',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        wifi._set_recovery_latch(
            'wlan0',
            'uuid-home',
            reason='activation failure cleanup',
            reauth_required=True,
        )

        with mock.patch.object(
                wifi,
                '_cancel_activation',
                return_value=True,
        ):
            self.assertTrue(wifi._reconcile_recovery())

        job = wifi.WifiJob('connect', net.ssid)
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery({'Home': [profile()]}),
                ), \
                mock.patch.object(wifi, '_activate_profile') as activate:
            wifi._connect_worker(job, net, None)

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'authentication_timeout')
        self.assertTrue(job.needs_password)
        activate.assert_not_called()

    def test_failed_ip_verification_cancels_and_discards_new_profile(self):
        net = wifi.Network('Home', security='WPA2', iface='wlan0')
        job = wifi.WifiJob('connect', net.ssid)

        def create_profile(net_arg, iface):
            net_arg.saved = True
            net_arg.profile_name = 'Home (BART test)'
            net_arg.profile_uuid = 'uuid-new'
            return 0, '', '', 'uuid-new'

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=discovery(),
                ), \
                mock.patch.object(
                    wifi,
                    '_create_profile',
                    side_effect=create_profile,
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(0, '', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(False, '', 'No IPv4 address'),
                ), \
                mock.patch.object(
                    wifi,
                    '_cancel_activation',
                    return_value=True,
                ) as cancel, \
                mock.patch.object(
                    wifi,
                    '_discard_new_profile',
                    return_value=True,
                ) as discard:
            wifi._connect_worker(job, net, 'password')

        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'dhcp_failed')
        cancel.assert_called_once_with('uuid-new', 'wlan0')
        discard.assert_called_once_with(net, 'uuid-new')

    def test_enterprise_and_wep_networks_are_unsupported(self):
        self.assertFalse(wifi.Network('Corp', security='WPA2 802.1X').supported)
        self.assertFalse(wifi.Network('Legacy', security='WEP').supported)
        self.assertTrue(wifi.Network('Home', security='WPA2 WPA3').supported)
        self.assertTrue(wifi.Network('Guest', security='--').supported)


if __name__ == '__main__':
    unittest.main()
