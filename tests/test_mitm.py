"""Tests for mimik.sources.mitm (mock Mitm object, no live server)."""
import os
import sys
import unittest
from unittest import mock

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
    def __init__(self, bodies=None, ws=None):
        self.bodies = bodies or {}
        self.ws = ws or {}

    def body(self, flow_id, side):
        return self.bodies.get((flow_id, side), b"")

    def ws_messages(self, flow_id):
        return self.ws.get(flow_id, [])


def ws_flow(fid, host):
    f = flow(fid, host, "GET", "/chat", 101)
    f["websocket"] = {"messages_meta": {"count": 2}}
    return f


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

    def test_endpoints_carry_sample_counts(self):
        # /v1/users was captured twice (page=1, page=2); /v1/like once.
        eps = mitm.endpoints(FakeMitm(), FLOWS, "api.example.com")
        by_path = {e["path"]: e for e in eps}
        self.assertEqual(by_path["/v1/users"]["samples"], 2)
        self.assertEqual(by_path["/v1/like"]["samples"], 1)
        eps = mitm.endpoints_from_saved(FLOWS, "api.example.com")
        by_path = {e["path"]: e for e in eps}
        self.assertEqual(by_path["/v1/users"]["samples"], 2)
        self.assertEqual(by_path["/v1/like"]["samples"], 1)

    def test_endpoints_live_ws_carries_messages(self):
        messages = [{"from_client": True, "timestamp": 1.0,
                     "text": "hello"}]
        flows = FLOWS + [ws_flow("w1", "api.example.com")]
        eps = mitm.endpoints(FakeMitm(ws={"w1": messages}), flows,
                             "api.example.com")
        by_path = {e["path"]: e for e in eps}
        self.assertTrue(by_path["/chat"]["websocket"])
        self.assertEqual(by_path["/chat"]["ws_messages"], messages)
        self.assertFalse(by_path["/v1/users"]["websocket"])
        self.assertEqual(by_path["/v1/users"]["ws_messages"], [])

    def test_endpoints_live_ws_fetch_failure_means_empty(self):
        class FailingMitm(FakeMitm):
            def ws_messages(self, flow_id):
                raise mitm.MitmError("boom")

        flows = FLOWS + [ws_flow("w1", "api.example.com")]
        eps = mitm.endpoints(FailingMitm(), flows, "api.example.com")
        by_path = {e["path"]: e for e in eps}
        self.assertTrue(by_path["/chat"]["websocket"])
        self.assertEqual(by_path["/chat"]["ws_messages"], [])

    def test_endpoints_from_saved_ws_uses_embedded(self):
        messages = [{"from_client": True, "timestamp": 1.0,
                     "text": "hello"}]
        f = ws_flow("w1", "api.example.com")
        f["_saved_ws_messages"] = messages
        eps = mitm.endpoints_from_saved(FLOWS + [f], "api.example.com")
        by_path = {e["path"]: e for e in eps}
        self.assertTrue(by_path["/chat"]["websocket"])
        self.assertEqual(by_path["/chat"]["ws_messages"], messages)

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


class NoProxyTests(unittest.TestCase):
    """Item 32g: localhost mitmweb calls never go through a proxy."""

    def test_requests_carry_no_proxy(self):
        m = mitm.Mitm()
        flows_resp = mock.Mock()
        flows_resp.status_code = 200
        flows_resp.json.return_value = []
        body_resp = mock.Mock()
        body_resp.content = b"body-bytes"
        with mock.patch.object(m._http, "get",
                               side_effect=[mock.Mock(), flows_resp,
                                            body_resp]) as get:
            self.assertEqual(m.flows(), [])
            self.assertEqual(m.body("f1", "response"), b"body-bytes")
        self.assertEqual(get.call_count, 3)  # _auth + /flows + body
        for call in get.call_args_list:
            self.assertEqual(call.kwargs.get("proxies"), mitm._NO_PROXY)

    def test_env_proxy_cannot_reach_localhost(self):
        """The mechanism, pinned: with HTTP/HTTPS/ALL proxies in the
        environment and NO_PROXY unset, the explicit None proxies
        still win — requests' select_proxy (the actual routing
        decision) finds no proxy for the localhost URL. The control
        proves the test would catch a regression: with no explicit
        proxies, the env proxy is selected."""
        import requests

        # Both cases: urllib's getproxies scans the environment and
        # the sandbox sets lowercase proxy vars too — whichever case
        # comes later in environ wins, so set both to the same value.
        env = {"HTTP_PROXY": "http://proxy.example.com:8080",
               "http_proxy": "http://proxy.example.com:8080",
               "HTTPS_PROXY": "http://proxy.example.com:8080",
               "https_proxy": "http://proxy.example.com:8080",
               "ALL_PROXY": "http://proxy.example.com:8080",
               "all_proxy": "http://proxy.example.com:8080"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("NO_PROXY", None)
            os.environ.pop("no_proxy", None)
            session = requests.Session()
            url = "http://127.0.0.1:8081/flows"
            merged = session.merge_environment_settings(
                url, dict(mitm._NO_PROXY), True, True, None)
            self.assertIsNone(requests.utils.select_proxy(
                url, merged["proxies"]))
            unguarded = session.merge_environment_settings(
                url, {}, True, True, None)
            self.assertEqual(requests.utils.select_proxy(
                url, unguarded["proxies"]),
                "http://proxy.example.com:8080")


if __name__ == "__main__":
    unittest.main()
