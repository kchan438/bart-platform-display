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
_SCAN_MIN_PASSES = 2
_SCAN_MAX_PASSES = 3
_SCAN_COLLAPSE_MIN_BASELINE = 3
_CONNECT_VERIFY_TIMEOUT_SEC = 15.0
_NMCLI_ACTIVATE_WAIT_SEC = 45
_NMCLI_ACTIVATE_TIMEOUT_SEC = 55
_NMCLI_PROFILE_WAIT_SEC = 10
_NMCLI_PROFILE_TIMEOUT_SEC = 15
_CANCEL_VERIFY_TIMEOUT_SEC = 4.0
_SUPERVISOR_POLL_SEC = 3.0
_SUPERVISOR_GRACE_SEC = 20.0
_SUPERVISOR_RETRY_DELAY_SEC = 8.0
_SUPERVISOR_MAX_RETRIES = 2

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
        'detected',
        'stale',
        'last_seen',
        'in_use',
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
            detected=True,
            stale=False,
            last_seen=0.0,
            in_use=False):
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
        self.detected = detected
        self.stale = stale
        self.last_seen = last_seen
        self.in_use = in_use

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

    __slots__ = (
        'networks',
        'ok',
        'code',
        'message',
        'partial',
        'completed_at',
    )

    def __init__(
            self,
            networks=None,
            ok=True,
            code='ok',
            message='',
            partial=False,
            completed_at=None):
        self.networks = networks or []
        self.ok = ok
        self.code = code
        self.message = message
        self.partial = partial
        self.completed_at = (
            time.time()
            if completed_at is None
            else completed_at
        )


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
        'completed_at',
        '_managed',
    )

    def __init__(self, kind, ssid=''):
        self.kind = kind
        self.ssid = ssid
        self.status = {
            'scan': 'Scanning...',
            'connect': 'Connecting...',
            'disconnect': 'Disconnecting...',
            'forget': 'Forgetting network...',
        }.get(kind, 'Working...')
        self.message = ''
        self.code = ''
        self.done = False
        self.ok = False
        self.needs_password = False
        self.networks = []
        self.partial = False
        self.completed_at = 0.0
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
            partial=False,
            completed_at=0.0):
        self.ok = ok
        self.message = message
        self.status = message
        self.code = code
        self.needs_password = needs_password
        if networks is not None:
            self.networks = networks
        self.partial = partial
        self.completed_at = completed_at
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
    if (
            'association rejected' in lower
            or 'supplicant failed' in lower
            or 'supplicant configuration' in lower):
        return 'authentication_failed', 'Router rejected the Wi-Fi connection'
    if (
            'not found' in lower
            or 'no network with ssid' in lower
            or 'no suitable network' in lower):
        return 'network_not_found', 'Network not found'
    if 'duplicate ip' in lower or 'ip address conflict' in lower:
        return 'ip_conflict', 'Another device is using this IP address'
    if 'dhcp' in lower or 'ip configuration' in lower:
        return 'dhcp_failed', 'Could not obtain IP'
    if 'unmanaged' in lower:
        return 'adapter_unmanaged', 'Wi-Fi adapter is unmanaged'
    if 'firmware' in lower and ('missing' in lower or 'unavailable' in lower):
        return 'firmware_missing', 'Wi-Fi firmware is unavailable'
    if _nmcli_timed_out(rc, out, err):
        return 'timeout', 'Timed out'
    if default == 'scan':
        return 'scan_failed', 'Scan failed'
    if default == 'disconnect':
        return 'disconnect_failed', 'Disconnect failed'
    if default == 'forget':
        return 'forget_failed', 'Could not forget network'
    return 'connect_failed', 'Failed to connect'


def _state_query_error(rc, out, err):
    """Classify a device-state read without calling it a connect failure."""
    code, message = _classify_error(rc, out, err)
    if code == 'connect_failed':
        return 'state_query_failed', 'Could not read Wi-Fi state'
    return code, message


def _leading_int(value, default=-1):
    try:
        return int(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return default


def _classify_device_reason(reason):
    """Map NetworkManager device reasons to stable user-facing failures."""
    reason_code = _leading_int(reason)
    if reason_code == 5:
        return 'dhcp_failed', 'Could not obtain IP'
    if reason_code == 6:
        return 'ip_expired', 'The network address expired'
    if reason_code == 7:
        return 'authentication_required', 'Saved password is unavailable'
    if reason_code in (9, 10, 11):
        return 'authentication_failed', 'Authentication failed'
    if reason_code == 8:
        return 'connection_lost', 'The Wi-Fi link was lost'
    if reason_code in (15, 16, 17):
        return 'dhcp_failed', 'Could not obtain IP'
    if reason_code == 35:
        return 'firmware_missing', 'Wi-Fi firmware is unavailable'
    if reason_code == 36:
        return 'adapter_missing', 'Wi-Fi adapter was removed'
    if reason_code == 37:
        return 'network_sleeping', 'NetworkManager is sleeping'
    if reason_code == 38:
        return 'profile_missing', 'The saved network profile was removed'
    if reason_code == 39:
        return 'user_disconnected', 'Disconnected by user request'
    if reason_code == 40:
        return 'connection_lost', 'The Wi-Fi link changed'
    if reason_code == 53:
        return 'network_not_found', 'Network is not currently detected'
    if reason_code == 64:
        return 'ip_conflict', 'Another device is using this IP address'
    if reason_code in (3, 69, 70, 71, 72, 73, 74):
        return 'adapter_unmanaged', 'Wi-Fi adapter is unmanaged'
    return 'connection_lost', 'The Wi-Fi connection was lost'


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


def _wifi_iface_detailed():
    """Return a managed Wi-Fi interface or an explicit discovery failure."""
    rc, out, err = _run(
        ['-t', '-f', 'DEVICE,TYPE,STATE', 'device'],
        timeout=10,
    )
    if rc != 0:
        code, message = _state_query_error(rc, out, err)
        if code == 'state_query_failed':
            code = 'adapter_query_failed'
            message = 'Could not read Wi-Fi adapter state'
        return '', code, message, _error_text(out, err)

    wifi_devices = []
    for line in out.splitlines():
        parts = _split_terse(line)
        if (
                len(parts) >= 3
                and parts[1] == 'wifi'
                and not parts[0].startswith('p2p-')):
            wifi_devices.append((parts[0], parts[2].strip().lower()))

    for iface, state in wifi_devices:
        if state not in ('unmanaged', 'unavailable'):
            return iface, 'ok', '', ''
    if any(state == 'unmanaged' for _, state in wifi_devices):
        return '', 'adapter_unmanaged', 'Wi-Fi adapter is unmanaged', ''
    if wifi_devices:
        return '', 'adapter_unavailable', 'Wi-Fi adapter unavailable', ''
    return '', 'adapter_missing', 'Wi-Fi adapter not found', ''


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
    ok, address, _, _ = _device_ipv4_detailed(iface)
    return address if ok else ''


def _device_ipv4_detailed(iface):
    rc, out, err = _run(
        ['-g', 'IP4.ADDRESS', 'device', 'show', iface],
        timeout=10,
    )
    if rc != 0:
        code, message = _state_query_error(rc, out, err)
        if code == 'state_query_failed':
            code = 'ip_query_failed'
            message = 'Could not read Wi-Fi address'
        return False, '', code, message
    address = next(
        (line.strip() for line in out.splitlines() if line.strip()),
        '',
    )
    return True, address, 'ok', ''


def _connection_access_warning(iface):
    """Return a non-fatal route/connectivity warning after link activation."""
    gateway_rc, gateway_out, _ = _run(
        ['-g', 'IP4.GATEWAY', 'device', 'show', iface],
        timeout=10,
    )
    if gateway_rc == 0 and not gateway_out.strip():
        return 'no_route', 'Connected locally, but no network route is available'

    connectivity_rc, connectivity_out, _ = _run(
        ['-t', '-f', 'CONNECTIVITY', 'general'],
        timeout=10,
    )
    if connectivity_rc != 0:
        return '', ''
    connectivity = connectivity_out.strip().lower()
    if connectivity == 'portal':
        return 'captive_portal', 'Connected, but a captive portal requires login'
    if connectivity in ('limited', 'none'):
        return 'internet_unavailable', 'Connected locally, but Internet is unavailable'
    return '', ''


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
        'detected': net.detected,
        'stale': net.stale,
        'last_seen': net.last_seen,
        'in_use': net.in_use,
    }
    values.update(changes)
    return Network(**values)


