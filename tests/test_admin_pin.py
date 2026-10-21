import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bartdisplay import admin_pin


class PinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Derive the shared legacy fixture once; per-test state remains isolated.
        cls.digest = hashlib.pbkdf2_hmac('sha256', b'739125', b's' * 32,
                                        admin_pin.ITERATIONS)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'pin.json'
        # Disposable fixture PIN; never the device owner's PIN.
        self.pin = '739125'
        self.salt = b's' * 32
        self.record = {'version': 1, 'iterations': admin_pin.ITERATIONS,
                       'salt': base64.b64encode(self.salt).decode(),
                       'hash': base64.b64encode(self.digest).decode()}
        self.path.write_text(json.dumps(self.record))
        self.verifier = admin_pin.PinVerifier(self.path)

    def test_correct_pin_and_wrong_pin(self):
        self.assertFalse(self.verifier.verify('1234').ok)
        self.assertTrue(self.verifier.verify(self.pin).ok)
        self.assertEqual(self.verifier._failures, 0)

    def test_five_failures_enforce_cooldown_even_for_correct_pin(self):
        with mock.patch.object(admin_pin.time, 'monotonic', return_value=100):
            for _ in range(5):
                self.assertFalse(self.verifier.verify('bad').ok)
            self.assertFalse(self.verifier.verify(self.pin).ok)
        with mock.patch.object(admin_pin.time, 'monotonic', return_value=131):
            self.assertTrue(self.verifier.verify(self.pin).ok)

    def test_missing_malformed_or_wrong_parameters_fail_closed(self):
        for value in ('missing', '{}', '[]', 'not json',
                      json.dumps(dict(self.record, iterations=1))):
            with self.subTest(value=value):
                if value == 'missing':
                    self.path.unlink()
                else:
                    self.path.write_text(value)
                result = self.verifier.verify(self.pin)
                self.assertFalse(result.ok)
                self.assertIn('setup required', result.message)

    def test_parallel_verification_does_not_bypass_lock(self):
        with self.verifier._lock:
            self.assertFalse(self.verifier.verify(self.pin).ok)

    def test_new_argon2_records_are_salted_and_verify_correctly(self):
        record = admin_pin.make_record(self.pin)
        self.assertEqual(record['version'], 2)
        self.assertNotEqual(record['hash'], admin_pin.make_record(self.pin)['hash'])
        self.path.write_text(json.dumps(record))
        self.assertFalse(self.verifier.verify('1234').ok)
        self.assertTrue(self.verifier.verify(self.pin).ok)

    def test_unexpected_argon2_cost_cannot_trigger_unbounded_memory_use(self):
        record = admin_pin.make_record(self.pin)
        record['hash'] = record['hash'].replace('m=19456', 'm=999999999')
        self.path.write_text(json.dumps(record))
        self.assertFalse(self.verifier.verify(self.pin).ok)

    def test_pin_format_is_ascii_digits_and_preserves_leading_zero(self):
        self.assertTrue(admin_pin.valid_pin('0001'))
        for pin in ('123', '1' * 13, '１２３４', '12 34', None):
            self.assertFalse(admin_pin.valid_pin(pin))


if __name__ == '__main__':
    unittest.main()
