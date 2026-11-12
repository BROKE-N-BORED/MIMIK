"""Tests for mimik.codegen — the deterministic $0 generator."""
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
                      "text=None):", self.src)

    def test_constants_stay_as_defaults(self):
        self.assertIn("'platform': 'ios'", self.src)
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
                inst.create_message(recipient_id="r1", text="hi")
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
        self.assertIn("def get_users_by_user_id(", src)
        self.assertNotIn("9f2c4e77a1b8", src)
        self.assertNotIn("SECRETTOKEN123", src)

    def test_gen_without_polish_needs_no_network(self):
        with mock.patch("requests.get", side_effect=AssertionError("no net")), \
             mock.patch("requests.post", side_effect=AssertionError("no net")):
            src = codegen.generate("api.example.com", EPS, polish="none")
        compile(src, "<gen>", "exec")


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


if __name__ == "__main__":
    unittest.main()