def _network_sort_key(net):
    """Keep the active and saved networks above unsaved scan results."""
    return (
        not net.active,
        not net.saved,
        net.stale,
        -net.signal,
        net.ssid.lower(),
    )


def _parse_scan_output(
        out,
        profiles=None,
        active=None,
        now=None,
        profiles_complete=True,
        deduplicate=True):
    """Parse access points, optionally retaining every BSSID."""
    profiles = profiles or {}
    active = active or ActiveConnection('')
    now = time.monotonic() if now is None else now
    by_bssid = {}
    detected_ssids = set()

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
            detected=True,
            last_seen=now,
            in_use=row_active,
        )
        key = (ssid, bssid)
        existing = by_bssid.get(key)
        if existing is None or _prefer_network(network, existing):
            by_bssid[key] = network
        detected_ssids.add(ssid)

    # Saved networks remain actionable state even when NetworkManager's current
    # AP list omits them. Keep one row per saved SSID and make the distinction
    # explicit so callers do not mistake a saved-only row for a detected AP.
    for ssid, candidates in profiles.items():
        profile = _profile_for_ssid(profiles, ssid, active.uuid)
        if profile is None:
            continue
        profile_active = bool(active.uuid and profile.uuid == active.uuid)
        if ssid in detected_ssids:
            continue
        by_bssid[(ssid, '')] = Network(
            ssid,
            saved=True,
            active=profile_active,
            iface=active.iface if profile_active else '',
            profile_name=profile.name,
            profile_uuid=profile.uuid,
            profile_known=True,
            autoconnect=profile.autoconnect,
            detected=False,
            last_seen=0.0,
        )

    networks = list(by_bssid.values())
    if deduplicate:
        return _deduplicate_scan_networks(networks)
    networks.sort(key=_network_sort_key)
    return networks


def _prefer_network(candidate, current):
    """Return whether a BSSID/SSID candidate is better evidence."""
    return (
        candidate.in_use,
        candidate.active,
        candidate.detected,
        candidate.signal,
    ) > (
        current.in_use,
        current.active,
        current.detected,
        current.signal,
    )


def _deduplicate_scan_networks(networks):
    """Choose the confirmed in-use or strongest detected AP for each SSID."""
    by_ssid = {}
    for network in networks:
        current = by_ssid.get(network.ssid)
        if current is None or _prefer_network(network, current):
            by_ssid[network.ssid] = network
    result = list(by_ssid.values())
    result.sort(key=_network_sort_key)
    return result


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
        code, message, detail = _normalize_scan_error(
            (code, message, _error_text(out, err))
        )
        return False, code, message, detail

    if not _HAVE_BUSCTL:
        # Desktop/mock environments may not expose LastScan. The request is
        # still asynchronous, so give NetworkManager a bounded settle window.
        time.sleep(2.0)
        return True, 'ok', '', ''
    if not path or before is None:
        # Do not claim a verified hardware pass when the Pi exposes busctl but
        # its NetworkManager LastScan state could not be read.
        time.sleep(2.0)
        return (
            False,
            'scan_verification_failed',
            'Could not verify scan completion',
            'NetworkManager LastScan state unavailable',
        )

    deadline = time.monotonic() + _SCAN_COMPLETE_TIMEOUT_SEC
    while time.monotonic() < deadline:
        current = _last_scan(path)
        if current is not None and current > before:
            return True, 'ok', '', ''
        time.sleep(0.25)
    return False, 'scan_timeout', 'Scan timed out', 'LastScan did not advance'


def _normalize_scan_error(error):
    """Use one public code for NetworkManager and verification timeouts."""
    if error is None or error[0] != 'timeout':
        return error
    return 'scan_timeout', 'Scan timed out', error[2]


def _prefer_scan_error(existing, candidate):
    """Keep the failure that gives the user the most actionable diagnosis."""
    existing = _normalize_scan_error(existing)
    candidate = _normalize_scan_error(candidate)
    if candidate is None:
        return existing
    if existing is None:
        return candidate
    priority = {
        'not_authorized': 100,
        'scan_timeout': 90,
        'scan_failed': 80,
        'scan_verification_failed': 70,
        'state_query_failed': 60,
        'ip_query_failed': 60,
        'profile_query_failed': 20,
    }
    return (
        candidate
        if priority.get(candidate[0], 50) > priority.get(existing[0], 50)
        else existing
    )


def _read_scan_rows(profiles, active, iface, profiles_complete=True):
    rc, out, err = _run(_wifi_list_args(iface=iface), timeout=15)
    if rc != 0:
        code, message = _classify_error(rc, out, err, default='scan')
        code, message, detail = _normalize_scan_error(
            (code, message, _error_text(out, err))
        )
        return [], code, message, detail
    return (
        _parse_scan_output(
            out,
            profiles,
            active,
            profiles_complete=profiles_complete,
            deduplicate=False,
        ),
        'ok',
        '',
        '',
    )


def _union_scan_passes(existing, incoming):
    """Union scan passes by BSSID before final SSID de-duplication."""
    by_bssid = {
        (network.ssid, network.bssid): network
        for network in existing
    }
    for network in incoming:
        key = (network.ssid, network.bssid)
        current = by_bssid.get(key)
        if current is None or _prefer_network(network, current):
            by_bssid[key] = network
    networks = list(by_bssid.values())
    networks.sort(key=_network_sort_key)
    return networks


