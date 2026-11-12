"""Tests for pattern presets — fingerprints that follow a shape across apps.

A pattern preset is learned from one app's captures and applies to any
other app that shows the same fingerprint (OAuth2 flow, JWT bearer
auth, cursor pagination, CDN asset shapes, ...). Matching is
conservative: at least two independent signals, or one unambiguous
one — and the negative tests below pin that similar-but-different
traffic does NOT match.
"""
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # for test_assets helpers

from test_assets import read_t9, saved_flow  # noqa: E402
from test_presets import _NoMitm, run_cmd  # noqa: E402

from mimik import assets, cli, codegen, fingerprints, presets, store  # noqa: E402
from mimik import reblend_bridge  # noqa: E402
from mimik.sources import mitm as mitm_mod  # noqa: E402

# A JWT-shaped token (the public jwt.io example — not a real secret).
JWT = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
       "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
OPAQUE = "opaque-session-token-abcdef"
API_KEY = "sk-live-pattern-987654321"
COOKIE_VALUE = "pattern-cookie-secret-xyz"
SIGNATURE = "sig-hmac-0123456789abcdef"

APP_A = "app-a.example.com"
APP_B = "app-b.example.com"


def flow(fid, host, path, method="GET", status=200, req_headers=None,
         resp_headers=None, req_body=None, resp_body=None):
    """A saved-flow dict. Bodies are embedded the way the store saves
    them, so fingerprinting reads them exactly as it would a capture."""
    f = {
        "id": fid,
        "request": {"host": host, "method": method, "path": path,
                    "scheme": "https", "port": 443,
                    "headers": req_headers or []},
        "response": {"status_code": status,
                     "headers": resp_headers or []},
    }
    if req_body is not None:
        f["_saved_request_body"] = req_body
    if resp_body is not None:
        f["_saved_response_body"] = resp_body
    return f


def jwt_flow(fid, host, path):
    return flow(fid, host, path,
                req_headers=[["Authorization", f"Bearer {JWT}"]])


def ids(matches):
    return [m["id"] for m in matches]


