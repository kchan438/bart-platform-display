import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from bartdisplay import tailscale_control as ts


def completed(state='Running', online=True, returncode=0):
    return SimpleNamespace(returncode=returncode, stderr='', stdout=json.dumps({
        'BackendState': state,
        'Self': {'Online': online, 'HostName': 'bart-platform-display',
                 'TailscaleIPs': ['fd7a::1', '100.119.127.47']},
    }))


class TailscaleTests(unittest.TestCase):
    def test_stopped_nonzero_status_is_not_a_daemon_failure(self):
        with mock.patch.object(ts, '_run', return_value=completed('Stopped', False, 1)):
            status = ts.read_status()
        self.assertEqual(status.label, 'DISCONNECTED')
        self.assertEqual(status.address, '100.119.127.47')

    def test_running_but_offline_is_not_shown_as_connected(self):
        with mock.patch.object(ts, '_run', return_value=completed(online=False)):
            status = ts.read_status()
        self.assertEqual(status.label, 'OFFLINE')
        self.assertIn('internet', status.message)

    def test_invalid_missing_and_timed_out_status_are_visible(self):
        for outcome in (FileNotFoundError(), subprocess.TimeoutExpired('tailscale', 8),
                        SimpleNamespace(stdout='not json')):
            with self.subTest(outcome=outcome):
                kwargs = ({'side_effect': outcome} if isinstance(outcome, Exception)
                          else {'return_value': outcome})
                with mock.patch.object(ts, '_run', **kwargs):
                    result = ts.read_status()
                self.assertEqual(result.state, 'Unavailable')
                self.assertTrue(result.message)

    def test_connect_preserves_existing_preferences_and_verifies_state(self):
        with (mock.patch.dict(ts.os.environ, {'BART_DEV': '0'}),
              mock.patch.object(ts, '_run', side_effect=[
                  completed('Stopped', False, 1), completed(), completed()]) as run):
            result = ts.change_connection(True)
        self.assertTrue(result.ok)
        self.assertEqual(run.call_args_list[1].args,
                         (['/usr/bin/sudo', '-n', '/usr/bin/tailscale', 'up'], 20))

    def test_disconnect_uses_down_and_retains_identity(self):
        with (mock.patch.dict(ts.os.environ, {'BART_DEV': '0'}),
              mock.patch.object(ts, '_run', side_effect=[
                  completed(), completed(), completed('Stopped', False, 1)]) as run):
            result = ts.change_connection(False)
        self.assertTrue(result.ok)
        self.assertIn('Login retained', result.message)
        self.assertEqual(run.call_args_list[1].args[0][-1], 'down')

    def test_login_and_approval_states_never_start_authentication(self):
        for state in ('NeedsLogin', 'NeedsMachineAuth'):
            with self.subTest(state=state):
                with (mock.patch.dict(ts.os.environ, {'BART_DEV': '0'}),
                      mock.patch.object(ts, '_run', return_value=completed(state)) as run):
                    result = ts.change_connection(True)
                self.assertFalse(result.ok)
                self.assertEqual(run.call_count, 1)

    def test_failed_or_pending_connect_does_not_claim_success(self):
        for middle, final in (
                (SimpleNamespace(returncode=1, stderr='sudo: a password is required'), completed('Stopped')),
                (completed(), completed('Starting')),
                (subprocess.TimeoutExpired('tailscale', 20), completed('Starting'))):
            with self.subTest(final=final.stdout):
                with (mock.patch.dict(ts.os.environ, {'BART_DEV': '0'}),
                      mock.patch.object(ts, '_run', side_effect=[completed('Stopped'), middle, final])):
                    result = ts.change_connection(True)
                self.assertFalse(result.ok)
                self.assertTrue(result.message)

    def test_dev_mode_and_duplicate_requests_cannot_change_connection(self):
        with (mock.patch.dict(ts.os.environ, {'BART_DEV': '1'}),
              mock.patch.object(ts, '_run') as run):
            self.assertFalse(ts.change_connection(True).ok)
        run.assert_not_called()
        with mock.patch.dict(ts.os.environ, {'BART_DEV': '0'}):
            with ts._action_lock:
                self.assertFalse(ts.change_connection(False).ok)

    def test_cli_is_bounded_noninteractive_and_uses_no_shell(self):
        with mock.patch.object(ts.subprocess, 'run', return_value=completed()) as run:
            ts.read_status()
        self.assertFalse(run.call_args.kwargs['shell'])
        self.assertEqual(run.call_args.kwargs['timeout'], 8)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/tailscale', 'status', '--json'])


if __name__ == '__main__':
    unittest.main()
