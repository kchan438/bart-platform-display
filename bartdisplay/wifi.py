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
import uuid as uuidlib


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
_NMCLI_ACTIVATE_WAIT_SEC = 45
_NMCLI_ACTIVATE_TIMEOUT_SEC = 55
_NMCLI_PROFILE_WAIT_SEC = 10
_NMCLI_PROFILE_TIMEOUT_SEC = 15
_CANCEL_VERIFY_TIMEOUT_SEC = 4.0

_operation_lock = threading.Lock()
_scan_cache = {}
_recovery_latch = None
_autoconnect_restore = {}
_reauth_required = set()

_AUTOCONNECT_MARKER = 'org.bartdisplay.autoconnect-original'


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

    __slots__ = (
        'iface',
        'state',
        'name',
        'uuid',
        'reason',
        'query_ok',
        'error_code',
        'error_message',
    )

    def __init__(
            self,
            iface,
            state='',
            name='',
            uuid='',
            reason='',
            query_ok=True,
            error_code='',
            error_message=''):
        self.iface = iface
        self.state = state
        self.name = name
        self.uuid = uuid
        self.reason = reason
        self.query_ok = query_ok
        self.error_code = error_code
        self.error_message = error_message


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
        'profile_known',
        'autoconnect',
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
            profile_known=True,
            autoconnect=None,
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
        self.profile_known = profile_known
        self.autoconnect = autoconnect
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


class ProfileDiscoveryResult:
    """Saved profiles plus whether NetworkManager returned a complete view."""

    __slots__ = ('profiles', 'complete', 'code', 'message')

    def __init__(
            self,
            profiles=None,
            complete=True,
            code='ok',
            message=''):
        self.profiles = profiles or {}
        self.complete = complete
        self.code = code
        self.message = message


class _RecoveryLatch:
    """Unverified cleanup that must be reconciled before another operation."""

    __slots__ = (
        'iface',
        'profile_uuid',
        'delete_profile',
        'network',
        'reason',
        'reauth_required',
    )

    def __init__(
            self,
            iface,
            profile_uuid,
            delete_profile=False,
            network=None,
            reason='',
            reauth_required=False):
        self.iface = iface
        self.profile_uuid = profile_uuid
        self.delete_profile = delete_profile
        self.network = network
        self.reason = reason
        self.reauth_required = reauth_required


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
        env = os.environ.copy()
        # Error classification depends on stable NetworkManager messages.
        # Passwords are never placed in this environment.
        env['LC_ALL'] = 'C'
        env['LANG'] = 'C'
        process = subprocess.run(
            [program] + list(args),
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
            env=env,
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


def _nmcli_timed_out(rc, out='', err=''):
    """Return whether nmcli or the subprocess wrapper exhausted its wait."""
    lower = _error_text(out, err).lower()
    return (
        rc in (3, 124)
        or 'timed out' in lower
        or 'timeout' in lower
    )


def _classify_error(rc, out, err, default='failed'):
    text = _error_text(out, err)
    lower = text.lower()
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
    if _nmcli_timed_out(rc, out, err):
        return 'timeout', 'Timed out'
    if default == 'scan':
        return 'scan_failed', 'Scan failed'
    if default == 'disconnect':
        return 'disconnect_failed', 'Disconnect failed'
    return 'connect_failed', 'Failed to connect'


def _state_query_error(rc, out, err):
    """Classify a device-state read without calling it a connect failure."""
    code, message = _classify_error(rc, out, err)
    if code == 'connect_failed':
        return 'state_query_failed', 'Could not read Wi-Fi state'
    return code, message


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


def _profile_ssid_detailed(uuid):
    rc, out, err = _run(
        ['-g', '802-11-wireless.ssid', 'connection', 'show', 'uuid', uuid],
        timeout=10,
    )
    if rc != 0:
        return False, '', _error_text(out, err)
    if not out.strip():
        return False, '', 'Profile has no wireless SSID'
    return True, _split_terse(out.strip().splitlines()[0])[0], ''


def _profile_ssid(uuid):
    """Compatibility helper returning only the resolved SSID."""
    _, ssid, _ = _profile_ssid_detailed(uuid)
    return ssid


def _saved_profiles_detailed():
    """Return saved profiles and whether every profile query succeeded."""
    rc, out, err = _run(
        ['-t', '-f', 'NAME,UUID,TYPE,AUTOCONNECT', 'connection', 'show'],
        timeout=15,
    )
    if rc != 0:
        print(
            f'[wifi] saved profile query failed rc={rc}: {_error_text(out, err)}',
            file=sys.stderr,
        )
        code, message = _state_query_error(rc, out, err)
        if code == 'state_query_failed':
            code = 'profile_query_failed'
            message = 'Saved network status unavailable'
        return ProfileDiscoveryResult(
            complete=False,
            code=code,
            message=message,
        )

    profiles = {}
    complete = True
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) < 3 or 'wireless' not in parts[2]:
            continue
        name, uuid = parts[0], parts[1]
        ssid_ok, ssid, detail = _profile_ssid_detailed(uuid)
        if not ssid_ok:
            complete = False
            print(
                f'[wifi] saved profile SSID query failed for {uuid!r}: '
                f'{detail or "unknown error"}',
                file=sys.stderr,
            )
            continue
        autoconnect = len(parts) < 4 or parts[3].lower() == 'yes'
        profiles.setdefault(ssid, []).append(
            SavedProfile(ssid, name, uuid, autoconnect=autoconnect)
        )
    return ProfileDiscoveryResult(
        profiles,
        complete=complete,
        code='ok' if complete else 'profile_query_failed',
        message='' if complete else 'Saved network status unavailable',
    )


