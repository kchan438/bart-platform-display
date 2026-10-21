"""Run with sudo on the Pi to set or change the touchscreen Tailscale PIN."""

import getpass
import json
import os
from pathlib import Path
import pwd
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bartdisplay.admin_pin import PIN_PATH, make_record, validate_record


def install_record(record):
    # Validate format without echoing any authentication material.
    validate_record(record)
    gid = pwd.getpwnam('kevinchan').pw_gid
    PIN_PATH.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if PIN_PATH.parent.is_symlink():
        raise ValueError('PIN directory must not be a symlink')
    os.chown(PIN_PATH.parent, 0, gid)
    os.chmod(PIN_PATH.parent, 0o750)
    temporary = PIN_PATH.with_name('admin-pin.json.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchown(stream.fileno(), 0, gid)
            os.fchmod(stream.fileno(), 0o640)
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, PIN_PATH)
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == '__main__':
    if os.geteuid() != 0:
        sys.exit('Run this setup with sudo.')
    if len(sys.argv) == 2 and sys.argv[1] == '--record-stdin':
        record = json.load(sys.stdin)
    elif len(sys.argv) == 1:
        pin = getpass.getpass('New Tailscale page PIN (4-12 digits): ')
        if pin != getpass.getpass('Confirm PIN: '):
            sys.exit('PINs did not match. Nothing changed.')
        record = make_record(pin)
        pin = None
    else:
        sys.exit('Usage: sudo python3 deploy/setup_admin_pin.py')
    install_record(record)
    print('Tailscale page PIN configured.')
