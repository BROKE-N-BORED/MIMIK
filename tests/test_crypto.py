"""Capture-store encryption (v0.8 G1) — Argon2id + AES-GCM, the
2026-11-13 decision: key in memory only, passphrase asked per start,
forgotten passphrase means unrecoverable by design.

Skipped when the cryptography / argon2-cffi packages are missing
(they are core dependencies, so a real install always has them).
"""
import argparse
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import crypto, store


def flow(fid, host="api.example.com", path="/v1/ping"):
    return {"id": fid, "request": {
        "host": host, "method": "GET", "path": path,
        "scheme": "https", "port": 443,
        "headers": [["Authorization", "Bearer SECRETTOKEN"]]},
        "response": {"status_code": 200, "headers": []}}


@unittest.skipUnless(crypto.available(), "crypto packages missing")
class CryptoPrimitiveTests(unittest.TestCase):
    def test_json_roundtrip(self):
        key = crypto.derive_key("pw", b"0" * 16)
        env = crypto.encrypt_json(key, {"a": [1, 2, {"b": "x"}]})
        self.assertTrue(crypto.is_envelope(env))
        self.assertEqual(crypto.decrypt_json(key, env),
                         {"a": [1, 2, {"b": "x"}]})

    def test_wrong_key_and_tamper_fail(self):
        key = crypto.derive_key("pw", b"0" * 16)
        other = crypto.derive_key("nope", b"0" * 16)
        env = crypto.encrypt_json(key, {"secret": 1})
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_json(other, env)
        env2 = dict(env, ct=env["ct"][:-4] + "AAAA")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_json(key, env2)

    def test_marker_check(self):
        marker, key = crypto.make_marker("correct horse")
        self.assertIsNotNone(crypto.check_marker(marker, "correct horse"))
        self.assertIsNone(crypto.check_marker(marker, "wrong"))
        self.assertEqual(
            crypto.check_marker(marker, "correct horse"), key)


@unittest.skipUnless(crypto.available(), "crypto packages missing")
class StoreEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store.save_flows([flow("1"), flow("2")], base=self.tmp)

    def tearDown(self):
        store.lock(base=self.tmp)

    def _capture_files(self):
        cdir = store.captures_dir(self.tmp)
        return [os.path.join(cdir, n) for n in os.listdir(cdir)]

    def test_enable_seals_existing_and_roundtrips(self):
        n = store.enable_encryption("pw123", base=self.tmp)
        self.assertEqual(n, 1)
        self.assertTrue(store.encryption_enabled(self.tmp))
        self.assertTrue(store.unlocked(self.tmp))
        for path in self._capture_files():
            with open(path) as fh:
                raw = fh.read()
            self.assertNotIn("SECRETTOKEN", raw)
            self.assertNotIn("api.example.com", raw)
            self.assertTrue(crypto.is_envelope(json.loads(raw)))
        loaded = store.load_host("api.example.com", base=self.tmp)
        self.assertEqual([f["id"] for f in loaded], ["1", "2"])

    def test_locked_reads_empty_but_reports_locked(self):
        store.enable_encryption("pw123", base=self.tmp)
        store.lock(base=self.tmp)
        self.assertEqual(store.load_host("api.example.com",
                                         base=self.tmp), [])
        self.assertEqual(store.list_captures(base=self.tmp), [])
        locked = store.locked_captures(base=self.tmp)
        self.assertEqual(len(locked), 1)
        self.assertTrue(locked[0].endswith(".json"))
        # The B7 registry must NOT call a locked file corrupt.
        self.assertEqual(store.unreadable_captures(base=self.tmp), [])

    def test_unlock_wrong_then_right(self):
        store.enable_encryption("pw123", base=self.tmp)
        store.lock(base=self.tmp)
        self.assertFalse(store.unlock("nope", base=self.tmp))
        self.assertFalse(store.unlocked(self.tmp))
        self.assertTrue(store.unlock("pw123", base=self.tmp))
        self.assertEqual(
            len(store.load_host("api.example.com", base=self.tmp)), 2)

    def test_save_while_locked_raises(self):
        store.enable_encryption("pw123", base=self.tmp)
        store.lock(base=self.tmp)
        with self.assertRaises(store.StoreLockedError):
            store.save_flows([flow("3")], base=self.tmp)

    def test_save_while_unlocked_seals(self):
        store.enable_encryption("pw123", base=self.tmp)
        store.save_flows([flow("3")], base=self.tmp)
        self.assertEqual(
            len(store.load_host("api.example.com", base=self.tmp)), 3)
        for path in self._capture_files():
            with open(path) as fh:
                self.assertNotIn("SECRETTOKEN", fh.read())

    def test_disable_restores_plaintext(self):
        store.enable_encryption("pw123", base=self.tmp)
        n = store.disable_encryption(base=self.tmp)
        self.assertEqual(n, 1)
        self.assertFalse(store.encryption_enabled(self.tmp))
        for path in self._capture_files():
            with open(path) as fh:
                data = json.load(fh)
            self.assertEqual(data["host"], "api.example.com")
        self.assertEqual(
            len(store.load_host("api.example.com", base=self.tmp)), 2)

    def test_enable_twice_raises(self):
        store.enable_encryption("pw123", base=self.tmp)
        with self.assertRaises(crypto.CryptoError):
            store.enable_encryption("pw123", base=self.tmp)


@unittest.skipUnless(crypto.available(), "crypto packages missing")
class CliEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        store.save_flows([flow("1")])

    def tearDown(self):
        store.lock()
        del os.environ["MIMIK_DATA_DIR"]

    def test_cli_on_off_roundtrip(self):
        from mimik import cli
        with mock.patch("getpass.getpass",
                        side_effect=["pw123", "pw123"]):
            cli.cmd_encrypt_on(argparse.Namespace())
        self.assertTrue(store.encryption_enabled())
        store.lock()
        with mock.patch("getpass.getpass", return_value="pw123"):
            cli.cmd_encrypt_off(argparse.Namespace())
        self.assertFalse(store.encryption_enabled())
        self.assertEqual(len(store.load_host("api.example.com")), 1)

    def test_maybe_unlock_wrong_passphrase_stops(self):
        from mimik import cli
        store.enable_encryption("pw123")
        store.lock()
        args = argparse.Namespace(cmd="hosts", har=None, curl=None)
        with mock.patch("getpass.getpass", return_value="nope"):
            with self.assertRaises(SystemExit):
                cli._maybe_unlock(args)
        with mock.patch("getpass.getpass", return_value="pw123"):
            cli._maybe_unlock(args)  # must not raise
        self.assertTrue(store.unlocked())

    def test_maybe_unlock_ignores_non_store_commands(self):
        from mimik import cli
        store.enable_encryption("pw123")
        store.lock()
        args = argparse.Namespace(cmd="doctor", har=None, curl=None)
        with mock.patch("getpass.getpass",
                        side_effect=AssertionError("prompted!")):
            cli._maybe_unlock(args)  # doctor is never interrupted


if __name__ == "__main__":
    unittest.main()