def saved_profiles():
    """Compatibility helper returning profiles keyed by their real SSID."""
    return _saved_profiles_detailed().profiles


def saved_ssids():
    """Compatibility helper returning actual saved SSIDs, not profile names."""
    return set(saved_profiles())


def active_connection(iface=None):
    iface = iface or wifi_iface()
    rc, out, err = _run(
        [
            '-t',
            '-f',
            'GENERAL.STATE,GENERAL.REASON,GENERAL.CONNECTION,GENERAL.CON-UUID',
            'device',
            'show',
            iface,
        ],
        timeout=10,
    )
    if rc != 0:
        code, message = _state_query_error(rc, out, err)
        print(
            f'[wifi] active connection query failed rc={rc}: '
            f'{_error_text(out, err)}',
            file=sys.stderr,
        )
        return ActiveConnection(
            iface,
            query_ok=False,
            error_code=code,
            error_message=message,
        )

    values = {}
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) >= 2:
            values[parts[0]] = ':'.join(parts[1:])
    if 'GENERAL.STATE' not in values:
        return ActiveConnection(
            iface,
            query_ok=False,
            error_code='state_query_failed',
            error_message='Could not read Wi-Fi state',
        )

    name = values.get('GENERAL.CONNECTION', '')
    uuid = values.get('GENERAL.CON-UUID', '')
    return ActiveConnection(
        iface,
        state=values.get('GENERAL.STATE', ''),
        name='' if name == '--' else name,
        uuid='' if uuid == '--' else uuid,
        reason=values.get('GENERAL.REASON', ''),
    )


def _profile_for_ssid(
        profiles,
        ssid,
        active_uuid='',
        preferred_uuid=''):
    candidates = sorted(
        profiles.get(ssid, []),
        key=lambda profile: (
            not profile.autoconnect,
            profile.name.casefold(),
            profile.uuid,
        ),
    )
    for profile in candidates:
        if profile.uuid == active_uuid:
            return profile
    for profile in candidates:
        if profile.uuid == preferred_uuid:
            return profile
    for profile in candidates:
        if profile.autoconnect:
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
        'profile_known': net.profile_known,
        'autoconnect': net.autoconnect,
        'stale': net.stale,
        'last_seen': net.last_seen,
    }
    values.update(changes)
    return Network(**values)