class FingerprintAuthTests(unittest.TestCase):
    def test_oauth2_code_from_grant_type(self):
        flows = [flow("t1", APP_A, "/oauth/token", method="POST",
                      req_body="grant_type=authorization_code"
                               "&code=abc123&redirect_uri=x")]
        self.assertIn("oauth2-code", ids(fingerprints.fingerprint_flows(flows)))

    def test_oauth2_code_from_authorize_plus_token(self):
        flows = [
            flow("a1", APP_A,
                 "/oauth/authorize?response_type=code&client_id=x"),
            flow("t1", APP_A, "/oauth/token", method="POST",
                 req_body="code=abc123"),
        ]
        self.assertIn("oauth2-code", ids(fingerprints.fingerprint_flows(flows)))

    def test_oauth2_code_negative_authorize_alone(self):
        flows = [flow("a1", APP_A,
                      "/oauth/authorize?response_type=code&client_id=x")]
        self.assertNotIn("oauth2-code",
                         ids(fingerprints.fingerprint_flows(flows)))

    def test_oauth2_client_credentials(self):
        flows = [flow("t1", APP_A, "/oauth/token", method="POST",
                      req_body="grant_type=client_credentials")]
        found = ids(fingerprints.fingerprint_flows(flows))
        self.assertIn("oauth2-client-credentials", found)
        self.assertNotIn("oauth2-code", found)

    def test_oauth2_negative_password_grant(self):
        flows = [flow("t1", APP_A, "/oauth/token", method="POST",
                      req_body="grant_type=password&username=u"
                               "&password=p")]
        found = ids(fingerprints.fingerprint_flows(flows))
        self.assertNotIn("oauth2-code", found)
        self.assertNotIn("oauth2-client-credentials", found)

    def test_jwt_bearer_two_endpoints(self):
        flows = [jwt_flow("f1", APP_A, "/api/users/1"),
                 jwt_flow("f2", APP_A, "/api/posts/2")]
        self.assertIn("jwt-bearer", ids(fingerprints.fingerprint_flows(flows)))

    def test_jwt_bearer_one_endpoint_plus_token_response(self):
        flows = [
            jwt_flow("f1", APP_A, "/api/users/1"),
            flow("t1", APP_A, "/oauth/token", method="POST",
                 resp_body=json.dumps({"access_token": JWT,
                                       "token_type": "Bearer"})),
        ]
        self.assertIn("jwt-bearer", ids(fingerprints.fingerprint_flows(flows)))

    def test_jwt_bearer_negative_opaque_token(self):
        flows = [flow("f1", APP_A, "/api/users/1",
                      req_headers=[["Authorization", f"Bearer {OPAQUE}"]]),
                 flow("f2", APP_A, "/api/posts/2",
                      req_headers=[["Authorization", f"Bearer {OPAQUE}"]]),
                 flow("f3", APP_A, "/api/other/3",
                      req_headers=[["Authorization", f"Bearer {OPAQUE}"]])]
        self.assertNotIn("jwt-bearer",
                         ids(fingerprints.fingerprint_flows(flows)))

    def test_jwt_bearer_negative_single_endpoint(self):
        flows = [jwt_flow("f1", APP_A, "/api/users/1")]
        self.assertNotIn("jwt-bearer",
                         ids(fingerprints.fingerprint_flows(flows)))

    def test_basic_auth_two_endpoints(self):
        basic = "Basic dXNlcjpwYXNz"  # base64 "user:pass"
        flows = [flow("f1", APP_A, "/api/a",
                      req_headers=[["Authorization", basic]]),
                 flow("f2", APP_A, "/api/b",
                      req_headers=[["Authorization", basic]])]
        self.assertIn("basic-auth", ids(fingerprints.fingerprint_flows(flows)))

    def test_basic_auth_one_endpoint_plus_401(self):
        basic = "Basic dXNlcjpwYXNz"
        flows = [flow("f1", APP_A, "/api/a",
                      req_headers=[["Authorization", basic]]),
                 flow("f2", APP_A, "/api/b", status=401,
                      resp_headers=[["WWW-Authenticate",
                                     'Basic realm="api"']])]
        self.assertIn("basic-auth", ids(fingerprints.fingerprint_flows(flows)))

    def test_basic_auth_negative_single_endpoint(self):
        flows = [flow("f1", APP_A, "/api/a",
                      req_headers=[["Authorization",
                                    "Basic dXNlcjpwYXNz"]])]
        self.assertNotIn("basic-auth",
                         ids(fingerprints.fingerprint_flows(flows)))

    def test_api_key_header_two_endpoints(self):
        flows = [flow("f1", APP_A, "/api/a",
                      req_headers=[["X-Api-Key", API_KEY]]),
                 flow("f2", APP_A, "/api/b",
                      req_headers=[["X-Api-Key", API_KEY]])]
        self.assertIn("api-key-header",
                      ids(fingerprints.fingerprint_flows(flows)))

    def test_api_key_header_negative_lone_header(self):
        flows = [flow("f1", APP_A, "/api/a",
                      req_headers=[["X-Api-Key", API_KEY]]),
                 flow("f2", APP_A, "/api/b")]
        self.assertNotIn("api-key-header",
                         ids(fingerprints.fingerprint_flows(flows)))

    def test_session_cookie(self):
        flows = [
            flow("f1", APP_A, "/login", method="POST",
                 resp_headers=[["Set-Cookie",
                                f"session_id={COOKIE_VALUE}; Path=/; "
                                "HttpOnly"]]),
            flow("f2", APP_A, "/api/me",
                 req_headers=[["Cookie",
                               f"session_id={COOKIE_VALUE}"]]),
        ]
        self.assertIn("session-cookie",
                      ids(fingerprints.fingerprint_flows(flows)))

    def test_session_cookie_negative_never_sent_back(self):
        flows = [flow("f1", APP_A, "/login", method="POST",
                      resp_headers=[["Set-Cookie",
                                     f"session_id={COOKIE_VALUE}; Path=/; "
                                     "HttpOnly"]]),
                 flow("f2", APP_A, "/api/me")]
        self.assertNotIn("session-cookie",
                         ids(fingerprints.fingerprint_flows(flows)))

    def test_session_cookie_negative_no_auth_attribute(self):
        flows = [
            flow("f1", APP_A, "/login", method="POST",
                 resp_headers=[["Set-Cookie",
                                "theme=dark; Path=/"]]),
            flow("f2", APP_A, "/api/me",
                 req_headers=[["Cookie", "theme=dark"]]),
        ]
        self.assertNotIn("session-cookie",
                         ids(fingerprints.fingerprint_flows(flows)))


