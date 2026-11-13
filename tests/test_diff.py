"""v0.4 item 12: compare two captures — new/removed/changed."""

import json
import os
import tempfile
import unittest

from mimik import diff, store

HAR_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                           "sample.har")


def _flow(fid, host, method, path, status=200, resp_body=None):
    flow = {
        "id": fid,
        "request": {"host": host, "method": method, "path": path,
                    "scheme": "https", "port": 443, "headers": []},
        "response": {"status_code": status, "headers": []},
    }
    if resp_body is not None:
        flow["_saved_response_body"] = resp_body
    return flow


class DiffTests(unittest.TestCase):
    def test_new_removed_changed_unchanged(self):
        flows_a = [
            _flow("1", "a.example.com", "GET", "/api/users",
                  resp_body='{"users": []}'),
            _flow("2", "a.example.com", "GET", "/api/old"),
            _flow("3", "a.example.com", "GET", "/api/same",
                  resp_body='{"ok": true}'),
        ]
        flows_b = [
            _flow("4", "b.example.com", "GET", "/api/users", 500,
                  resp_body='{"users": [], "error": "boom"}'),
            _flow("5", "b.example.com", "GET", "/api/new"),
            _flow("6", "b.example.com", "GET", "/api/same",
                  resp_body='{"ok": true}'),
        ]
        result = diff.diff_captures(flows_a, flows_b)
        self.assertEqual(result["total_a"], 3)
        self.assertEqual(result["total_b"], 3)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual([(e["method"], e["path"]) for e in result["new"]],
                         [("GET", "/api/new")])
        self.assertEqual(
            [(e["method"], e["path"]) for e in result["removed"]],
            [("GET", "/api/old")])
        self.assertEqual(len(result["changed"]), 1)
        changed = result["changed"][0]
        self.assertEqual(changed["path"], "/api/users")
        self.assertEqual(changed["old_status"], 200)
        self.assertEqual(changed["new_status"], 500)
        self.assertEqual(changed["added_keys"], ["error"])
        self.assertEqual(changed["removed_keys"], [])

    def test_ids_generalize_so_a_new_id_is_not_a_new_endpoint(self):
        flows_a = [_flow("1", "a.example.com", "GET", "/users/123")]
        flows_b = [_flow("2", "b.example.com", "GET", "/users/456")]
        result = diff.diff_captures(flows_a, flows_b)
        self.assertEqual(result["new"], [])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["unchanged"], 1)

    def test_same_shape_different_values_is_not_a_change(self):
        flows_a = [_flow("1", "a.example.com", "GET", "/api/me",
                         resp_body='{"name": "Ada", "id": 1}')]
        flows_b = [_flow("2", "b.example.com", "GET", "/api/me",
                         resp_body='{"name": "Bo", "id": 2}')]
        result = diff.diff_captures(flows_a, flows_b)
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["unchanged"], 1)

    def test_removed_response_keys_are_named(self):
        flows_a = [_flow("1", "a.example.com", "GET", "/api/me",
                         resp_body='{"name": "Ada", "nick": "ad"}')]
        flows_b = [_flow("2", "b.example.com", "GET", "/api/me",
                         resp_body='{"name": "Ada"}')]
        result = diff.diff_captures(flows_a, flows_b)
        self.assertEqual(len(result["changed"]), 1)
        self.assertEqual(result["changed"][0]["removed_keys"], ["nick"])
        self.assertEqual(result["changed"][0].get("added_keys"), [])

    def test_status_only_when_no_saved_body(self):
        flows_a = [_flow("1", "a.example.com", "GET", "/api/x", 200)]
        flows_b = [_flow("2", "b.example.com", "GET", "/api/x", 404)]
        result = diff.diff_captures(flows_a, flows_b)
        self.assertEqual(len(result["changed"]), 1)
        self.assertEqual(result["changed"][0]["old_status"], 200)
        self.assertEqual(result["changed"][0]["new_status"], 404)
        self.assertNotIn("added_keys", result["changed"][0])

    def test_sample_counts_come_through(self):
        flows_a = [_flow("1", "a.example.com", "GET", "/api/x")]
        flows_b = [_flow("2", "b.example.com", "GET", "/api/x"),
                   _flow("3", "b.example.com", "GET", "/api/x"),
                   _flow("4", "b.example.com", "GET", "/api/y")]
        result = diff.diff_captures(flows_a, flows_b)
        self.assertEqual(result["new"][0]["samples"], 1)
        self.assertEqual(result["unchanged"], 1)

    def test_format_diff(self):
        result = diff.diff_captures(
            [_flow("1", "a.example.com", "GET", "/api/old"),
             _flow("2", "a.example.com", "GET", "/api/users", 200,
                   resp_body='{"a": 1}')],
            [_flow("3", "b.example.com", "GET", "/api/new"),
             _flow("4", "b.example.com", "GET", "/api/users", 500,
                   resp_body='{"a": 1, "b": 2}')])
        text = diff.format_diff(result, "before", "after")
        self.assertIn("Capture diff: before -> after", text)
        self.assertIn("2 endpoints -> 2 endpoints", text)
        self.assertIn("1 new, 1 removed, 1 changed, 0 unchanged", text)
        self.assertIn("--- New ---", text)
        self.assertIn("GET /api/new", text)
        self.assertIn("--- Removed ---", text)
        self.assertIn("GET /api/old", text)
        self.assertIn("--- Changed ---", text)
        self.assertIn("GET /api/users: status 200 -> 500; "
                      "response keys +b", text)

    def test_format_diff_no_differences(self):
        result = diff.diff_captures(
            [_flow("1", "a.example.com", "GET", "/api/x")],
            [_flow("2", "b.example.com", "GET", "/api/x")])
        text = diff.format_diff(result, "a", "b")
        self.assertIn("No differences.", text)


class LoadSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_load_source_host(self):
        store.save_flows([_flow("1", "a.example.com", "GET", "/api/x")])
        flows, label = diff.load_source("a.example.com")
        self.assertEqual(len(flows), 1)
        self.assertEqual(label, "a.example.com")

    def test_load_source_unknown_host_raises(self):
        with self.assertRaises(diff.DiffError):
            diff.load_source("nobody.example.com")

    def test_load_source_capture_file(self):
        path = os.path.join(self.tmp, "cap.json")
        with open(path, "w") as fh:
            json.dump({"host": "a.example.com", "saved_at": "x",
                       "flows": [_flow("1", "a.example.com",
                                       "GET", "/api/x")]}, fh)
        flows, label = diff.load_source(path)
        self.assertEqual(len(flows), 1)
        self.assertEqual(label, path)

    def test_load_source_bad_file_raises(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(diff.DiffError):
            diff.load_source(path)

    def test_load_source_har_file(self):
        flows, label = diff.load_source(HAR_FIXTURE)
        self.assertEqual(len(flows), 4)
        self.assertEqual(label, HAR_FIXTURE)


if __name__ == "__main__":
    unittest.main()