def _reconcile_scan_activity(networks, final_rows, active, profiles=None):
    """Replace stale pass flags with the final NetworkManager observation."""
    profiles = profiles or {}
    final_by_bssid = {
        (network.ssid, network.bssid): network
        for network in final_rows
    }
    final_in_use = {
        (network.ssid, network.bssid)
        for network in final_rows
        if network.in_use
    }
    active_uuid = (
        active.uuid
        if active.query_ok and _leading_int(active.state) == 100
        else ''
    )
    active_ssid = ''
    active_profile = None
    if active_uuid:
        for ssid, candidates in profiles.items():
            active_profile = next(
                (
                    profile
                    for profile in candidates
                    if profile.uuid == active_uuid
                ),
                None,
            )
            if active_profile is not None:
                active_ssid = ssid
                break
    reconciled = []
    for network in networks:
        key = (network.ssid, network.bssid)
        final_network = final_by_bssid.get(key)
        if final_network is not None:
            # The active UUID can change between passes for duplicate saved
            # profiles. Keep unioned signal evidence, but make identity come
            # from the same final observation used for activity.
            network = _clone_network(
                network,
                saved=final_network.saved,
                profile_name=final_network.profile_name,
                profile_uuid=final_network.profile_uuid,
                profile_known=final_network.profile_known,
                autoconnect=final_network.autoconnect,
            )
        identity_rebound = bool(
            active_profile is not None
            and network.ssid == active_ssid
            and network.profile_uuid != active_uuid
        )
        if active_profile is not None and network.ssid == active_ssid:
            # A later unverified pass can still observe a new connected UUID.
            # Resolve that UUID through saved-profile state without treating
            # the failed pass's AP cache as current discovery evidence.
            network = _clone_network(
                network,
                saved=True,
                profile_name=active_profile.name,
                profile_uuid=active_profile.uuid,
                profile_known=True,
                autoconnect=active_profile.autoconnect,
            )
        row_in_use = key in final_in_use
        if active.query_ok:
            profile_active = bool(
                active_uuid
                and network.profile_uuid
                and network.profile_uuid == active_uuid
            )
            # A final IN-USE marker is still useful when saved-profile
            # discovery was incomplete and could not resolve the active UUID.
            unresolved_active = bool(
                active_uuid and row_in_use and not network.profile_uuid
            )
            is_active = profile_active or unresolved_active
            # IN-USE is BSSID evidence from the last readable pass. If the
            # current device state belongs to a different profile, retain the
            # verified active UUID but do not transfer that older BSSID marker.
            is_in_use = row_in_use and is_active and not identity_rebound
        else:
            # If the state query failed, retain only the final list command's
            # direct evidence; never carry an earlier pass's marker forward.
            is_in_use = row_in_use
            is_active = row_in_use
        reconciled.append(
            _clone_network(
                network,
                active=is_active,
                in_use=is_in_use,
            )
        )
    return reconciled


def _detected_network_count(networks):
    return len({
        network.ssid
        for network in networks
        if network.detected
    })


def _recent_scan_baseline_count(now=None):
    """Return the number of distinct AP SSIDs detected in the recent cache."""
    now = time.monotonic() if now is None else now
    return sum(
        1
        for network in _scan_cache.values()
        if (
            network.detected
            and network.last_seen > 0
            and now - network.last_seen <= _SCAN_CACHE_TTL_SEC
        )
    )


def _scan_severely_collapsed(networks, baseline_count):
    """Detect a result below half of a meaningful recent baseline."""
    return (
        baseline_count >= _SCAN_COLLAPSE_MIN_BASELINE
        and _detected_network_count(networks) * 2 < baseline_count
    )


def _merge_scan_cache(fresh, preserve_profile_identity=False):
    now = time.monotonic()
    fresh_by_ssid = {}
    for network in fresh:
        cached = _scan_cache.get(network.ssid)
        last_seen = now if network.detected else (
            cached.last_seen if cached is not None else network.last_seen
        )
        current = _clone_network(network, stale=False, last_seen=last_seen)
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
        if current.detected:
            _scan_cache[current.ssid] = _clone_network(current)

    merged = dict(fresh_by_ssid)
    expired = []
    for ssid, cached in _scan_cache.items():
        age = now - cached.last_seen
        if age > _SCAN_CACHE_TTL_SEC:
            expired.append(ssid)
            continue
        if ssid not in merged:
            merged[ssid] = _clone_network(
                cached,
                active=False,
                detected=False,
                stale=True,
            )
    for ssid in expired:
        _scan_cache.pop(ssid, None)

    networks = list(merged.values())
    networks.sort(key=_network_sort_key)
    return networks, any(net.stale for net in networks)


def _failed_scan_networks(profiles, active, profiles_complete=True):
    """Return saved/current identity plus TTL-bounded cache after no usable pass."""
    recent, partial = _merge_scan_cache([])
    identities = _parse_scan_output(
        '',
        profiles,
        active,
        profiles_complete=profiles_complete,
    )

    # Neither source is current AP evidence. Reconcile activity from the final
    # device state, while keeping cached signal/security strictly stale.
    recent = _reconcile_scan_activity(recent, [], active, profiles=profiles)
    identities = _reconcile_scan_activity(
        identities,
        [],
        active,
        profiles=profiles,
    )
    by_ssid = {network.ssid: network for network in recent}
    for identity in identities:
        cached = by_ssid.get(identity.ssid)
        if cached is None:
            by_ssid[identity.ssid] = _clone_network(
                identity,
                detected=False,
                stale=False,
                last_seen=0.0,
                in_use=False,
            )
            continue
        by_ssid[identity.ssid] = _clone_network(
            cached,
            saved=identity.saved,
            active=identity.active,
            iface=identity.iface or cached.iface,
            profile_name=identity.profile_name,
            profile_uuid=identity.profile_uuid,
            profile_known=identity.profile_known,
            autoconnect=identity.autoconnect,
            detected=False,
            in_use=False,
        )

    networks = list(by_ssid.values())
    networks.sort(key=_network_sort_key)
    return networks, partial


def _recent_cached_networks():
    networks, partial = _merge_scan_cache([])
    return networks, partial


def _log_connection_snapshot(iface, active, profiles):
    """Write a secret-free state snapshot for intermittent-drop diagnosis."""
    profile_count = sum(len(candidates) for candidates in profiles.values())
    autoconnect_off = sum(
        1
        for candidates in profiles.values()
        for profile in candidates
        if not profile.autoconnect
    )
    if active.query_ok:
        print(
            f'[wifi] device={iface!r} state={active.state!r} '
            f'reason={active.reason!r} active_profile={bool(active.uuid)} '
            f'saved_profiles={profile_count} '
            f'autoconnect_off={autoconnect_off}',
            file=sys.stderr,
        )
    else:
        print(
            f'[wifi] device={iface!r} state unavailable '
            f'({active.error_code or "state_query_failed"}); '
            f'saved_profiles={profile_count} '
            f'autoconnect_off={autoconnect_off}',
            file=sys.stderr,
        )


