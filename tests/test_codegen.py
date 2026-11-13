"""Tests for mimik.codegen — the deterministic $0 generator."""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import codegen
from mimik.sources import har

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mimik.har")

EPS = [
    {"method": "GET", "path": "/rec/v2", "status": 200, "query": "",
     "request_body": "", "response_body": "{}"},
    {"method": "POST", "path": "/like", "status": 200, "query": "",
     "request_body": json.dumps({"subjectId": "550e8400-e29b-41d4-a716-446655440000",
                                 "comment": "hi lol"}),
     "response_body": '{"liked": true}'},
    {"method": "POST", "path": "/v1/messages", "status": 201, "query": "",
     "request_body": json.dumps({"recipientId": "user-9f2c4e77a1b8",
                                 "text": "hello there friend",
                                 "platform": "ios", "retry": 2}),
     "response_body": '{"id": "msg-001"}'},
    {"method": "GET", "path": "/v1/users/{user_id}", "status": 200, "query": "",
     "request_body": "", "response_body": '{"name": "Ada"}'},
    {"method": "DELETE", "path": "/v1/messages/{message_id}", "status": 204,
     "query": "", "request_body": "", "response_body": ""},
]


class NamingTests(unittest.TestCase):
    def test_class_name_skips_generic_labels(self):
        self.assertEqual(codegen.class_name_for_host("api.example.com"),
                         "ExampleClient")
        self.assertEqual(codegen.class_name_for_host("prod-api.hingeaws.net"),
                         "HingeawsClient")
        self.assertEqual(codegen.class_name_for_host("www.foo.io"), "FooClient")

    def test_method_get_rec(self):
        self.assertEqual(codegen.method_name_for("GET", "/rec/v2"), "get_recs")

    def test_method_post_like(self):
        self.assertEqual(codegen.method_name_for("POST", "/like"), "like")

    def test_method_get_users_by_id(self):
        self.assertEqual(codegen.method_name_for("GET", "/users/{user_id}"),
                         "get_users_by_user_id")

    def test_method_post_messages_creates(self):
        self.assertEqual(codegen.method_name_for("POST", "/v1/messages"),
                         "create_message")

    def test_method_delete(self):
        self.assertEqual(codegen.method_name_for("DELETE", "/v1/messages/123"),
                         "delete_message_by_message_id")

    def test_method_put(self):
        self.assertEqual(codegen.method_name_for("PUT", "/v1/users/123"),
                         "update_user_by_user_id")

    def test_method_numeric_id_in_path(self):
        self.assertEqual(codegen.method_name_for("GET", "/users/123"),
                         "get_users_by_user_id")

    def test_method_prefixed_id_in_path(self):
        self.assertEqual(
            codegen.method_name_for("GET", "/v1/users/user-9f2c4e77a1b8"),
            "get_users_by_user_id")

    def test_method_uuid_in_path(self):
        self.assertEqual(
            codegen.method_name_for(
                "GET", "/v1/posts/550e8400-e29b-41d4-a716-446655440000"),
            "get_posts_by_post_id")


