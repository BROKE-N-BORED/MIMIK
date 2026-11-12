"""Tests for mimik.sources.mitm (mock Mitm object, no live server)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik.sources import mitm


def flow(fid, host, method, path, status=200, headers=None):
    return {
        "id": fid,
        "request": {
            "host": host, "method": method, "path": path,
            "scheme": "https", "port": 443,
            "headers": headers or [],
        },
        "response": {"status_code": status},
    }


class FakeMitm:
    def __init__(self, bodies=None):
        self.bodies = bodies or {}

    def body(self, flow_id, side):
        return self.bodies.get((flow_id, side), b"")


FLOWS = [
    flow("1", "api.example.com", "GET", "/v1/users?page=1", 500),
    flow("2", "api.example.com", "GET", "/v1/users?page=2", 200),
    flow("3", "api.example.com", "POST", "/v1/like", 200),
    flow("4", "cdn.example.com", "GET", "/img.png", 200),
]


class MitmTests(unittest.TestCase):
    def test_hosts_counts_sorted(self):
        self.assertEqual(mitm.hosts(FLOWS),
                         [("api.example.com", 3), ("cdn.example.com", 1)])

    def test_hosts_empty(self):
        self.assertEqual(mitm.hosts([]), [])

    def test_endpoints_dedup_latest_wins(self):
        eps = mitm.endpoints(FakeMitm(), FLOWS, "api.example.com")
        self.assertEqual(len(eps), 2)
        users = [e for e in eps if e["path"] == "/v1/users"][0]
        self.assertEqual(users["status"], 200)
        self.assertEqual(users["query"], "page=2")

    def test_endpoints_other_host_excluded(self):
        eps = mitm.endpoints(FakeMitm(), FLOWS, "cdn.example.com")
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["path"], "/img.png")

    def test_endpoints_bodies_decoded(self):
        bodies = {("3", "request"): b'{"subjectId": "abc"}',
                  ("3", "response"): b'{"liked": true}'}
        eps = mitm.endpoints(FakeMitm(bodies), FLOWS, "api.example.com")
        like = [e for e in eps if e["path"] == "/v1/like"][0]
        self.assertIn("subjectId", like["request_body"])
        self.assertIn("liked", like["response_body"])

    def test_endpoints_binary_body_flagged(self):
        bodies = {("3", "request"): b"\x00\xff\xfe\xfd" * 10}
        eps = mitm.endpoints(FakeMitm(bodies), FLOWS, "api.example.com")
        like = [e for e in eps if e["path"] == "/v1/like"][0]
        self.assertIn("bytes binary", like["request_body"])

    def test_decode_truncates_long_bodies(self):
        long_body = b"x" * 9000
        out = mitm._decode(long_body)
        self.assertLess(len(out), 5000)
        self.assertIn("truncated", out)

    def test_endpoints_from_saved_uses_embedded_bodies(self):
        flows = [dict(f) for f in FLOWS]
        flows[2]["_saved_request_body"] = '{"subjectId": "abc"}'
        flows[2]["_saved_response_body"] = '{"liked": true}'
        eps = mitm.endpoints_from_saved(flows, "api.example.com")
        like = [e for e in eps if e["path"] == "/v1/like"][0]
        self.assertIn("subjectId", like["request_body"])
        self.assertIn("liked", like["response_body"])

    def test_endpoints_from_saved_base64_body(self):
        import base64

        flows = [dict(f) for f in FLOWS]
        flows[2]["_saved_request_body"] = base64.b64encode(
            b'{"a": 1}').decode()
        flows[2]["_saved_request_body_b64"] = True
        eps = mitm.endpoints_from_saved(flows, "api.example.com")
        like = [e for e in eps if e["path"] == "/v1/like"][0]
        self.assertIn('"a": 1', like["request_body"])


if __name__ == "__main__":
    unittest.main()