def _scan_detailed_unlocked(rescan=True):
    if not _HAVE_NMCLI:
        return ScanResult(_mock_scan(), ok=True)

    radio = _wifi_radio()
    if not radio:
        cached, partial = _recent_cached_networks()
        return ScanResult(
            cached,
            ok=False,
            code='radio_query_failed',
            message='Could not read Wi-Fi radio state',
            partial=partial,
        )
    if radio == 'disabled':
        cached, partial = _recent_cached_networks()
        return ScanResult(
            cached,
            ok=False,
            code='radio_disabled',
            message='Wi-Fi is disabled',
            partial=partial,
        )
    if radio != 'enabled':
        cached, partial = _recent_cached_networks()
        return ScanResult(
            cached,
            ok=False,
            code='radio_unavailable',
            message='Wi-Fi adapter unavailable',
            partial=partial,
        )

    iface, iface_code, iface_message, iface_detail = _wifi_iface_detailed()
    if not iface:
        cached, partial = _recent_cached_networks()
        if iface_detail:
            print(
                f'[wifi] adapter discovery failed ({iface_code}): '
                f'{iface_detail}',
                file=sys.stderr,
            )
        return ScanResult(
            cached,
            ok=False,
            code=iface_code,
            message=iface_message,
            partial=partial,
        )

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
    baseline_count = _recent_scan_baseline_count()
    fresh = []
    pass_limit = _SCAN_MIN_PASSES if rescan else 1
    pass_number = 0
    active = ActiveConnection(iface)
    final_pass_rows = []
    usable_passes = 0

    while pass_number < pass_limit:
        pass_number += 1
        pass_completed = not rescan
        if rescan:
            completed, code, message, detail = _request_rescan_and_wait(iface)
            pass_completed = completed
            if not completed:
                scan_error = _prefer_scan_error(
                    scan_error,
                    (code, message, detail),
                )
                print(
                    f'[wifi] scan pass {pass_number} failed ({code}): '
                    f'{detail or message}',
                    file=sys.stderr,
                )

        active = active_connection(iface)
        if not active.query_ok:
            scan_error = _prefer_scan_error(
                scan_error,
                (
                    active.error_code or 'state_query_failed',
                    active.error_message or 'Could not read Wi-Fi state',
                    active.error_message or 'Could not read Wi-Fi state',
                ),
            )
        if pass_completed:
            pass_rows, list_code, list_message, list_detail = _read_scan_rows(
                profiles,
                active,
                iface,
                profiles_complete=profile_result.complete,
            )
            if list_code != 'ok':
                scan_error = _prefer_scan_error(
                    scan_error,
                    (
                        list_code,
                        list_message,
                        list_detail,
                    ),
                )
                print(
                    f'[wifi] scan list pass {pass_number} failed ({list_code}): '
                    f'{list_detail or list_message}',
                    file=sys.stderr,
                )
            else:
                usable_passes += 1
                final_pass_rows = pass_rows
                fresh = _union_scan_passes(fresh, pass_rows)

        if (
                rescan
                and pass_number == _SCAN_MIN_PASSES
                and pass_limit < _SCAN_MAX_PASSES
                and _scan_severely_collapsed(fresh, baseline_count)):
            pass_limit = _SCAN_MAX_PASSES

    _log_connection_snapshot(iface, active, profiles)
    scan_degraded = (
        rescan
        and _scan_severely_collapsed(fresh, baseline_count)
    )
    if usable_passes:
        fresh = _reconcile_scan_activity(
            fresh,
            final_pass_rows,
            active,
            profiles=profiles,
        )
        fresh = _deduplicate_scan_networks(fresh)
        networks, partial = _merge_scan_cache(
            fresh,
            preserve_profile_identity=not profile_result.complete,
        )
    else:
        networks, partial = _failed_scan_networks(
            profiles,
            active,
            profiles_complete=profile_result.complete,
        )
    print(
        f'[wifi] scan passes={pass_number} usable={usable_passes} detected='
        f'{_detected_network_count(fresh)} baseline={baseline_count}, '
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

    if scan_degraded:
        return ScanResult(
            networks,
            ok=True,
            code='scan_degraded',
            message='Scan may be incomplete - showing recent results',
            partial=True,
        )
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
            completed_at=result.completed_at,
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
    security_known = bool(
        net.security not in ('', '--')
        or net.detected
        or net.stale
    )
    rows = [
        ('SSID', net.ssid),
        ('SIGNAL', f'{net.signal}%'),
        (
            'SECURITY',
            (net.security or 'Open') if security_known else 'Unknown',
        ),
        (
            'PROTECTED',
            ('Yes' if net.protected else 'No')
            if security_known
            else 'Unknown',
        ),
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
    elif (
            not latch.delete_profile
            and (
                latch.network is None
                or (
                    latch.network.saved
                    and latch.network.profile_uuid == latch.profile_uuid
                )
            )):
        enabled = (
            True
            if latch.network is None or latch.network.autoconnect is None
            else bool(latch.network.autoconnect)
        )
        unblock_rc, unblock_out, unblock_err = (
            _unblock_profile_autoconnect(
                latch.profile_uuid,
                enabled=enabled,
            )
        )
        if unblock_rc != 0:
            print(
                f'[wifi] pending cleanup could not restore auto-reconnect: '
                f'{_error_text(unblock_out, unblock_err)}',
                file=sys.stderr,
            )
            return False
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


def _ensure_profile_autoconnect(profile_uuid):
    """Enable auto-reconnect and verify NetworkManager retained the setting."""
    read_rc, read_out, read_err, enabled = _profile_autoconnect(profile_uuid)
    if read_rc != 0 or not enabled:
        set_rc, set_out, set_err = _enable_profile_autoconnect(profile_uuid)
        if set_rc != 0:
            return set_rc, set_out, set_err

    verify_rc, verify_out, verify_err, verified = _profile_autoconnect(
        profile_uuid
    )
    if verify_rc != 0:
        return verify_rc, verify_out, verify_err
    if not verified:
        return (
            1,
            verify_out,
            'NetworkManager left autoconnect disabled',
        )
    return 0, verify_out, verify_err


def _unblock_profile_autoconnect(profile_uuid, enabled=True):
    """Rewrite the intended policy to clear connection-down's internal block."""
    if enabled:
        set_rc, set_out, set_err = _enable_profile_autoconnect(profile_uuid)
    else:
        set_rc, set_out, set_err = _run(
            [
                '--wait',
                str(_NMCLI_PROFILE_WAIT_SEC),
                'connection',
                'modify',
                'uuid',
                profile_uuid,
                'connection.autoconnect',
                'no',
            ],
            timeout=_NMCLI_PROFILE_TIMEOUT_SEC,
        )
    if set_rc != 0:
        return set_rc, set_out, set_err

    verify_rc, verify_out, verify_err, verified = _profile_autoconnect(
        profile_uuid
    )
    if verify_rc != 0:
        return verify_rc, verify_out, verify_err
    if verified is not enabled:
        return (
            1,
            verify_out,
            (
                'NetworkManager left autoconnect disabled'
                if enabled
                else 'NetworkManager left autoconnect enabled'
            ),
        )
    return 0, verify_out, verify_err


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


def _delete_profile(profile_uuid):
    """Delete an nmcli connection profile and verify it no longer exists."""
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
    return _profile_exists(profile_uuid) is False, rc, out, err


def _clear_saved_network_state(net):
    """Reset a Network's saved-profile fields after its profile is gone."""
    net.saved = False
    net.profile_name = ''
    net.profile_uuid = ''
    net.autoconnect = None


def _discard_new_profile(net, profile_uuid):
    """Remove a profile that never reached a verified connection."""
    deleted, rc, out, err = _delete_profile(profile_uuid)
    if not deleted:
        print(
            f'[wifi] failed to remove unverified profile '
            f'{profile_uuid!r}: '
            f'{_error_text(out, err) or "deletion could not be verified"}',
            file=sys.stderr,
        )
        return False

    if net is not None and net.profile_uuid == profile_uuid:
        _clear_saved_network_state(net)
        net.profile_known = True
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
    pending_autoconnect = False
    restored_value = None

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
        if timed_out or code == 'connect_failed':
            timeout_stage = (
                'authentication'
                if code == 'authentication_failed'
                else _activation_timeout_stage(
                    iface,
                    expected_uuid,
                    protected=net.protected,
                )
            )

            if not timed_out and code == 'connect_failed':
                if timeout_stage == 'authentication':
                    code = 'authentication_failed'
                    message = 'Authentication failed'
                elif timeout_stage == 'ip':
                    code = 'dhcp_failed'
                    message = 'Could not obtain IP'

        if timed_out:
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
            elif (
                    timed_out
                    and not created_profile
                    and not reauth_after_cleanup):
                unblock_rc, unblock_out, unblock_err = (
                    _unblock_profile_autoconnect(
                        expected_uuid,
                        enabled=bool(net.autoconnect),
                    )
                )
                if unblock_rc != 0:
                    print(
                        f'[wifi] could not restore auto-reconnect for '
                        f'{net.ssid!r}: '
                        f'{_error_text(unblock_out, unblock_err)}',
                        file=sys.stderr,
                    )
                    code = 'autoconnect_failed'
                    message = 'Could not restore automatic reconnect'
            needs_password = (
                cleanup_ok and reauth_after_cleanup
            )
            if needs_password and not created_profile:
                # Keep a cancelled/ignored password prompt from causing the
                # next Connect tap to retry the same rejected secret for
                # another full activation timeout.
                _reauth_required.add(expected_uuid)
            if cleanup_ok and reauth_after_cleanup:
                _supervisor_note_auth_failure(expected_uuid)
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
        if not created_profile:
            unblock_rc, unblock_out, unblock_err = (
                _unblock_profile_autoconnect(
                    expected_uuid,
                    enabled=bool(net.autoconnect),
                )
            )
            if unblock_rc != 0:
                print(
                    f'[wifi] could not restore auto-reconnect for '
                    f'{net.ssid!r}: '
                    f'{_error_text(unblock_out, unblock_err)}',
                    file=sys.stderr,
                )
                job.finish(
                    False,
                    'Could not restore automatic reconnect',
                    code='autoconnect_failed',
                )
                return
        job.finish(False, 'Could not obtain IP', code='dhcp_failed')
        return

    net.profile_uuid = active_uuid
    net.saved = True
    net.active = True
    net.profile_known = True
    _reauth_required.discard(active_uuid)
    # Register every verified link immediately. Warning paths below still
    # represent a live connection and are the ones most likely to need bounded
    # recovery if NetworkManager later drops them.
    _supervisor_watch_connected(active_uuid, net.ssid, iface)

    if not created_profile:
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

    auto_rc, auto_out, auto_err = _ensure_profile_autoconnect(active_uuid)
    if auto_rc != 0:
        print(
            f'[wifi] connected to {net.ssid!r}, but verifying autoconnect '
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
    access_code, access_message = _connection_access_warning(iface)
    if access_code:
        job.finish(
            True,
            f'Connected to {net.ssid}; {access_message}',
            code='connected_warning',
        )
        return
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

    _supervisor_note_intentional_disconnect(active.uuid)
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


def forget_async(net):
    job = WifiJob('forget', net.ssid)
    _launch_job(job, lambda: _forget_worker(job, net))
    return job


def _forget_worker(job, net):
    if not _HAVE_NMCLI:
        ok, message = _mock_forget(net)
        job.finish(ok, message, code='forgotten' if ok else 'forget_failed')
        return

    job.set_status('Checking saved network...')
    discovery = _saved_profiles_detailed()
    if not discovery.complete:
        job.finish(
            False,
            discovery.message or 'Saved network status unavailable',
            code=discovery.code or 'profile_query_failed',
        )
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
    active_uuid = active.uuid
    profile = _profile_for_ssid(
        discovery.profiles,
        net.ssid,
        active_uuid=active_uuid,
        preferred_uuid=net.profile_uuid,
    )
    if profile is None:
        _clear_saved_network_state(net)
        job.finish(True, f'{net.ssid} was not saved', code='not_saved')
        return

    job.set_status('Forgetting network...')
    deleted, rc, out, err = _delete_profile(profile.uuid)
    if not deleted:
        code, message = _classify_error(rc, out, err, default='forget')
        print(
            f'[wifi] forget {net.ssid!r} failed ({code}): '
            f'{_error_text(out, err)}',
            file=sys.stderr,
        )
        job.finish(False, message, code=code)
        return

    if profile.uuid == active_uuid:
        # Deletion is confirmed, so the drop it already caused is
        # intentional; the supervisor must not try to recover a
        # connection whose profile no longer exists.
        _supervisor_note_intentional_disconnect(profile.uuid)
        net.active = False
    _clear_saved_network_state(net)
    _autoconnect_restore.pop(profile.uuid, None)
    _reauth_required.discard(profile.uuid)
    job.finish(True, f'Forgot {net.ssid}', code='forgotten')


# ---------------------------------------------------------------------------
# Connection supervision
# ---------------------------------------------------------------------------

class ConnectionStatus:
    """Thread-safe snapshot published by the connection supervisor."""

    __slots__ = (
        'sequence',
        'phase',
        'code',
        'message',
        'ssid',
        'profile_uuid',
        'active_uuid',
        'reason_code',
    )

    def __init__(
            self,
            sequence=0,
            phase='idle',
            code='',
            message='',
            ssid='',
            profile_uuid='',
            active_uuid='',
            reason_code=-1):
        self.sequence = sequence
        self.phase = phase
        self.code = code
        self.message = message
        self.ssid = ssid
        self.profile_uuid = profile_uuid
        self.active_uuid = active_uuid
        self.reason_code = reason_code

    def copy(self):
        return ConnectionStatus(
            sequence=self.sequence,
            phase=self.phase,
            code=self.code,
            message=self.message,
            ssid=self.ssid,
            profile_uuid=self.profile_uuid,
            active_uuid=self.active_uuid,
            reason_code=self.reason_code,
        )


class ConnectionSupervisor:
    """Observe unexpected drops and perform narrowly bounded recovery."""

    _TRANSIENT_REASONS = frozenset((5, 6, 8, 15, 16, 17, 40, 53))
    _AUTH_REASONS = frozenset((7, 9, 10, 11))

    def __init__(
            self,
            poll_sec=_SUPERVISOR_POLL_SEC,
            grace_sec=_SUPERVISOR_GRACE_SEC,
            retry_delay_sec=_SUPERVISOR_RETRY_DELAY_SEC,
            max_retries=_SUPERVISOR_MAX_RETRIES):
        self.poll_sec = poll_sec
        self.grace_sec = grace_sec
        self.retry_delay_sec = retry_delay_sec
        self.max_retries = max_retries
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        self._status = ConnectionStatus()
        self._history = []
        self._target_uuid = ''
        self._target_ssid = ''
        self._iface = ''
        self._intentional_uuid = ''
        self._auth_blocked = set()
        self._lost_at = None
        self._next_retry_at = None
        self._drop_reason = -1
        self._retry_count = 0
        self._recovery_exhausted = False
        self._generation = 0

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name='wifi-connection-supervisor',
                daemon=True,
            )
            self._thread.start()

    def stop(self):
        with self._lock:
            thread = self._thread
            self._thread = None
            self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.poll_sec))

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception as exc:
                print(
                    f'[wifi] connection supervisor failed: {exc}',
                    file=sys.stderr,
                )
                self._publish(
                    'failed',
                    'supervisor_failed',
                    'Wi-Fi connection monitoring failed',
                )
            self._stop_event.wait(self.poll_sec)

    def _publish(
            self,
            phase,
            code,
            message,
            active_uuid='',
            reason_code=-1,
            expected_generation=None):
        with self._lock:
            if (
                    expected_generation is not None
                    and expected_generation != self._generation):
                return False
            previous = self._status
            values = (
                phase,
                code,
                message,
                self._target_ssid,
                self._target_uuid,
                active_uuid,
                reason_code,
            )
            old_values = (
                previous.phase,
                previous.code,
                previous.message,
                previous.ssid,
                previous.profile_uuid,
                previous.active_uuid,
                previous.reason_code,
            )
            if values == old_values:
                return True
            self._status = ConnectionStatus(
                sequence=previous.sequence + 1,
                phase=phase,
                code=code,
                message=message,
                ssid=self._target_ssid,
                profile_uuid=self._target_uuid,
                active_uuid=active_uuid,
                reason_code=reason_code,
            )
            self._history.append(self._status.copy())
            del self._history[:-12]
        print(
            f'[wifi] supervisor phase={phase} code={code or "ok"} '
            f'reason={reason_code}',
            file=sys.stderr,
        )
        return True

    def snapshot(self):
        with self._lock:
            return self._status.copy()

    def history(self):
        with self._lock:
            return [status.copy() for status in self._history]

    def recovery_busy(self):
        with self._lock:
            return self._status.phase == 'recovering'

    def watch_connected(self, profile_uuid, ssid, iface):
        if not profile_uuid:
            return
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._target_uuid = profile_uuid
            self._target_ssid = ssid
            self._iface = iface
            self._intentional_uuid = ''
            self._auth_blocked.discard(profile_uuid)
            self._lost_at = None
            self._next_retry_at = None
            self._drop_reason = -1
            self._retry_count = 0
            self._recovery_exhausted = False
            _reauth_required.discard(profile_uuid)
        self._publish(
            'connected',
            'connected',
            f'Connected to {ssid}' if ssid else 'Wi-Fi connected',
            active_uuid=profile_uuid,
            reason_code=0,
            expected_generation=generation,
        )

    def note_intentional_disconnect(self, profile_uuid):
        if not profile_uuid:
            return
        with self._lock:
            self._generation += 1
            self._intentional_uuid = profile_uuid

    def note_auth_failure(self, profile_uuid):
        if not profile_uuid:
            return
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._auth_blocked.add(profile_uuid)
            if profile_uuid != self._target_uuid:
                return
        self._publish(
            'attention',
            'authentication_failed',
            'Authentication failed - password required',
            reason_code=7,
            expected_generation=generation,
        )

    def _observe(self):
        with self._lock:
            iface = self._iface
        if not iface:
            iface, code, message, _ = _wifi_iface_detailed()
            if not iface:
                return None, '', code, message

        # Read-only polling must yield to user-triggered mutations, but it must
        # not hold the mutation lock across potentially slow nmcli queries.
        if not _operation_lock.acquire(blocking=False):
            return None, iface, 'busy', ''
        _operation_lock.release()

        active = active_connection(iface)
        if (
                active.query_ok
                and _leading_int(active.state) == 100
                and active.uuid):
            ok, address, code, message = _device_ipv4_detailed(iface)
            if not ok:
                return active, '', code, message
        else:
            address = ''
        return active, address, 'ok', ''

    def _adopt_active(
            self,
            active,
            iface,
            address,
            recovered=False,
            expected_generation=None):
        with self._lock:
            if (
                    expected_generation is not None
                    and expected_generation != self._generation):
                return False
            previous_uuid = self._target_uuid
            previous_ssid = self._target_ssid
        ssid = previous_ssid if previous_uuid == active.uuid else ''
        if not ssid:
            ssid = _profile_ssid(active.uuid)
        with self._lock:
            if (
                    expected_generation is not None
                    and expected_generation != self._generation):
                return False
            self._target_uuid = active.uuid
            self._target_ssid = ssid
            self._iface = iface
            self._intentional_uuid = ''
            self._auth_blocked.discard(active.uuid)
            _reauth_required.discard(active.uuid)
            self._lost_at = None
            self._next_retry_at = None
            self._drop_reason = -1
            self._retry_count = 0
            self._recovery_exhausted = False
        if recovered and previous_uuid == active.uuid:
            message_ssid = previous_ssid or ssid
            self._publish(
                'reconnected',
                'reconnected',
                (
                    f'Reconnected to {message_ssid}'
                    if message_ssid
                    else 'Wi-Fi reconnected'
                ),
                active_uuid=active.uuid,
                reason_code=0,
                expected_generation=expected_generation,
            )
        else:
            self._publish(
                'connected',
                'connected',
                f'Connected to {ssid}' if ssid else 'Wi-Fi connected',
                active_uuid=active.uuid,
                reason_code=0,
                expected_generation=expected_generation,
            )
        return True

    def _attempt_recovery(
            self,
            profile_uuid,
            ssid,
            iface,
            expected_generation=None):
        if not _operation_lock.acquire(blocking=False):
            return 'defer', 'busy', 'Wi-Fi is busy', '', -1
        try:
            with self._lock:
                if (
                        expected_generation is not None
                        and expected_generation != self._generation):
                    return 'defer', 'stale', '', '', -1
            if not _reconcile_recovery():
                return (
                    'terminal',
                    'cleanup_pending',
                    'Previous connection cleanup is still pending',
                    '',
                    -1,
                )
            with self._lock:
                if (
                        expected_generation is not None
                        and expected_generation != self._generation):
                    return 'defer', 'stale', '', '', -1
                authentication_blocked = (
                    profile_uuid in self._auth_blocked
                    or profile_uuid in _reauth_required
                )
            if authentication_blocked:
                code, message = _classify_device_reason(7)
                return 'attention', code, message, '', 7

            active = active_connection(iface)
            if not active.query_ok:
                return (
                    'failed',
                    active.error_code or 'state_query_failed',
                    active.error_message or 'Could not read Wi-Fi state',
                    '',
                    _leading_int(active.reason),
                )
            state = _leading_int(active.state)
            if state == 100 and active.uuid:
                ip_ok, address, ip_code, ip_message = _device_ipv4_detailed(
                    iface
                )
                if not ip_ok:
                    return 'failed', ip_code, ip_message, '', -1
                if address:
                    result = (
                        'reconnected'
                        if active.uuid == profile_uuid
                        else 'other_connected'
                    )
                    return result, 'connected', 'Wi-Fi connected', active.uuid, 0
            if 40 <= state <= 90:
                return (
                    'defer',
                    'reconnecting',
                    (
                        'NetworkManager is connecting another saved network'
                        if active.uuid and active.uuid != profile_uuid
                        else 'NetworkManager is reconnecting'
                    ),
                    '',
                    _leading_int(active.reason),
                )

            exists = _profile_exists(profile_uuid)
            if exists is False:
                return (
                    'failed',
                    'profile_missing',
                    'Saved network profile is missing',
                    '',
                    _leading_int(active.reason),
                )
            if exists is None:
                return (
                    'failed',
                    'profile_query_failed',
                    'Could not read saved network profile',
                    '',
                    _leading_int(active.reason),
                )

            if ssid:
                identity_ok, profile_ssid, identity_detail = (
                    _profile_ssid_detailed(profile_uuid)
                )
                if not identity_ok:
                    return (
                        'failed',
                        'profile_query_failed',
                        'Could not verify saved network identity',
                        '',
                        _leading_int(active.reason),
                    )
                if profile_ssid != ssid:
                    print(
                        '[wifi] supervisor refused profile with changed SSID',
                        file=sys.stderr,
                    )
                    return (
                        'terminal',
                        'profile_identity_changed',
                        'Saved network identity changed; press Rescan',
                        '',
                        _leading_int(active.reason),
                    )

            rc, out, err = _activate_profile(
                profile_uuid,
                iface,
                password=None,
            )
            if rc != 0:
                code, message = _classify_error(rc, out, err)
                failed = active_connection(iface)
                reason_code = (
                    _leading_int(failed.reason)
                    if failed.query_ok
                    else -1
                )
                if (
                        failed.query_ok
                        and _leading_int(failed.state) == 100
                        and failed.uuid):
                    ip_ok, address, ip_code, ip_message = (
                        _device_ipv4_detailed(iface)
                    )
                    if not ip_ok:
                        return 'failed', ip_code, ip_message, '', reason_code
                    if address:
                        result = (
                            'reconnected'
                            if failed.uuid == profile_uuid
                            else 'other_connected'
                        )
                        return (
                            result,
                            'connected',
                            'Wi-Fi connected',
                            failed.uuid,
                            0,
                        )
                if code in ('connect_failed', 'timeout') and reason_code > 1:
                    code, message = _classify_device_reason(reason_code)
                reauth_required = code in (
                    'authentication_failed',
                    'authentication_required',
                )
                if not _cancel_activation(profile_uuid, iface):
                    _set_recovery_latch(
                        iface,
                        profile_uuid,
                        reason='supervisor activation cleanup',
                        reauth_required=reauth_required,
                    )
                    return (
                        'terminal',
                        'cleanup_failed',
                        'Could not stop failed reconnect',
                        '',
                        reason_code,
                    )
                if not reauth_required:
                    unblock_rc, unblock_out, unblock_err = (
                        _unblock_profile_autoconnect(profile_uuid)
                    )
                    if unblock_rc != 0:
                        print(
                            '[wifi] supervisor could not restore '
                            f'auto-reconnect: '
                            f'{_error_text(unblock_out, unblock_err)}',
                            file=sys.stderr,
                        )
                        return (
                            'terminal',
                            'autoconnect_failed',
                            'Could not restore automatic reconnect',
                            '',
                            reason_code,
                        )
                outcome = (
                    'attention'
                    if reauth_required
                    else 'failed'
                )
                return outcome, code, message, '', reason_code

            verified, active_uuid, detail = _verify_connected(
                iface,
                expected_uuid=profile_uuid,
                expected_ssid=ssid,
            )
            if verified:
                return (
                    'reconnected',
                    'reconnected',
                    'Wi-Fi reconnected',
                    active_uuid,
                    0,
                )
            if not _cancel_activation(profile_uuid, iface):
                _set_recovery_latch(
                    iface,
                    profile_uuid,
                    reason='supervisor verification cleanup',
                )
                return (
                    'terminal',
                    'cleanup_failed',
                    'Could not stop failed reconnect',
                    '',
                    -1,
                )
            unblock_rc, unblock_out, unblock_err = (
                _unblock_profile_autoconnect(profile_uuid)
            )
            if unblock_rc != 0:
                print(
                    '[wifi] supervisor could not restore auto-reconnect: '
                    f'{_error_text(unblock_out, unblock_err)}',
                    file=sys.stderr,
                )
                return (
                    'terminal',
                    'autoconnect_failed',
                    'Could not restore automatic reconnect',
                    '',
                    -1,
                )
            return (
                'failed',
                'dhcp_failed',
                detail or 'Could not obtain IP',
                '',
                -1,
            )
        finally:
            _operation_lock.release()

    def step(self, now=None):
        if not _HAVE_NMCLI:
            return self.snapshot()
        now = time.monotonic() if now is None else now
        with self._lock:
            observation_generation = self._generation
        active, address, observe_code, observe_message = self._observe()
        with self._lock:
            if observation_generation != self._generation:
                return self._status.copy()
        if observe_code == 'busy':
            return self.snapshot()
        if active is None:
            self._publish(
                'failed',
                observe_code,
                observe_message or 'Could not read Wi-Fi adapter state',
                expected_generation=observation_generation,
            )
            return self.snapshot()
        if observe_code != 'ok':
            self._publish(
                'failed',
                observe_code,
                observe_message or 'Could not read Wi-Fi connection state',
                reason_code=_leading_int(active.reason),
                expected_generation=observation_generation,
            )
            return self.snapshot()
        if not active.query_ok:
            self._publish(
                'failed',
                active.error_code or 'state_query_failed',
                active.error_message or 'Could not read Wi-Fi state',
                reason_code=_leading_int(active.reason),
                expected_generation=observation_generation,
            )
            return self.snapshot()

        state = _leading_int(active.state)
        connected = state == 100 and bool(active.uuid and address)
        with self._lock:
            if observation_generation != self._generation:
                return self._status.copy()
            target_uuid = self._target_uuid
            target_ssid = self._target_ssid
            intentional_uuid = self._intentional_uuid
            lost_at = self._lost_at

        if connected:
            self._adopt_active(
                active,
                active.iface,
                address,
                recovered=lost_at is not None,
                expected_generation=observation_generation,
            )
            return self.snapshot()

        if not target_uuid:
            return self.snapshot()

        reason_code = _leading_int(active.reason)
        if (
                state == 100
                and active.uuid
                and not address
                and reason_code <= 1):
            # Layer-2 activation without IPv4 is an address-assignment failure,
            # not a healthy connection with an unknown generic reason.
            reason_code = 5
        if intentional_uuid == target_uuid or reason_code == 39:
            with self._lock:
                if observation_generation != self._generation:
                    return self._status.copy()
                self._target_uuid = ''
                self._target_ssid = ''
                self._intentional_uuid = ''
                self._lost_at = None
                self._next_retry_at = None
                self._retry_count = 0
                self._recovery_exhausted = False
            self._publish(
                'disconnected',
                'user_disconnected',
                (
                    f'Disconnected from {target_ssid}'
                    if target_ssid
                    else 'Wi-Fi disconnected'
                ),
                reason_code=39,
                expected_generation=observation_generation,
            )
            return self.snapshot()

        with self._lock:
            if observation_generation != self._generation:
                return self._status.copy()
            auth_blocked = (
                target_uuid in self._auth_blocked
                or target_uuid in _reauth_required
            )
        auth_applies_to_target = (
            not active.uuid
            or active.uuid == target_uuid
        )
        if (
                auth_applies_to_target
                and (
                    auth_blocked
                    or state == 60
                    or reason_code in self._AUTH_REASONS
                )):
            code, message = _classify_device_reason(
                reason_code if reason_code in self._AUTH_REASONS else 7
            )
            with self._lock:
                if observation_generation != self._generation:
                    return self._status.copy()
                self._auth_blocked.add(target_uuid)
                self._recovery_exhausted = True
                _reauth_required.add(target_uuid)
            self._publish(
                'attention',
                code,
                message,
                reason_code=reason_code,
                expected_generation=observation_generation,
            )
            return self.snapshot()

        if 40 <= state <= 90:
            with self._lock:
                if observation_generation != self._generation:
                    return self._status.copy()
                if self._lost_at is None:
                    self._lost_at = now
                    self._drop_reason = reason_code
                    self._retry_count = 0
                    self._recovery_exhausted = False
                if self._next_retry_at is None:
                    self._next_retry_at = now + self.grace_sec
                elif self._drop_reason <= 1 and reason_code > 1:
                    self._drop_reason = reason_code
            self._publish(
                'grace',
                'reconnecting',
                (
                    'NetworkManager is connecting another saved network'
                    if active.uuid and active.uuid != target_uuid
                    else 'Connection lost - NetworkManager is reconnecting'
                ),
                reason_code=reason_code,
                expected_generation=observation_generation,
            )
            return self.snapshot()

        with self._lock:
            if observation_generation != self._generation:
                return self._status.copy()
            if self._lost_at is None:
                self._lost_at = now
                self._next_retry_at = now + self.grace_sec
                self._drop_reason = reason_code
                self._retry_count = 0
                self._recovery_exhausted = False
            elif self._next_retry_at is None:
                self._next_retry_at = now + self.grace_sec
            elif self._drop_reason <= 1 and reason_code > 1:
                self._drop_reason = reason_code
            retry_at = self._next_retry_at
            drop_reason = self._drop_reason
            retry_count = self._retry_count
            recovery_exhausted = self._recovery_exhausted

        if retry_at is None or now < retry_at:
            self._publish(
                'grace',
                'reconnecting',
                'Connection lost - NetworkManager is reconnecting',
                reason_code=drop_reason,
                expected_generation=observation_generation,
            )
            return self.snapshot()

        if drop_reason not in self._TRANSIENT_REASONS:
            code, message = _classify_device_reason(drop_reason)
            with self._lock:
                if observation_generation != self._generation:
                    return self._status.copy()
                self._recovery_exhausted = True
            self._publish(
                'failed',
                code,
                message,
                reason_code=drop_reason,
                expected_generation=observation_generation,
            )
            return self.snapshot()

        if recovery_exhausted:
            return self.snapshot()

        if retry_count >= self.max_retries:
            code, message = _classify_device_reason(drop_reason)
            with self._lock:
                if observation_generation != self._generation:
                    return self._status.copy()
                self._recovery_exhausted = True
            self._publish(
                'failed',
                code,
                f'{message} - reconnect failed',
                reason_code=drop_reason,
                expected_generation=observation_generation,
            )
            return self.snapshot()

        self._publish(
            'recovering',
            'reconnecting',
            (
                f'Reconnecting to {target_ssid}'
                if target_ssid
                else 'Reconnecting to Wi-Fi'
            ),
            reason_code=drop_reason,
            expected_generation=observation_generation,
        )
        with self._lock:
            if observation_generation != self._generation:
                return self._status.copy()
            recovery_iface = self._iface
        outcome, code, message, active_uuid, recovery_reason = (
            self._attempt_recovery(
                target_uuid,
                target_ssid,
                recovery_iface,
                expected_generation=observation_generation,
            )
        )
        if outcome == 'defer':
            with self._lock:
                if observation_generation != self._generation:
                    return self._status.copy()
                self._next_retry_at = now + self.poll_sec
            self._publish(
                'grace',
                code,
                message,
                reason_code=drop_reason,
                expected_generation=observation_generation,
            )
            return self.snapshot()
        if outcome == 'other_connected':
            observed, observed_address, observed_code, _ = self._observe()
            if (
                    observed_code == 'ok'
                    and observed is not None
                    and observed.query_ok
                    and observed.uuid == active_uuid):
                self._adopt_active(
                    observed,
                    self._iface,
                    observed_address,
                    expected_generation=observation_generation,
                )
            return self.snapshot()
        if outcome == 'reconnected':
            with self._lock:
                if observation_generation != self._generation:
                    return self._status.copy()
                self._lost_at = None
                self._next_retry_at = None
                self._drop_reason = -1
                self._retry_count = 0
                self._recovery_exhausted = False
            self._publish(
                'reconnected',
                'reconnected',
                (
                    f'Reconnected to {target_ssid}'
                    if target_ssid
                    else 'Wi-Fi reconnected'
                ),
                active_uuid=active_uuid or target_uuid,
                reason_code=0,
                expected_generation=observation_generation,
            )
            return self.snapshot()
        if outcome == 'attention':
            with self._lock:
                if observation_generation != self._generation:
                    return self._status.copy()
                self._auth_blocked.add(target_uuid)
                self._recovery_exhausted = True
                _reauth_required.add(target_uuid)
            self._publish(
                'attention',
                code,
                message,
                reason_code=recovery_reason,
                expected_generation=observation_generation,
            )
            return self.snapshot()
        if outcome == 'terminal':
            with self._lock:
                if observation_generation != self._generation:
                    return self._status.copy()
                self._recovery_exhausted = True
            self._publish(
                'failed',
                code,
                message,
                reason_code=recovery_reason,
                expected_generation=observation_generation,
            )
            return self.snapshot()

        with self._lock:
            if observation_generation != self._generation:
                return self._status.copy()
            self._retry_count += 1
            retries = self._retry_count
            self._next_retry_at = (
                now + self.retry_delay_sec * (2 ** max(0, retries - 1))
            )
            if retries >= self.max_retries:
                self._recovery_exhausted = True
        if retries >= self.max_retries:
            self._publish(
                'failed',
                code,
                f'{message} - reconnect failed',
                reason_code=recovery_reason,
                expected_generation=observation_generation,
            )
        else:
            self._publish(
                'grace',
                code,
                f'{message} - retrying',
                reason_code=recovery_reason,
                expected_generation=observation_generation,
            )
        return self.snapshot()