class DeterministicGenTests(unittest.TestCase):
    def setUp(self):
        self.src = codegen.generate_deterministic("api.example.com", EPS)

    def test_output_compiles(self):
        compile(self.src, "<gen>", "exec")

    def test_class_name_and_host(self):
        self.assertIn("class ExampleClient(App):", self.src)
        self.assertIn('HOST = "api.example.com"', self.src)
        self.assertIn("from mimik import App", self.src)

    def test_methods_exist(self):
        for name in ("get_recs", "like", "create_message",
                     "get_users_by_user_id", "delete_message_by_message_id"):
            self.assertIn(f"def {name}(", self.src)

    def test_docstrings_present(self):
        self.assertIn('"""GET /rec/v2 -> 200."""', self.src)
        self.assertIn('"""POST /like -> 200."""', self.src)

    def test_variable_values_become_params(self):
        self.assertIn("def like(self, subject_id=None, comment=None):",
                      self.src)
        self.assertIn("def create_message(self, recipient_id=None, "
                      "text=None, platform=None):", self.src)

    def test_strings_become_params_not_literals(self):
        # Review #12: a short string like "ios" looks constant but is a
        # value from the user's own traffic — it becomes an optional
        # parameter, never a baked literal.
        self.assertNotIn("'platform': 'ios'", self.src)
        self.assertNotIn('"ios"', self.src)
        self.assertIn("platform=None", self.src)

    def test_safe_constants_still_baked(self):
        # The allow-list: None, bool, small int, empty string.
        self.assertIn("'retry': 2", self.src)
        eps = [{"method": "POST", "path": "/v1/prefs", "status": 200,
                "query": "",
                "request_body": json.dumps(
                    {"enabled": True, "note": "", "count": 3,
                     "nothing": None}),
                "response_body": "{}"}]
        src = codegen.generate_deterministic("api.example.com", eps)
        self.assertIn("'enabled': True", src)
        self.assertIn("'note': ''", src)
        self.assertIn("'count': 3", src)
        self.assertIn("'nothing': None", src)

    def test_short_personal_strings_not_baked(self):
        # The review's verify case: name/city/role must not appear
        # anywhere in the generated client — not as body literals, not
        # as parameter defaults.
        eps = [{"method": "POST", "path": "/v1/profile", "status": 200,
                "query": "",
                "request_body": json.dumps(
                    {"name": "Josh", "city": "Dover", "role": "admin"}),
                "response_body": "{}"}]
        src = codegen.generate_deterministic("api.example.com", eps)
        self.assertNotIn("Josh", src)
        self.assertNotIn("Dover", src)
        self.assertNotIn("admin", src)
        self.assertIn("def create_profile(self, name=None, city=None, "
                      "role=None):", src)

    def test_id_varying_flows_collapse_without_preset(self):
        # Review #2: 1,200 flows over one id-varying path used to make a
        # 1,200-method client without a preset. Generalization is the
        # default now — one method, no preset needed.
        eps = [{"method": "GET", "path": "/api/users/%d" % i,
                "status": 200, "query": "", "request_body": "",
                "response_body": "{}"} for i in range(1200)]
        src = codegen.generate_deterministic("api.example.com", eps)
        compile(src, "<gen>", "exec")
        self.assertEqual(src.count("def get_users_by_id("), 1)
        self.assertNotIn("get_users_by_id_2", src)
        self.assertLess(len(src), 20000)

    def test_preset_output_unchanged_by_default_generalization(self):
        # A preset's output is the same as before: generalization plus
        # the auth block. (The preset path already generalized.)
        preset = {"auth": {"headers": ["Authorization"],
                           "schemes": ["Bearer"], "cookies": [],
                           "token_endpoints": []},
                  "endpoints": []}
        eps = [{"method": "GET", "path": "/api/users/123",
                "status": 200, "query": "", "request_body": "",
                "response_body": "{}"},
               {"method": "GET", "path": "/api/users/456",
                "status": 200, "query": "", "request_body": "",
                "response_body": "{}"}]
        src = codegen.generate_deterministic("api.example.com", eps,
                                             preset=preset)
        self.assertEqual(src.count("def get_users_by_id("), 1)
        self.assertIn("AUTH_HEADERS = ('Authorization',)", src)
        self.assertIn("'retry': 2", self.src)

    def test_no_token_literals_in_output(self):
        self.assertNotIn("550e8400-e29b-41d4-a716-446655440000", self.src)
        self.assertNotIn("user-9f2c4e77a1b8", self.src)
        self.assertNotIn("hello there friend", self.src)
        self.assertNotIn("Bearer", self.src)

    def test_secret_keys_never_emitted(self):
        eps = [{"method": "POST", "path": "/auth", "status": 200, "query": "",
                "request_body": json.dumps(
                    {"accessToken": "SECRET-VALUE-12345", "deviceId": "d1"}),
                "response_body": "{}"}]
        src = codegen.generate_deterministic("api.example.com", eps)
        self.assertNotIn("SECRET-VALUE-12345", src)
        self.assertIn("access_token=None", src)

    def test_duplicate_names_deduped(self):
        eps = [
            {"method": "GET", "path": "/v1/items", "status": 200, "query": "",
             "request_body": "", "response_body": ""},
            {"method": "GET", "path": "/v2/items", "status": 200, "query": "",
             "request_body": "", "response_body": ""},
        ]
        src = codegen.generate_deterministic("api.example.com", eps)
        self.assertIn("def get_items(", src)
        self.assertIn("def get_items_2(", src)

    def test_path_param_in_url(self):
        self.assertIn('f"/v1/users/{user_id}"', self.src)

    def test_deterministic_same_input_same_output(self):
        again = codegen.generate_deterministic("api.example.com", EPS)
        self.assertEqual(self.src, again)

    def test_generated_client_imports_and_calls(self):
        # Import the generated client and call methods against a stubbed
        # Session.request — no network, no mitmweb.
        with tempfile.NamedTemporaryFile("w", suffix="_client.py",
                                         delete=False, dir="/tmp") as f:
            f.write(self.src)
            path = f.name
        try:
            spec = importlib.util.spec_from_file_location("gen_client", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls = mod.ExampleClient
            calls = []

            def fake_request(self, method, path, json=None, params=None,
                             **kw):
                calls.append((method, path, json, params))
                return {"ok": True}

            with mock.patch.object(cls, "request", fake_request):
                inst = cls.__new__(cls)
                inst.get_recs()
                inst.like(subject_id="s1", comment="hey")
                inst.create_message(recipient_id="r1", text="hi",
                                    platform="ios")
                inst.get_users_by_user_id(user_id="u9")
            self.assertEqual(calls[0][0], "GET")
            self.assertEqual(calls[0][1], "/rec/v2")
            self.assertEqual(calls[1][0], "POST")
            self.assertEqual(calls[1][1], "/like")
            self.assertEqual(calls[1][2]["subjectId"], "s1")
            self.assertEqual(calls[2][2]["platform"], "ios")
            self.assertEqual(calls[2][2]["recipientId"], "r1")
            self.assertEqual(calls[3][1], "/v1/users/u9")
        finally:
            os.unlink(path)

    def test_gen_from_har_fixture(self):
        eps = har.endpoints(FIXTURE, "api.example.com")
        src = codegen.generate("api.example.com", eps, polish="none")
        compile(src, "<gen>", "exec")
        self.assertIn("class ExampleClient(App):", src)
        self.assertIn("def like(", src)
        # The fixture's concrete user id generalizes to {id} by default.
        self.assertIn("def get_users_by_id(", src)
        self.assertNotIn("9f2c4e77a1b8", src)
        self.assertNotIn("SECRETTOKEN123", src)

    def test_gen_without_polish_needs_no_network(self):
        with mock.patch("requests.get", side_effect=AssertionError("no net")), \
             mock.patch("requests.post", side_effect=AssertionError("no net")):
            src = codegen.generate("api.example.com", EPS, polish="none")
        compile(src, "<gen>", "exec")


class GeneratedMarkerTests(unittest.TestCase):
    """Item 29: the generated-client marker the smoke test trusts."""

    def test_generated_source_carries_marker(self):
        src = codegen.generate_deterministic("api.example.com", EPS)
        self.assertIn(codegen.GENERATED_MARKER, src)

    def test_is_generated_client(self):
        gen = os.path.join(tempfile.mkdtemp(), "gen_client.py")
        with open(gen, "w") as fh:
            fh.write(codegen.generate_deterministic("api.example.com",
                                                    EPS))
        foreign = os.path.join(tempfile.mkdtemp(), "foreign_client.py")
        with open(foreign, "w") as fh:
            fh.write("print('hello')\n")
        binary = os.path.join(tempfile.mkdtemp(), "binary_client.py")
        with open(binary, "wb") as fh:
            fh.write(b"\x00\x01\x02 not text")
        self.assertTrue(codegen.is_generated_client(gen))
        self.assertFalse(codegen.is_generated_client(foreign))
        self.assertFalse(codegen.is_generated_client(binary))
        self.assertFalse(codegen.is_generated_client(
            os.path.join(tempfile.mkdtemp(), "missing_client.py")))


class PromptTests(unittest.TestCase):
    def test_build_prompt_mentions_mimik_app(self):
        prompt = codegen.build_prompt("api.example.com", EPS)
        self.assertIn("mimik.App", prompt)
        self.assertIn('HOST = "api.example.com"', prompt)
        self.assertIn("### GET /rec/v2", prompt)

    def test_build_digest_includes_bodies(self):
        digest = codegen.build_digest(EPS)
        self.assertIn("subjectId", digest)
        self.assertIn("request body:", digest)


class PolishTests(unittest.TestCase):
    def test_pick_ollama_model_prefers_small(self):
        models = ["llama3.2:3b", "qwen3.5:2b", "mistral:7b"]
        self.assertEqual(codegen.pick_ollama_model(models), "qwen3.5:2b")

    def test_pick_ollama_model_none_when_empty(self):
        self.assertIsNone(codegen.pick_ollama_model([]))

    def test_ollama_models_unreachable_returns_empty(self):
        with mock.patch("requests.get", side_effect=Exception("down")):
            self.assertEqual(codegen.ollama_models(), [])

    def test_polish_ollama_falls_back_without_ollama(self):
        src = codegen.generate_deterministic("api.example.com", EPS)
        with mock.patch.object(codegen, "ollama_models", return_value=[]):
            out, ok = codegen.polish_ollama(src, "api.example.com")
        self.assertFalse(ok)
        self.assertEqual(out, src)

    def test_polish_cli_missing_binary_falls_back(self):
        src = codegen.generate_deterministic("api.example.com", EPS)
        with mock.patch("subprocess.run",
                        side_effect=FileNotFoundError("no claude")):
            out, ok = codegen.polish_cli(src, "api.example.com", "claude")
        self.assertFalse(ok)
        self.assertEqual(out, src)

    def test_polish_cli_rejects_bad_output(self):
        src = codegen.generate_deterministic("api.example.com", EPS)
        bad = mock.Mock(returncode=0, stdout="this is not python (((")
        with mock.patch("subprocess.run", return_value=bad):
            out, ok = codegen.polish_cli(src, "api.example.com", "claude")
        self.assertFalse(ok)
        self.assertEqual(out, src)


class SingleSampleTests(unittest.TestCase):
    """v0.4 item 11: single-sample methods are flagged."""

    def _ep(self, method, path, samples):
        ep = {"method": method, "path": path, "status": 200,
              "query": "", "request_body": "", "response_body": "{}"}
        if samples is not None:
            ep["samples"] = samples
        return ep

    def test_single_sample_method_gets_a_comment(self):
        src = codegen.generate_deterministic(
            "api.example.com",
            [self._ep("GET", "/rec/v2", 1),
             self._ep("POST", "/like", 3)])
        self.assertEqual(
            src.count("# Single sample: one captured call built this "
                      "method."),
            1)
        # The comment sits in the single-sample method, not the other.
        get_block = src.split("def get_recs(")[1].split("def like(")[0]
        self.assertIn("# Single sample", get_block)
        like_block = src.split("def like(")[1]
        self.assertNotIn("# Single sample", like_block)

    def test_docstring_carries_the_count(self):
        src = codegen.generate_deterministic(
            "api.example.com",
            [self._ep("GET", "/rec/v2", 1),
             self._ep("POST", "/like", 1),
             self._ep("GET", "/v1/users/123", 4)])
        self.assertIn("2 of 3 endpoints come from a single captured "
                      "sample", src)

    def test_no_singles_means_no_flag_and_no_count(self):
        src = codegen.generate_deterministic(
            "api.example.com",
            [self._ep("GET", "/rec/v2", 2),
             self._ep("POST", "/like", 5)])
        self.assertNotIn("Single sample", src)
        self.assertNotIn("from a single captured sample", src)

    def test_missing_samples_means_no_flag(self):
        # Fixtures from before the count existed carry no "samples" —
        # don't claim single-sample when we can't know.
        src = codegen.generate_deterministic("api.example.com", EPS)
        self.assertNotIn("Single sample", src)
        self.assertNotIn("from a single captured sample", src)

    def test_generalize_sums_samples_across_merged_endpoints(self):
        eps = codegen._generalize_endpoints(
            [self._ep("GET", "/users/123", 1),
             self._ep("GET", "/users/456", 1)])
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["samples"], 2)
        # So the merged method is never flagged single-sample.
        src = codegen.generate_deterministic(
            "api.example.com",
            [self._ep("GET", "/users/123", 1),
             self._ep("GET", "/users/456", 1)])
        self.assertNotIn("Single sample", src)


