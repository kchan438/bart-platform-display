import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bartdisplay import system_control


class ImmediateThread:
    def __init__(self, target, daemon=None):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


class SystemControlTests(unittest.TestCase):
    def test_reboot_uses_exact_loginctl_command_without_a_shell(self):
        completed = SimpleNamespace(returncode=0, stdout='', stderr='')
        with (
                mock.patch.object(
                    system_control.shutil,
                    'which',
                    return_value='/usr/bin/loginctl',
                ),
                mock.patch.object(
                    system_control.subprocess,
                    'run',
                    return_value=completed,
                ) as run):
            result = system_control.request_reboot()

        self.assertTrue(result.ok)
        self.assertEqual(result.code, 'accepted')
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['/usr/bin/loginctl', 'reboot'])
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['timeout'], system_control._REBOOT_TIMEOUT_SEC)
        self.assertEqual(kwargs['env']['LC_ALL'], 'C')
        self.assertEqual(kwargs['env']['LANG'], 'C')

    def test_missing_loginctl_is_a_visible_failure(self):
        with mock.patch.object(
                system_control.shutil,
                'which',
                return_value=None):
            result = system_control.request_reboot()

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'command_missing')
        self.assertEqual(result.message, 'Device restart is unavailable')

    def test_authorization_failure_is_classified(self):
        completed = SimpleNamespace(
            returncode=1,
            stdout='',
            stderr='Interactive authentication required.',
        )
        with (
                mock.patch.object(
                    system_control.shutil,
                    'which',
                    return_value='/usr/bin/loginctl',
                ),
                mock.patch.object(
                    system_control.subprocess,
                    'run',
                    return_value=completed,
                )):
            result = system_control.request_reboot()

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'permission_denied')
        self.assertEqual(result.message, 'Device restart is not authorized')

    def test_timeout_is_classified(self):
        with (
                mock.patch.object(
                    system_control.shutil,
                    'which',
                    return_value='/usr/bin/loginctl',
                ),
                mock.patch.object(
                    system_control.subprocess,
                    'run',
                    side_effect=subprocess.TimeoutExpired(
                        ['/usr/bin/loginctl', 'reboot'],
                        10,
                    ),
                )):
            result = system_control.request_reboot()

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'timeout')
        self.assertEqual(result.message, 'Device restart request timed out')

    def test_non_authorization_command_failure_is_classified(self):
        completed = SimpleNamespace(
            returncode=1,
            stdout='',
            stderr='Failed to connect to bus: No such file or directory',
        )
        with (
                mock.patch.object(
                    system_control.shutil,
                    'which',
                    return_value='/usr/bin/loginctl',
                ),
                mock.patch.object(
                    system_control.subprocess,
                    'run',
                    return_value=completed,
                )):
            result = system_control.request_reboot()

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'reboot_failed')
        self.assertEqual(result.message, 'Could not restart Raspberry Pi')

    def test_async_reboot_returns_a_completed_pollable_job(self):
        result = system_control.ActionResult(
            True,
            'accepted',
            'Restart accepted',
        )
        with (
                mock.patch.object(
                    system_control,
                    'request_reboot',
                    return_value=result,
                ),
                mock.patch.object(
                    system_control.threading,
                    'Thread',
                    ImmediateThread,
                )):
            job = system_control.reboot_async()

        self.assertTrue(job.done)
        self.assertTrue(job.ok)
        self.assertEqual(job.code, 'accepted')
        self.assertEqual(job.message, 'Restart accepted')

    def test_second_system_action_is_rejected_while_busy(self):
        acquired = system_control._action_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            job = system_control.reboot_async()
        finally:
            system_control._action_lock.release()

        self.assertTrue(job.done)
        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'busy')


class PowerPolicyTests(unittest.TestCase):
    def test_policy_is_scoped_to_reboot_actions_and_display_service(self):
        root = Path(__file__).resolve().parents[1]
        rule = (
            root
            / 'deploy'
            / 'polkit'
            / '51-bart-platform-display-reboot.rules'
        ).read_text(encoding='utf-8')

        self.assertIn('subject.user == "kevinchan"', rule)
        self.assertIn(
            'subject.system_unit == "bart-platform-display.service"',
            rule,
        )
        self.assertIn('"org.freedesktop.login1.reboot"', rule)
        self.assertIn(
            '"org.freedesktop.login1.reboot-multiple-sessions"',
            rule,
        )
        self.assertNotIn('reboot-ignore-inhibit', rule)
        self.assertNotIn('org.freedesktop.login1.power-off', rule)
        self.assertNotIn('org.freedesktop.login1.suspend', rule)
        self.assertNotIn('org.freedesktop.systemd1.manage-units', rule)


if __name__ == '__main__':
    unittest.main()
