"""Tests for mimik.store — the persistent capture store."""
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

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
    def __init__(self, ws=None):
        self.ws = ws or {}

    def body(self, flow_id, side):
        return b'{"ok": true}' if side == "response" else b""

    def ws_messages(self, flow_id):
        return self.ws.get(flow_id, [])


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

    def test_save_embeds_ws_messages(self):
        messages = [{"from_client": True, "timestamp": 1.0,
                     "text": "hello"}]
        f = flow("w1", "api.example.com")
        f["request"]["path"] = "/chat"
        f["response"]["status_code"] = 101
        f["websocket"] = {"messages_meta": {"count": 1}}
        store.save_flows([f], mitm=FakeMitm(ws={"w1": messages}),
                         base=self.tmp)
        loaded = store.load_host("api.example.com", base=self.tmp)
        self.assertEqual(loaded[0].get("_saved_ws_messages"), messages)

    def test_save_ws_fetch_failure_keeps_the_flow(self):
        class FailingMitm(FakeMitm):
            def ws_messages(self, flow_id):
                raise RuntimeError("boom")

        f = flow("w1", "api.example.com")
        f["websocket"] = {"messages_meta": {"count": 1}}
        store.save_flows([f], mitm=FailingMitm(), base=self.tmp)
        loaded = store.load_host("api.example.com", base=self.tmp)
        self.assertEqual(len(loaded), 1)
        self.assertNotIn("_saved_ws_messages", loaded[0])

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


class CountingMitm:
    """Fake mitmweb that counts body fetches per flow id."""

    def __init__(self):
        self.calls = []

    def body(self, flow_id, side):
        self.calls.append((flow_id, side))
        return b'{"ok": true}' if side == "response" else b""


class IncrementalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _dir_size(self):
        cdir = store.captures_dir(self.tmp)
        total = 0
        for name in os.listdir(cdir):
            total += os.path.getsize(os.path.join(cdir, name))
        return total

    def test_repeat_save_writes_nothing(self):
        flows = [flow(str(i), "api.example.com") for i in range(5)]
        first = store.save_flows(flows, base=self.tmp)
        self.assertEqual(len(first), 1)
        size_after_first = self._dir_size()
        second = store.save_flows(flows, base=self.tmp)
        self.assertEqual(second, [])
        self.assertEqual(self._dir_size(), size_after_first)
        # Three polls of the same flows: no growth at all.
        store.save_flows(flows, base=self.tmp)
        self.assertEqual(self._dir_size(), size_after_first)
        self.assertEqual(len(store.load_host("api.example.com",
                                             base=self.tmp)), 5)

    def test_only_new_bodies_fetched(self):
        mitm = CountingMitm()
        store.save_flows([flow("1", "api.example.com")], mitm=mitm,
                         base=self.tmp)
        self.assertEqual(len(mitm.calls), 2)  # request + response, flow 1
        mitm.calls.clear()
        store.save_flows([flow("1", "api.example.com"),
                          flow("2", "api.example.com")], mitm=mitm,
                         base=self.tmp)
        # Only flow 2's bodies fetched — flow 1 never re-fetched.
        self.assertEqual(mitm.calls, [("2", "request"), ("2", "response")])
        self.assertEqual(len(store.load_host("api.example.com",
                                             base=self.tmp)), 2)

    def test_same_second_collision_gets_suffix(self):
        with mock.patch.object(store, "_stamp",
                               return_value="20260101-120000"):
            p1 = store.save_flows([flow("1", "api.example.com")],
                                  base=self.tmp)
            p2 = store.save_flows([flow("2", "api.example.com")],
                                  base=self.tmp)
        self.assertEqual(len(p1), 1)
        self.assertEqual(len(p2), 1)
        self.assertNotEqual(p1[0], p2[0])
        self.assertTrue(p2[0].endswith("_2.json"), p2[0])
        loaded = store.load_host("api.example.com", base=self.tmp)
        self.assertEqual([f["id"] for f in loaded], ["1", "2"])

    def test_list_captures_agrees_with_load_host(self):
        flows = [flow(str(i), "api.example.com") for i in range(3)]
        store.save_flows(flows, base=self.tmp)
        store.save_flows(flows, base=self.tmp)  # repeat: no dupes saved
        store.save_flows([flow("4", "api.example.com")], base=self.tmp)
        rows = dict((h, n) for h, n, _t in store.list_captures(base=self.tmp))
        self.assertEqual(rows["api.example.com"], 4)
        self.assertEqual(rows["api.example.com"],
                         len(store.load_host("api.example.com",
                                             base=self.tmp)))

    def test_truncation_visible(self):
        flows = [flow(str(i), "api.example.com") for i in range(5)]
        with mock.patch.object(store, "MAX_FLOWS_PER_HOST", 3):
            store.save_flows(flows, base=self.tmp)
            self.assertEqual(len(store.load_host("api.example.com",
                                                 base=self.tmp)), 3)
            rows = dict((h, n)
                        for h, n, _t in store.list_captures(base=self.tmp))
            self.assertEqual(rows["api.example.com"], 3)
            stats = store.capture_stats("api.example.com", base=self.tmp)
            self.assertTrue(stats["truncated"])
            self.assertEqual(stats["flows"], 5)
            self.assertEqual(stats["kept"], 3)

    def test_prune_keeps_newest_saves(self):
        stamps = ["20260101-12000%d" % i for i in range(1, 5)]
        with mock.patch.object(store, "_stamp", side_effect=stamps):
            for i in range(4):
                store.save_flows([flow(str(i), "api.example.com")],
                                 base=self.tmp)
        cdir = store.captures_dir(self.tmp)
        self.assertEqual(len(os.listdir(cdir)), 4)
        result = store.prune(base=self.tmp, keep=2)
        self.assertEqual(len(result["deleted"]), 2)
        self.assertEqual(result["flows_lost"], 2)
        self.assertEqual(result["kept"], 2)
        self.assertEqual(len(os.listdir(cdir)), 2)
        # The two newest flows survive; the two oldest are gone.
        loaded = store.load_host("api.example.com", base=self.tmp)
        self.assertEqual([f["id"] for f in loaded], ["2", "3"])

    def test_prune_dry_run_deletes_nothing(self):
        stamps = ["20260101-12000%d" % i for i in range(1, 4)]
        with mock.patch.object(store, "_stamp", side_effect=stamps):
            for i in range(3):
                store.save_flows([flow(str(i), "api.example.com")],
                                 base=self.tmp)
        result = store.prune(base=self.tmp, keep=1, dry_run=True)
        self.assertEqual(len(result["deleted"]), 2)
        cdir = store.captures_dir(self.tmp)
        self.assertEqual(len(os.listdir(cdir)), 3)


