"""Bounded Tailscale status and connection jobs for the touchscreen.

Only up/down need privilege; deploy/sudoers grants those exact commands.
Disconnect retains the saved identity. This module never logs in or logs out.
"""

from dataclasses import dataclass
import json
import os
import subprocess
import threading


_CLI = '/usr/bin/tailscale'
_action_lock = threading.Lock()


@dataclass(frozen=True)
class Status:
    state: str = 'Checking'
    online: bool = False
    address: str = ''
    hostname: str = ''
    message: str = ''

    @property
    def label(self):
        return {
            'Checking': 'CHECKING...',
            'Running': 'CONNECTED' if self.online else 'OFFLINE',
            'Stopped': 'DISCONNECTED',
            'Starting': 'CONNECTING...',
            'NeedsLogin': 'SIGN-IN REQUIRED',
            'NeedsMachineAuth': 'APPROVAL REQUIRED',
        }.get(self.state, 'UNAVAILABLE')


def _run(args, timeout):
    env = os.environ.copy()
    env.update(LC_ALL='C', LANG='C')
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, env=env, shell=False)


def read_status():
    try:
        process = _run([_CLI, 'status', '--json'], 8)
        # A stopped client can return nonzero with a valid status document.
        data = json.loads(process.stdout)
        if not isinstance(data, dict):
            raise ValueError('Invalid status')
        node = data.get('Self') or {}
        addresses = node.get('TailscaleIPs') or []
        state = data.get('BackendState', 'Unavailable')
        message = {
            'NeedsLogin': 'Sign in over local SSH.',
            'NeedsMachineAuth': 'Approve Pi in Tailscale.',
        }.get(state, '')
        if state == 'Running' and not node.get('Online', False):
            message = 'Check Wi-Fi and internet.'
        return Status(state, bool(node.get('Online', False)),
                      next((ip for ip in addresses if ':' not in ip), ''),
                      str(node.get('HostName') or ''), message)
    except FileNotFoundError:
        return Status('Unavailable', message='Tailscale is not installed.')
    except subprocess.TimeoutExpired:
        return Status('Unavailable', message='Status check timed out.')
    except (OSError, ValueError, TypeError, AttributeError):
        return Status('Unavailable', message='Cannot read Tailscale status.')


@dataclass(frozen=True)
class Result:
    snapshot: Status
    ok: bool = True
    message: str = ''


def change_connection(connect):
    """Change only connection state, retaining saved Tailscale preferences."""
    if os.environ.get('BART_DEV') == '1':
        return Result(Status('Unavailable'), False, 'Disabled in preview mode.')
    if not _action_lock.acquire(blocking=False):
        return Result(Status(), False, 'Another request is running.')
    try:
        before = read_status()
        if before.state not in ('Running', 'Stopped', 'Starting'):
            return Result(before, False, before.message or 'Try refreshing status.')
        try:
            # No flags: up preserves existing settings. Bound the CLI process
            # externally so an unavailable network never blocks the render loop.
            process = _run(['/usr/bin/sudo', '-n', _CLI,
                            'up' if connect else 'down'], 20)
        except subprocess.TimeoutExpired:
            return Result(read_status(), False, 'Request timed out. Check status.')
        except OSError:
            return Result(read_status(), False, 'Cannot run Tailscale control.')
        after = read_status()
        if process.returncode:
            detail = (process.stderr or '').lower()
            denied = any(word in detail for word in (
                'password', 'sudoers', 'not allowed', 'permission', 'access denied'))
            return Result(after, False, 'Control is not authorized.' if denied
                          else 'Request failed. Check status.')
        if connect and after.state == 'Running' and after.online:
            return Result(after, message='Connected. Remote SSH ready.')
        if not connect and after.state == 'Stopped':
            return Result(after, message='Disconnected. Login retained.')
        return Result(after, False, after.message or 'Connection not yet ready.')
    finally:
        _action_lock.release()


class Job:
    """Publish a complete result before signalling completion to the UI."""

    def __init__(self, connect=None):
        self.action = connect is not None
        self.result = None
        self._done = threading.Event()

        def work():
            try:
                self.result = (Result(read_status()) if connect is None
                               else change_connection(connect))
            except Exception:
                self.result = Result(Status('Unavailable'), False,
                                     'Tailscale request failed.')
            finally:
                self._done.set()

        threading.Thread(target=work, daemon=True).start()

    @property
    def done(self):
        return self._done.is_set()
