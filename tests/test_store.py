"""Tests for mimik.store — the persistent capture store."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import store


def flow(fid, host, headers=None):
    return {
        "id": fid,
        "request": {
            "host": host, "method": "GET", "path": "/v1/x",
            "scheme": "https", "port": 443,
            "headers": headers or [["Authorization", "Bearer abc"]],
        },
        "response": {"status_code": 200},
    }


class FakeMitm:
    def body(self, flow_id, side):
        return b'{"ok": true}' if side == "response" else b""


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_save_and_load_roundtrip(self):
        flows = [flow("1", "api.example.com"), flow("2", "api.example.com")]
        paths = store.save_flows(flows, base=self.tmp)
        self.assertEqual(len(paths), 1)
        loaded = store.load_host("api.example.com", base=self.tmp)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["id"], "1")

    def test_save_groups_by_host(self):
        flows = [flow("1", "a.example.com"), flow("2", "b.example.com")]
        paths = store.save_flows(flows, base=self.tmp)
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(store.load_host("a.example.com", base=self.tmp)), 1)
        self.assertEqual(len(store.load_host("b.example.com", base=self.tmp)), 1)

    def test_save_embeds_bodies(self):
        store.save_flows([flow("1", "api.example.com")],
                         mitm=FakeMitm(), base=self.tmp)
        loaded = store.load_host("api.example.com", base=self.tmp)
        self.assertIn("ok", loaded[0].get("_saved_response_body", ""))

    def test_save_records_meta(self):
        store.save_flows([flow("1", "api.example.com")], base=self.tmp,
                         meta={"launched_file": "/tmp/app.AppImage",
                               "kind": "appimage"})
        cdir = store.captures_dir(self.tmp)
        name = os.listdir(cdir)[0]
        with open(os.path.join(cdir, name)) as f:
            data = json.load(f)
        self.assertEqual(data["meta"]["kind"], "appimage")

    def test_load_merges_and_dedupes(self):
        store.save_flows([flow("1", "api.example.com")], base=self.tmp)
        store.save_flows([flow("1", "api.example.com"),
                          flow("2", "api.example.com")], base=self.tmp)
        loaded = store.load_host("api.example.com", base=self.tmp)
        self.assertEqual([f["id"] for f in loaded], ["1", "2"])

    def test_load_unknown_host_empty(self):
        self.assertEqual(store.load_host("nope.example.com", base=self.tmp), [])

    def test_list_captures(self):
        store.save_flows([flow("1", "a.example.com"),
                          flow("2", "a.example.com")], base=self.tmp)
        store.save_flows([flow("3", "b.example.com")], base=self.tmp)
        rows = store.list_captures(base=self.tmp)
        self.assertEqual(rows[0][0], "a.example.com")
        self.assertEqual(rows[0][1], 2)
        self.assertEqual(rows[1][0], "b.example.com")

    def test_load_all(self):
        store.save_flows([flow("1", "a.example.com")], base=self.tmp)
        store.save_flows([flow("2", "b.example.com")], base=self.tmp)
        self.assertEqual(len(store.load_all(base=self.tmp)), 2)

    def test_save_client_copy_and_list(self):
        path = store.save_client_copy("example_client.py", "x = 1\n",
                                      base=self.tmp)
        self.assertTrue(os.path.exists(path))
        clients = store.list_clients(base=self.tmp)
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0][0], "example_client.py")

    def test_data_dir_env_override(self):
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        try:
            self.assertEqual(store.data_dir(), self.tmp)
        finally:
            del os.environ["MIMIK_DATA_DIR"]


if __name__ == "__main__":
    unittest.main()
