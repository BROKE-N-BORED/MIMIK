"""Tests for mimik.session — verbs, 401 refresh, saved-capture fallback."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import App, Session, store


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body if body is not None else {"ok": True}

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body

    @property
    def text(self):
        return self._body if isinstance(self._body, str) else "{}"

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


def flow(fid, host, token):
    return {
        "id": fid,
        "request": {
            "host": host, "method": "GET", "path": "/v1/x",
            "scheme": "https", "port": 443,
            "headers": [["Authorization", f"Bearer {token}"]],
        },
        "response": {"status_code": 200},
    }


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def _session(self, responses):
        s = Session("https://api.example.com",
                    {"Authorization": "Bearer old"})
        s._http = mock.Mock()
        s._http.request.side_effect = responses
        return s

    def test_get_returns_parsed_json(self):
        s = self._session([FakeResponse(200, {"users": []})])
        self.assertEqual(s.get("/v1/users"), {"users": []})
        s._http.request.assert_called_once()
        args = s._http.request.call_args
        self.assertEqual(args[0][0], "GET")
        self.assertEqual(args[0][1], "https://api.example.com/v1/users")

    def test_post_sends_json(self):
        s = self._session([FakeResponse(201, {"id": 1})])
        s.post("/v1/like", {"a": 1})
        kwargs = s._http.request.call_args[1]
        self.assertEqual(kwargs["json"], {"a": 1})

    def test_put_patch_delete_head(self):
        s = self._session([FakeResponse(200)] * 4)
        s.put("/a", {"x": 1})
        s.patch("/b", {"x": 1})
        s.delete("/c")
        s.head("/d")
        methods = [c[0][0] for c in s._http.request.call_args_list]
        self.assertEqual(methods, ["PUT", "PATCH", "DELETE", "HEAD"])

    def test_non_json_body_returns_text(self):
        s = self._session([FakeResponse(200, "plain text")])
        self.assertEqual(s.get("/x"), "plain text")

    def test_error_raises(self):
        import requests

        s = self._session([FakeResponse(500)])
        with self.assertRaises(requests.HTTPError):
            s.get("/x")

    def test_401_refreshes_idempotent_once(self):
        s = self._session([FakeResponse(401), FakeResponse(200, {"ok": 1})])
        s.host = "api.example.com"
        fake_mitm = mock.Mock()
        fake_mitm.flows.return_value = [flow("1", "api.example.com", "newtok")]
        s._mitm = fake_mitm
        out = s.get("/v1/users")
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(s.headers["Authorization"], "Bearer newtok")
        self.assertEqual(s._http.request.call_count, 2)

    def test_401_no_refresh_when_headers_unchanged(self):
        import requests

        s = self._session([FakeResponse(401)])
        s.host = "api.example.com"
        fake_mitm = mock.Mock()
        fake_mitm.flows.return_value = [flow("1", "api.example.com", "old")]
        s._mitm = fake_mitm
        with self.assertRaises(requests.HTTPError):
            s.get("/v1/users")
        self.assertEqual(s._http.request.call_count, 1)

    def test_401_post_not_refreshed_by_default(self):
        import requests

        s = self._session([FakeResponse(401)])
        s.host = "api.example.com"
        s._mitm = mock.Mock()
        with self.assertRaises(requests.HTTPError):
            s.post("/v1/like", {"a": 1})
        self.assertEqual(s._http.request.call_count, 1)

    def test_401_post_refreshes_when_explicit(self):
        s = self._session([FakeResponse(401), FakeResponse(200, {"ok": 1})])
        s.host = "api.example.com"
        fake_mitm = mock.Mock()
        fake_mitm.flows.return_value = [flow("1", "api.example.com", "newtok")]
        s._mitm = fake_mitm
        out = s.post("/v1/like", {"a": 1}, refresh=True)
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(s._http.request.call_count, 2)

    def test_from_saved(self):
        store.save_flows([flow("1", "api.example.com", "savedtok")],
                         base=self.tmp)
        s = Session.from_saved("api.example.com")
        self.assertEqual(s.base_url, "https://api.example.com")
        self.assertEqual(s.headers["Authorization"], "Bearer savedtok")

    def test_from_saved_missing_raises(self):
        with self.assertRaises(RuntimeError):
            Session.from_saved("nope.example.com")

    def test_app_falls_back_to_saved_when_proxy_down(self):
        store.save_flows([flow("1", "api.example.com", "savedtok")],
                         base=self.tmp)

        class ExampleApp(App):
            HOST = "api.example.com"

        with mock.patch("mimik.session.Mitm") as mock_mitm_cls:
            from mimik.sources.mitm import MitmError

            mock_mitm_cls.return_value.flows.side_effect = MitmError("down")
            app = ExampleApp()
        self.assertEqual(app.headers["Authorization"], "Bearer savedtok")

    def test_app_prefers_live_proxy(self):
        store.save_flows([flow("1", "api.example.com", "savedtok")],
                         base=self.tmp)

        class ExampleApp(App):
            HOST = "api.example.com"

        fake_mitm = mock.Mock()
        fake_mitm.flows.return_value = [flow("9", "api.example.com", "livetok")]
        with mock.patch("mimik.session.Mitm", return_value=fake_mitm):
            app = ExampleApp()
        self.assertEqual(app.headers["Authorization"], "Bearer livetok")

    def test_app_construction_writes_no_capture_files(self):
        # Item 25 / review #23: constructing a client must not save
        # flows — the recorder already saves on every poll. Hinge()
        # with live mitmweb writes no new capture files.
        class ExampleApp(App):
            HOST = "api.example.com"

        fake_mitm = mock.Mock()
        fake_mitm.flows.return_value = [flow("1", "api.example.com",
                                             "livetok")]
        with mock.patch("mimik.session.Mitm",
                        return_value=fake_mitm), \
                mock.patch.object(store, "save_flows") as save:
            app = ExampleApp()
        self.assertEqual(app.headers["Authorization"], "Bearer livetok")
        save.assert_not_called()
        self.assertEqual(store.list_captures(), [])

    def test_app_explicit_args_skip_mitm(self):
        class ExampleApp(App):
            HOST = "api.example.com"

        app = ExampleApp(base_url="https://x.example.com",
                         headers={"Authorization": "Bearer z"})
        self.assertEqual(app.base_url, "https://x.example.com")


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def _session(self, responses, **kw):
        s = Session("https://api.example.com",
                    {"Authorization": "Bearer <redacted>"}, **kw)
        s._http = mock.Mock()
        s._http.request.side_effect = responses
        return s

    def test_default_timeout_is_30(self):
        s = self._session([FakeResponse(200)])
        self.assertEqual(s.timeout, 30)
        s.get("/v1/users")
        self.assertEqual(s._http.request.call_args[1]["timeout"], 30)

    def test_constructor_override(self):
        s = self._session([FakeResponse(200)], timeout=5)
        s.get("/v1/users")
        self.assertEqual(s._http.request.call_args[1]["timeout"], 5)

    def test_per_call_override_wins(self):
        s = self._session([FakeResponse(200)], timeout=5)
        s.get("/v1/users", timeout=120)
        self.assertEqual(s._http.request.call_args[1]["timeout"], 120)

    def test_explicit_none_disables(self):
        s = self._session([FakeResponse(200)])
        s.get("/v1/users", timeout=None)
        self.assertIsNone(s._http.request.call_args[1]["timeout"])

    def test_app_inherits_default_and_override(self):
        store.save_flows([flow("1", "api.example.com", "savedtok")],
                         base=self.tmp)

        class ExampleApp(App):
            HOST = "api.example.com"

        from mimik.sources.mitm import MitmError

        with mock.patch("mimik.session.Mitm") as mock_mitm_cls:
            mock_mitm_cls.return_value.flows.side_effect = MitmError("down")
            app = ExampleApp()
            self.assertEqual(app.timeout, 30)
            app2 = ExampleApp(timeout=9)
            self.assertEqual(app2.timeout, 9)

    def test_hung_server_does_not_hang_forever(self):
        import threading
        import time
        from http.server import (BaseHTTPRequestHandler,
                                 ThreadingHTTPServer)

        import requests

        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(5)  # hang longer than the timeout
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            s = Session(f"http://127.0.0.1:{port}", {}, timeout=0.2)
            start = time.monotonic()
            with self.assertRaises(requests.Timeout):
                s.get("/slow")
            self.assertLess(time.monotonic() - start, 3)
        finally:
            server.shutdown()
            server.server_close()

    def test_generated_client_carries_timeout(self):
        from mimik import codegen

        eps = [{"method": "GET", "path": "/v1/users", "status": 200,
                "query": "", "request_body": "",
                "response_body": '{"users": []}'}]
        src = codegen.generate_deterministic("api.example.com", eps)
        self.assertIn("time out after 30 s", src)
        namespace = {}
        exec(compile(src, "api_example_com_client.py", "exec"), namespace)
        cls = namespace["ExampleClient"]
        client = cls(base_url="https://api.example.com", headers={})
        self.assertEqual(client.timeout, 30)
        client._http = mock.Mock()
        client._http.request.return_value = FakeResponse(200, {"users": []})
        client.get_users()
        self.assertEqual(client._http.request.call_args[1]["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
