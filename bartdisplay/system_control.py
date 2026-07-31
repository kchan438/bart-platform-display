"""Narrow, asynchronous Raspberry Pi system-control operations.

The display process remains unprivileged.  Full-device restart requests go
through systemd-logind, whose PolicyKit policy is deployed separately.  The UI
polls :class:`SystemActionJob` so a slow or denied request never blocks pygame's
render and touch loop.
"""

import os
import shutil
import subprocess
import sys
import threading


_REBOOT_TIMEOUT_SEC = 10
_action_lock = threading.Lock()


class ActionResult:
    """Synchronous system-action result."""

    __slots__ = ('ok', 'code', 'message')

    def __init__(self, ok, code, message):
        self.ok = ok
        self.code = code
        self.message = message


class SystemActionJob:
    """Pollable system action used by the settings UI."""

    __slots__ = ('kind', 'status', 'message', 'code', 'done', 'ok')

    def __init__(self, kind):
        self.kind = kind
        self.status = 'Requesting Raspberry Pi restart...'
        self.message = ''
        self.code = ''
        self.done = False
        self.ok = False

    def finish(self, result):
        self.ok = result.ok
        self.code = result.code
        self.message = result.message
        self.status = result.message
        self.done = True


def _command_error(stdout, stderr):
    return ' '.join(part.strip() for part in (stderr, stdout) if part.strip())


def request_reboot():
    """Request a normal systemd-logind reboot and return an ``ActionResult``."""
    loginctl = shutil.which('loginctl')
    if not loginctl:
        message = 'Device restart is unavailable'
        print('[system] loginctl was not found', file=sys.stderr)
        return ActionResult(False, 'command_missing', message)

    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    env['LANG'] = 'C'
    try:
        process = subprocess.run(
            [loginctl, 'reboot'],
            capture_output=True,
            text=True,
            timeout=_REBOOT_TIMEOUT_SEC,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        message = 'Device restart request timed out'
        print('[system] loginctl reboot timed out', file=sys.stderr)
        return ActionResult(False, 'timeout', message)
    except OSError as exc:
        message = 'Could not request device restart'
        print(f'[system] could not run loginctl: {exc}', file=sys.stderr)
        return ActionResult(False, 'command_failed', message)

    if process.returncode == 0:
        print('[system] Raspberry Pi restart request accepted', file=sys.stderr)
        return ActionResult(
            True,
            'accepted',
            'Restart accepted. Waiting for Raspberry Pi...',
        )

    detail = _command_error(process.stdout, process.stderr)
    lowered = detail.lower()
    denied = any(token in lowered for token in (
        'access denied',
        'authentication is required',
        'interactive authentication required',
        'not authorized',
        'permission denied',
    ))
    code = 'permission_denied' if denied else 'reboot_failed'
    message = (
        'Device restart is not authorized'
        if denied
        else 'Could not restart Raspberry Pi'
    )
    suffix = f': {detail}' if detail else ''
    print(
        f'[system] loginctl reboot failed with exit '
        f'{process.returncode}{suffix}',
        file=sys.stderr,
    )
    return ActionResult(False, code, message)


def reboot_async():
    """Start one reboot request and return a pollable job."""
    job = SystemActionJob('reboot')
    if not _action_lock.acquire(blocking=False):
        job.finish(ActionResult(
            False,
            'busy',
            'A system action is already in progress',
        ))
        return job

    def run():
        try:
            job.finish(request_reboot())
        except Exception as exc:
            print(
                f'[system] reboot request failed unexpectedly: {exc}',
                file=sys.stderr,
            )
            job.finish(ActionResult(
                False,
                'unexpected',
                'Could not restart Raspberry Pi',
            ))
        finally:
            _action_lock.release()

    threading.Thread(target=run, daemon=True).start()
    return job
