"""Wi-Fi control via NetworkManager's `nmcli`.

Exposes scanning, connect/disconnect, and per-network info to the settings UI.
Connects run asynchronously (ConnectJob) so the UI can show live status. A
successful connect auto-saves the NetworkManager profile, so auto-reconnect on
boot is handled by NetworkManager with no extra work.

If nmcli is unavailable (e.g. desktop dev machine), a small in-memory mock backs
the same API so the UI can be exercised with BART_DEV=1.
"""

import shutil
import subprocess
import sys
import threading
import time

_HAVE_NMCLI = shutil.which('nmcli') is not None


class Network:
    __slots__ = ('ssid', 'signal', 'security', 'protected', 'saved', 'active', 'bssid')

    def __init__(self, ssid, signal=0, security='', saved=False, active=False, bssid=''):
        self.ssid = ssid
        self.signal = signal
        self.security = security
        self.protected = security not in ('', '--')
        self.saved = saved
        self.active = active
        self.bssid = bssid


class ConnectJob:
    """Runs a connect attempt on a thread; exposes live status to the UI."""

    def __init__(self, ssid):
        self.ssid = ssid
        self.status = 'Connecting...'
        self.done = False
        self.ok = False

    def _finish(self, ok, message):
        self.ok = ok
        self.status = message
        self.done = True


# ---------------------------------------------------------------------------
# terse (-t) output parsing
# ---------------------------------------------------------------------------

def _split_terse(line):
    """Split an nmcli -t line on unescaped ':' and unescape '\\:' / '\\\\'."""
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


