"""Bounded, private network evidence retained across display/Pi restarts.

Only explicit scalar fields are recorded. Never pass command output, request
URLs, credentials, network names or exception messages to this module.
"""

import argparse
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
import zipfile

MAX_BYTES = 4 * 1024 * 1024
BACKUPS = 3
_FIELDS = {
    'session_start': {'pid'},
    'session_stop': set(),
    'wifi_operation_start': {'operation'},
    'wifi_operation_end': {'operation', 'ok', 'code', 'partial'},
    'wifi_observation': {'state', 'reason', 'query_ok', 'has_ipv4', 'code'},
    'wifi_supervisor': {'phase', 'code', 'reason', 'retry_count', 'exhausted'},
    'bart_request': {'ok', 'error_kind', 'http_status', 'duration_ms'},
    'radio_health': {'link_query', 'associated', 'signal_dbm', 'frequency_mhz',
                     'power_save_query', 'power_save', 'throttle_query',
                     'throttled_bits', 'adapter_query', 'adapter_found',
                     'ip_config_query', 'has_gateway', 'has_dns'},
    'collector_error': {'error_kind'},
}
_lock = threading.RLock()
_recorder = None


def log_directory():
    return Path.home() / '.local' / 'state' / 'bart-platform-display' / 'diagnostics'


class _Handler(RotatingFileHandler):
    def handleError(self, record):
        # Standard logging error reports include the record. Keep failures safe.
        if not getattr(self, '_warned', False):
            self._warned = True
            print('[network-diagnostics] Cannot write diagnostic log', file=sys.stderr)


class Recorder:
    def __init__(self, directory, max_bytes=MAX_BYTES, backups=BACKUPS):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handler = _Handler(directory / 'network.jsonl', maxBytes=max_bytes,
                                backupCount=backups, encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.session = uuid.uuid4().hex
        try:
            self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:
            self.boot = 'unavailable'
        self.last = {}
        self.lock = threading.RLock()

    def record(self, event, heartbeat=None, **fields):
        if event not in _FIELDS:
            return
        # Discard unrecognized fields and non-scalar values at the boundary.
        clean = {k: v for k, v in fields.items() if k in _FIELDS[event]
                 and (v is None or isinstance(v, (bool, int, float))
                      or (isinstance(v, str)
                          and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', v)))}
        with self.lock:
            now = time.monotonic()
            previous = self.last.get(event)
            if heartbeat is not None and previous is not None:
                old_fields, old_time = previous
                if clean == old_fields and now - old_time < heartbeat:
                    return
            self.last[event] = (clean, now)
            payload = dict(schema=1, time_utc=datetime.now(timezone.utc).isoformat(),
                           monotonic_sec=round(now, 3), boot_id=self.boot,
                           session_id=self.session, event=event, **clean)
            entry = logging.LogRecord('network-diagnostics', logging.INFO, '', 0,
                                      json.dumps(payload, sort_keys=True), (), None)
            self.handler.handle(entry)  # Flushes each record; no userspace backlog.

    def close(self):
        with self.lock:
            self.handler.close()


def record(event, heartbeat=None, **fields):
    """Best-effort diagnostics must never break Wi-Fi or the display."""
    with _lock:
        if _recorder is not None:
            try:
                _recorder.record(event, heartbeat=heartbeat, **fields)
            except Exception:
                print('[network-diagnostics] Recording failed', file=sys.stderr)


def _command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3,
                                env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})
        return ('ok', result.stdout) if result.returncode == 0 else ('failed', '')
    except FileNotFoundError:
        return 'unavailable', ''
    except subprocess.TimeoutExpired:
        return 'timeout', ''
    except OSError:
        return 'failed', ''


def collect_radio_health():
    """Read existing link data only: no scan, reconnect, or power setting change."""
    result = {}
    status, devices = _command(['nmcli', '-t', '-f', 'DEVICE,TYPE', 'device', 'status'])
    result['adapter_query'] = status
    iface = next((line.split(':')[0] for line in devices.splitlines()
                  if line.endswith(':wifi')
                  and re.fullmatch(r'[A-Za-z0-9_.-]{1,15}:wifi', line)), None)
    result['adapter_found'] = bool(iface) if status == 'ok' else None
    if iface:
        status, link = _command(['iw', 'dev', iface, 'link'])
        result['link_query'] = status
        result['associated'] = ('Connected to ' in link) if status == 'ok' else None
        for key, pattern in [('signal_dbm', r'signal:\s*(-?\d+)'),
                             ('frequency_mhz', r'freq:\s*(\d+)')]:
            match = re.search(pattern, link)
            result[key] = int(match[1]) if match else None
        status, power = _command(['iw', 'dev', iface, 'get', 'power_save'])
        result['power_save_query'] = status
        match = re.search(r'Power save:\s*(on|off)\b', power)
        result['power_save'] = match[1] if match else 'unknown'
        status, ip_config = _command([
            'nmcli', '-t', '-f', 'IP4.GATEWAY,IP4.DNS', 'device', 'show', iface])
        result['ip_config_query'] = status
        for field, key in [('IP4.GATEWAY', 'has_gateway'), ('IP4.DNS', 'has_dns')]:
            result[key] = (any(
                line.startswith(field) and ':' in line
                and line.split(':', 1)[1].strip() not in ('', '--')
                for line in ip_config.splitlines()) if status == 'ok' else None)
    status, throttle = _command(['vcgencmd', 'get_throttled'])
    result['throttle_query'] = status
    match = re.search(r'throttled=(0x[0-9a-fA-F]+)', throttle)
    result['throttled_bits'] = int(match[1], 16) if match else None
    return result


class RadioMonitor:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='network-diagnostics', daemon=True)

    def run(self):
        while not self.stop_event.is_set():
            try:
                record('radio_health', **collect_radio_health())
            except Exception as exc:
                record('collector_error', error_kind=type(exc).__name__)
            self.stop_event.wait(60)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)


