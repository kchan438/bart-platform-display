"""Station directory and station-specific platforms from BART's station API."""

import requests

from . import config

_URL = 'https://api.bart.gov/api/stn.aspx'


def _list(value):
    if value is None or value == '':
        return []
    return value if isinstance(value, list) else [value]


def _request(command, **params):
    response = requests.get(_URL, params={
        'cmd': command, 'key': config.get_api_key(), 'json': 'y', **params,
    }, timeout=10)
    response.raise_for_status()
    return response.json().get('root', {}).get('stations', {}).get('station')


def fetch_stations():
    """Return unique (code, name) pairs in alphabetical display-name order."""
    entries = {}
    for station in _list(_request('stns')):
        code = str(station.get('abbr', '')).strip().upper()
        name = str(station.get('name', '')).strip()
        if code and name:
            entries[code] = name
    if not entries:
        raise ValueError('No stations returned')
    return sorted(entries.items(), key=lambda item: (item[1].casefold(), item[0]))


def fetch_platforms(code):
    """Use station metadata, so quiet platforms remain selectable."""
    entries = _list(_request('stninfo', orig=code))
    station = next((s for s in entries if s.get('abbr', '').upper() == code.upper()), None)
    if station is None:
        raise ValueError('Station information unavailable')
    platforms = set()
    for direction in ('north_platforms', 'south_platforms'):
        group = station.get(direction) or {}
        for platform in _list(group.get('platform')):
            value = str(platform).strip()
            if value.isdigit() and int(value) > 0:
                platforms.add(str(int(value)))
    if not platforms:
        raise ValueError('No platforms returned')
    return sorted(platforms, key=int)
