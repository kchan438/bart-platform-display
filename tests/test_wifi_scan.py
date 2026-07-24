import unittest
from unittest import mock

from bartdisplay import wifi


class WifiScanTests(unittest.TestCase):
    def test_parse_scan_output_deduplicates_and_unescapes_ssids(self):
        out = '\n'.join([
            r':45:WPA2:Home\:Lab',
            r':82:WPA2:Home\:Lab',
            ':30:--:Guest',
        ])

        nets = wifi._parse_scan_output(out, {'Guest'})

        self.assertEqual([n.ssid for n in nets], ['Home:Lab', 'Guest'])
        self.assertEqual(nets[0].signal, 82)
        self.assertFalse(nets[0].saved)
        self.assertTrue(nets[1].saved)
        self.assertFalse(nets[1].protected)

    def test_parse_scan_output_preserves_active_duplicate(self):
        out = '\n'.join([
            '*:30:WPA2:Home',
            ':80:WPA2:Home',
        ])

        nets = wifi._parse_scan_output(out, set())

        self.assertEqual(len(nets), 1)
        self.assertEqual(nets[0].ssid, 'Home')
        self.assertEqual(nets[0].signal, 80)
        self.assertTrue(nets[0].active)

    def test_scan_falls_back_after_empty_rescan(self):
        calls = []

        def fake_run(args, timeout=20):
            calls.append(tuple(args))
            if args == ['-t', '-f', 'WIFI', 'radio']:
                return 0, 'enabled\n', ''
            if args == ['-t', '-f', 'DEVICE,TYPE', 'device']:
                return 0, 'wlan0:wifi\n', ''
            if args == ['-t', '-f', 'NAME,TYPE', 'connection', 'show']:
                return 0, 'Home:802-11-wireless\n', ''
            if args[:len(wifi._WIFI_LIST_FIELDS)] == wifi._WIFI_LIST_FIELDS:
                if '--rescan' in args:
                    rescan = args[args.index('--rescan') + 1]
                    if rescan == 'yes':
                        return 0, '', ''
                if '--rescan' in args and args[args.index('--rescan') + 1] == 'no':
                    return 0, '*:91:WPA2:Home\n:44:--:Guest\n', ''
                return 0, '', ''
            if args[:3] == ['device', 'wifi', 'rescan']:
                return 0, '', ''
            self.fail(f'unexpected nmcli args: {args}')

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(wifi, '_run', side_effect=fake_run), \
                mock.patch.object(wifi.time, 'sleep'):
            nets = wifi.scan(rescan=True)

        self.assertEqual([n.ssid for n in nets], ['Home', 'Guest'])
        self.assertTrue(nets[0].active)
        self.assertTrue(nets[0].saved)
        self.assertIn(
            tuple(wifi._WIFI_LIST_FIELDS + ['--rescan', 'yes']),
            calls,
        )
        self.assertIn(
            tuple(wifi._WIFI_LIST_FIELDS + ['--rescan', 'no']),
            calls,
        )

    def test_scan_enables_disabled_radio_before_listing(self):
        calls = []

        def fake_run(args, timeout=20):
            calls.append(tuple(args))
            if args == ['-t', '-f', 'WIFI', 'radio']:
                return 0, 'disabled\n', ''
            if args == ['radio', 'wifi', 'on']:
                return 0, '', ''
            if args == ['-t', '-f', 'DEVICE,TYPE', 'device']:
                return 0, 'wlan0:wifi\n', ''
            if args == ['-t', '-f', 'NAME,TYPE', 'connection', 'show']:
                return 0, '', ''
            if args[:len(wifi._WIFI_LIST_FIELDS)] == wifi._WIFI_LIST_FIELDS:
                return 0, ':70:WPA2:Home\n', ''
            self.fail(f'unexpected nmcli args: {args}')

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(wifi, '_run', side_effect=fake_run), \
                mock.patch.object(wifi.time, 'sleep'):
            nets = wifi.scan(rescan=True)

        self.assertEqual([n.ssid for n in nets], ['Home'])
        self.assertIn(('radio', 'wifi', 'on'), calls)

    def test_connect_with_password_passes_password_to_nmcli(self):
        calls = []

        def fake_run(args, timeout=20):
            calls.append(tuple(args))
            return 0, 'success', ''

        net = wifi.Network('Home', security='WPA2')
        job = wifi.ConnectJob(net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(wifi, '_run', side_effect=fake_run):
            wifi._connect_worker(job, net, 'secret123')

        self.assertTrue(job.done)
        self.assertTrue(job.ok)
        self.assertEqual(job.status, 'Connected')
        self.assertEqual(calls, [
            ('dev', 'wifi', 'connect', 'Home', 'password', 'secret123'),
        ])

    def test_connect_wrong_password_sets_wrong_password_status(self):
        def fake_run(args, timeout=20):
            return 1, '', 'secrets were required, but not provided'

        net = wifi.Network('Home', security='WPA2')
        job = wifi.ConnectJob(net.ssid)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(wifi, '_run', side_effect=fake_run):
            wifi._connect_worker(job, net, 'bad-password')

        self.assertTrue(job.done)
        self.assertFalse(job.ok)
        self.assertEqual(job.status, 'Wrong password')


if __name__ == '__main__':
    unittest.main()