class FingerprintShapeTests(unittest.TestCase):
    def test_json_rpc(self):
        flows = [flow("f1", APP_A, "/rpc", method="POST",
                      req_body=json.dumps({"jsonrpc": "2.0",
                                           "method": "getUser",
                                           "id": 1}))]
        self.assertIn("json-rpc", ids(fingerprints.fingerprint_flows(flows)))

    def test_json_rpc_negative_method_only(self):
        flows = [flow("f1", APP_A, "/rpc", method="POST",
                      req_body=json.dumps({"method": "getUser"}))]
        self.assertNotIn("json-rpc", ids(fingerprints.fingerprint_flows(flows)))

    def test_graphql_path(self):
        flows = [flow("f1", APP_A, "/graphql", method="POST",
                      req_body=json.dumps({"query": "{ users { id } }"}))]
        self.assertIn("graphql", ids(fingerprints.fingerprint_flows(flows)))

    def test_graphql_query_body(self):
        flows = [flow("f1", APP_A, "/api", method="POST",
                      req_body=json.dumps(
                          {"query": "query GetUser { user { id } }"}))]
        self.assertIn("graphql", ids(fingerprints.fingerprint_flows(flows)))

    def test_graphql_negative_plain_text_query(self):
        flows = [flow("f1", APP_A, "/search", method="POST",
                      req_body=json.dumps({"query": "red shoes"}))]
        self.assertNotIn("graphql", ids(fingerprints.fingerprint_flows(flows)))

    def test_cursor_pagination(self):
        flows = [flow("f1", APP_A, "/api/items?cursor=aaa"),
                 flow("f2", APP_A, "/api/items?cursor=bbb"),
                 flow("f3", APP_A, "/api/items?cursor=ccc")]
        self.assertIn("cursor-pagination",
                      ids(fingerprints.fingerprint_flows(flows)))

    def test_cursor_pagination_negative_single_value(self):
        flows = [flow("f1", APP_A, "/api/items?cursor=aaa")]
        self.assertNotIn("cursor-pagination",
                         ids(fingerprints.fingerprint_flows(flows)))

    def test_cursor_pagination_negative_same_value_twice(self):
        flows = [flow("f1", APP_A, "/api/items?cursor=aaa"),
                 flow("f2", APP_A, "/api/items?cursor=aaa")]
        self.assertNotIn("cursor-pagination",
                         ids(fingerprints.fingerprint_flows(flows)))

    def test_offset_pagination(self):
        flows = [flow("f1", APP_A, "/api/items?offset=0&limit=20"),
                 flow("f2", APP_A, "/api/items?offset=20&limit=20")]
        self.assertIn("offset-pagination",
                      ids(fingerprints.fingerprint_flows(flows)))

    def test_offset_pagination_page_pair(self):
        flows = [flow("f1", APP_A, "/api/items?page=1&per_page=20"),
                 flow("f2", APP_A, "/api/items?page=2&per_page=20")]
        self.assertIn("offset-pagination",
                      ids(fingerprints.fingerprint_flows(flows)))

    def test_offset_pagination_negative_single_request(self):
        flows = [flow("f1", APP_A, "/api/items?offset=0&limit=20")]
        self.assertNotIn("offset-pagination",
                         ids(fingerprints.fingerprint_flows(flows)))


class FingerprintFrameworkTests(unittest.TestCase):
    def test_express_powered_by(self):
        flows = [flow("f1", APP_A, "/api/a",
                      resp_headers=[["X-Powered-By", "Express"]])]
        self.assertIn("express", ids(fingerprints.fingerprint_flows(flows)))

    def test_aspnet_powered_by(self):
        flows = [flow("f1", APP_A, "/api/a",
                      resp_headers=[["X-Powered-By", "ASP.NET"]])]
        self.assertIn("aspnet", ids(fingerprints.fingerprint_flows(flows)))

    def test_php_powered_by(self):
        flows = [flow("f1", APP_A, "/api/a",
                      resp_headers=[["X-Powered-By", "PHP/8.2"]])]
        self.assertIn("php", ids(fingerprints.fingerprint_flows(flows)))

    def test_framework_negative_unknown_powered_by(self):
        flows = [flow("f1", APP_A, "/api/a",
                      resp_headers=[["X-Powered-By", "SomethingElse"]])]
        found = ids(fingerprints.fingerprint_flows(flows))
        self.assertNotIn("express", found)
        self.assertNotIn("aspnet", found)
        self.assertNotIn("php", found)

    def test_stripe_api_by_host(self):
        flows = [flow("f1", "api.stripe.com", "/v1/charges")]
        self.assertIn("stripe-api", ids(fingerprints.fingerprint_flows(flows)))

    def test_stripe_api_by_header(self):
        flows = [flow("f1", APP_A, "/api/a",
                      resp_headers=[["Stripe-Version", "2024-06-20"]])]
        self.assertIn("stripe-api", ids(fingerprints.fingerprint_flows(flows)))

    def test_catalog_order(self):
        # jwt-bearer (auth) sorts before cursor-pagination (request)
        # regardless of the order the flows arrived in.
        flows = [flow("f1", APP_A, "/api/items?cursor=aaa"),
                 flow("f2", APP_A, "/api/items?cursor=bbb"),
                 jwt_flow("f3", APP_A, "/api/users/1"),
                 jwt_flow("f4", APP_A, "/api/posts/2")]
        found = ids(fingerprints.fingerprint_flows(flows))
        self.assertIn("jwt-bearer", found)
        self.assertIn("cursor-pagination", found)
        self.assertLess(found.index("jwt-bearer"),
                        found.index("cursor-pagination"))