_monitor = None


def start():
    global _recorder, _monitor
    if sys.platform != 'linux' or os.environ.get('BART_DEV') == '1':
        return
    with _lock:
        if _recorder is not None:
            return
        try:
            _recorder = Recorder(log_directory())
        except OSError:
            print('[network-diagnostics] Cannot open persistent log', file=sys.stderr)
            return
        record('session_start', pid=os.getpid())
        _monitor = RadioMonitor()
        _monitor.thread.start()


def stop():
    global _recorder, _monitor
    if _monitor is not None:
        _monitor.stop()
        _monitor = None
    with _lock:
        if _recorder is not None:
            record('session_stop')
            _recorder.close()
            _recorder = None


def export_logs(directory, destination):
    """Copy only our bounded logs, never config.json, profiles, or raw journals.

    Stop the display first for a stable snapshot across rotation.
    """
    directory = Path(directory)
    files = [directory / ('network.jsonl' + (f'.{n}' if n else ''))
             for n in range(BACKUPS, -1, -1)]
    files = [path for path in files if path.is_file()]
    if not files:
        raise FileNotFoundError('No diagnostic logs found; install and run the updated app first')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=path.name)
        archive.writestr('README.txt',
                         'BART network diagnostics, schema 1. Read JSONL files oldest first: '
                         'network.jsonl.3, .2, .1, then network.jsonl.\n'
                         'UTC may jump when the Pi corrects its clock. Use boot_id, '
                         'session_id and monotonic_sec to correlate events.\n'
                         'Missing/unavailable measurements do not mean healthy. '
                         'Logs cover only the time the updated display was running.\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-dir', type=Path, default=log_directory())
    parser.add_argument('--export', type=Path, metavar='ZIP', required=True)
    args = parser.parse_args()
    try:
        export_logs(args.log_dir, args.export)
    except (OSError, zipfile.BadZipFile) as exc:
        parser.exit(1, f'Export failed: {exc}\n')
    print(f'Saved diagnostics to {args.export.resolve()}')


if __name__ == '__main__':
    main()