_connection_supervisor = None


def start_connection_supervisor():
    global _connection_supervisor
    if not _HAVE_NMCLI:
        return
    if _connection_supervisor is None:
        _connection_supervisor = ConnectionSupervisor()
    _connection_supervisor.start()


def stop_connection_supervisor():
    global _connection_supervisor
    supervisor = _connection_supervisor
    _connection_supervisor = None
    if supervisor is not None:
        supervisor.stop()


def connection_supervisor_snapshot():
    if _connection_supervisor is None:
        return ConnectionStatus()
    return _connection_supervisor.snapshot()


def connection_supervisor_recovery_busy():
    return (
        _connection_supervisor is not None
        and _connection_supervisor.recovery_busy()
    )


def _supervisor_watch_connected(profile_uuid, ssid, iface):
    if _connection_supervisor is not None:
        _connection_supervisor.watch_connected(profile_uuid, ssid, iface)


def _supervisor_note_intentional_disconnect(profile_uuid):
    if _connection_supervisor is not None:
        _connection_supervisor.note_intentional_disconnect(profile_uuid)


def _supervisor_note_auth_failure(profile_uuid):
    if _connection_supervisor is not None:
        _connection_supervisor.note_auth_failure(profile_uuid)


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


def _mock_forget(net):
    for network in _MOCK:
        if network.ssid == net.ssid:
            network.saved = False
            network.active = False
            network.profile_name = ''
            network.profile_uuid = ''
    net.saved = False
    net.active = False
    net.profile_name = ''
    net.profile_uuid = ''
    return True, f'Forgot {net.ssid}'