class FingerprintAssetTests(unittest.TestCase):
    def _entry(self, url):
        return {"url": url, "kind": "model", "name": "x.glb"}

    def test_cdn_assets_two(self):
        entries = [self._entry("https://a.example.com/cdn/m/crate.glb"),
                   self._entry("https://a.example.com/cdn/m/house.glb")]
        found = ids(fingerprints.fingerprint_entries(entries))
        self.assertEqual(found, ["cdn-assets"])

    def test_cdn_assets_negative_lone_asset(self):
        entries = [self._entry("https://a.example.com/cdn/m/crate.glb")]
        self.assertEqual(fingerprints.fingerprint_entries(entries), [])

    def test_cdn_assets_negative_no_cdn_shape(self):
        entries = [self._entry("https://a.example.com/m/crate.glb"),
                   self._entry("https://a.example.com/m/house.glb")]
        self.assertEqual(fingerprints.fingerprint_entries(entries), [])

    def test_cdn_host_counts(self):
        entries = [self._entry("https://cdn.example.com/m/crate.glb"),
                   self._entry("https://cdn.example.com/m/house.glb")]
        found = ids(fingerprints.fingerprint_entries(entries))
        self.assertEqual(found, ["cdn-assets"])

    def test_is_cdn_pattern(self):
        self.assertTrue(fingerprints.is_cdn_pattern("/cdn/m/*.glb"))
        self.assertTrue(fingerprints.is_cdn_pattern("/assets/*.png"))
        self.assertFalse(fingerprints.is_cdn_pattern("/m/*.glb"))

    def test_is_key_header(self):
        self.assertTrue(fingerprints.is_key_header("X-Api-Key"))
        self.assertTrue(fingerprints.is_key_header("X-Custom-Key"))
        self.assertFalse(fingerprints.is_key_header("Authorization"))
        self.assertFalse(fingerprints.is_key_header("Content-Type"))


class CodeSignalTests(unittest.TestCase):
    def test_code_signal_for_name(self):
        self.assertEqual(
            fingerprints.code_signal_for_name("unityplayer.dll"),
            "unity-engine")
        self.assertEqual(
            fingerprints.code_signal_for_name("libgodot.so"),
            "godot-engine")
        self.assertEqual(
            fingerprints.code_signal_for_name("app.asar"),
            "electron-app")
        self.assertEqual(
            fingerprints.code_signal_for_name("libcef.so"), "cef-app")
        self.assertIsNone(fingerprints.code_signal_for_name("random.dll"))
        self.assertIsNone(fingerprints.code_signal_for_name("main.py"))

    def test_code_signals_for_names(self):
        found = fingerprints.code_signals_for_names(
            ["unityplayer.dll", "libunity.so", "main.py"])
        self.assertEqual(found, {"unity-engine": ["unityplayer.dll",
                                                  "libunity.so"]})

    def test_code_signal_never_creates_fingerprint_alone(self):
        tmp = tempfile.mkdtemp()
        try:
            os.environ["MIMIK_DATA_DIR"] = tmp
            program = os.path.join(tmp, "game")
            os.makedirs(program)
            for name in ("unityplayer.dll", "game.exe"):
                with open(os.path.join(program, name), "wb") as fh:
                    fh.write(b"fake")
            # A real asset file, so the scan produces a group at all.
            with open(os.path.join(program, "logo.png"), "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            gdir, manifest = assets.scan_program_tree(
                program, "TestGame", base=tmp)
            self.assertIsNotNone(manifest)
            self.assertIn("unity-engine",
                          manifest.get("code_signals", {}))
            found = fingerprints.fingerprint_scope("TestGame", base=tmp)
            self.assertEqual(found["fingerprints"], [])
            self.assertIn("unity-engine", found["code_signals"])
        finally:
            del os.environ["MIMIK_DATA_DIR"]