class WebSocketCodegenTests(unittest.TestCase):
    """v0.4 item 13: WS endpoints become ws_replay methods."""

    def _ws_ep(self, path="/chat", samples=1, messages=None):
        if messages is None:
            messages = [
                {"from_client": True, "timestamp": 1.0, "text": "hello"},
                {"from_client": False, "timestamp": 1.1, "text": "hello"},
                {"from_client": True, "timestamp": 1.2,
                 "b64": base64.b64encode(b"\x00\x01").decode()},
            ]
        return {"method": "GET", "path": path, "status": 101, "query": "",
                "request_body": "", "response_body": "",
                "samples": samples, "websocket": True,
                "ws_messages": messages}

    def test_ws_method_generated(self):
        src = codegen.generate_deterministic(
            "api.example.com",
            [self._ws_ep(), EPS[0]])
        self.assertIn("def get_chats_ws(self, messages=None):", src)
        self.assertIn('"""GET /chat -> 101 (WebSocket)."""', src)
        # Only the client messages embed (text as a string, binary as
        # bytes.fromhex) — the server reply is not replayed.
        self.assertIn("messages = ['hello', bytes.fromhex('0001')]", src)
        self.assertIn('return self.ws_replay("/chat", messages)', src)
        self.assertIn("# Single sample", src)
        # The plain endpoint next to it is untouched.
        self.assertIn("def get_recs(self):", src)

    def test_ws_method_with_path_params(self):
        # Generalization runs first (/rooms/123 -> /rooms/{id}), so the
        # param is `id` — the same as an HTTP method would get.
        src = codegen.generate_deterministic(
            "api.example.com", [self._ws_ep("/rooms/123/ws")])
        self.assertIn("def get_rooms_by_id_ws(self, id, "
                      "messages=None):", src)
        self.assertIn('return self.ws_replay(f"/rooms/{id}/ws", '
                      "messages)", src)

    def test_ws_method_no_client_messages(self):
        src = codegen.generate_deterministic(
            "api.example.com", [self._ws_ep(messages=[])])
        self.assertIn("messages = []  # no client messages captured",
                      src)

    def test_ip_host_gives_a_valid_class_name(self):
        # v0.4 item 13 e2e found this: 127.0.0.1 gave 127Client, which
        # is a SyntaxError. The generated source must compile.
        self.assertEqual(codegen.class_name_for_host("127.0.0.1"),
                         "Host127Client")
        src = codegen.generate_deterministic(
            "127.0.0.1", [self._ws_ep()])
        compile(src, "<generated>", "exec")
        self.assertIn("class Host127Client(App):", src)

    def test_digest_marks_websocket(self):
        digest = codegen.build_digest([self._ws_ep()])
        self.assertIn("### GET /chat  -> 101 (WebSocket)", digest)
        self.assertIn("ws session: 2 client -> server, "
                      "1 server -> client", digest)


if __name__ == "__main__":
    unittest.main()
