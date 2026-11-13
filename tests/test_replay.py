"""v0.4 item 10: replay one captured flow, verified vs a local stub."""

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mimik import replay


class _Stub(BaseHTTPRequestHandler):
    """Records what it receives; answers from the class-level script."""

    seen = []
    body = b'{"ok": true}'
    status = 200

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        _Stub.seen.append({
            "method": self.command, "path": self.path,
            "headers": dict(self.headers), "body": data,
        })
        payload = _Stub.body
        self.send_response(_Stub.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle

    def log_message(self, *args):
        pass


def _flow(port, method="GET", path="/api/hello", headers=None, **extra):
    flow = {
        "id": "r1",
        "request": {"host": "127.0.0.1", "method": method, "path": path,
                    "scheme": "http", "port": port,
                    "headers": headers or []},
        "response": {"status_code": 200, "headers": []},
    }
    flow.update(extra)
    return flow


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Stub.seen = []
        _Stub.body = b'{"ok": true}'
        _Stub.status = 200

    def test_get_replay_returns_status_and_body(self):
        result = replay.replay_flow(_flow(self.port))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["reason"], "OK")
        self.assertEqual(json.loads(result["body"]), {"ok": True})
        self.assertIsNone(result["error"])
        self.assertEqual(len(_Stub.seen), 1)
        self.assertEqual(_Stub.seen[0]["method"], "GET")
        self.assertEqual(_Stub.seen[0]["path"], "/api/hello")

    def test_post_replays_the_captured_body(self):
        result = replay.replay_flow(
            _flow(self.port, method="POST", path="/api/send",
                  _saved_request_body="hello=1"))
        self.assertTrue(result["ok"])
        self.assertEqual(_Stub.seen[0]["method"], "POST")
        self.assertEqual(_Stub.seen[0]["body"], b"hello=1")

    def test_binary_body_replays_exact_bytes(self):
        raw = b"\x00\x01\x02binary"
        result = replay.replay_flow(
            _flow(self.port, method="POST",
                  _saved_request_body=base64.b64encode(raw).decode(),
                  _saved_request_body_b64=True))
        self.assertTrue(result["ok"])
        self.assertEqual(_Stub.seen[0]["body"], raw)

    def test_body_override_wins_over_the_captured_body(self):
        result = replay.replay_flow(
            _flow(self.port, method="POST",
                  _saved_request_body="stale"),
            body="fresh")
        self.assertTrue(result["ok"])
        self.assertEqual(_Stub.seen[0]["body"], b"fresh")

    def test_auth_header_forwarded_noise_dropped(self):
        headers = [["Authorization", "Bearer fake-token"],
                   ["X-Device-Id", "fake-device"],
                   ["X-Request-Id", "fake-request-id"],
                   ["Proxy-Authorization", "Basic fake-proxy"],
                   ["Content-Length", "999"]]
        result = replay.replay_flow(_flow(self.port, headers=headers))
        self.assertTrue(result["ok"])
        seen = _Stub.seen[0]["headers"]
        self.assertEqual(seen.get("Authorization"), "Bearer fake-token")
        self.assertEqual(seen.get("X-Device-Id"), "fake-device")
        self.assertNotIn("X-Request-Id", seen)
        self.assertNotIn("Proxy-Authorization", seen)
        # requests sends no Content-Length for a bodyless GET — the
        # captured 999 must not leak through.
        self.assertNotEqual(seen.get("Content-Length"), "999")

    def test_server_down_is_a_result_not_an_exception(self):
        # Port 9 (discard) — nothing listens there.
        result = replay.replay_flow(_flow(9), timeout=5)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["status"])
        self.assertTrue(result["error"])

    def test_large_body_is_truncated_to_the_preview(self):
        _Stub.body = b"x" * 5000
        result = replay.replay_flow(_flow(self.port))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["body"]), replay.BODY_PREVIEW_CHARS)
        self.assertTrue(result["truncated"])

    def test_flow_url(self):
        base = {"request": {"host": "example.com", "path": "/a?b=c"}}
        self.assertEqual(replay.flow_url(base),
                         "https://example.com/a?b=c")
        base["request"]["scheme"] = "http"
        base["request"]["port"] = 80
        self.assertEqual(replay.flow_url(base),
                         "http://example.com/a?b=c")
        base["request"]["port"] = 8080
        self.assertEqual(replay.flow_url(base),
                         "http://example.com:8080/a?b=c")
        base["request"]["scheme"] = "https"
        base["request"]["port"] = 8443
        self.assertEqual(replay.flow_url(base),
                         "https://example.com:8443/a?b=c")


if __name__ == "__main__":
    unittest.main()
