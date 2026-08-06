"""Safe, asynchronous application updates from ``origin/main``.

The display service runs from a Git checkout. Updates are intentionally narrow:
only a clean ``main`` checkout may fast-forward from ``origin/main``. The UI
polls :class:`UpdateJob` so Git and network activity never blocks pygame.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time


_REPO_ROOT = Path(__file__).resolve().parents[1]
_QUERY_TIMEOUT_SEC = 8
_PULL_TIMEOUT_SEC = 90
_DETAIL_LIMIT = 96
_update_lock = threading.Lock()


class UpdateResult:
    """Synchronous application-update result."""

    __slots__ = (
        'ok',
        'changed',
        'code',
        'message',
        'old_commit',
        'new_commit',
    )

    def __init__(
            self,
            ok,
            changed,
            code,
            message,
            old_commit='',
            new_commit=''):
        self.ok = ok
        self.changed = changed
        self.code = code
        self.message = message
        self.old_commit = old_commit
        self.new_commit = new_commit


class UpdateJob:
    """Pollable update job used by the settings UI."""

    __slots__ = (
        'status',
        'message',
        'code',
        'done',
        'ok',
        'changed',
        'old_commit',
        'new_commit',
    )

    def __init__(self):
        self.status = 'Checking for updates...'
        self.message = ''
        self.code = ''
        self.done = False
        self.ok = False
        self.changed = False
        self.old_commit = ''
        self.new_commit = ''

    def finish(self, result):
        self.ok = result.ok
        self.changed = result.changed
        self.code = result.code
        self.message = result.message
        self.old_commit = result.old_commit
        self.new_commit = result.new_commit
        self.status = result.message
        self.done = True


def _git_env():
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GCM_INTERACTIVE'] = 'Never'
    env['LC_ALL'] = 'C'
    env['LANG'] = 'C'
    return env


def _run_git(git, args, repo_root, timeout):
    return subprocess.run(
        [git] + list(args),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_git_env(),
        shell=False,
    )


def _command_error(process):
    return '\n'.join(
        part.strip()
        for part in (process.stderr, process.stdout)
        if part and part.strip()
    )


def _only_runtime_config_changed(status_output):
    """Allow the app's normal unstaged ``config.json`` persistence only."""
    lines = [line for line in status_output.splitlines() if line.strip()]
    return bool(lines) and all(
        len(line) >= 4
        and line[:2] == ' M'
        and line[3:] == 'config.json'
        for line in lines
    )


def _safe_detail(detail):
    """Return one short Git error line without remote URLs or credentials."""
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', str(detail or ''))
    text = re.sub(
        r'[a-z][a-z0-9+.-]*://\S+',
        '[remote]',
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r'\b[\w.+-]+@[\w.-]+:[^\s]+',
        '[remote]',
        text,
    )
    text = re.sub(
        r'\bgh[pousr]_[A-Za-z0-9_]+\b',
        '[redacted]',
        text,
    )
    text = re.sub(
        r'(?i)\b(token|password|authorization)[=:]\S+',
        r'\1=[redacted]',
        text,
    )
    lines = [' '.join(line.split()) for line in text.splitlines()]
    line = next((line for line in lines if line), 'Git command failed')
    line = re.sub(r'^(fatal|error):\s*', '', line, flags=re.IGNORECASE)
    if len(line) > _DETAIL_LIMIT:
        line = line[:_DETAIL_LIMIT - 3].rstrip() + '...'
    return line or 'Git command failed'


def _classify_pull_failure(detail):
    lowered = str(detail or '').lower()
    if any(token in lowered for token in (
            'authentication failed',
            'could not read username',
            'terminal prompts disabled',
            'permission denied (publickey)',
            'repository not found',
            'access denied',
            'http 401',
            'http 403',
            'requested url returned error: 401',
            'requested url returned error: 403')):
        return (
            'authentication_failed',
            'GitHub authentication failed.',
        )
    if any(token in lowered for token in (
            'local changes to the following files would be overwritten',
            'please commit your changes or stash them',
            'untracked working tree files would be overwritten',
            'your local changes would be overwritten')):
        return (
            'local_changes',
            'Local changes prevent the update.',
        )
    if any(token in lowered for token in (
            'not possible to fast-forward',
            'cannot fast-forward',
            'diverging branches',
            'refusing to merge unrelated histories',
            'non-fast-forward')):
        return (
            'non_fast_forward',
            'Device branch cannot fast-forward to origin/main.',
        )
    if any(token in lowered for token in (
            'could not resolve host',
            'failed to connect',
            'connection timed out',
            'network is unreachable',
            'connection reset',
            'temporary failure in name resolution',
            'remote end hung up',
            'tls connection',
            'ssl connection')):
        return (
            'remote_unreachable',
            'Could not reach GitHub.',
        )
    if any(token in lowered for token in (
            'not a git repository',
            'does not appear to be a git repository',
            'no such remote',
            'couldn\'t find remote ref main',
            'could not find remote ref main',
            'unknown revision',
            'bad revision')):
        return (
            'setup_incomplete',
            'Update setup is incomplete.',
        )
    return (
        'pull_failed',
        f'Update failed: {_safe_detail(detail)}',
    )


def _failure(code, message, started_at, detail=''):
    elapsed = time.monotonic() - started_at
    suffix = f' detail={_safe_detail(detail)!r}' if detail else ''
    print(
        f'[update] failed code={code} elapsed={elapsed:.2f}s{suffix}',
        file=sys.stderr,
    )
    return UpdateResult(False, False, code, message)


