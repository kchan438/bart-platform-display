"""Reliable Wi-Fi control through NetworkManager's command-line interface.

The settings UI must remain responsive while NetworkManager scans, connects, or
disconnects. Public async helpers therefore return :class:`WifiJob` instances
that the UI can poll. A process-wide operation lock prevents overlapping
mutating operations from racing and overwriting newer state.

NetworkManager connection profile names are deliberately kept separate from
Wi-Fi SSIDs. Profiles are addressed by UUID, which is stable even when netplan
or an administrator gives the profile a name that differs from its SSID.

If NetworkManager is unavailable (for example during desktop development), a
small in-memory mock backs the same API.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time


_HAVE_NMCLI = shutil.which('nmcli') is not None
_HAVE_BUSCTL = shutil.which('busctl') is not None

_WIFI_LIST_FIELDS = [
    '-t',
    '-f',
    'IN-USE,SIGNAL,SECURITY,SSID,BSSID,DEVICE',
    'device',
    'wifi',
    'list',
]

_SCAN_CACHE_TTL_SEC = 120.0
_SCAN_COMPLETE_TIMEOUT_SEC = 12.0
_CONNECT_VERIFY_TIMEOUT_SEC = 15.0

_operation_lock = threading.Lock()
_scan_cache = {}


class SavedProfile:
    """A NetworkManager Wi-Fi connection profile."""

    __slots__ = ('ssid', 'name', 'uuid', 'autoconnect')

    def __init__(self, ssid, name, uuid, autoconnect=True):
        self.ssid = ssid
        self.name = name
        self.uuid = uuid
        self.autoconnect = autoconnect


class ActiveConnection:
    """The connection currently active on a Wi-Fi device."""

    __slots__ = ('iface', 'state', 'name', 'uuid')

    def __init__(self, iface, state='', name='', uuid=''):
        self.iface = iface
        self.state = state
        self.name = name
        self.uuid = uuid


class Network:
    """One visible SSID, represented by its best/active access point."""

    __slots__ = (
        'ssid',
        'signal',
        'security',
        'protected',
        'saved',
        'active',
        'bssid',
        'iface',
        'profile_name',
        'profile_uuid',
        'stale',
        'last_seen',
    )

    def __init__(
            self,
            ssid,
            signal=0,
            security='',
            saved=False,
            active=False,
            bssid='',
            iface='',
            profile_name='',
            profile_uuid='',
            stale=False,
            last_seen=0.0):
        self.ssid = ssid
        self.signal = signal
        self.security = security
        self.protected = security not in ('', '--')
        self.saved = saved
        self.active = active
        self.bssid = bssid
        self.iface = iface
        self.profile_name = profile_name
        self.profile_uuid = profile_uuid
        self.stale = stale
        self.last_seen = last_seen

    @property
    def supported(self):
        """Whether the touchscreen flow supports this network's security."""
        security = self.security.upper()
        if not self.protected:
            return True
        if '802.1X' in security or 'ENTERPRISE' in security:
            return False
        if 'WEP' in security or 'WPA1' in security:
            return False
        return 'WPA2' in security or 'WPA3' in security


class ScanResult:
    """Synchronous scan result used by tests and the async job wrapper."""

    __slots__ = ('networks', 'ok', 'code', 'message', 'partial')

    def __init__(self, networks=None, ok=True, code='ok', message='', partial=False):
        self.networks = networks or []
        self.ok = ok
        self.code = code
        self.message = message
        self.partial = partial


class WifiJob:
    """Pollable scan/connect/disconnect operation."""

    __slots__ = (
        'kind',
        'ssid',
        'status',
        'message',
        'code',
        'done',
        'ok',
        'needs_password',
        'networks',
        'partial',
        '_managed',
    )

    def __init__(self, kind, ssid=''):
        self.kind = kind
        self.ssid = ssid
        self.status = {
            'scan': 'Scanning...',
            'connect': 'Connecting...',
            'disconnect': 'Disconnecting...',
        }.get(kind, 'Working...')
        self.message = ''
        self.code = ''
        self.done = False
        self.ok = False
        self.needs_password = False
        self.networks = []
        self.partial = False
        self._managed = False

    def set_status(self, status):
        self.status = status

    def finish(
            self,
            ok,
            message,
            code='ok',
            needs_password=False,
            networks=None,
            partial=False):
        self.ok = ok
        self.message = message
        self.status = message
        self.code = code
        self.needs_password = needs_password
        if networks is not None:
            self.networks = networks
        self.partial = partial
        if not self._managed:
            self.done = True