def _run(args, timeout=20):
    """Run an nmcli command; return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(['nmcli'] + args, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, '', 'timed out'
    except Exception as e:
        return 1, '', str(e)


def wifi_iface():
    rc, out, _ = _run(['-t', '-f', 'DEVICE,TYPE', 'device'])
    if rc == 0:
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and parts[1] == 'wifi':
                return parts[0]
    return 'wlan0'


def saved_ssids():
    rc, out, _ = _run(['-t', '-f', 'NAME,TYPE', 'connection', 'show'])
    names = set()
    if rc == 0:
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and 'wireless' in parts[1]:
                names.add(parts[0])
    return names


def scan(rescan=True):
    """Return a de-duplicated list of Network, strongest signal per SSID."""
    if not _HAVE_NMCLI:
        print('[wifi] nmcli not found on PATH — showing mock list', file=sys.stderr)
        return _mock_scan()

    # `--rescan yes` forces a fresh scan and blocks until it finishes, unlike a
    # separate `wifi rescan` (async) whose results aren't ready for `list` yet.
    fields = ['-t', '-f', 'IN-USE,SIGNAL,SECURITY,SSID', 'device', 'wifi', 'list']
    rc, out, err = _run(fields + ['--rescan', 'yes' if rescan else 'no'], timeout=25)
    if rc != 0:
        print(f'[wifi] list --rescan failed rc={rc}: {err.strip()}', file=sys.stderr)
        # A forced rescan can be rejected if one ran seconds ago; fall back to
        # the cached results rather than showing nothing.
        rc, out, err = _run(fields + ['--rescan', 'no'], timeout=15)
        if rc != 0:
            print(f'[wifi] list failed rc={rc}: {err.strip()}', file=sys.stderr)

    saved = saved_ssids()
    by_ssid = {}
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) < 4:
            continue
        in_use, signal, security, ssid = parts[0], parts[1], parts[2], parts[3]
        if not ssid:
            continue
        try:
            sig = int(signal)
        except ValueError:
            sig = 0
        active = in_use.strip() == '*'
        existing = by_ssid.get(ssid)
        if existing is None or sig > existing.signal:
            by_ssid[ssid] = Network(ssid, sig, security,
                                    saved=ssid in saved, active=active)
        if active and ssid in by_ssid:
            by_ssid[ssid].active = True

    nets = list(by_ssid.values())
    nets.sort(key=lambda n: (not n.active, -n.signal))
    print(f'[wifi] scan found {len(nets)} network(s)', file=sys.stderr)
    if not nets:
        radio = _run(['-t', '-f', 'WIFI', 'radio'])[1].strip()
        print(f'[wifi] radio={radio!r}; if "disabled" run: nmcli radio wifi on',
              file=sys.stderr)
    return nets


def info_basic(net):
    """Static (label, value) rows describing a network — no subprocess calls."""
    return [
        ('SSID', net.ssid),
        ('SIGNAL', f'{net.signal}%'),
        ('SECURITY', net.security or 'Open'),
        ('PROTECTED', 'Yes' if net.protected else 'No'),
        ('SAVED', 'Yes' if net.saved else 'No'),
        ('STATUS', 'Connected' if net.active else 'Not connected'),
    ]


def info(net):
    """Full info rows, including IP/gateway for the active network (spawns nmcli).

    Call off the render thread — see info_basic() for the cheap subset.
    """
    rows = info_basic(net)
    if net.active and _HAVE_NMCLI:
        iface = wifi_iface()
        rc, out, _ = _run(['-t', '-f', 'IP4.ADDRESS,IP4.GATEWAY', 'device', 'show', iface])
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(':', 1)
                if len(parts) == 2 and parts[1]:
                    if parts[0].startswith('IP4.ADDRESS'):
                        rows.append(('IP', parts[1]))
                    elif parts[0].startswith('IP4.GATEWAY'):
                        rows.append(('GATEWAY', parts[1]))
    return rows


def connect_async(net, password=None):
    """Start a connect attempt; return a ConnectJob the UI can poll."""
    job = ConnectJob(net.ssid)
    threading.Thread(target=_connect_worker, args=(job, net, password),
                     daemon=True).start()
    return job


def _connect_worker(job, net, password):
    if not _HAVE_NMCLI:
        return _mock_connect(job, net, password)
    if net.saved and not password:
        rc, out, err = _run(['connection', 'up', 'id', net.ssid], timeout=45)
    elif password:
        rc, out, err = _run(['dev', 'wifi', 'connect', net.ssid, 'password', password],
                           timeout=45)
    else:
        rc, out, err = _run(['dev', 'wifi', 'connect', net.ssid], timeout=45)

    if rc == 0:
        job._finish(True, 'Connected')
        return
    msg = (err or out).strip().lower()
    if 'secrets were required' in msg or 'no secrets' in msg or 'password' in msg:
        job._finish(False, 'Wrong password')
    elif 'not authorized' in msg or 'permission' in msg:
        job._finish(False, 'Not authorized (permissions)')
    elif 'timed out' in msg or rc == 124:
        job._finish(False, 'Timed out')
    else:
        job._finish(False, 'Failed to connect')


def disconnect(net):
    """Disconnect a network. Returns (ok, message)."""
    if not _HAVE_NMCLI:
        return _mock_disconnect(net)
    if net.saved:
        rc, _, err = _run(['connection', 'down', 'id', net.ssid], timeout=20)
    else:
        rc, _, err = _run(['device', 'disconnect', wifi_iface()], timeout=20)
    if rc == 0:
        return True, 'Disconnected'
    return False, (err.strip() or 'Disconnect failed')


# ---------------------------------------------------------------------------
# Dev-machine mock (no nmcli)
# ---------------------------------------------------------------------------
_MOCK = [
    Network('HomeWiFi', 88, 'WPA2', saved=True, active=True),
    Network('Neighbor_5G', 62, 'WPA2'),
    Network('CoffeeShop', 40, ''),
    Network('OpenGuest', 30, ''),
]


def _mock_scan():
    return list(_MOCK)


def _mock_connect(job, net, password):
    time.sleep(1.0)
    job.status = 'Authenticating...'
    time.sleep(1.0)
    if net.protected and not net.saved and (password or '') != 'password':
        job._finish(False, 'Wrong password')
        return
    for n in _MOCK:
        n.active = (n.ssid == net.ssid)
        if n.ssid == net.ssid:
            n.saved = True
    job._finish(True, 'Connected')


def _mock_disconnect(net):
    for n in _MOCK:
        if n.ssid == net.ssid:
            n.active = False
    return True, 'Disconnected'
