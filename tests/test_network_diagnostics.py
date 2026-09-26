import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

import requests

from bartdisplay import departures, network_diagnostics as diagnostics, wifi


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.recorder = diagnostics.Recorder(self.directory)
        self.patch = mock.patch.object(diagnostics, '_recorder', self.recorder)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.recorder.close()
        self.temp.cleanup()

    def records(self):
        return [json.loads(line) for line in
                (self.directory / 'network.jsonl').read_text().splitlines()]

    def test_start_is_automatic_on_linux_and_idempotent(self):
        other = self.directory / 'startup'
        with mock.patch.object(diagnostics, '_recorder', None), \
                mock.patch.object(diagnostics, '_monitor', None), \
                mock.patch.object(diagnostics.sys, 'platform', 'linux'), \
                mock.patch.dict(diagnostics.os.environ, {'BART_DEV': '0'}), \
                mock.patch.object(diagnostics, 'log_directory', return_value=other), \
                mock.patch.object(diagnostics, 'RadioMonitor') as monitor:
            try:
                diagnostics.start()
                diagnostics.start()
                monitor.return_value.thread.start.assert_called_once()
            finally:
                diagnostics.stop()
            self.assertIsNone(diagnostics._recorder)
        records = [json.loads(line) for line in (other / 'network.jsonl').read_text().splitlines()]
        self.assertEqual([r['event'] for r in records], ['session_start', 'session_stop'])

    def test_start_failure_and_development_mode_do_not_break_app(self):
        with mock.patch.object(diagnostics, '_recorder', None), \
                mock.patch.object(diagnostics.sys, 'platform', 'linux'), \
                mock.patch.dict(diagnostics.os.environ, {'BART_DEV': '1'}), \
                mock.patch.object(diagnostics, 'Recorder') as recorder:
            diagnostics.start()
            recorder.assert_not_called()
        with mock.patch.object(diagnostics, '_recorder', None), \
                mock.patch.object(diagnostics.sys, 'platform', 'linux'), \
                mock.patch.dict(diagnostics.os.environ, {'BART_DEV': '0'}), \
                mock.patch.object(diagnostics, 'Recorder', side_effect=PermissionError), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            diagnostics.start()
            self.assertIsNone(diagnostics._recorder)
        self.assertIn('Cannot open persistent log', stderr.getvalue())

    def test_disconnect_and_recovery_survive_recorder_restart(self):
        supervisor = wifi.ConnectionSupervisor(grace_sec=10)
        supervisor.watch_connected('private-uuid', 'private-network', 'wlan0')
        lost = wifi.ActiveConnection('wlan0', state='30 (disconnected)', reason='53')
        restored = wifi.ActiveConnection('wlan0', state='100 (connected)',
                                         reason='0', uuid='private-uuid')
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(supervisor, '_observe', side_effect=[
                    (lost, '', 'ok', ''), (restored, '10.1.2.3/24', 'ok', '')]):
            supervisor.step(now=100)
            supervisor.step(now=105)
        self.recorder.close()
        self.recorder = diagnostics.Recorder(self.directory)
        self.recorder.record('session_start', pid=123)
        records = self.records()
        self.assertEqual([r['phase'] for r in records if r['event'] == 'wifi_supervisor'],
                         ['connected', 'grace', 'reconnected'])
        self.assertEqual([r['reason'] for r in records if r['event'] == 'wifi_observation'],
                         [53, 0])
        self.assertNotEqual(records[0]['session_id'], records[-1]['session_id'])
        text = (self.directory / 'network.jsonl').read_text()
        for secret in ['private-uuid', 'private-network', '10.1.2.3']:
            self.assertNotIn(secret, text)

    def test_startup_disconnected_is_recorded_without_known_profile(self):
        supervisor = wifi.ConnectionSupervisor()
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(supervisor, '_observe', return_value=(
                    wifi.ActiveConnection('wlan0', state='30', reason='7'), '', 'ok', '')):
            supervisor.step(now=100)
        self.assertEqual(self.records()[0]['reason'], 7)
        self.assertFalse(self.records()[0]['has_ipv4'])

    def test_operation_failure_does_not_store_credentials_or_message(self):
        job = wifi.WifiJob('connect', 'private-network')
        job.finish(False, 'password=secret123 private-network', code='authentication_failed')
        self.recorder.record('wifi_operation_end', operation='connect',
                             password='secret123', message='private-network', ok=False)
        text = (self.directory / 'network.jsonl').read_text()
        self.assertNotIn('secret123', text)
        self.assertNotIn('private-network', text)
        self.assertEqual(self.records()[1]['code'], 'authentication_failed')

    def test_observation_heartbeat_suppresses_duplicates_but_not_changes(self):
        with mock.patch.object(diagnostics.time, 'monotonic', side_effect=[0, 3, 6, 66]):
            for reason in [0, 0, 53, 53]:
                self.recorder.record('wifi_observation', heartbeat=60, reason=reason)
        self.assertEqual([r['reason'] for r in self.records()], [0, 53, 53])

    def test_rotation_stays_bounded_and_export_contains_only_logs(self):
        self.recorder.close()
        self.recorder = diagnostics.Recorder(self.directory, max_bytes=1024, backups=3)
        for i in range(40):
            self.recorder.record('wifi_observation', reason=i)
        (self.directory / 'config.json').write_text('secret-api-key')
        destination = self.directory / 'export.zip'
        diagnostics.export_logs(self.directory, destination)
        logs = list(self.directory.glob('network.jsonl*'))
        self.assertEqual(len(logs), 4)
        self.assertTrue(all(p.stat().st_size <= 1024 for p in logs))
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(set(archive.namelist()),
                             {p.name for p in logs} | {'README.txt'})
            for name in archive.namelist():
                self.assertNotIn(b'secret-api-key', archive.read(name))
        with self.assertRaises(FileExistsError):
            diagnostics.export_logs(self.directory, destination)

    def test_empty_export_is_actionable_and_does_not_create_archive(self):
        empty = self.directory / 'missing'
        destination = self.directory / 'export.zip'
        with self.assertRaisesRegex(FileNotFoundError, 'install and run'):
            diagnostics.export_logs(empty, destination)
        self.assertFalse(destination.exists())

    def test_disk_write_failure_does_not_interrupt_wifi_operation(self):
        with mock.patch.object(self.recorder.handler, 'shouldRollover',
                               side_effect=OSError('disk full')), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            job = wifi.WifiJob('connect')
            job.finish(False, 'secret', code='timeout')
        self.assertTrue(job.done)
        self.assertEqual(stderr.getvalue().count('Cannot write'), 1)
        self.assertNotIn('secret', stderr.getvalue())

    def test_bart_timeout_and_recovery_are_recorded_without_request_url(self):
        with mock.patch.object(departures, '_fetch', side_effect=[
                requests.Timeout('https://example.test?key=secret-api-key'), []]), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            departures._poll_once()
            departures._poll_once()
        records = self.records()
        self.assertEqual([r['ok'] for r in records], [False, True])
        self.assertEqual(records[0]['error_kind'], 'Timeout')
        self.assertNotIn('secret-api-key', json.dumps(records) + stderr.getvalue())

    def test_bart_http_failure_preserves_status_without_body(self):
        response = requests.Response()
        response.status_code = 503
        with mock.patch.object(departures, '_fetch', side_effect=requests.HTTPError(
                'secret body', response=response)), contextlib.redirect_stderr(io.StringIO()):
            departures._poll_once()
        self.assertEqual(self.records()[0]['http_status'], 503)

    def test_radio_measurements_exclude_network_identity_and_use_read_only_commands(self):
        outputs = [('ok', 'wlan0:wifi\neth0:ethernet'),
                   ('ok', 'Connected to aa:bb:cc:dd:ee:ff\nSSID: private-network\n'
                    'signal: -67 dBm\nfreq: 2437\n'),
                   ('ok', 'Power save: on'),
                   ('ok', 'IP4.GATEWAY:10.1.2.1\nIP4.DNS[1]:10.1.2.1'),
                   ('ok', 'throttled=0x50005')]
        with mock.patch.object(diagnostics, '_command', side_effect=outputs) as command:
            health = diagnostics.collect_radio_health()
        self.assertEqual(health['signal_dbm'], -67)
        self.assertEqual(health['power_save'], 'on')
        self.assertEqual(health['throttled_bits'], 0x50005)
        self.assertTrue(health['associated'])
        self.assertTrue(health['has_gateway'])
        self.assertTrue(health['has_dns'])
        self.assertNotIn('10.1.2.1', json.dumps(health))
        self.assertNotIn('private-network', json.dumps(health))
        self.assertNotIn('aa:bb', json.dumps(health))
        self.assertEqual(command.call_args_list, [
            mock.call(['nmcli', '-t', '-f', 'DEVICE,TYPE', 'device', 'status']),
            mock.call(['iw', 'dev', 'wlan0', 'link']),
            mock.call(['iw', 'dev', 'wlan0', 'get', 'power_save']),
            mock.call(['nmcli', '-t', '-f', 'IP4.GATEWAY,IP4.DNS', 'device', 'show', 'wlan0']),
            mock.call(['vcgencmd', 'get_throttled'])])

    def test_missing_tools_and_timeouts_are_explicit(self):
        with mock.patch.object(diagnostics.subprocess, 'run', side_effect=FileNotFoundError):
            health = diagnostics.collect_radio_health()
        self.assertEqual(health['adapter_query'], 'unavailable')
        self.assertIsNone(health['adapter_found'])
        self.assertIsNone(health['throttled_bits'])
        with mock.patch.object(diagnostics.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('iw', 3)):
            self.assertEqual(diagnostics._command(['iw'])[0], 'timeout')

    def test_unknown_link_measurements_are_not_healthy(self):
        with mock.patch.object(diagnostics, '_command', side_effect=[
                ('ok', 'wlan0:wifi'), ('failed', ''), ('timeout', ''), ('failed', ''), ('failed', '')]):
            health = diagnostics.collect_radio_health()
        self.assertIsNone(health['associated'])
        self.assertIsNone(health['signal_dbm'])
        self.assertEqual(health['power_save'], 'unknown')


if __name__ == '__main__':
    unittest.main()