def _parse_scan_output(
        out,
        profiles=None,
        active=None,
        now=None,
        profiles_complete=True):
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
            profile_known=profile is not None or profiles_complete,
            autoconnect=profile.autoconnect if profile else None,
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
                by_ssid[ssid].profile_known = True
                by_ssid[ssid].autoconnect = profile.autoconnect
            else:
                by_ssid[ssid] = Network(
                    ssid,
                    saved=True,
                    active=True,
                    iface=active.iface,
                    profile_name=profile.name,
                    profile_uuid=profile.uuid,
                    profile_known=True,
                    autoconnect=profile.autoconnect,
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


def _read_scan_rows(profiles, active, iface, profiles_complete=True):
    rc, out, err = _run(_wifi_list_args(iface=iface), timeout=15)
    if rc != 0:
        code, message = _classify_error(rc, out, err, default='scan')
        return [], code, message, _error_text(out, err)
    return (
        _parse_scan_output(
            out,
            profiles,
            active,
            profiles_complete=profiles_complete,
        ),
        'ok',
        '',
        '',
    )


def _merge_scan_cache(fresh, preserve_profile_identity=False):
    now = time.monotonic()
    fresh_by_ssid = {}
    for network in fresh:
        current = _clone_network(network, stale=False, last_seen=now)
        cached = _scan_cache.get(current.ssid)
        if (
                preserve_profile_identity
                and not current.profile_known
                and cached is not None
                and cached.profile_known):
            current = _clone_network(
                current,
                saved=cached.saved,
                profile_name=cached.profile_name,
                profile_uuid=cached.profile_uuid,
                # The identity is useful as a recent hint, but it is not
                # authoritative until a complete profile query confirms it.
                profile_known=False,
                autoconnect=cached.autoconnect,
            )
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
    profile_result = _saved_profiles_detailed()
    profiles = profile_result.profiles
    scan_error = (
        None
        if profile_result.complete
        else (
            profile_result.code,
            profile_result.message,
            profile_result.message,
        )
    )

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
        profiles_complete=profile_result.complete,
    )
    if list_code != 'ok':
        scan_error = scan_error or (list_code, list_message, list_detail)
        print(
            f'[wifi] scan list failed ({list_code}): '
            f'{list_detail or list_message}',
            file=sys.stderr,
        )

    networks, partial = _merge_scan_cache(
        fresh,
        preserve_profile_identity=not profile_result.complete,
    )
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
        if not _reconcile_recovery():
            cached, partial = _recent_cached_networks()
            return ScanResult(
                cached,
                ok=False,
                code='cleanup_pending',
                message='Previous connection cleanup is still pending',
                partial=partial,
            )
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
    saved_value = (
        'Yes'
        if net.saved
        else ('No' if net.profile_known else 'Unknown')
    )
    rows = [
        ('SSID', net.ssid),
        ('SIGNAL', f'{net.signal}%'),
        ('SECURITY', net.security or 'Open'),
        ('PROTECTED', 'Yes' if net.protected else 'No'),
        ('SAVED', saved_value),
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

def _set_recovery_latch(
        iface,
        profile_uuid,
        delete_profile=False,
        network=None,
        reason='',
        reauth_required=False):
    """Remember cleanup that was not safe to consider complete."""
    global _recovery_latch
    _recovery_latch = _RecoveryLatch(
        iface,
        profile_uuid,
        delete_profile=delete_profile,
        network=network,
        reason=reason,
        reauth_required=reauth_required,
    )


def _reconcile_recovery(job=None):
    """Retry and verify pending cleanup before any later Wi-Fi operation."""
    global _recovery_latch
    latch = _recovery_latch
    if latch is None:
        return True

    if job is not None:
        job.set_status('Recovering Wi-Fi...')
    if not _cancel_activation(latch.profile_uuid, latch.iface):
        return False
    if (
            latch.delete_profile
            and not _discard_new_profile(latch.network, latch.profile_uuid)):
        return False

    if latch.reauth_required and not latch.delete_profile:
        _reauth_required.add(latch.profile_uuid)
    _recovery_latch = None
    return True


def _launch_job(job, worker):
    def run():
        if not _operation_lock.acquire(blocking=False):
            job.finish(False, 'Wi-Fi is busy', code='busy')
            return
        job._managed = True
        try:
            if not _reconcile_recovery(job):
                job.finish(
                    False,
                    'Previous connection cleanup is still pending',
                    code='cleanup_pending',
                )
                return
            worker()
        except Exception as exc:
            print(f'[wifi] {job.kind} failed unexpectedly: {exc}', file=sys.stderr)
            job.finish(False, f'{job.kind.title()} failed', code='unexpected')
        finally:
            _operation_lock.release()
            job._managed = False
            job.done = True

    threading.Thread(target=run, daemon=True).start()


def _activate_profile(profile_uuid, iface, password=None):
    """Activate a UUID-addressed profile without interactive nmcli prompts."""
    args = [
        '--wait',
        str(_NMCLI_ACTIVATE_WAIT_SEC),
        'connection',
        'up',
        'uuid',
        profile_uuid,
        'ifname',
        iface,
    ]
    if password is None:
        return _run(args, timeout=_NMCLI_ACTIVATE_TIMEOUT_SEC)

    fd, path = tempfile.mkstemp(prefix='bart-wifi-', text=True)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as secret_file:
            secret_file.write(
                '802-11-wireless-security.psk:' + password + '\n'
            )
        return _run(
            args + ['passwd-file', path],
            timeout=_NMCLI_ACTIVATE_TIMEOUT_SEC,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _wifi_key_mgmt(net):
    """Return the least restrictive supported key management for a scan row."""
    security = net.security.upper()
    if 'WPA3' in security and 'WPA2' not in security:
        return 'sae'
    return 'wpa-psk'


def _new_profile_name(net):
    """Return a unique, human-readable profile name for one SSID."""
    return f'{net.ssid} (BART {uuidlib.uuid4().hex[:8]})'


def _create_profile(net, iface):
    """Create a non-interactive profile and retain its NetworkManager UUID."""
    profile_name = _new_profile_name(net)
    profile_uuid = str(uuidlib.uuid4())
    args = [
        '--wait',
        str(_NMCLI_PROFILE_WAIT_SEC),
        'connection',
        'add',
        'type',
        'wifi',
        'con-name',
        profile_name,
        'ifname',
        iface,
        'ssid',
        net.ssid,
        'connection.uuid',
        profile_uuid,
        'connection.autoconnect',
        'no',
    ]
    if net.protected:
        args += [
            'wifi-sec.key-mgmt',
            _wifi_key_mgmt(net),
            # System-owned secrets returned by the authorized nmcli agent are
            # persisted by NetworkManager for disconnect/reboot reconnects.
            'wifi-sec.psk-flags',
            '0',
        ]

    # Do not pass a BSSID: persisting it would pin mesh networks to one access
    # point and break roaming. The password is supplied only during activation.
    rc, out, err = _run(args, timeout=_NMCLI_PROFILE_TIMEOUT_SEC)
    if rc != 0:
        # A timed-out add can have reached NetworkManager even though nmcli did
        # not observe the reply. Because the UUID is caller-assigned, the
        # uncertain profile can still be located and removed without relying
        # on a non-unique human-readable name.
        exists = _profile_exists(profile_uuid)
        if exists is True:
            if not _discard_new_profile(None, profile_uuid):
                _set_recovery_latch(
                    iface,
                    profile_uuid,
                    delete_profile=True,
                    reason='profile creation cleanup',
                )
        elif exists is None:
            _set_recovery_latch(
                iface,
                profile_uuid,
                delete_profile=True,
                reason='ambiguous profile creation',
            )
        return rc, out, err, ''

    # Keep the identity through activation so cleanup targets the exact profile.
    # Failed first-time profiles are discarded before a later retry.
    net.profile_name = profile_name
    net.profile_uuid = profile_uuid
    net.saved = True
    net.profile_known = True
    net.autoconnect = False
    return 0, out, err, profile_uuid


def _enable_profile_autoconnect(profile_uuid):
    """Enable autoconnect only after a profile has connected successfully."""
    return _run(
        [
            '--wait',
            str(_NMCLI_PROFILE_WAIT_SEC),
            'connection',
            'modify',
            'uuid',
            profile_uuid,
            'connection.autoconnect',
            'yes',
        ],
        timeout=_NMCLI_PROFILE_TIMEOUT_SEC,
    )


def _profile_autoconnect(profile_uuid):
    """Read a profile's current autoconnect preference."""
    rc, out, err = _run(
        [
            '-g',
            'connection.autoconnect',
            'connection',
            'show',
            'uuid',
            profile_uuid,
        ],
        timeout=_NMCLI_PROFILE_TIMEOUT_SEC,
    )
    if rc != 0:
        return rc, out, err, None
    value = next(
        (line.strip().lower() for line in out.splitlines() if line.strip()),
        '',
    )
    if value not in ('yes', 'no'):
        return 1, out, 'Could not read profile autoconnect setting', None
    return 0, out, err, value == 'yes'


def _profile_autoconnect_marker(profile_uuid):
    """Read the crash-safe original autoconnect value stored on the profile."""
    rc, out, err = _run(
        [
            '-g',
            'user.data',
            'connection',
            'show',
            'uuid',
            profile_uuid,
        ],
        timeout=_NMCLI_PROFILE_TIMEOUT_SEC,
    )
    if rc != 0:
        return rc, out, err, None

    # nmcli renders dictionary entries as key=value pairs. It may separate
    # entries with commas or newlines depending on output mode/version.
    for entry in out.replace('\n', ',').split(','):
        key, separator, value = entry.partition('=')
        if separator and key.strip() == _AUTOCONNECT_MARKER:
            normalized = value.strip().lower()
            if normalized in ('yes', 'no'):
                return 0, out, err, normalized == 'yes'
            return 1, out, 'Invalid saved autoconnect recovery value', None
    return 0, out, err, None


def _load_autoconnect_restore(profile_uuid):
    """Load a pending original preference from NetworkManager after restart."""
    if profile_uuid in _autoconnect_restore:
        return 0, '', ''
    rc, out, err, original = _profile_autoconnect_marker(profile_uuid)
    if rc == 0 and original is not None:
        _autoconnect_restore[profile_uuid] = original
    return rc, out, err


def _prepare_saved_psk(profile_uuid):
    """Block autoconnect before accepting a replacement system-owned PSK."""
    marker_rc, marker_out, marker_err, marker_original = (
        _profile_autoconnect_marker(profile_uuid)
    )
    if marker_rc != 0:
        return marker_rc, marker_out, marker_err, None

    marker_exists = marker_original is not None
    if marker_exists:
        original_autoconnect = marker_original
    elif profile_uuid in _autoconnect_restore:
        original_autoconnect = _autoconnect_restore[profile_uuid]
    else:
        rc, out, err, current_autoconnect = _profile_autoconnect(profile_uuid)
        if rc != 0:
            return rc, out, err, None
        original_autoconnect = current_autoconnect

    args = [
        '--wait',
        str(_NMCLI_PROFILE_WAIT_SEC),
        'connection',
        'modify',
        'uuid',
        profile_uuid,
        # One atomic profile update: modification unblocks a connection after
        # `connection down`, so it must already be ineligible for autoconnect
        # when the stale PSK is removed.
        'connection.autoconnect',
        'no',
        'wifi-sec.psk-flags',
        '0',
        # passwd-file is a secret-agent source. Clearing the stored value makes
        # NetworkManager request and persist the replacement typed by the user.
        'wifi-sec.psk',
        '',
    ]
    if not marker_exists:
        args += [
            '+user.data',
            (
                f'{_AUTOCONNECT_MARKER}='
                f'{"yes" if original_autoconnect else "no"}'
            ),
        ]

    rc, out, err = _run(args, timeout=_NMCLI_PROFILE_TIMEOUT_SEC)
    if rc == 0:
        _autoconnect_restore[profile_uuid] = original_autoconnect
    return rc, out, err, original_autoconnect


def _restore_saved_autoconnect(profile_uuid):
    """Restore the preference captured before a replacement-password attempt."""
    rc, out, err = _load_autoconnect_restore(profile_uuid)
    if rc != 0:
        return rc, out, err
    if profile_uuid not in _autoconnect_restore:
        return 0, '', ''

    original = _autoconnect_restore[profile_uuid]
    rc, out, err = _run(
        [
            '--wait',
            str(_NMCLI_PROFILE_WAIT_SEC),
            'connection',
            'modify',
            'uuid',
            profile_uuid,
            'connection.autoconnect',
            'yes' if original else 'no',
            '-user.data',
            _AUTOCONNECT_MARKER,
        ],
        timeout=_NMCLI_PROFILE_TIMEOUT_SEC,
    )
    if rc == 0:
        _autoconnect_restore.pop(profile_uuid, None)
    return rc, out, err


def _wait_activation_stopped(profile_uuid, iface):
    """Confirm that a profile is no longer active or activating on a device."""
    deadline = time.monotonic() + _CANCEL_VERIFY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        current = active_connection(iface)
        if current.query_ok and current.uuid != profile_uuid:
            return True
        time.sleep(0.25)
    return False


def _cancel_activation(profile_uuid, iface):
    """Stop and verify an activation that outlived a bounded wait."""
    args = [
        '--wait',
        str(_NMCLI_PROFILE_WAIT_SEC),
        'connection',
        'down',
        'uuid',
        profile_uuid,
    ]
    for _ in range(2):
        _run(args, timeout=_NMCLI_PROFILE_TIMEOUT_SEC)
        if _wait_activation_stopped(profile_uuid, iface):
            return True
    return False


def _profile_exists(profile_uuid):
    """Return True/False for a verified profile lookup, or None on read error."""
    rc, out, err = _run(
        [
            '-g',
            'connection.uuid',
            'connection',
            'show',
            'uuid',
            profile_uuid,
        ],
        timeout=_NMCLI_PROFILE_TIMEOUT_SEC,
    )
    if rc == 0:
        return bool(out.strip())
    lower = _error_text(out, err).lower()
    if (
            rc == 10
            or 'not found' in lower
            or 'unknown connection' in lower
            or 'does not exist' in lower):
        return False
    return None


def _discard_new_profile(net, profile_uuid):
    """Remove a profile that never reached a verified connection."""
    rc, out, err = _run(
        [
            '--wait',
            str(_NMCLI_PROFILE_WAIT_SEC),
            'connection',
            'delete',
            'uuid',
            profile_uuid,
        ],
        timeout=_NMCLI_PROFILE_TIMEOUT_SEC,
    )
    exists = _profile_exists(profile_uuid)
    if exists is not False:
        print(
            f'[wifi] failed to remove unverified profile '
            f'{profile_uuid!r}: '
            f'{_error_text(out, err) or "deletion could not be verified"}',
            file=sys.stderr,
        )
        return False

    if net is not None and net.profile_uuid == profile_uuid:
        net.profile_name = ''
        net.profile_uuid = ''
        net.saved = False
        net.profile_known = True
        net.autoconnect = None
    _autoconnect_restore.pop(profile_uuid, None)
    _reauth_required.discard(profile_uuid)
    return True


def _verify_connected(iface, expected_uuid='', expected_ssid=''):
    deadline = time.monotonic() + _CONNECT_VERIFY_TIMEOUT_SEC
    last_state_error = ''
    saw_valid_state = False
    while time.monotonic() < deadline:
        active = active_connection(iface)
        if not active.query_ok:
            last_state_error = active.error_message
            time.sleep(0.5)
            continue
        saw_valid_state = True
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
    if not saw_valid_state and last_state_error:
        return False, '', last_state_error
    return False, '', 'Connection activated without an IPv4 address'


def _activation_timeout_stage(iface, expected_uuid='', protected=False):
    """Best-effort classification of where a timed-out activation stalled."""
    active = active_connection(iface)
    if not active.query_ok:
        return 'unknown'
    if expected_uuid and active.uuid and active.uuid != expected_uuid:
        return 'unknown'

    reason = active.reason.lower()
    try:
        reason_code = int(reason.split()[0])
    except (ValueError, IndexError):
        reason_code = -1
    if any(
            token in reason
            for token in (
                'secret',
                'password',
                'authentication',
                'no-secrets',
            )) or reason_code in range(7, 12):
        return 'authentication'
    if any(
            token in reason
            for token in (
                'dhcp',
                'ip-config',
                'ip config',
                'ip configuration',
                'lease',
            )):
        return 'ip'

    try:
        state = int(active.state.split()[0])
    except (ValueError, IndexError):
        return 'unknown'
    if protected and state == 50:  # NM_DEVICE_STATE_CONFIG (Wi-Fi association)
        return 'authentication'
    if state == 60:  # NM_DEVICE_STATE_NEED_AUTH
        return 'authentication'
    if 70 <= state <= 100:  # IP_CONFIG through ACTIVATED
        return 'ip'
    return 'unknown'


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

    iface = net.iface or wifi_iface()
    preferred_uuid = net.profile_uuid
    expected_uuid = ''
    created_profile = False
    prepared_saved_psk = False
    original_autoconnect = None

    # Re-resolve every selected identity immediately before activation. Cached
    # scan data is a display hint, not proof that a UUID still exists or still
    # belongs to this SSID.
    job.set_status('Checking saved network...')
    discovery = _saved_profiles_detailed()
    if not discovery.complete:
        net.profile_known = False
        job.finish(
            False,
            discovery.message or 'Saved network status unavailable',
            code=discovery.code or 'profile_query_failed',
        )
        return
    active = active_connection(iface)
    active_uuid = active.uuid if active.query_ok else ''
    profile = _profile_for_ssid(
        discovery.profiles,
        net.ssid,
        active_uuid=active_uuid,
        preferred_uuid=preferred_uuid,
    )
    net.profile_known = True
    if profile is not None:
        net.saved = True
        net.profile_name = profile.name
        net.profile_uuid = profile.uuid
        net.autoconnect = profile.autoconnect
        expected_uuid = profile.uuid
    else:
        net.saved = False
        net.profile_name = ''
        net.profile_uuid = ''
        net.autoconnect = None
        if preferred_uuid:
            _reauth_required.discard(preferred_uuid)

    if expected_uuid in _reauth_required and password is None:
        job.finish(
            False,
            'Password required after the previous timed-out attempt',
            code='authentication_timeout',
            needs_password=True,
        )
        return

    if net.protected and not expected_uuid and not password:
        job.finish(
            False,
            'Password required',
            code='authentication_required',
            needs_password=True,
        )
        return

    job.set_status('Authenticating...' if net.protected else 'Connecting...')
    if not expected_uuid:
        rc, out, err, expected_uuid = _create_profile(net, iface)
        if rc != 0:
            code, message = _classify_error(rc, out, err)
            print(
                f'[wifi] profile creation for {net.ssid!r} failed ({code}): '
                f'{_error_text(out, err)}',
                file=sys.stderr,
            )
            job.finish(False, message, code=code)
            return
        created_profile = True

    secret = password if net.protected and password is not None else None
    if secret is not None and not created_profile:
        (
            flags_rc,
            flags_out,
            flags_err,
            original_autoconnect,
        ) = _prepare_saved_psk(expected_uuid)
        if flags_rc != 0:
            code, message = _classify_error(flags_rc, flags_out, flags_err)
            print(
                f'[wifi] could not prepare saved credentials for '
                f'{net.ssid!r} ({code}): {_error_text(flags_out, flags_err)}',
                file=sys.stderr,
            )
            job.finish(False, message, code=code)
            return
        prepared_saved_psk = True
        net.autoconnect = False

    rc, out, err = _activate_profile(
        expected_uuid,
        iface,
        password=secret,
    )

    verified = False
    active_uuid = ''
    detail = ''
    if rc != 0:
        code, message = _classify_error(rc, out, err)
        timed_out = _nmcli_timed_out(rc, out, err)
        timeout_stage = 'unknown'
        if timed_out:
            timeout_stage = (
                'authentication'
                if code == 'authentication_failed'
                else _activation_timeout_stage(
                    iface,
                    expected_uuid,
                    protected=net.protected,
                )
            )

            # nmcli can exhaust its wait at the same instant NetworkManager
            # enters IP configuration or finishes activating. Give that stage
            # the normal bounded verification window before tearing down what
            # may already be a valid connection.
            if timeout_stage == 'ip':
                job.set_status('Obtaining IP...')
                verified, active_uuid, detail = _verify_connected(
                    iface,
                    expected_uuid=expected_uuid,
                    expected_ssid=net.ssid,
                )
                if verified:
                    rc = 0

        if rc != 0:
            if (
                    secret is not None
                    and not created_profile
                    and (
                        code == 'dhcp_failed'
                        or timeout_stage == 'ip'
                    )):
                # Reaching IP configuration proves the replacement secret was
                # accepted. Do not make a later retry ask for it again merely
                # because address assignment failed.
                _reauth_required.discard(expected_uuid)
            cancelled = True
            if timed_out:
                job.set_status('Stopping failed attempt...')
                cancelled = _cancel_activation(expected_uuid, iface)
                if code in (
                        'authentication_failed',
                        'network_not_found',
                        'dhcp_failed',
                ):
                    pass
                elif net.protected and timeout_stage == 'authentication':
                    code = 'authentication_timeout'
                    message = 'Authentication timed out'
                elif timeout_stage == 'ip':
                    code = 'dhcp_failed'
                    message = 'Could not obtain IP'
                else:
                    code = 'timeout'
                    message = 'Connection timed out'
            reauth_after_cleanup = (
                net.protected
                and code in ('authentication_failed', 'authentication_timeout')
            )
            discarded = True
            if created_profile:
                discarded = _discard_new_profile(net, expected_uuid)
                if timed_out and not cancelled and discarded:
                    cancelled = _wait_activation_stopped(expected_uuid, iface)
            cleanup_ok = (not timed_out or cancelled) and discarded
            if not cleanup_ok:
                _set_recovery_latch(
                    iface,
                    expected_uuid,
                    delete_profile=created_profile and not discarded,
                    network=net,
                    reason='activation failure cleanup',
                    reauth_required=(
                        reauth_after_cleanup and not created_profile
                    ),
                )
                code = 'cleanup_failed'
                message = 'Could not stop connection attempt'
            needs_password = (
                cleanup_ok and reauth_after_cleanup
            )
            if needs_password and not created_profile:
                # Keep a cancelled/ignored password prompt from causing the
                # next Connect tap to retry the same rejected secret for
                # another full activation timeout.
                _reauth_required.add(expected_uuid)
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

    if not verified:
        job.set_status('Obtaining IP...')
        verified, active_uuid, detail = _verify_connected(
            iface,
            expected_uuid=expected_uuid,
            expected_ssid=net.ssid,
        )
    if not verified:
        print(f'[wifi] connect verification failed: {detail}', file=sys.stderr)
        if secret is not None and not created_profile:
            # A successful activation command has already accepted the
            # supplied secret even if UUID/IP verification later fails.
            _reauth_required.discard(expected_uuid)
        job.set_status('Stopping failed attempt...')
        cancelled = _cancel_activation(expected_uuid, iface)
        discarded = True
        if created_profile:
            discarded = _discard_new_profile(net, expected_uuid)
        if not cancelled and discarded:
            cancelled = _wait_activation_stopped(expected_uuid, iface)
        if not cancelled or not discarded:
            _set_recovery_latch(
                iface,
                expected_uuid,
                delete_profile=created_profile and not discarded,
                network=net,
                reason='connection verification cleanup',
            )
            job.finish(
                False,
                'Could not stop connection attempt',
                code='cleanup_failed',
            )
            return
        job.finish(False, 'Could not obtain IP', code='dhcp_failed')
        return

    net.profile_uuid = active_uuid
    net.saved = True
    net.active = True
    net.profile_known = True
    _reauth_required.discard(active_uuid)

    if created_profile:
        auto_rc, auto_out, auto_err = _enable_profile_autoconnect(active_uuid)
        if auto_rc != 0:
            print(
                f'[wifi] connected to {net.ssid!r}, but enabling autoconnect '
                f'failed: {_error_text(auto_out, auto_err)}',
                file=sys.stderr,
            )
            job.finish(
                True,
                f'Connected to {net.ssid}; auto-reconnect unavailable',
                code='connected_warning',
            )
            return
        net.autoconnect = True
    else:
        if (
                not prepared_saved_psk
                and active_uuid not in _autoconnect_restore
                and net.autoconnect is False):
            marker_rc, marker_out, marker_err = _load_autoconnect_restore(
                active_uuid
            )
            if marker_rc != 0:
                print(
                    f'[wifi] connected to {net.ssid!r}, but reading pending '
                    f'autoconnect recovery failed: '
                    f'{_error_text(marker_out, marker_err)}',
                    file=sys.stderr,
                )
                job.finish(
                    True,
                    f'Connected to {net.ssid}; auto-reconnect status unknown',
                    code='connected_warning',
                )
                return
        pending_autoconnect = (
            prepared_saved_psk
            or active_uuid in _autoconnect_restore
        )
        restored_value = (
            original_autoconnect
            if prepared_saved_psk
            else _autoconnect_restore.get(active_uuid)
        )
    if not created_profile and pending_autoconnect:
        auto_rc, auto_out, auto_err = _restore_saved_autoconnect(active_uuid)
        if auto_rc != 0:
            print(
                f'[wifi] connected to {net.ssid!r}, but restoring autoconnect '
                f'failed: {_error_text(auto_out, auto_err)}',
                file=sys.stderr,
            )
            job.finish(
                True,
                f'Connected to {net.ssid}; auto-reconnect unavailable',
                code='connected_warning',
            )
            return
        net.autoconnect = restored_value
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
    if not active.query_ok:
        job.finish(
            False,
            active.error_message or 'Could not read Wi-Fi state',
            code=active.error_code or 'state_query_failed',
        )
        return

    if not active.uuid:
        net.active = False
        job.finish(True, f'{net.ssid} is already disconnected', code='disconnected')
        return

    target_uuid = net.profile_uuid
    if target_uuid != active.uuid:
        ssid_ok, active_ssid, detail = _profile_ssid_detailed(active.uuid)
        if not ssid_ok:
            print(
                f'[wifi] active profile identity query failed: {detail}',
                file=sys.stderr,
            )
            job.finish(
                False,
                'Could not identify active connection',
                code='identity_failed',
            )
            return
        if active_ssid != net.ssid:
            net.active = False
            job.finish(
                True,
                f'{net.ssid} is already disconnected',
                code='disconnected',
            )
            return
        target_uuid = active.uuid

    if not target_uuid:
        job.finish(
            False,
            'Could not identify active connection',
            code='identity_failed',
        )
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
    last_query_error = None
    while time.monotonic() < deadline:
        current = active_connection(iface)
        if not current.query_ok:
            last_query_error = current
            time.sleep(0.25)
            continue
        last_query_error = None
        if current.uuid != active.uuid:
            net.active = False
            job.finish(
                True,
                f'Disconnected from {net.ssid}',
                code='disconnected',
            )
            return
        time.sleep(0.25)

    if last_query_error is not None:
        job.finish(
            False,
            last_query_error.error_message or 'Could not read Wi-Fi state',
            code=last_query_error.error_code or 'state_query_failed',
        )
        return
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
        if not _reconcile_recovery():
            return False, 'Previous connection cleanup is still pending'
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
