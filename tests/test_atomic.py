"""Tests for 0700 data dirs + atomic saves (v0.4 item 3).

Captures hold live session tokens: the data dir is 0700, saves are
0600 and atomic (temp file + fsync + rename), so an interrupted save
leaves the previous good file intact — never a partial file.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import presets, store


def flow(fid, host):
    return {
        "id": fid,
        "request": {"host": host, "method": "GET", "path": "/v1/x",
                    "scheme": "https", "port": 443, "headers": []},
        "response": {"status_code": 200},
    }


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class EnsurePrivateDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_creates_0700(self):
        target = os.path.join(self.tmp, "newdir")
        store.ensure_private_dir(target)
        self.assertEqual(mode(target), 0o700)

    def test_tightens_an_existing_0755_dir(self):
        target = os.path.join(self.tmp, "old")
        os.makedirs(target)
        os.chmod(target, 0o755)
        self.assertEqual(mode(target), 0o755)
        store.ensure_private_dir(target)
        self.assertEqual(mode(target), 0o700)

    def test_nested_creation_tightens_the_leaf(self):
        target = os.path.join(self.tmp, "a", "b", "c")
        store.ensure_private_dir(target)
        self.assertEqual(mode(target), 0o700)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_writes_str_and_bytes(self):
        p1 = os.path.join(self.tmp, "a.txt")
        store.atomic_write(p1, "hello")
        with open(p1) as fh:
            self.assertEqual(fh.read(), "hello")
        p2 = os.path.join(self.tmp, "b.bin")
        store.atomic_write(p2, b"\x00\x01\x02")
        with open(p2, "rb") as fh:
            self.assertEqual(fh.read(), b"\x00\x01\x02")

    def test_private_is_0600(self):
        path = os.path.join(self.tmp, "secret.json")
        store.atomic_write(path, "{}")
        self.assertEqual(mode(path), 0o600)

    def test_public_is_0644(self):
        path = os.path.join(self.tmp, "plain.txt")
        store.atomic_write(path, "x", private=False)
        self.assertEqual(mode(path), 0o644)

    def test_replaces_an_existing_file(self):
        path = os.path.join(self.tmp, "f.txt")
        store.atomic_write(path, "v1")
        store.atomic_write(path, "v2")
        with open(path) as fh:
            self.assertEqual(fh.read(), "v2")

    def test_failed_replace_leaves_the_previous_file_intact(self):
        path = os.path.join(self.tmp, "f.txt")
        store.atomic_write(path, "good")
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                store.atomic_write(path, "partial")
        with open(path) as fh:
            self.assertEqual(fh.read(), "good")
        # No temp file left behind.
        self.assertEqual([n for n in os.listdir(self.tmp)
                          if n.startswith(".mimik-tmp-")], [])

    def test_failed_replace_with_no_previous_file_leaves_nothing(self):
        path = os.path.join(self.tmp, "f.txt")
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                store.atomic_write(path, "partial")
        self.assertFalse(os.path.exists(path))
        self.assertEqual(os.listdir(self.tmp), [])


class StoreAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_save_flows_creates_data_dir_0700(self):
        base = os.path.join(self.tmp, "fresh")
        store.save_flows([flow("1", "api.example.com")], base=base)
        self.assertEqual(mode(base), 0o700)
        self.assertEqual(mode(os.path.join(base, "captures")), 0o700)

    def test_save_flows_files_are_0600(self):
        paths = store.save_flows([flow("1", "api.example.com")],
                                 base=self.tmp)
        self.assertEqual(len(paths), 1)
        self.assertEqual(mode(paths[0]), 0o600)

    def test_interrupted_save_leaves_no_partial_capture(self):
        store.save_flows([flow("1", "api.example.com")], base=self.tmp)
        before = sorted(os.listdir(os.path.join(self.tmp, "captures")))
        with mock.patch("os.replace", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                store.save_flows([flow("2", "api.example.com")],
                                 base=self.tmp)
        after = sorted(os.listdir(os.path.join(self.tmp, "captures")))
        # No new .json file, no temp file — the old capture is all there is.
        self.assertEqual(after, before)
        loaded = store.load_host("api.example.com", base=self.tmp)
        self.assertEqual([f["id"] for f in loaded], ["1"])

    def test_stray_temp_file_does_not_break_the_store(self):
        store.save_flows([flow("1", "api.example.com")], base=self.tmp)
        stray = os.path.join(self.tmp, "captures", ".mimik-tmp-crashed")
        with open(stray, "w") as fh:
            fh.write('{"host": "api.example.com", "flows": [garbage')
        self.assertEqual(len(store.list_captures(base=self.tmp)), 1)
        loaded = store.load_host("api.example.com", base=self.tmp)
        self.assertEqual([f["id"] for f in loaded], ["1"])

    def test_save_client_copy_is_0644_and_atomic(self):
        path = store.save_client_copy("x_client.py", "print(1)\n",
                                      base=self.tmp)
        self.assertEqual(mode(path), 0o644)
        with mock.patch("os.replace", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                store.save_client_copy("x_client.py", "print(2)\n",
                                       base=self.tmp)
        with open(path) as fh:
            self.assertEqual(fh.read(), "print(1)\n")


class PresetAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _preset(self, name="test"):
        return {"name": name, "scope": "app", "version": 1,
                "headers": {"X-Test": "yes"}}

    def test_preset_save_is_0600_and_atomic(self):
        path = presets._save_preset(self._preset(), base=self.tmp)
        self.assertEqual(mode(path), 0o600)
        with open(path) as fh:
            before = fh.read()
        with mock.patch("os.replace", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                presets._save_preset(self._preset(), base=self.tmp)
        with open(path) as fh:
            self.assertEqual(fh.read(), before)

    def test_settings_write_is_atomic(self):
        presets.set_auto_learn(True, base=self.tmp)
        settings_path = os.path.join(self.tmp, "settings.json")
        with open(settings_path) as fh:
            before = fh.read()
        self.assertIn('"auto_learn_presets": true', before)
        with mock.patch("os.replace", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                presets.set_auto_learn(False, base=self.tmp)
        with open(settings_path) as fh:
            self.assertEqual(fh.read(), before)
        self.assertTrue(presets.auto_learn_enabled(base=self.tmp))


if __name__ == "__main__":
    unittest.main()
