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


class WifiTests(unittest.TestCase):
    def setUp(self):
        wifi._scan_cache.clear()

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
                mock.patch.object(wifi, '_run', return_value=(0, '', '')) as run, \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-home', ''),
                ):
            wifi._connect_worker(job, net, None)

        self.assertTrue(job.ok)
        run.assert_called_once_with(
            [
                'connection',
                'up',
                'uuid',
                'uuid-home',
                'ifname',
                'wlan0',
            ],
            timeout=60,
        )

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
                    '_run',
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

    def test_new_connection_password_is_passed_on_stdin_not_argv(self):
        net = wifi.Network(
            'Home',
            security='WPA2 WPA3',
            bssid='AA:BB:CC:DD:EE:FF',
            iface='wlan0',
        )
        password = 'special:$ pass'

        with mock.patch.object(wifi, '_run', return_value=(0, '', '')) as run:
            wifi._connect_new(net, 'wlan0', password)

        args, kwargs = run.call_args
        self.assertNotIn(password, args[0])
        self.assertNotIn('bssid', args[0])
        self.assertEqual(kwargs['input_text'], password + '\n')
        self.assertEqual(args[0][0:4], ['--ask', 'device', 'wifi', 'connect'])

    def test_saved_replacement_password_uses_private_temporary_file(self):
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
            wifi._connection_up_with_password(
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

    def test_enterprise_and_wep_networks_are_unsupported(self):
        self.assertFalse(wifi.Network('Corp', security='WPA2 802.1X').supported)
        self.assertFalse(wifi.Network('Legacy', security='WEP').supported)
        self.assertTrue(wifi.Network('Home', security='WPA2 WPA3').supported)
        self.assertTrue(wifi.Network('Guest', security='--').supported)


if __name__ == '__main__':
    unittest.main()