def update_app(repo_root=None):
    """Fast-forward a clean ``main`` checkout from ``origin/main``."""
    started_at = time.monotonic()
    print('[update] checking origin/main', file=sys.stderr)

    git = shutil.which('git')
    if not git:
        return _failure(
            'command_missing',
            'Update setup is incomplete.',
            started_at,
            'git was not found',
        )

    root = Path(repo_root).resolve() if repo_root else _REPO_ROOT
    if not root.is_dir():
        return _failure(
            'repository_missing',
            'Update setup is incomplete.',
            started_at,
            'repository directory was not found',
        )

    try:
        inside = _run_git(
            git,
            ['rev-parse', '--is-inside-work-tree'],
            root,
            _QUERY_TIMEOUT_SEC,
        )
        if inside.returncode != 0 or inside.stdout.strip() != 'true':
            return _failure(
                'repository_invalid',
                'Update setup is incomplete.',
                started_at,
                _command_error(inside),
            )

        branch = _run_git(
            git,
            ['symbolic-ref', '--short', 'HEAD'],
            root,
            _QUERY_TIMEOUT_SEC,
        )
        if branch.returncode != 0 or branch.stdout.strip() != 'main':
            return _failure(
                'wrong_branch',
                'Update setup is incomplete.',
                started_at,
                _command_error(branch) or 'checkout is not on main',
            )

        remote = _run_git(
            git,
            ['remote', 'get-url', 'origin'],
            root,
            _QUERY_TIMEOUT_SEC,
        )
        if remote.returncode != 0 or not remote.stdout.strip():
            return _failure(
                'remote_missing',
                'Update setup is incomplete.',
                started_at,
                _command_error(remote) or 'origin remote is missing',
            )

        status = _run_git(
            git,
            ['status', '--porcelain', '--untracked-files=no'],
            root,
            _QUERY_TIMEOUT_SEC,
        )
        if status.returncode != 0:
            return _failure(
                'status_failed',
                'Update setup is incomplete.',
                started_at,
                _command_error(status),
            )
        if (
                status.stdout.strip()
                and not _only_runtime_config_changed(status.stdout)):
            return _failure(
                'local_changes',
                'Local changes prevent the update.',
                started_at,
                'tracked working tree changes are present',
            )

        old_head = _run_git(
            git,
            ['rev-parse', 'HEAD'],
            root,
            _QUERY_TIMEOUT_SEC,
        )
        if old_head.returncode != 0 or not old_head.stdout.strip():
            return _failure(
                'head_unavailable',
                'Update setup is incomplete.',
                started_at,
                _command_error(old_head),
            )
        old_commit = old_head.stdout.strip()

        pull = _run_git(
            git,
            ['pull', '--ff-only', 'origin', 'main'],
            root,
            _PULL_TIMEOUT_SEC,
        )
        if pull.returncode != 0:
            detail = _command_error(pull)
            code, message = _classify_pull_failure(detail)
            return _failure(code, message, started_at, detail)

        fetched_head = _run_git(
            git,
            ['rev-parse', 'FETCH_HEAD'],
            root,
            _QUERY_TIMEOUT_SEC,
        )
        if fetched_head.returncode != 0 or not fetched_head.stdout.strip():
            return _failure(
                'head_unavailable',
                'Update completed, but status could not be verified.',
                started_at,
                _command_error(fetched_head),
            )
        fetched_commit = fetched_head.stdout.strip()

        new_head = _run_git(
            git,
            ['rev-parse', 'HEAD'],
            root,
            _QUERY_TIMEOUT_SEC,
        )
        if new_head.returncode != 0 or not new_head.stdout.strip():
            return _failure(
                'head_unavailable',
                'Update completed, but status could not be verified.',
                started_at,
                _command_error(new_head),
            )
        new_commit = new_head.stdout.strip()
        if new_commit != fetched_commit:
            return _failure(
                'non_fast_forward',
                'Device branch cannot fast-forward to origin/main.',
                started_at,
                'device HEAD differs from fetched origin/main',
            )
    except subprocess.TimeoutExpired as exc:
        command = (
            list(exc.cmd)
            if isinstance(exc.cmd, (list, tuple))
            else []
        )
        if command[1:3] == ['pull', '--ff-only']:
            return _failure(
                'remote_unreachable',
                'Could not reach GitHub.',
                started_at,
                'git pull timed out',
            )
        return _failure(
            'setup_timeout',
            'Update setup is incomplete.',
            started_at,
            'git preflight timed out',
        )
    except OSError as exc:
        return _failure(
            'command_failed',
            'Update setup is incomplete.',
            started_at,
            str(exc),
        )

    changed = old_commit != new_commit
    elapsed = time.monotonic() - started_at
    print(
        f'[update] completed changed={str(changed).lower()} '
        f'old={old_commit[:8]} new={new_commit[:8]} '
        f'elapsed={elapsed:.2f}s',
        file=sys.stderr,
    )
    return UpdateResult(
        True,
        changed,
        'updated' if changed else 'current',
        (
            'App updated successfully. Restart display to apply.'
            if changed
            else 'Already up to date.'
        ),
        old_commit=old_commit,
        new_commit=new_commit,
    )


def update_async(repo_root=None):
    """Start one application update and return a pollable job."""
    job = UpdateJob()
    if not _update_lock.acquire(blocking=False):
        job.finish(UpdateResult(
            False,
            False,
            'busy',
            'An app update is already in progress.',
        ))
        return job

    def run():
        try:
            job.finish(update_app(repo_root=repo_root))
        except Exception as exc:
            print(
                f'[update] unexpected failure: {_safe_detail(exc)}',
                file=sys.stderr,
            )
            job.finish(UpdateResult(
                False,
                False,
                'unexpected',
                'Update failed: unexpected error.',
            ))
        finally:
            _update_lock.release()

    threading.Thread(
        target=run,
        name='app-update',
        daemon=True,
    ).start()
    return job