def oauth_app_flows(host):
    """App A: an OAuth2 authorization-code flow with JWT bearers —
    the end-to-end fixture (learn here, apply on a different host)."""
    return [
        flow("a1", host, "/oauth/authorize?response_type=code"
                         "&client_id=abc"),
        flow("t1", host, "/oauth/token", method="POST",
             req_body="grant_type=authorization_code&code=xyz",
             resp_body=json.dumps({"access_token": JWT,
                                   "token_type": "Bearer"})),
        jwt_flow("f1", host, "/api/users/123"),
        jwt_flow("f2", host, "/api/users/456"),
        jwt_flow("f3", host, "/api/posts/789"),
    ]


class _TmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]


class LearnPatternTests(_TmpBase):
    def test_learn_creates_pattern_presets_with_provenance(self):
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        learned = presets.learn_patterns(APP_A, base=self.tmp)
        names = [p["name"] for p, _c in learned]
        self.assertIn("oauth2-code", names)
        self.assertIn("jwt-bearer", names)
        jwt = presets.load_pattern_preset("jwt-bearer", base=self.tmp)
        self.assertIsNotNone(jwt)
        self.assertEqual(jwt["learned_from"], [APP_A])
        self.assertEqual(jwt["fingerprint"]["id"], "jwt-bearer")
        self.assertTrue(jwt["fingerprint"]["signals"])
        self.assertEqual(jwt["auth"]["headers"], ["Authorization"])
        self.assertEqual(jwt["auth"]["schemes"], ["Bearer"])
        # The pattern preset lives under presets/patterns/.
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "presets", "patterns",
                         "jwt-bearer.json")))

    def test_learned_from_grows_across_two_apps(self):
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        store.save_flows(oauth_app_flows(APP_B), base=self.tmp)
        presets.learn_patterns(APP_B, base=self.tmp)
        jwt = presets.load_pattern_preset("jwt-bearer", base=self.tmp)
        self.assertEqual(sorted(jwt["learned_from"]),
                         sorted([APP_A, APP_B]))

    def test_relearn_merges_and_keeps_old_endpoints(self):
        # Two DISTINCT generalized endpoints per learn — two ids on
        # the same path generalize to one endpoint, which alone does
        # not fingerprint jwt-bearer.
        store.save_flows([jwt_flow("f1", APP_A, "/api/users/123"),
                          jwt_flow("f2", APP_A, "/api/posts/456")],
                         base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        before = presets.load_pattern_preset("jwt-bearer", base=self.tmp)
        self.assertIsNotNone(before)
        self.assertTrue(any(e["path"] == "/api/users/{id}"
                            for e in before["endpoints"]))
        # A second capture with new endpoints — re-learning adds them
        # and keeps the old ones.
        store.save_flows([jwt_flow("f3", APP_A, "/api/widgets/1"),
                          jwt_flow("f4", APP_A, "/api/gadgets/2")],
                         base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        after = presets.load_pattern_preset("jwt-bearer", base=self.tmp)
        paths = {e["path"] for e in after["endpoints"]}
        self.assertIn("/api/users/{id}", paths)
        self.assertIn("/api/widgets/{id}", paths)

    def test_pattern_preset_holds_no_secrets(self):
        signed = flow("s1", APP_A, "/api/signed",
                      req_headers=[["X-Signature", SIGNATURE],
                                   ["X-Api-Key", API_KEY],
                                   ["Authorization", f"Bearer {JWT}"]])
        flows = [signed,
                 flow("s2", APP_A, "/api/other",
                      req_headers=[["X-Api-Key", API_KEY],
                                   ["Authorization", f"Bearer {JWT}"]])]
        store.save_flows(flows, base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        for fid, name in (("jwt-bearer", b"Authorization"),
                          ("api-key-header", b"X-Api-Key")):
            path = os.path.join(self.tmp, "presets", "patterns",
                                f"{fid}.json")
            with open(path, "rb") as fh:
                raw = fh.read()
            for secret in (JWT, API_KEY, SIGNATURE):
                self.assertNotIn(secret.encode(), raw,
                                 f"{fid} preset leaked a secret value")
            # Names and shapes are fine.
            self.assertIn(name, raw)

    def test_learn_cli_reports_patterns(self):
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        out, code = run_cmd(cli.cmd_presets_learn,
                            argparse.Namespace(name=APP_A))
        self.assertEqual(code, 0)
        self.assertIn(f"Learned a preset for {APP_A}", out)
        self.assertIn("Learned/updated", out)
        self.assertIn("jwt-bearer", out)
        self.assertIn("oauth2-code", out)

    def test_describe_pattern_preset(self):
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        preset = presets.load_pattern_preset("jwt-bearer", base=self.tmp)
        text = "\n".join(presets.describe(preset))
        self.assertIn("Pattern preset", text)
        self.assertIn(APP_A, text)
        self.assertIn("What defines this pattern:", text)
        self.assertNotIn(JWT, text)


class MatchTests(_TmpBase):
    def test_match_names_patterns_and_signals(self):
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        # App B: same shapes, no app preset, no learning yet.
        store.save_flows(oauth_app_flows(APP_B), base=self.tmp)
        matches, _code = presets.match(APP_B, base=self.tmp)
        matched = {m["id"]: p for m, p, _c in matches}
        self.assertIn("jwt-bearer", matched)
        self.assertIn("oauth2-code", matched)
        # The saved pattern presets are the ones that would apply.
        self.assertIsNotNone(matched["jwt-bearer"])
        self.assertTrue(matched["jwt-bearer"]["fingerprint"]["signals"])

    def test_match_dry_run_changes_nothing(self):
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        store.save_flows(oauth_app_flows(APP_B), base=self.tmp)
        before = sorted(os.listdir(
            os.path.join(self.tmp, "presets", "patterns")))
        out, code = run_cmd(cli.cmd_presets_match,
                            argparse.Namespace(name=APP_B))
        self.assertEqual(code, 0)
        self.assertIn("jwt-bearer", out)
        self.assertIn("oauth2-code", out)
        self.assertIn("signal:", out)
        after = sorted(os.listdir(
            os.path.join(self.tmp, "presets", "patterns")))
        self.assertEqual(before, after)
        self.assertIsNone(presets.load_preset(APP_B, base=self.tmp))

    def test_match_no_patterns(self):
        store.save_flows([flow("f1", APP_A, "/index.html")],
                         base=self.tmp)
        out, code = run_cmd(cli.cmd_presets_match,
                            argparse.Namespace(name=APP_A))
        self.assertEqual(code, 0)
        self.assertIn("no patterns matched", out)

    def test_matched_pattern_ids(self):
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        store.save_flows(oauth_app_flows(APP_B), base=self.tmp)
        found = presets.matched_pattern_ids(APP_B, base=self.tmp)
        self.assertIn("jwt-bearer", found)
        self.assertIn("oauth2-code", found)


class ApplyTests(_TmpBase):
    # Endpoints for the generator (fixed, like CodegenPresetTests) —
    # the flows in the store only drive fingerprinting/resolution.
    EPS = [
        {"method": "GET", "path": "/api/users/123", "status": 200,
         "query": "", "request_body": "", "response_body": "{}"},
        {"method": "GET", "path": "/api/posts/456", "status": 200,
         "query": "", "request_body": "", "response_body": "{}"},
    ]

    def test_gen_for_new_app_uses_pattern_auth(self):
        # Learn the pattern from App A; generate for App B, which has
        # the same shapes but no app preset of its own.
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        store.save_flows(oauth_app_flows(APP_B), base=self.tmp)
        preset, applied, _pids, label = presets.resolve_for_gen(
            APP_B, base=self.tmp)
        self.assertIsNotNone(preset)
        self.assertIn("pattern jwt-bearer", applied)
        src = codegen.generate(APP_B, self.EPS,
                               preset=preset, preset_label=label,
                               applied=applied)
        self.assertIn(
            "Presets applied: pattern oauth2-code; pattern jwt-bearer",
            src)
        self.assertIn("header: Authorization (Bearer)", src)
        self.assertNotIn(JWT, src)

    def test_app_preset_wins_per_section(self):
        # App B has its own preset with Basic auth, learned BEFORE
        # any JWT traffic appeared; the jwt-bearer pattern must NOT
        # override it — and the pattern supplies nothing else, since
        # the app preset already has endpoints.
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        basic = "Basic dXNlcjpwYXNz"
        store.save_flows(
            [flow("f1", APP_B, "/api/a",
                  req_headers=[["Authorization", basic]]),
             flow("f2", APP_B, "/api/b",
                  req_headers=[["Authorization", basic]])],
            base=self.tmp)
        presets.learn(APP_B, base=self.tmp)
        # JWT traffic shows up later — the pattern matches, but the
        # app preset's own auth (Basic) wins.
        store.save_flows([jwt_flow("f3", APP_B, "/api/users/1"),
                          jwt_flow("f4", APP_B, "/api/posts/2")],
                         base=self.tmp)
        preset, applied, _pids, label = presets.resolve_for_gen(
            APP_B, base=self.tmp)
        self.assertEqual(preset["auth"]["schemes"], ["Basic"])
        self.assertEqual(applied, [f"app preset for {APP_B}"])
        src = codegen.generate(APP_B, self.EPS,
                               preset=preset, preset_label=label,
                               applied=applied)
        self.assertIn("header: Authorization (Basic)", src)
        self.assertNotIn("(Bearer)", src)

    def test_pattern_fills_missing_auth_section(self):
        # App B's preset has endpoints but no auth (plain flows);
        # the jwt-bearer pattern supplies the auth section.
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        store.save_flows([flow("f1", APP_B, "/api/plain/1"),
                          flow("f2", APP_B, "/api/plain/2")],
                         base=self.tmp)
        presets.learn(APP_B, base=self.tmp)
        app_preset = presets.load_preset(APP_B, base=self.tmp)
        self.assertEqual(app_preset["auth"]["headers"], [])
        # Now B shows JWT bearers too — the pattern fills the gap.
        store.save_flows([jwt_flow("f3", APP_B, "/api/users/1"),
                          jwt_flow("f4", APP_B, "/api/posts/2")],
                         base=self.tmp)
        preset, applied, _pids, label = presets.resolve_for_gen(
            APP_B, base=self.tmp)
        self.assertEqual(preset["auth"]["schemes"], ["Bearer"])
        self.assertIn("pattern jwt-bearer", applied)
        src = codegen.generate(APP_B, self.EPS,
                               preset=preset, preset_label=label,
                               applied=applied)
        self.assertIn("header: Authorization (Bearer)", src)
        self.assertIn("pattern jwt-bearer", src)

    def test_no_match_gen_is_byte_identical(self):
        # Gen output for an app with no preset and no matching pattern
        # is exactly the pre-pattern output — learning patterns from
        # another app must not change it.
        store.save_flows([flow("f1", APP_B, "/index.html"),
                          flow("f2", APP_B, "/about.html")],
                         base=self.tmp)
        before = codegen.generate_deterministic(APP_B, self.EPS)
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        preset, applied, _pids, _label = presets.resolve_for_gen(
            APP_B, base=self.tmp)
        self.assertIsNone(preset)
        self.assertEqual(applied, [])
        after = codegen.generate(APP_B, self.EPS,
                                 preset=preset, applied=applied)
        self.assertEqual(after, before)

    def test_resolve_for_gen_no_flows(self):
        preset, applied, pids, label = presets.resolve_for_gen(
            "nobody.example.com", base=self.tmp)
        self.assertIsNone(preset)
        self.assertEqual(applied, [])
        self.assertEqual(pids, [])
        self.assertIsNone(label)


class PatternCliTests(_TmpBase):
    def test_list_shows_both_sections(self):
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn(APP_A, base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        out, code = run_cmd(cli.cmd_presets,
                            argparse.Namespace(auto_learn=None))
        self.assertEqual(code, 0)
        self.assertIn("App presets:", out)
        self.assertIn(f"{APP_A} (host)", out)
        self.assertIn("Pattern presets:", out)
        self.assertIn("jwt-bearer", out)
        self.assertIn("learned from 1 app", out)

    def test_show_pattern_includes_learned_from(self):
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        out, code = run_cmd(cli.cmd_presets_show,
                            argparse.Namespace(name="jwt-bearer"))
        self.assertEqual(code, 0)
        self.assertIn("Pattern preset", out)
        self.assertIn(APP_A, out)
        self.assertIn("What defines this pattern:", out)

    def test_delete_pattern(self):
        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        self.assertIsNotNone(
            presets.load_pattern_preset("jwt-bearer", base=self.tmp))
        out, code = run_cmd(cli.cmd_presets_delete,
                            argparse.Namespace(name="jwt-bearer",
                                               yes=True))
        self.assertEqual(code, 0)
        self.assertIn("Deleted the pattern preset", out)
        self.assertIsNone(
            presets.load_pattern_preset("jwt-bearer", base=self.tmp))

    def test_gen_cli_applies_pattern(self):
        from unittest import mock

        store.save_flows(oauth_app_flows(APP_A), base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        store.save_flows(oauth_app_flows(APP_B), base=self.tmp)
        out_path = os.path.join(self.tmp, "client_b.py")
        args = argparse.Namespace(host=APP_B, har=None, curl=None,
                                  out=out_path, model=None,
                                  polish="none", prompt_only=False)
        with mock.patch.object(mitm_mod, "Mitm", _NoMitm):
            out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        with open(out_path) as fh:
            src = fh.read()
        self.assertIn(
            "Presets applied: pattern oauth2-code; pattern jwt-bearer",
            src)
        self.assertIn("header: Authorization (Bearer)", src)


def cdn_asset_flows(host):
    """Two real model assets served from CDN-shaped paths."""
    return [
        saved_flow("c1", host, "/cdn/m/crate.glb",
                   "model/gltf-binary", read_t9("examples/sample.glb")),
        saved_flow("c2", host, "/cdn/m/house.obj",
                   "text/plain", read_t9("examples/house.obj")),
    ]


class PatternPackTests(_TmpBase):
    def _seed_cdn_app(self, host):
        flows = cdn_asset_flows(host)
        store.save_flows(flows, base=self.tmp)
        assets.extract_from_flows(flows, host, host=host, base=self.tmp)

    def test_pattern_scale_used_when_no_app_preset(self):
        # App A teaches the cdn-assets pattern its scale; App B, a
        # different host with the same CDN shape and no app preset,
        # gets that scale for its pack.
        self._seed_cdn_app(APP_A)
        presets.learn(APP_A, base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        reblend_bridge.build_pack(host=APP_A, reblend=None,
                                  scale=250.0, base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        cdn = presets.load_pattern_preset("cdn-assets", base=self.tmp)
        self.assertEqual(cdn["pack"]["scale"], 250.0)

        self._seed_cdn_app(APP_B)
        scale, app_applied, pattern_ids = presets.resolve_pack_scale(
            host=APP_B, base=self.tmp)
        self.assertEqual(scale, 250.0)
        self.assertFalse(app_applied)
        self.assertEqual(pattern_ids, ["cdn-assets"])

        result = reblend_bridge.build_pack(host=APP_B, reblend=None,
                                           base=self.tmp)
        self.assertEqual(result["scale"], 250.0)
        self.assertFalse(result["preset_applied"])
        self.assertEqual(result["pattern_presets_applied"],
                         ["cdn-assets"])
        with open(os.path.join(result["pack_dir"], "pack.json")) as fh:
            pack = json.load(fh)
        self.assertEqual(pack["scale"], 250.0)
        self.assertEqual(pack["pattern_presets_applied"],
                         ["cdn-assets"])

    def test_app_preset_scale_wins_over_pattern(self):
        self._seed_cdn_app(APP_A)
        presets.learn(APP_A, base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)
        reblend_bridge.build_pack(host=APP_A, reblend=None,
                                  scale=250.0, base=self.tmp)
        presets.learn_patterns(APP_A, base=self.tmp)

        self._seed_cdn_app(APP_B)
        presets.learn(APP_B, base=self.tmp)
        reblend_bridge.build_pack(host=APP_B, reblend=None,
                                  scale=50.0, base=self.tmp)
        scale, app_applied, pattern_ids = presets.resolve_pack_scale(
            host=APP_B, base=self.tmp)
        self.assertEqual(scale, 50.0)
        self.assertTrue(app_applied)
        self.assertEqual(pattern_ids, [])
        result = reblend_bridge.build_pack(host=APP_B, reblend=None,
                                           base=self.tmp)
        self.assertEqual(result["scale"], 50.0)
        self.assertTrue(result["preset_applied"])
        self.assertEqual(result["pattern_presets_applied"], [])

    def test_no_pattern_means_default_scale(self):
        self._seed_cdn_app(APP_B)
        scale, app_applied, pattern_ids = presets.resolve_pack_scale(
            host=APP_B, base=self.tmp)
        self.assertIsNone(scale)
        self.assertFalse(app_applied)
        self.assertEqual(pattern_ids, [])


try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class PatternGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = cls.tmp
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)
        store.save_flows(oauth_app_flows(APP_A), base=cls.tmp)
        presets.learn(APP_A, base=cls.tmp)
        presets.learn_patterns(APP_A, base=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        del os.environ["MIMIK_DATA_DIR"]

    def test_presets_tab_lists_pattern_preset(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertGreaterEqual(win.pattern_presets_list.count(), 1)
        texts = [win.pattern_presets_list.item(i).text()
                 for i in range(win.pattern_presets_list.count())]
        jwt_rows = [t for t in texts if "JWT bearer" in t]
        self.assertTrue(jwt_rows, texts)
        self.assertIn("learned from 1 app", jwt_rows[0])
        row = texts.index(jwt_rows[0])
        win.pattern_presets_list.setCurrentRow(row)
        self.app.processEvents()
        detail = win.preset_detail.toPlainText()
        self.assertIn("Pattern preset", detail)
        self.assertIn(APP_A, detail)
        self.assertIn("What defines this pattern:", detail)
        self.assertNotIn(JWT, detail)
        win.close()


if __name__ == "__main__":
    unittest.main()
