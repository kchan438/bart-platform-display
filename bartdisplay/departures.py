"""BART Real-Time Departures (ETD) fetching.

Runs a background thread that polls the BART ETD API and publishes sorted
departure rows for the configured platform. The API key and station are read
live from `config` on every fetch, so a key saved from the settings panel takes
effect on the next poll without a restart.
"""

import sys
import threading
import time

import requests

from . import config

_BART_URL = 'https://api.bart.gov/api/etd.aspx'

_lock = threading.Lock()
_rows = []
_loading = True


def _fetch():
    """Call the BART ETD API and return sorted departure rows for the platform."""
    platform = config.get_platform()
    resp = requests.get(_BART_URL, params={
        'cmd':  'etd',
        'orig': config.get_station(),
        'key':  config.get_api_key(),
        'json': 'y',
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    station_list = data.get('root', {}).get('station', [])
    if isinstance(station_list, dict):
        station_list = [station_list]
    raw_etd = station_list[0].get('etd', []) if station_list else []
    if isinstance(raw_etd, dict):
        raw_etd = [raw_etd]

    rows = []
    for etd in raw_etd:
        estimates_raw = etd.get('estimate', [])
        if isinstance(estimates_raw, dict):
            estimates_raw = [estimates_raw]

        pool = [e for e in estimates_raw if str(e.get('platform', '')) == platform]
        valid = [e for e in pool if e.get('cancelflag', '0') != '1'] or pool
        if not valid:
            continue
        valid.sort(key=lambda e: (
            0 if e.get('minutes') in ('Leaving', '0')
            else (int(e['minutes']) if str(e.get('minutes', '')).isdigit() else 999)
        ))
        rows.append({
            'destination': etd.get('destination', '').upper(),
            'minutes':     [e.get('minutes', '') for e in valid[:2]],
        })

    rows.sort(key=lambda r: (
        0 if r['minutes'][0] in ('Leaving', '0')
        else (int(r['minutes'][0]) if r['minutes'][0].isdigit() else 999)
    ))
    return rows


def _loop():
    global _rows, _loading
    while True:
        try:
            new_rows = _fetch()
            with _lock:
                _rows = new_rows
                _loading = False
        except Exception as exc:
            # Keep previous rows; stop spinner so the screen isn't stuck on LOADING.
            with _lock:
                _loading = False
            print(f'[fetch error] {exc}', file=sys.stderr)
        time.sleep(config.get_refresh_interval())


def start():
    """Start the background fetch thread (daemon)."""
    threading.Thread(target=_loop, daemon=True).start()


def snapshot():
    """Return (rows, loading) for the current frame."""
    with _lock:
        return list(_rows), _loading