class ConnectJob(WifiJob):
    """Backward-compatible connect job used by older callers/tests."""

    def __init__(self, ssid):
        super().__init__('connect', ssid)

    def _finish(self, ok, message):
        self.finish(ok, message)


# ---------------------------------------------------------------------------
# Command and terse-output helpers
# ---------------------------------------------------------------------------

def _split_terse(line):
    """Split an ``nmcli -t`` line on unescaped colons."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == '\\' and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ':':
            fields.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append(''.join(cur))
    return fields


def _run_program(program, args, timeout=20, input_text=None):
    """Run a subprocess and return ``(returncode, stdout, stderr)``."""
    try:
        process = subprocess.run(
            [program] + list(args),
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
        )
        return process.returncode, process.stdout, process.stderr
    except subprocess.TimeoutExpired:
        return 124, '', 'timed out'
    except Exception as exc:
        return 1, '', str(exc)


def _run(args, timeout=20, input_text=None):
    """Run ``nmcli`` without ever logging command arguments or stdin."""
    return _run_program('nmcli', args, timeout=timeout, input_text=input_text)


def _error_text(out, err):
    return (err or out or '').strip()


def _classify_error(rc, out, err, default='failed'):
    text = _error_text(out, err)
    lower = text.lower()
    if rc == 124 or 'timed out' in lower or 'timeout' in lower:
        return 'timeout', 'Timed out'
    if 'not authorized' in lower or 'permission' in lower:
        return 'not_authorized', 'Not authorized'
    if (
            'secrets were required' in lower
            or 'no secrets' in lower
            or 'wrong password' in lower
            or 'invalid password' in lower
            or '802-11-wireless-security.psk' in lower):
        return 'authentication_failed', 'Wrong password'
    if 'not found' in lower or 'no network with ssid' in lower:
        return 'network_not_found', 'Network not found'
    if 'dhcp' in lower or 'ip configuration' in lower:
        return 'dhcp_failed', 'Could not obtain IP'
    if default == 'scan':
        return 'scan_failed', 'Scan failed'
    if default == 'disconnect':
        return 'disconnect_failed', 'Disconnect failed'
    return 'connect_failed', 'Failed to connect'


# ---------------------------------------------------------------------------
# NetworkManager identity and state
# ---------------------------------------------------------------------------

def _wifi_radio():
    rc, out, err = _run(['-t', '-f', 'WIFI', 'radio'], timeout=10)
    if rc != 0:
        print(
            f'[wifi] radio check failed rc={rc}: {_error_text(out, err)}',
            file=sys.stderr,
        )
        return ''
    return out.strip().lower()


def wifi_iface():
    """Return the managed Wi-Fi interface, preferring a real device over P2P."""
    rc, out, _ = _run(['-t', '-f', 'DEVICE,TYPE', 'device'], timeout=10)
    if rc == 0:
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and parts[1] == 'wifi' and not parts[0].startswith('p2p-'):
                return parts[0]
    return 'wlan0'


def _profile_ssid(uuid):
    rc, out, _ = _run(
        ['-g', '802-11-wireless.ssid', 'connection', 'show', 'uuid', uuid],
        timeout=10,
    )
    if rc != 0 or not out.strip():
        return ''
    return _split_terse(out.strip().splitlines()[0])[0]


def saved_profiles():
    """Return Wi-Fi profiles keyed by their real SSID."""
    rc, out, err = _run(
        ['-t', '-f', 'NAME,UUID,TYPE,AUTOCONNECT', 'connection', 'show'],
        timeout=15,
    )
    if rc != 0:
        print(
            f'[wifi] saved profile query failed rc={rc}: {_error_text(out, err)}',
            file=sys.stderr,
        )
        return {}

    profiles = {}
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) < 3 or 'wireless' not in parts[2]:
            continue
        name, uuid = parts[0], parts[1]
        ssid = _profile_ssid(uuid)
        if not ssid:
            continue
        autoconnect = len(parts) < 4 or parts[3].lower() == 'yes'
        profiles.setdefault(ssid, []).append(
            SavedProfile(ssid, name, uuid, autoconnect=autoconnect)
        )
    return profiles


def saved_ssids():
    """Compatibility helper returning actual saved SSIDs, not profile names."""
    return set(saved_profiles())


def active_connection(iface=None):
    iface = iface or wifi_iface()
    rc, out, err = _run(
        [
            '-t',
            '-f',
            'GENERAL.STATE,GENERAL.CONNECTION,GENERAL.CON-UUID',
            'device',
            'show',
            iface,
        ],
        timeout=10,
    )
    if rc != 0:
        print(
            f'[wifi] active connection query failed rc={rc}: '
            f'{_error_text(out, err)}',
            file=sys.stderr,
        )
        return ActiveConnection(iface)

    values = {}
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) >= 2:
            values[parts[0]] = ':'.join(parts[1:])
    return ActiveConnection(
        iface,
        state=values.get('GENERAL.STATE', ''),
        name=values.get('GENERAL.CONNECTION', ''),
        uuid=values.get('GENERAL.CON-UUID', ''),
    )


def _profile_for_ssid(profiles, ssid, active_uuid=''):
    candidates = profiles.get(ssid, [])
    for profile in candidates:
        if profile.uuid == active_uuid:
            return profile
    return candidates[0] if candidates else None


def _device_ipv4(iface):
    rc, out, _ = _run(['-g', 'IP4.ADDRESS', 'device', 'show', iface], timeout=10)
    if rc != 0:
        return ''
    return next((line.strip() for line in out.splitlines() if line.strip()), '')


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _wifi_list_args(iface=None):
    args = list(_WIFI_LIST_FIELDS)
    args += ['--rescan', 'no']
    if iface:
        args += ['ifname', iface]
    return args


def _clone_network(net, **changes):
    values = {
        'ssid': net.ssid,
        'signal': net.signal,
        'security': net.security,
        'saved': net.saved,
        'active': net.active,
        'bssid': net.bssid,
        'iface': net.iface,
        'profile_name': net.profile_name,
        'profile_uuid': net.profile_uuid,
        'stale': net.stale,
        'last_seen': net.last_seen,
    }
    values.update(changes)
    return Network(**values)


def _parse_scan_output(out, profiles=None, active=None, now=None):
    """Parse access points and de-duplicate them by SSID."""
    profiles = profiles or {}
    active = active or ActiveConnection('')
    now = time.monotonic() if now is None else now
    by_ssid = {}
    active_rows = set()

    # Older callers passed a set of saved SSIDs. Keep parsing compatible while
    # using UUID-rich profile data in production.
    if isinstance(profiles, set):
        profiles = {
            ssid: [SavedProfile(ssid, ssid, '', autoconnect=True)]
            for ssid in profiles
        }

    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) < 6:
            continue
        in_use, signal, security, ssid, bssid, iface = parts[:6]
        if not ssid:
            continue
        try:
            sig = int(signal)
        except ValueError:
            sig = 0

        row_active = in_use.strip() == '*'
        profile = _profile_for_ssid(profiles, ssid, active.uuid)
        network = Network(
            ssid,
            signal=sig,
            security=security,
            saved=profile is not None,
            active=row_active or bool(profile and profile.uuid == active.uuid),
            bssid=bssid,
            iface=iface,
            profile_name=profile.name if profile else '',
            profile_uuid=profile.uuid if profile else '',
            last_seen=now,
        )

        existing = by_ssid.get(ssid)
        should_replace = (
            existing is None
            or (row_active and ssid not in active_rows)
            or (ssid not in active_rows and network.signal > existing.signal)
        )
        if should_replace:
            by_ssid[ssid] = network
        if row_active:
            active_rows.add(ssid)

    # A connected network must remain visible even if a degraded scan omitted
    # its access point.
    if active.uuid:
        for ssid, candidates in profiles.items():
            profile = next((p for p in candidates if p.uuid == active.uuid), None)
            if profile is None:
                continue
            if ssid in by_ssid:
                by_ssid[ssid].active = True
                by_ssid[ssid].saved = True
                by_ssid[ssid].profile_name = profile.name
                by_ssid[ssid].profile_uuid = profile.uuid
            else:
                by_ssid[ssid] = Network(
                    ssid,
                    saved=True,
                    active=True,
                    iface=active.iface,
                    profile_name=profile.name,
                    profile_uuid=profile.uuid,
                    last_seen=now,
                )
            break

    networks = list(by_ssid.values())
    networks.sort(key=lambda net: (not net.active, -net.signal, net.ssid.lower()))
    return networks


def _device_dbus_path(iface):
    rc, out, _ = _run(
        ['-g', 'GENERAL.DBUS-PATH', 'device', 'show', iface],
        timeout=10,
    )
    return out.strip() if rc == 0 else ''


def _last_scan(path):
    if not _HAVE_BUSCTL or not path:
        return None
    rc, out, _ = _run_program(
        'busctl',
        [
            'get-property',
            'org.freedesktop.NetworkManager',
            path,
            'org.freedesktop.NetworkManager.Device.Wireless',
            'LastScan',
        ],
        timeout=5,
    )
    if rc != 0:
        return None
    try:
        return int(out.strip().split()[-1])
    except (ValueError, IndexError):
        return None


def _request_rescan_and_wait(iface):
    path = _device_dbus_path(iface)
    before = _last_scan(path)
    rc, out, err = _run(
        ['device', 'wifi', 'rescan', 'ifname', iface],
        timeout=15,
    )
    if rc != 0:
        code, message = _classify_error(rc, out, err, default='scan')
        return False, code, message, _error_text(out, err)

    if before is None:
        # busctl is not available on desktop/mock environments. The explicit
        # rescan request is asynchronous, so allow NetworkManager to complete.
        time.sleep(2.0)
        return True, 'ok', '', ''

    deadline = time.monotonic() + _SCAN_COMPLETE_TIMEOUT_SEC
    while time.monotonic() < deadline:
        current = _last_scan(path)
        if current is not None and current > before:
            return True, 'ok', '', ''
        time.sleep(0.25)
    return False, 'scan_timeout', 'Scan timed out', 'LastScan did not advance'


def _read_scan_rows(profiles, active, iface):
    rc, out, err = _run(_wifi_list_args(iface=iface), timeout=15)
    if rc != 0:
        code, message = _classify_error(rc, out, err, default='scan')
        return [], code, message, _error_text(out, err)
    return _parse_scan_output(out, profiles, active), 'ok', '', ''


def _merge_scan_cache(fresh):
    now = time.monotonic()
    fresh_by_ssid = {}
    for network in fresh:
        current = _clone_network(network, stale=False, last_seen=now)
        fresh_by_ssid[current.ssid] = current
        _scan_cache[current.ssid] = _clone_network(current)

    merged = dict(fresh_by_ssid)
    expired = []
    for ssid, cached in _scan_cache.items():
        age = now - cached.last_seen
        if age > _SCAN_CACHE_TTL_SEC:
            expired.append(ssid)
            continue
        if ssid not in merged:
            merged[ssid] = _clone_network(cached, active=False, stale=True)
    for ssid in expired:
        _scan_cache.pop(ssid, None)

    networks = list(merged.values())
    networks.sort(
        key=lambda net: (
            not net.active,
            net.stale,
            -net.signal,
            net.ssid.lower(),
        )
    )
    return networks, any(net.stale for net in networks)


def _recent_cached_networks():
    networks, partial = _merge_scan_cache([])
    return networks, partial


def _scan_detailed_unlocked(rescan=True):
    if not _HAVE_NMCLI:
        return ScanResult(_mock_scan(), ok=True)

    radio = _wifi_radio()
    if radio == 'disabled':
        cached, partial = _recent_cached_networks()
        return ScanResult(
            cached,
            ok=False,
            code='radio_disabled',
            message='Wi-Fi is disabled',
            partial=partial,
        )
    if radio not in ('enabled', ''):
        cached, partial = _recent_cached_networks()
        return ScanResult(
            cached,
            ok=False,
            code='radio_unavailable',
            message='Wi-Fi adapter unavailable',
            partial=partial,
        )

    iface = wifi_iface()
    profiles = saved_profiles()
    scan_error = None

    if rescan:
        completed, code, message, detail = _request_rescan_and_wait(iface)
        if not completed:
            scan_error = (code, message, detail)
            print(
                f'[wifi] fresh scan failed ({code}): {detail or message}',
                file=sys.stderr,
            )

    active = active_connection(iface)
    fresh, list_code, list_message, list_detail = _read_scan_rows(
        profiles,
        active,
        iface,
    )
    if list_code != 'ok':
        scan_error = scan_error or (list_code, list_message, list_detail)
        print(
            f'[wifi] scan list failed ({list_code}): '
            f'{list_detail or list_message}',
            file=sys.stderr,
        )

    networks, partial = _merge_scan_cache(fresh)
    print(
        f'[wifi] scan found {len(fresh)} fresh network(s), '
        f'{len(networks)} displayed',
        file=sys.stderr,
    )

    if scan_error:
        code, message, _ = scan_error
        if networks:
            return ScanResult(
                networks,
                ok=False,
                code=code,
                message=f'{message} - showing recent results',
                partial=True,
            )
        return ScanResult([], ok=False, code=code, message=message)

    if partial:
        return ScanResult(
            networks,
            ok=True,
            code='partial',
            message='Showing recent results',
            partial=True,
        )
    if not networks:
        return ScanResult([], ok=True, code='empty', message='No networks found')
    return ScanResult(networks, ok=True)


def scan_detailed(rescan=True):
    """Synchronously scan while excluding connect/disconnect operations."""
    if not _operation_lock.acquire(blocking=False):
        cached, partial = _recent_cached_networks()
        return ScanResult(
            cached,
            ok=False,
            code='busy',
            message='Wi-Fi is busy',
            partial=partial,
        )
    try:
        return _scan_detailed_unlocked(rescan=rescan)
    finally:
        _operation_lock.release()


def scan(rescan=True):
    """Compatibility wrapper returning only the network list."""
    return scan_detailed(rescan=rescan).networks


def scan_async(rescan=True):
    job = WifiJob('scan')

    def work():
        result = _scan_detailed_unlocked(rescan=rescan)
        job.finish(
            result.ok,
            result.message,
            code=result.code,
            networks=result.networks,
            partial=result.partial,
        )

    _launch_job(job, work)
    return job


# ---------------------------------------------------------------------------
# Network detail
# ---------------------------------------------------------------------------

def info_basic(net):
    rows = [
        ('SSID', net.ssid),
        ('SIGNAL', f'{net.signal}%'),
        ('SECURITY', net.security or 'Open'),
        ('PROTECTED', 'Yes' if net.protected else 'No'),
        ('SAVED', 'Yes' if net.saved else 'No'),
        ('STATUS', 'Connected' if net.active else 'Not connected'),
    ]
    if not net.supported:
        rows.append(('SUPPORT', 'Unsupported'))
    return rows


def info(net):
    """Return detail rows; call off the render thread."""
    rows = info_basic(net)
    if net.active and _HAVE_NMCLI:
        iface = net.iface or wifi_iface()
        rc, out, _ = _run(
            ['-t', '-f', 'IP4.ADDRESS,IP4.GATEWAY', 'device', 'show', iface],
            timeout=10,
        )
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(':', 1)
                if len(parts) != 2 or not parts[1]:
                    continue
                if parts[0].startswith('IP4.ADDRESS'):
                    rows.append(('IP', parts[1]))
                elif parts[0].startswith('IP4.GATEWAY'):
                    rows.append(('GATEWAY', parts[1]))
    return rows


# ---------------------------------------------------------------------------
# Connection and authentication
# ---------------------------------------------------------------------------

def _launch_job(job, worker):
    def run():
        if not _operation_lock.acquire(blocking=False):
            job.finish(False, 'Wi-Fi is busy', code='busy')
            return
        job._managed = True
        try:
            worker()
        except Exception as exc:
            print(f'[wifi] {job.kind} failed unexpectedly: {exc}', file=sys.stderr)
            job.finish(False, f'{job.kind.title()} failed', code='unexpected')
        finally:
            _operation_lock.release()
            job._managed = False
            job.done = True

    threading.Thread(target=run, daemon=True).start()


def _connection_up_with_password(uuid, iface, password):
    """Activate a saved profile with a secret supplied outside argv."""
    fd, path = tempfile.mkstemp(prefix='bart-wifi-', text=True)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as secret_file:
            secret_file.write(
                '802-11-wireless-security.psk:' + password + '\n'
            )
        return _run(
            [
                'connection',
                'up',
                'uuid',
                uuid,
                'ifname',
                iface,
                'passwd-file',
                path,
            ],
            timeout=60,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _connect_new(net, iface, password):
    args = ['--ask', 'device', 'wifi', 'connect', net.ssid, 'ifname', iface]
    # Do not pass ``bssid`` here: nmcli persists it on a newly-created profile,
    # which would pin mesh networks to one access point and break roaming.
    input_text = None
    if net.protected:
        input_text = (password or '') + '\n'
    return _run(args, timeout=60, input_text=input_text)


def _verify_connected(iface, expected_uuid='', expected_ssid=''):
    deadline = time.monotonic() + _CONNECT_VERIFY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        active = active_connection(iface)
        state_connected = active.state.startswith('100')
        uuid_matches = not expected_uuid or active.uuid == expected_uuid
        if state_connected and uuid_matches and active.uuid:
            ssid_matches = True
            if not expected_uuid and expected_ssid:
                profiles = saved_profiles()
                active_ssid = next(
                    (
                        ssid
                        for ssid, candidates in profiles.items()
                        if any(profile.uuid == active.uuid for profile in candidates)
                    ),
                    '',
                )
                ssid_matches = active_ssid == expected_ssid
            if ssid_matches:
                address = _device_ipv4(iface)
                if address:
                    return True, active.uuid, ''
        time.sleep(0.5)
    return False, '', 'Connection activated without an IPv4 address'


def connect_async(net, password=None):
    job = WifiJob('connect', net.ssid)
    _launch_job(job, lambda: _connect_worker(job, net, password))
    return job


def _connect_worker(job, net, password):
    if not _HAVE_NMCLI:
        return _mock_connect(job, net, password)
    if not net.supported:
        job.finish(False, 'Unsupported network security', code='unsupported')
        return
    if net.protected and not net.saved and not password:
        job.finish(
            False,
            'Password required',
            code='authentication_required',
            needs_password=True,
        )
        return

    iface = net.iface or wifi_iface()
    job.set_status('Authenticating...' if net.protected else 'Connecting...')

    expected_uuid = net.profile_uuid
    if net.saved and expected_uuid:
        if password is not None:
            rc, out, err = _connection_up_with_password(
                expected_uuid,
                iface,
                password,
            )
        else:
            rc, out, err = _run(
                [
                    'connection',
                    'up',
                    'uuid',
                    expected_uuid,
                    'ifname',
                    iface,
                ],
                timeout=60,
            )
    else:
        rc, out, err = _connect_new(net, iface, password)

    if rc != 0:
        code, message = _classify_error(rc, out, err)
        needs_password = net.protected and code == 'authentication_failed'
        print(
            f'[wifi] connect to {net.ssid!r} failed ({code}): '
            f'{_error_text(out, err)}',
            file=sys.stderr,
        )
        job.finish(
            False,
            message,
            code=code,
            needs_password=needs_password,
        )
        return

    job.set_status('Obtaining IP...')
    verified, active_uuid, detail = _verify_connected(
        iface,
        expected_uuid=expected_uuid,
        expected_ssid=net.ssid,
    )
    if not verified:
        print(f'[wifi] connect verification failed: {detail}', file=sys.stderr)
        job.finish(False, 'Could not obtain IP', code='dhcp_failed')
        return

    # Refresh the profile identity for a newly created connection.
    if not net.profile_uuid:
        profiles = saved_profiles()
        profile = _profile_for_ssid(profiles, net.ssid, active_uuid)
        if profile:
            net.profile_name = profile.name
            net.profile_uuid = profile.uuid
            net.saved = True
    net.active = True
    job.finish(True, f'Connected to {net.ssid}', code='connected')


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------

def _disconnect_worker(job, net):
    if not _HAVE_NMCLI:
        ok, message = _mock_disconnect(net)
        job.finish(ok, message, code='disconnected' if ok else 'disconnect_failed')
        return

    iface = net.iface or wifi_iface()
    active = active_connection(iface)
    profiles = saved_profiles()
    target = _profile_for_ssid(profiles, net.ssid, active.uuid)
    target_uuid = target.uuid if target is not None else net.profile_uuid

    if not active.uuid:
        net.active = False
        job.finish(True, f'{net.ssid} is already disconnected', code='disconnected')
        return
    if not target_uuid:
        job.finish(
            False,
            'Could not identify active connection',
            code='identity_failed',
        )
        return
    if target_uuid != active.uuid:
        net.active = False
        job.finish(True, f'{net.ssid} is already disconnected', code='disconnected')
        return

    job.set_status('Disconnecting...')
    rc, out, err = _run(
        ['connection', 'down', 'uuid', active.uuid],
        timeout=30,
    )
    if rc != 0:
        code, message = _classify_error(rc, out, err, default='disconnect')
        print(
            f'[wifi] disconnect from {net.ssid!r} failed ({code}): '
            f'{_error_text(out, err)}',
            file=sys.stderr,
        )
        job.finish(False, message, code=code)
        return

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        current = active_connection(iface)
        if current.uuid != active.uuid:
            net.active = False
            job.finish(
                True,
                f'Disconnected from {net.ssid}',
                code='disconnected',
            )
            return
        time.sleep(0.25)

    job.finish(False, 'Disconnect could not be verified', code='disconnect_timeout')


def disconnect_async(net):
    job = WifiJob('disconnect', net.ssid)
    _launch_job(job, lambda: _disconnect_worker(job, net))
    return job


def disconnect(net):
    """Compatibility synchronous disconnect returning ``(ok, message)``."""
    if not _operation_lock.acquire(blocking=False):
        return False, 'Wi-Fi is busy'
    try:
        job = WifiJob('disconnect', net.ssid)
        _disconnect_worker(job, net)
        return job.ok, job.message
    finally:
        _operation_lock.release()


# ---------------------------------------------------------------------------
# Desktop mock
# ---------------------------------------------------------------------------

_MOCK = [
    Network(
        'HomeWiFi',
        88,
        'WPA2',
        saved=True,
        active=True,
        bssid='02:00:00:00:00:01',
        iface='wlan0',
        profile_name='Home profile',
        profile_uuid='mock-home',
    ),
    Network('Neighbor_5G', 62, 'WPA2', bssid='02:00:00:00:00:02', iface='wlan0'),
    Network('CoffeeShop', 40, '', bssid='02:00:00:00:00:03', iface='wlan0'),
    Network('OpenGuest', 30, '', bssid='02:00:00:00:00:04', iface='wlan0'),
]


def _mock_scan():
    now = time.monotonic()
    return [_clone_network(network, last_seen=now) for network in _MOCK]


def _mock_connect(job, net, password):
    time.sleep(0.25)
    job.set_status('Authenticating...' if net.protected else 'Connecting...')
    time.sleep(0.25)
    if net.protected and not net.saved and (password or '') != 'password':
        job.finish(
            False,
            'Wrong password',
            code='authentication_failed',
            needs_password=True,
        )
        return
    for network in _MOCK:
        network.active = network.ssid == net.ssid
        if network.ssid == net.ssid:
            network.saved = True
            network.profile_name = network.profile_name or f'{network.ssid} profile'
            network.profile_uuid = network.profile_uuid or f'mock-{network.ssid}'
    net.active = True
    net.saved = True
    job.finish(True, f'Connected to {net.ssid}', code='connected')


def _mock_disconnect(net):
    for network in _MOCK:
        if network.ssid == net.ssid:
            network.active = False
    net.active = False
    return True, f'Disconnected from {net.ssid}'
