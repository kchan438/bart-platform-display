"""Local touchscreen PIN verification; no plaintext PIN is persisted or logged."""

from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
from pathlib import Path
import re
import threading
import time


PIN_PATH = Path('/etc/bart-platform-display/admin-pin.json')
ITERATIONS = 600_000
_ARGON_PREFIX = r'\$argon2id\$v=19\$m=19456,t=2,p=1\$[A-Za-z0-9+/]{43}\$[A-Za-z0-9+/]{43}'


def _hasher():
    # Lazy import keeps legacy records usable if this optional OS package is
    # missing; new records fail closed rather than weakening their hash.
    from argon2 import PasswordHasher, Type
    return PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1,
                          hash_len=32, salt_len=32, type=Type.ID)


@dataclass(frozen=True)
class Verification:
    ok: bool
    message: str = ''


def valid_pin(pin):
    return isinstance(pin, str) and pin.isascii() and pin.isdecimal() and 4 <= len(pin) <= 12


def make_record(pin):
    if not valid_pin(pin):
        raise ValueError('Use 4 to 12 digits for the PIN.')
    return {'version': 2, 'algorithm': 'argon2id', 'hash': _hasher().hash(pin)}


def validate_record(record):
    if record.get('version') == 2:
        if (record.get('algorithm') != 'argon2id'
                or not isinstance(record.get('hash'), str)
                or not re.fullmatch(_ARGON_PREFIX, record['hash'])):
            raise ValueError('Invalid PIN record')
    elif (record.get('version') != 1 or record.get('iterations') != ITERATIONS
          or len(base64.b64decode(record['salt'], validate=True)) != 32
          or len(base64.b64decode(record['hash'], validate=True)) != 32):
        raise ValueError('Invalid PIN record')


class PinVerifier:
    def __init__(self, path=PIN_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._failures = 0
        self._blocked_until = 0.0

    def verify(self, pin):
        if not self._lock.acquire(blocking=False):
            return Verification(False, 'PIN check already running.')
        try:
            now = time.monotonic()
            if now < self._blocked_until:
                return Verification(False, 'Too many tries. Wait 30s.')
            if self._failures >= 5:
                self._failures = 0
            try:
                if self.path.stat().st_size > 4096:
                    raise ValueError('Invalid PIN record')
                record = json.loads(self.path.read_text())
                validate_record(record)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                return Verification(False, 'PIN setup required via SSH.')
            ok = False
            if valid_pin(pin):
                if record['version'] == 2:
                    try:
                        from argon2.exceptions import VerifyMismatchError, VerificationError
                        hasher = _hasher()
                    except ImportError:
                        return Verification(False, 'PIN verification unavailable.')
                    try:
                        ok = hasher.verify(record['hash'], pin)
                    except VerifyMismatchError:
                        ok = False
                    except VerificationError:
                        return Verification(False, 'PIN verification unavailable.')
                else:
                    salt = base64.b64decode(record['salt'])
                    expected = base64.b64decode(record['hash'])
                    actual = hashlib.pbkdf2_hmac('sha256', pin.encode('ascii'), salt, ITERATIONS)
                    ok = hmac.compare_digest(actual, expected)
            if ok:
                self._failures = 0
                return Verification(True)
            self._failures += 1
            if self._failures >= 5:
                self._blocked_until = time.monotonic() + 30
                return Verification(False, 'Too many tries. Wait 30s.')
            return Verification(False, 'Incorrect PIN. Try again.')
        finally:
            self._lock.release()


_verifier = PinVerifier()


class VerifyJob:
    def __init__(self, pin):
        self.result = None
        self._done = threading.Event()

        def work():
            nonlocal pin
            try:
                self.result = _verifier.verify(pin)
            except Exception:
                self.result = Verification(False, 'PIN verification unavailable.')
            finally:
                pin = None
                self._done.set()

        threading.Thread(target=work, daemon=True).start()

    @property
    def done(self):
        return self._done.is_set()