class DeleteTests(unittest.TestCase):
    """Item 5: per-host/client delete + disk usage reporting."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_format_size(self):
        self.assertEqual(store.format_size(0), "0 B")
        self.assertEqual(store.format_size(512), "512 B")
        self.assertEqual(store.format_size(2048), "2.0 KB")
        self.assertEqual(store.format_size(3 * 1024 * 1024), "3.0 MB")
        self.assertEqual(store.format_size(2 * 1024 * 1024 * 1024),
                         "2.0 GB")

    def test_capture_stats_includes_bytes(self):
        store.save_flows([flow("1", "a.example.com")], base=self.tmp)
        stats = store.capture_stats("a.example.com", base=self.tmp)
        self.assertGreater(stats["bytes"], 0)
        cdir = os.path.join(self.tmp, "captures")
        total = sum(os.path.getsize(os.path.join(cdir, f))
                    for f in os.listdir(cdir))
        self.assertEqual(stats["bytes"], total)

    def test_delete_host_removes_all_saves(self):
        store.save_flows([flow("1", "a.example.com")], base=self.tmp)
        store.save_flows([flow("2", "b.example.com")], base=self.tmp)
        result = store.delete_host("a.example.com", base=self.tmp)
        self.assertEqual(result["flows_lost"], 1)
        self.assertGreater(result["bytes_freed"], 0)
        self.assertEqual(len(result["deleted"]), 1)
        self.assertEqual(store.load_host("a.example.com", base=self.tmp),
                         [])
        self.assertEqual(len(store.load_host("b.example.com",
                                             base=self.tmp)), 1)

    def test_delete_host_missing_is_empty_report(self):
        result = store.delete_host("nobody.example.com", base=self.tmp)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["flows_lost"], 0)
        self.assertEqual(result["bytes_freed"], 0)

    def test_delete_client_removes_file(self):
        path = os.path.join(self.tmp, "x_client.py")
        with open(path, "w") as fh:
            fh.write("# client\n")
        store.save_client_copy("x_client.py", path, base=self.tmp)
        result = store.delete_client("x_client.py", base=self.tmp)
        self.assertIsNotNone(result["deleted"])
        self.assertGreater(result["bytes_freed"], 0)
        self.assertFalse(os.path.exists(result["deleted"]))
        again = store.delete_client("x_client.py", base=self.tmp)
        self.assertIsNone(again["deleted"])
        self.assertEqual(again["bytes_freed"], 0)

    def test_delete_client_refuses_traversal(self):
        self.assertIsNone(
            store.delete_client("../x_client.py",
                                base=self.tmp)["deleted"])
        self.assertIsNone(
            store.delete_client("x.py", base=self.tmp)["deleted"])

    def test_disk_usage_sums_areas(self):
        store.save_flows([flow("1", "a.example.com")], base=self.tmp)
        path = os.path.join(self.tmp, "x_client.py")
        with open(path, "w") as fh:
            fh.write("# client\n")
        store.save_client_copy("x_client.py", path, base=self.tmp)
        usage = store.disk_usage(base=self.tmp)
        self.assertGreater(usage["captures"], 0)
        self.assertGreater(usage["clients"], 0)
        self.assertEqual(usage["total"],
                         usage["captures"] + usage["clients"]
                         + usage["assets"] + usage["packs"]
                         + usage["presets"] + usage["other"])


class PrivatePermsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_save_flows_writes_0600(self):
        # Review #13: captures hold live tokens — owner only.
        paths = store.save_flows([flow("1", "api.example.com")],
                                 base=self.tmp)
        self.assertEqual(len(paths), 1)
        mode = stat.S_IMODE(os.stat(paths[0]).st_mode)
        self.assertEqual(mode, 0o600)

    def test_open_private_tightens_existing_file(self):
        # A file left 0644 by an older version tightens on the next
        # write to the same path (os.open's mode only applies at
        # creation, so open_private chmods afterwards too).
        path = os.path.join(self.tmp, "old.json")
        with open(path, "w") as fh:
            fh.write("{}")
        os.chmod(path, 0o644)
        with store.open_private(path, "w") as fh:
            fh.write("{}")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
