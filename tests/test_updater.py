import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bartdisplay import updater


def completed(returncode=0, stdout='', stderr=''):
    return SimpleNamespace(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class ImmediateThread:
    def __init__(self, target, name=None, daemon=None):
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self):
        self.target()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[1]

    @staticmethod
    def successful_commands(
            old='a' * 40,
            new='b' * 40,
            fetched=None,
            status=''):
        fetched = new if fetched is None else fetched
        return [
            completed(stdout='true\n'),
            completed(stdout='main\n'),
            completed(stdout='https://github.com/example/repo.git\n'),
            completed(stdout=status),
            completed(stdout=old + '\n'),
            completed(stdout='Updating files\n'),
            completed(stdout=fetched + '\n'),
            completed(stdout=new + '\n'),
        ]

    def test_git_command_is_noninteractive_bounded_and_has_no_shell(self):
        process = completed()
        with mock.patch.object(
                updater.subprocess,
                'run',
                return_value=process) as run:
            result = updater._run_git(
                '/usr/bin/git',
                ['pull', '--ff-only', 'origin', 'main'],
                self.repo_root,
                updater._PULL_TIMEOUT_SEC,
            )

        self.assertIs(result, process)
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [
                '/usr/bin/git',
                'pull',
                '--ff-only',
                'origin',
                'main',
            ],
        )
        self.assertEqual(kwargs['cwd'], str(self.repo_root))
        self.assertEqual(kwargs['timeout'], updater._PULL_TIMEOUT_SEC)
        self.assertFalse(kwargs['shell'])
        self.assertTrue(kwargs['capture_output'])
        self.assertTrue(kwargs['text'])
        self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(kwargs['env']['GCM_INTERACTIVE'], 'Never')
        self.assertEqual(kwargs['env']['LC_ALL'], 'C')
        self.assertEqual(kwargs['env']['LANG'], 'C')

    def test_changed_update_runs_exact_fast_forward_pull(self):
        commands = self.successful_commands()
        with (
                mock.patch.object(
                    updater.shutil,
                    'which',
                    return_value='/usr/bin/git',
                ),
                mock.patch.object(
                    updater,
                    '_run_git',
                    side_effect=commands,
                ) as run_git):
            result = updater.update_app(self.repo_root)

        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertEqual(result.code, 'updated')
        self.assertEqual(
            result.message,
            'App updated successfully. Restart display to apply.',
        )
        self.assertEqual(result.old_commit, 'a' * 40)
        self.assertEqual(result.new_commit, 'b' * 40)
        self.assertEqual(
            run_git.call_args_list[5].args,
            (
                '/usr/bin/git',
                ['pull', '--ff-only', 'origin', 'main'],
                self.repo_root,
                updater._PULL_TIMEOUT_SEC,
            ),
        )
        self.assertEqual(
            run_git.call_args_list[6].args[1],
            ['rev-parse', 'FETCH_HEAD'],
        )

    def test_unchanged_head_reports_already_current(self):
        commit = 'c' * 40
        commands = self.successful_commands(old=commit, new=commit)
        with (
                mock.patch.object(
                    updater.shutil,
                    'which',
                    return_value='/usr/bin/git',
                ),
                mock.patch.object(
                    updater,
                    '_run_git',
                    side_effect=commands,
                )):
            result = updater.update_app(self.repo_root)

        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(result.code, 'current')
        self.assertEqual(result.message, 'Already up to date.')

    def test_local_ahead_main_is_not_reported_as_current(self):
        remote_commit = 'a' * 40
        local_commit = 'b' * 40
        commands = self.successful_commands(
            old=local_commit,
            new=local_commit,
            fetched=remote_commit,
        )
        with (
                mock.patch.object(
                    updater.shutil,
                    'which',
                    return_value='/usr/bin/git',
                ),
                mock.patch.object(
                    updater,
                    '_run_git',
                    side_effect=commands,
                )):
            result = updater.update_app(self.repo_root)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'non_fast_forward')
        self.assertEqual(
            result.message,
            'Device branch cannot fast-forward to origin/main.',
        )

    def test_tracked_changes_block_pull(self):
        commands = [
            completed(stdout='true\n'),
            completed(stdout='main\n'),
            completed(stdout='https://github.com/example/repo.git\n'),
            completed(stdout=' M bartdisplay/ui/settings.py\n'),
        ]
        with (
                mock.patch.object(
                    updater.shutil,
                    'which',
                    return_value='/usr/bin/git',
                ),
                mock.patch.object(
                    updater,
                    '_run_git',
                    side_effect=commands,
                ) as run_git):
            result = updater.update_app(self.repo_root)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'local_changes')
        self.assertEqual(
            result.message,
            'Local changes prevent the update.',
        )
        self.assertEqual(run_git.call_count, 4)

    def test_unstaged_runtime_config_change_can_pull_safely(self):
        commit = 'd' * 40
        commands = self.successful_commands(
            old=commit,
            new=commit,
            status=' M config.json\n',
        )
        with (
                mock.patch.object(
                    updater.shutil,
                    'which',
                    return_value='/usr/bin/git',
                ),
                mock.patch.object(
                    updater,
                    '_run_git',
                    side_effect=commands,
                ) as run_git):
            result = updater.update_app(self.repo_root)

        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(result.code, 'current')
        self.assertEqual(
            run_git.call_args_list[5].args[1],
            ['pull', '--ff-only', 'origin', 'main'],
        )

    def test_staged_or_additional_changes_are_not_runtime_config(self):
        cases = (
            'M  config.json\n',
            ' M config.json\n M main.py\n',
            ' D config.json\n',
        )
        for status in cases:
            with self.subTest(status=status):
                self.assertFalse(
                    updater._only_runtime_config_changed(status),
                )

    def test_non_main_checkout_is_rejected_before_remote_access(self):
        commands = [
            completed(stdout='true\n'),
            completed(stdout='feature/work\n'),
        ]
        with (
                mock.patch.object(
                    updater.shutil,
                    'which',
                    return_value='/usr/bin/git',
                ),
                mock.patch.object(
                    updater,
                    '_run_git',
                    side_effect=commands,
                ) as run_git):
            result = updater.update_app(self.repo_root)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'wrong_branch')
        self.assertEqual(result.message, 'Update setup is incomplete.')
        self.assertEqual(run_git.call_count, 2)

    def test_missing_git_is_visible_setup_failure(self):
        with (
                mock.patch.object(
                    updater.shutil,
                    'which',
                    return_value=None,
                ),
                mock.patch.object(updater, '_run_git') as run_git):
            result = updater.update_app(self.repo_root)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'command_missing')
        self.assertEqual(result.message, 'Update setup is incomplete.')
        run_git.assert_not_called()

    def test_pull_timeout_is_reported_as_unreachable(self):
        command = [
            '/usr/bin/git',
            'pull',
            '--ff-only',
            'origin',
            'main',
        ]
        commands = self.successful_commands()[:5]
        commands.append(subprocess.TimeoutExpired(
            command,
            updater._PULL_TIMEOUT_SEC,
        ))
        with (
                mock.patch.object(
                    updater.shutil,
                    'which',
                    return_value='/usr/bin/git',
                ),
                mock.patch.object(
                    updater,
                    '_run_git',
                    side_effect=commands,
                )):
            result = updater.update_app(self.repo_root)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'remote_unreachable')
        self.assertEqual(result.message, 'Could not reach GitHub.')

    def test_common_pull_errors_are_concise_and_categorized(self):
        cases = (
            (
                'fatal: Authentication failed for https://example/repo',
                'authentication_failed',
                'GitHub authentication failed.',
            ),
            (
                'Your local changes would be overwritten by merge',
                'local_changes',
                'Local changes prevent the update.',
            ),
            (
                'fatal: Not possible to fast-forward, aborting.',
                'non_fast_forward',
                'Device branch cannot fast-forward to origin/main.',
            ),
            (
                'fatal: unable to access: Could not resolve host: github.com',
                'remote_unreachable',
                'Could not reach GitHub.',
            ),
            (
                'fatal: origin does not appear to be a git repository',
                'setup_incomplete',
                'Update setup is incomplete.',
            ),
        )
        for detail, expected_code, expected_message in cases:
            with self.subTest(detail=detail):
                code, message = updater._classify_pull_failure(detail)
                self.assertEqual(code, expected_code)
                self.assertEqual(message, expected_message)

    def test_unknown_error_is_sanitized_and_bounded(self):
        detail = (
            'fatal: unable to access '
            'https://secret-user:secret-pass@example.com/repo?token=secret '
            + ('x' * 200)
        )

        code, message = updater._classify_pull_failure(detail)

        self.assertEqual(code, 'pull_failed')
        self.assertTrue(message.startswith('Update failed: '))
        self.assertNotIn('secret-user', message)
        self.assertNotIn('secret-pass', message)
        self.assertNotIn('token=secret', message)
        self.assertLessEqual(
            len(message),
            len('Update failed: ') + updater._DETAIL_LIMIT,
        )

    def test_async_update_returns_completed_pollable_job(self):
        result = updater.UpdateResult(
            True,
            True,
            'updated',
            'App updated successfully. Restart display to apply.',
            old_commit='a' * 40,
            new_commit='b' * 40,
        )
        with (
                mock.patch.object(
                    updater,
                    'update_app',
                    return_value=result,
                ),
                mock.patch.object(
                    updater.threading,
                    'Thread',
                    ImmediateThread,
                )):
            job = updater.update_async(self.repo_root)

        self.assertTrue(job.done)
        self.assertTrue(job.ok)
        self.assertTrue(job.changed)
        self.assertEqual(job.code, 'updated')
        self.assertEqual(job.old_commit, 'a' * 40)
        self.assertEqual(job.new_commit, 'b' * 40)

    def test_second_update_is_rejected_while_busy(self):
        acquired = updater._update_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            job = updater.update_async(self.repo_root)
        finally:
            updater._update_lock.release()

        self.assertTrue(job.done)
        self.assertFalse(job.ok)
        self.assertEqual(job.code, 'busy')
        self.assertEqual(
            job.message,
            'An app update is already in progress.',
        )


if __name__ == '__main__':
    unittest.main()
