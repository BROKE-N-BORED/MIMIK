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

    def test_app_explicit_args_skip_mitm(self):
        class ExampleApp(App):
            HOST = "api.example.com"

        app = ExampleApp(base_url="https://x.example.com",
                         headers={"Authorization": "Bearer z"})
        self.assertEqual(app.base_url, "https://x.example.com")


if __name__ == "__main__":
    unittest.main()
