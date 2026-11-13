"""Tests for the first-run sample capture (item 28)."""
import argparse
import base64
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # for test_presets helpers

from mimik import assets, cli, codegen, presets, sample, store
from mimik.sources.mitm import endpoints_from_saved
from test_presets import run_cmd


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def test_sample_flows_shape(self):
        flows = sample.sample_flows()
        self.assertEqual(len(flows), 6)
        ids = [f["id"] for f in flows]
        self.assertEqual(len(set(ids)), 6)
        for f in flows:
            self.assertEqual(f["request"]["host"], sample.SAMPLE_HOST)
        logo = [f for f in flows if f["id"] == "sample-logo"][0]
        raw = base64.b64decode(logo["_saved_response_body"])
        self.assertTrue(raw.startswith(b"\x89PNG"))
        # Fresh dicts each call — mutating one copy can't corrupt the
        # next.
        sample.sample_flows()[0]["request"]["path"] = "/mutated"
        self.assertEqual(sample.sample_flows()[0]["request"]["path"],
                         "/api/auth/login")

    def test_load_sample_saves_flows_assets_preset(self):
        result = sample.load_sample(base=self.tmp)
        self.assertEqual(result,
                         {"flows": 6, "assets": 1,
                          "preset": sample.SAMPLE_HOST,
                          "already_loaded": False})
        self.assertEqual(len(store.load_host(sample.SAMPLE_HOST,
                                             self.tmp)), 6)
        groups = assets.list_asset_groups(base=self.tmp)
        self.assertEqual(len(groups), 1)
        names = [a["name"] for a in groups[0][1]["assets"]]
        self.assertEqual(names, ["logo.png"])
        preset = presets.load_preset(sample.SAMPLE_HOST, base=self.tmp)
        self.assertIsNotNone(preset)
        paths = [e["path"] for e in preset["endpoints"]]
        # Id-varying paths generalize to one endpoint (item 2).
        self.assertIn("/api/users/{id}", paths)
        self.assertNotIn("/api/users/123", paths)

    def test_load_sample_idempotent(self):
        sample.load_sample(base=self.tmp)
        second = sample.load_sample(base=self.tmp)
        self.assertTrue(second["already_loaded"])
        self.assertEqual(len(store.load_host(sample.SAMPLE_HOST,
                                             self.tmp)), 6)
        self.assertEqual(len(assets.list_asset_groups(base=self.tmp)), 1)

    def test_is_fresh(self):
        self.assertTrue(sample.is_fresh(base=self.tmp))
        presets.set_sample_offered(base=self.tmp)
        self.assertFalse(sample.is_fresh(base=self.tmp))
        # A data dir with any capture is not fresh, offer or not.
        presets.set_sample_offered(base=self.tmp)
        store.save_flows([{
            "id": "x1",
            "request": {"host": "other.example.com", "method": "GET",
                        "path": "/", "scheme": "https", "port": 443,
                        "headers": []},
            "response": {"status_code": 200, "headers": []},
        }], base=self.tmp)
        self.assertFalse(sample.is_fresh(base=self.tmp))

    def test_sample_client_works(self):
        """The guided first client: load the sample, generate a
        client from it, import it, and call its methods against a
        stubbed Session.request — no network, no mitmweb."""
        sample.load_sample(base=self.tmp)
        flows = store.load_host(sample.SAMPLE_HOST, self.tmp)
        eps = endpoints_from_saved(flows, sample.SAMPLE_HOST)
        src = codegen.generate(sample.SAMPLE_HOST, eps)
        self.assertIn("class DemoClient(App):", src)
        self.assertIn("def post_auth_login(", src)
        self.assertIn("def get_users_by_id(", src)
        self.assertIn("def get_items(", src)
        path = os.path.join(self.tmp, "demo_client.py")
        with open(path, "w") as fh:
            fh.write(src)
        spec = importlib.util.spec_from_file_location("demo_client", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls = mod.DemoClient
        calls = []

        def fake_request(self, method, path, json=None, params=None,
                         **kw):
            calls.append((method, path, json))
            return {"ok": True}

        with mock.patch.object(cls, "request", fake_request):
            inst = cls.__new__(cls)
            inst.post_auth_login(username="demo-player",
                                 password="fake-demo-password-sample-only")
            inst.get_users_by_id(id="123")
            inst.get_items()
            inst.get_items_by_id(id="7")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "/api/auth/login")
        self.assertEqual(calls[0][2]["username"], "demo-player")
        self.assertEqual(calls[1], ("GET", "/api/users/123", None))
        self.assertEqual(calls[2][1], "/api/items")
        self.assertEqual(calls[3][1], "/api/items/7")

    def test_sample_cli(self):
        out, code = run_cmd(cli.cmd_sample, argparse.Namespace())
        self.assertEqual(code, 0)
        self.assertIn("Loaded the sample capture", out)
        self.assertIn("mimik gen demo.example.com", out)
        out, code = run_cmd(cli.cmd_sample, argparse.Namespace())
        self.assertEqual(code, 0)
        self.assertIn("already loaded", out)


if __name__ == "__main__":
    unittest.main()
