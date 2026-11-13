"""Tests for learned presets — rule extraction, merging, and applying."""
import argparse
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # for test_assets helpers

from mimik import assets, cli, codegen, paths, presets, reblend_bridge, store
from mimik.sources import mitm as mitm_mod
from test_assets import read_t9, saved_flow
from test_reblend_bridge import seed_game_assets

REBLEND_T9 = os.environ.get(
    "REBLEND_FIXTURE",
    os.path.join(os.path.expanduser("~"), "workspace",
                 "reblend-v02-t9-build", "reblend"))
HAS_REBLEND = reblend_bridge._is_reblend_dir(REBLEND_T9)

TOKEN = "fake-bearer-token-for-tests"
COOKIE_VALUE = "fake-session-cookie-for-tests"
API_KEY = "fake-api-key-for-tests"
DEVICE_VALUE = "dev-999"

HOST = "game.example.com"


def auth_flow(fid, host, path, method="GET", status=200):
    """A flow with auth headers — values that must NEVER reach a preset."""
    return {
        "id": fid,
        "request": {"host": host, "method": method, "path": path,
                    "scheme": "https", "port": 443,
                    "headers": [
                        ["Authorization", f"Bearer {TOKEN}"],
                        ["X-Api-Key", API_KEY],
                        ["Cookie",
                         f"session_id={COOKIE_VALUE}; theme=dark"],
                        ["User-Agent", "GameApp/1.0"],
                    ]},
        "response": {"status_code": status,
                     "headers": [["Set-Cookie",
                                  f"device_id={DEVICE_VALUE}; Path=/; "
                                  "HttpOnly"]]},
    }


def run_cmd(func, args):
    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out):
            old_err = sys.stderr
            sys.stderr = err
            try:
                func(args)
            finally:
                sys.stderr = old_err
    except SystemExit as e:
        return out.getvalue() + err.getvalue(), e.code
    return out.getvalue() + err.getvalue(), 0


class _NoMitm:
    """Stand-in for Mitm that is always down — forces the saved path."""

    def __init__(self, *a, **k):
        pass

    def flows(self):
        raise mitm_mod.MitmError("no proxy")


class GeneralizeTests(unittest.TestCase):
    def test_numeric_segments_generalize(self):
        self.assertEqual(paths.generalize_path("/api/users/123"),
                         "/api/users/{id}")
        self.assertEqual(paths.generalize_path("/api/users/456"),
                         "/api/users/{id}")

    def test_uuid_and_long_hex_generalize(self):
        self.assertEqual(
            paths.generalize_path(
                "/v1/posts/550e8400-e29b-41d4-a716-446655440000"),
            "/v1/posts/{id}")
        self.assertEqual(
            paths.generalize_path("/dl/9f2c4e77a1b8c3d4e5f6a7b8"),
            "/dl/{id}")

    def test_literals_stay(self):
        self.assertEqual(paths.generalize_path("/api/users/profile"),
                         "/api/users/profile")
        self.assertEqual(paths.generalize_path("/v1/rec"),
                         "/v1/rec")

    def test_braced_segments_stay(self):
        self.assertEqual(
            paths.generalize_path("/v1/users/{user_id}"),
            "/v1/users/{user_id}")

    def test_query_is_dropped(self):
        self.assertEqual(paths.generalize_path("/api/users/123?x=1"),
                         "/api/users/{id}")


class LearnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def _save_auth_flows(self):
        store.save_flows([
            auth_flow("f1", HOST, "/api/users/123"),
            auth_flow("f2", HOST, "/api/users/456"),
            auth_flow("f3", HOST, "/api/users/789"),
            auth_flow("f4", HOST, "/api/auth/login", method="POST",
                      status=200),
            auth_flow("f5", HOST, "/api/posts/"
                      "550e8400-e29b-41d4-a716-446655440000"),
        ], base=self.tmp)

    def test_learn_auth_names_not_values(self):
        self._save_auth_flows()
        preset, path, created = presets.learn(HOST)
        self.assertTrue(created)
        self.assertEqual(preset["scope"], "host")
        self.assertEqual(preset["name"], HOST)
        auth = preset["auth"]
        self.assertEqual(sorted(auth["headers"]),
                         ["Authorization", "X-Api-Key"])
        self.assertEqual(auth["schemes"], ["Bearer"])
        self.assertIn("session_id", auth["cookies"])
        self.assertIn("device_id", auth["cookies"])
        self.assertIn("theme", auth["cookies"])
        # Set-Cookie attributes are NOT cookie names.
        self.assertNotIn("Path", auth["cookies"])
        self.assertNotIn("HttpOnly", auth["cookies"])
        self.assertIn("POST /api/auth/login", auth["token_endpoints"])

    def test_secret_values_never_reach_the_preset_file(self):
        self._save_auth_flows()
        _preset, path, _created = presets.learn(HOST)
        with open(path, "rb") as fh:
            raw = fh.read()
        for secret in (TOKEN, COOKIE_VALUE, API_KEY, DEVICE_VALUE, "dark"):
            self.assertNotIn(secret.encode(), raw, secret)
        # ...while the (non-secret) shapes ARE there.
        self.assertIn(b"Authorization", raw)
        self.assertIn(b"Bearer", raw)
        self.assertIn(b"session_id", raw)

    def test_endpoints_generalized_and_counted(self):
        self._save_auth_flows()
        preset, _path, _created = presets.learn(HOST)
        by_key = {(e["method"], e["path"]): e for e in preset["endpoints"]}
        self.assertEqual(by_key[("GET", "/api/users/{id}")]["seen"], 3)
        self.assertIn(("GET", "/api/posts/{id}"), by_key)
        self.assertIn(("POST", "/api/auth/login"), by_key)

    def test_merge_keeps_old_adds_new(self):
        store.save_flows([
            auth_flow("f1", HOST, "/api/users/123"),
            auth_flow("f2", HOST, "/api/shop"),
        ], base=self.tmp)
        preset, path, _created = presets.learn(HOST)
        # Pin an old first_seen so the merge's min() is observable.
        with open(path) as fh:
            data = json.load(fh)
        for e in data["endpoints"]:
            if e["path"] == "/api/users/{id}":
                e["first_seen"] = "2026-01-01T00:00:00"
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
        # Second batch: the users endpoint again (SAME flow id, so the
        # saved-capture dedupe keeps the count honest either side of a
        # second boundary) plus a brand-new endpoint.
        store.save_flows([
            auth_flow("f1", HOST, "/api/users/999"),
            auth_flow("f3", HOST, "/api/friends"),
        ], base=self.tmp)
        preset, _path, created = presets.learn(HOST)
        self.assertFalse(created)
        by_key = {(e["method"], e["path"]): e for e in preset["endpoints"]}
        self.assertEqual(by_key[("GET", "/api/users/{id}")]["seen"], 2)
        self.assertEqual(by_key[("GET", "/api/users/{id}")]["first_seen"],
                         "2026-01-01T00:00:00")
        self.assertIn(("GET", "/api/shop"), by_key)
        self.assertIn(("GET", "/api/friends"), by_key)

    def test_learn_nothing_returns_none(self):
        self.assertIsNone(presets.learn("nobody.example.com"))

    def test_learn_group_from_scan(self):
        root = os.path.join(self.tmp, "mygame")
        os.makedirs(root)
        with open(os.path.join(root, "crate.glb"), "wb") as fh:
            fh.write(b"glTF-fake")
        with open(os.path.join(root, "logo.png"), "wb") as fh:
            fh.write(b"\x89PNG-fake")
        with open(os.path.join(root, "tune.wav"), "wb") as fh:
            fh.write(b"RIFF-fake")
        assets.scan_program_tree(root, "mygame", base=self.tmp)
        preset, _path, created = presets.learn("mygame")
        self.assertTrue(created)
        self.assertEqual(preset["scope"], "group")
        self.assertEqual(preset["name"], "mygame")
        self.assertEqual(preset["endpoints"], [])
        self.assertEqual(preset["auth"]["headers"], [])
        mapping = {(m["pattern"], m["kind"]): m["seen"]
                   for m in preset["asset_mapping"]}
        self.assertEqual(mapping[("*.glb", "model")], 1)
        self.assertEqual(mapping[("*.png", "image")], 1)
        self.assertEqual(mapping[("*.wav", "audio")], 1)

    def test_learn_group_by_dir_name(self):
        root = os.path.join(self.tmp, "mygame")
        os.makedirs(root)
        with open(os.path.join(root, "crate.glb"), "wb") as fh:
            fh.write(b"glTF-fake")
        gdir, _m = assets.scan_program_tree(root, "mygame", base=self.tmp)
        preset, _path, _created = presets.learn(os.path.basename(gdir))
        self.assertEqual(preset["scope"], "group")
        self.assertEqual(preset["name"], "mygame")

    @unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
    def test_asset_mapping_from_traffic(self):
        seed_game_assets(self.tmp)
        preset, _path, _created = presets.learn(HOST)
        mapping = {(m["pattern"], m["kind"]): m["seen"]
                   for m in preset["asset_mapping"]}
        self.assertEqual(mapping[("/m/*.glb", "model")], 1)
        self.assertEqual(mapping[("/m/*.obj", "model")], 1)
        self.assertEqual(mapping[("/i/*.png", "image")], 1)
        self.assertEqual(mapping[("/s/*.wav", "audio")], 1)
        self.assertEqual(mapping[("/f/*.woff2", "font")], 1)

    @unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
    def test_scale_learned_from_pack(self):
        seed_game_assets(self.tmp)
        reblend_bridge.build_pack(host=HOST, reblend=REBLEND_T9,
                                  scale=250.0, base=self.tmp)
        preset, _path, _created = presets.learn(HOST)
        self.assertEqual(preset["pack"]["scale"], 250.0)

    def test_list_and_delete(self):
        self._save_auth_flows()
        presets.learn(HOST)
        found = presets.list_presets(base=self.tmp)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], HOST)
        self.assertTrue(presets.delete_preset(HOST, base=self.tmp))
        self.assertEqual(presets.list_presets(base=self.tmp), [])
        self.assertFalse(presets.delete_preset(HOST, base=self.tmp))


class CodegenPresetTests(unittest.TestCase):
    EPS = [
        {"method": "GET", "path": "/api/users/123", "status": 200,
         "query": "", "request_body": "", "response_body": "{}"},
        {"method": "GET", "path": "/api/users/456", "status": 200,
         "query": "", "request_body": "", "response_body": "{}"},
        {"method": "POST", "path": "/api/auth/login", "status": 200,
         "query": "", "request_body": "{}", "response_body": "{}"},
    ]

    def _preset(self):
        return {
            "version": 1, "scope": "host", "name": HOST,
            "learned_at": "2026-11-12T20:30:00",
            "auth": {"headers": ["Authorization", "X-Api-Key"],
                     "schemes": ["Bearer"],
                     "cookies": ["session_id"],
                     "token_endpoints": ["POST /api/auth/login"]},
            "endpoints": [{"method": "GET", "path": "/api/users/{id}",
                           "seen": 2, "first_seen": "2026-11-12T20:30:00",
                           "last_seen": "2026-11-12T20:30:00"}],
            "asset_mapping": [],
            "pack": {},
        }

    def test_preset_collapses_endpoints_and_adds_auth(self):
        src = codegen.generate_deterministic(HOST, self.EPS,
                                             preset=self._preset())
        compile(src, "<gen>", "exec")
        self.assertIn("AUTH_HEADERS = ('Authorization', 'X-Api-Key')", src)
        self.assertIn("AUTH_COOKIES = ('session_id',)", src)
        self.assertIn("#   header: Authorization (Bearer)", src)
        self.assertIn("#   cookie: session_id", src)
        self.assertIn("#   sign-in: POST /api/auth/login", src)
        # Two concrete user ids became ONE generalized method.
        self.assertEqual(src.count("def get_users_by_id("), 1)
        self.assertNotIn("get_users_by_id_2", src)
        self.assertIn('"/api/users/{id}"', src)

    def test_no_preset_output_is_byte_identical(self):
        plain = codegen.generate_deterministic(HOST, self.EPS)
        explicit_none = codegen.generate_deterministic(
            HOST, self.EPS, preset=None)
        self.assertEqual(plain, explicit_none)
        self.assertNotIn("preset", plain.lower())
        self.assertNotIn("AUTH_HEADERS", plain)
        # Generalization is the default now, preset or not: the two
        # concrete ids collapse to ONE generalized method, exactly as
        # they do with a preset — the preset only adds the auth block.
        self.assertEqual(plain.count("def get_users_by_id("), 1)
        self.assertNotIn("get_users_by_id_2", plain)
        self.assertIn('"/api/users/{id}"', plain)

    def test_preset_without_auth_adds_no_auth_block(self):
        preset = self._preset()
        preset["auth"] = {"headers": [], "schemes": [], "cookies": [],
                          "token_endpoints": []}
        src = codegen.generate_deterministic(HOST, self.EPS, preset=preset)
        self.assertNotIn("AUTH_HEADERS", src)
        self.assertNotIn("Auth, learned", src)
        # ...but the endpoints still collapse.
        self.assertEqual(src.count("def get_users_by_id("), 1)

    def test_input_endpoints_not_mutated(self):
        eps = [dict(e) for e in self.EPS]
        codegen.generate_deterministic(HOST, eps, preset=self._preset())
        self.assertEqual(eps[0]["path"], "/api/users/123")
        self.assertEqual(eps[1]["path"], "/api/users/456")


@unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
class BuildPackPresetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)
        seed_game_assets(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def _pack_json(self, pack_dir):
        with open(os.path.join(pack_dir, "pack.json")) as fh:
            return json.load(fh)

    def test_preset_scale_used_when_scale_absent(self):
        reblend_bridge.build_pack(host=HOST, reblend=REBLEND_T9,
                                  scale=250.0, base=self.tmp)
        presets.learn(HOST)
        result = reblend_bridge.build_pack(host=HOST, reblend=REBLEND_T9,
                                           base=self.tmp)
        self.assertEqual(result["scale"], 250.0)
        self.assertTrue(result["preset_applied"])
        pack = self._pack_json(result["pack_dir"])
        self.assertEqual(pack["scale"], 250.0)
        self.assertTrue(pack["preset_applied"])
        self.assertEqual(pack["preset"], HOST)

    def test_explicit_scale_overrides_and_updates_preset(self):
        reblend_bridge.build_pack(host=HOST, reblend=REBLEND_T9,
                                  scale=250.0, base=self.tmp)
        presets.learn(HOST)
        result = reblend_bridge.build_pack(host=HOST, reblend=REBLEND_T9,
                                           scale=50.0, base=self.tmp)
        self.assertEqual(result["scale"], 50.0)
        self.assertFalse(result["preset_applied"])
        pack = self._pack_json(result["pack_dir"])
        self.assertEqual(pack["scale"], 50.0)
        self.assertFalse(pack["preset_applied"])
        # The preset learned the scale that was actually used.
        self.assertEqual(
            presets.load_preset(HOST, scope="host")["pack"]["scale"], 50.0)

    def test_no_preset_means_default_scale(self):
        result = reblend_bridge.build_pack(host=HOST, reblend=REBLEND_T9,
                                           base=self.tmp)
        self.assertEqual(result["scale"], 100.0)
        self.assertFalse(result["preset_applied"])
        pack = self._pack_json(result["pack_dir"])
        self.assertEqual(pack["scale"], 100.0)
        self.assertIsNone(pack["preset"])

    def test_build_pack_never_creates_a_preset(self):
        reblend_bridge.build_pack(host=HOST, reblend=REBLEND_T9,
                                  scale=250.0, base=self.tmp)
        self.assertIsNone(presets.load_preset(HOST, scope="host"))


class PresetCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def _save_auth_flows(self):
        store.save_flows([
            auth_flow("f1", HOST, "/api/users/123"),
            auth_flow("f2", HOST, "/api/users/456"),
            auth_flow("f3", HOST, "/api/auth/login", method="POST"),
        ], base=self.tmp)

    def test_list_learn_show_delete_round_trip(self):
        self._save_auth_flows()
        out, code = run_cmd(cli.cmd_presets_learn,
                            argparse.Namespace(name=HOST))
        self.assertEqual(code, 0)
        self.assertIn("Learned a preset for game.example.com (host)", out)
        self.assertIn("endpoint", out)

        out, code = run_cmd(cli.cmd_presets,
                            argparse.Namespace(auto_learn=None))
        self.assertEqual(code, 0)
        self.assertIn("game.example.com (host)", out)
        self.assertIn("last learned", out)
        self.assertIn("Learn presets from captures: off", out)
        # The same learn also extracted pattern presets — they list
        # in their own section (the fixture traffic carries an API
        # key header on two endpoints, plus JWT bearers).
        self.assertIn("Pattern presets:", out)
        self.assertIn("api-key-header", out)

        out, code = run_cmd(cli.cmd_presets_show,
                            argparse.Namespace(name=HOST))
        self.assertEqual(code, 0)
        self.assertIn("How this app signs in:", out)
        self.assertIn("header: Authorization (Bearer)", out)
        self.assertIn("GET /api/users/{id}", out)
        # Plain language, not a JSON dump — and no secrets.
        self.assertNotIn('"auth"', out)
        self.assertNotIn('"endpoints"', out)
        self.assertNotIn(TOKEN, out)

        out, code = run_cmd(cli.cmd_presets_delete,
                            argparse.Namespace(name=HOST, yes=True))
        self.assertEqual(code, 0)
        self.assertIn("Deleted the preset", out)
        self.assertIsNone(presets.load_preset(HOST))

        # Deleting the app preset leaves the pattern presets alone —
        # they are a separate store, learned from the same traffic.
        out, code = run_cmd(cli.cmd_presets,
                            argparse.Namespace(auto_learn=None))
        self.assertNotIn("game.example.com (host)", out)
        self.assertIn("Pattern presets:", out)

        out, code = run_cmd(cli.cmd_presets_delete,
                            argparse.Namespace(name="api-key-header",
                                               yes=True))
        self.assertEqual(code, 0)
        self.assertIn("Deleted the pattern preset", out)
        self.assertIsNone(presets.load_pattern_preset("api-key-header"))

        out, code = run_cmd(cli.cmd_presets,
                            argparse.Namespace(auto_learn=None))
        self.assertIn("no presets yet", out)

    def test_delete_asks_without_yes(self):
        self._save_auth_flows()
        presets.learn(HOST)
        with mock.patch("builtins.input", return_value="n"):
            out, code = run_cmd(cli.cmd_presets_delete,
                                argparse.Namespace(name=HOST, yes=False))
        self.assertEqual(code, 0)
        self.assertIn("kept it", out)
        self.assertIsNotNone(presets.load_preset(HOST))
        with mock.patch("builtins.input", return_value="y"):
            out, code = run_cmd(cli.cmd_presets_delete,
                                argparse.Namespace(name=HOST, yes=False))
        self.assertIn("Deleted the preset", out)
        self.assertIsNone(presets.load_preset(HOST))

    def test_learn_unknown_host_exits(self):
        out, code = run_cmd(cli.cmd_presets_learn,
                            argparse.Namespace(name="nobody.example.com"))
        self.assertNotEqual(code, 0)

    def test_show_unknown_preset_exits(self):
        out, code = run_cmd(cli.cmd_presets_show,
                            argparse.Namespace(name="nobody.example.com"))
        self.assertNotEqual(code, 0)

    def test_auto_learn_flag(self):
        self.assertFalse(presets.auto_learn_enabled())
        out, code = run_cmd(cli.cmd_presets,
                            argparse.Namespace(auto_learn="on"))
        self.assertEqual(code, 0)
        self.assertIn("Learn presets from captures: on", out)
        self.assertTrue(presets.auto_learn_enabled())
        run_cmd(cli.cmd_presets, argparse.Namespace(auto_learn="off"))
        self.assertFalse(presets.auto_learn_enabled())

    def test_gen_uses_preset_for_its_host_only(self):
        self._save_auth_flows()
        store.save_flows([
            auth_flow("o1", "other.example.com", "/v1/things/1"),
        ], base=self.tmp)
        presets.learn(HOST)
        out_path = os.path.join(self.tmp, "game_client.py")
        args = argparse.Namespace(host=HOST, har=None, curl=None,
                                  out=out_path, model=None,
                                  polish="none", prompt_only=False)
        with mock.patch.object(mitm_mod, "Mitm", _NoMitm):
            out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        with open(out_path) as fh:
            src = fh.read()
        compile(src, "<gen>", "exec")
        self.assertIn("AUTH_HEADERS", src)
        self.assertEqual(src.count("def get_users_by_id("), 1)

        # The other host has no preset: plain output, byte-identical
        # to the generator called without a preset.
        other_path = os.path.join(self.tmp, "other_client.py")
        args = argparse.Namespace(host="other.example.com", har=None,
                                  curl=None, out=other_path, model=None,
                                  polish="none", prompt_only=False)
        with mock.patch.object(mitm_mod, "Mitm", _NoMitm):
            out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        with open(other_path) as fh:
            other_src = fh.read()
        self.assertNotIn("AUTH_HEADERS", other_src)
        eps = mitm_mod.endpoints_from_saved(store.load_all(),
                                            "other.example.com")
        self.assertEqual(
            other_src,
            codegen.generate_deterministic("other.example.com", eps))

    def test_main_parses_presets_learn(self):
        with mock.patch.object(cli, "cmd_presets_learn") as learn:
            cli.main(["presets", "learn", HOST])
        learn.assert_called_once()
        self.assertEqual(learn.call_args[0][0].name, HOST)

    def test_main_parses_presets_list(self):
        with mock.patch.object(cli, "cmd_presets") as listing:
            cli.main(["presets"])
        listing.assert_called_once()


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_default_off(self):
        self.assertFalse(presets.auto_learn_enabled(base=self.tmp))

    def test_set_on_and_off(self):
        presets.set_auto_learn(True, base=self.tmp)
        self.assertTrue(presets.auto_learn_enabled(base=self.tmp))
        presets.set_auto_learn(False, base=self.tmp)
        self.assertFalse(presets.auto_learn_enabled(base=self.tmp))

    def test_corrupt_settings_means_off(self):
        with open(os.path.join(self.tmp, "settings.json"), "w") as fh:
            fh.write("{not json")
        self.assertFalse(presets.auto_learn_enabled(base=self.tmp))

    def test_screenshot_default_off(self):
        # v0.4 item 15: a desktop screenshot is opt-in.
        self.assertFalse(presets.screenshot_enabled(base=self.tmp))

    def test_screenshot_set_on_and_off(self):
        presets.set_screenshot_enabled(True, base=self.tmp)
        self.assertTrue(presets.screenshot_enabled(base=self.tmp))
        presets.set_screenshot_enabled(False, base=self.tmp)
        self.assertFalse(presets.screenshot_enabled(base=self.tmp))


class AfterRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_off_reports_existing_preset_without_refreshing(self):
        store.save_flows([auth_flow("f1", HOST, "/api/users/123")],
                         base=self.tmp)
        presets.learn(HOST)
        store.save_flows([auth_flow("f2", HOST, "/api/new-thing")],
                         base=self.tmp)
        rows = presets.after_record([HOST], base=self.tmp)
        self.assertEqual(rows, [(HOST, False, True, [])])
        preset = presets.load_preset(HOST, scope="host")
        paths = {e["path"] for e in preset["endpoints"]}
        self.assertNotIn("/api/new-thing", paths)

    def test_on_refreshes_and_creates(self):
        presets.set_auto_learn(True, base=self.tmp)
        store.save_flows([auth_flow("f1", HOST, "/api/users/123")],
                         base=self.tmp)
        rows = presets.after_record([HOST], base=self.tmp)
        self.assertEqual(rows, [(HOST, True, True, [])])
        preset = presets.load_preset(HOST, scope="host")
        paths = {e["path"] for e in preset["endpoints"]}
        self.assertIn("/api/users/{id}", paths)

    def test_record_stop_summary_says_so(self):
        store.save_flows([auth_flow("f1", HOST, "/api/users/123")],
                         base=self.tmp)
        presets.learn(HOST)
        out, code = run_cmd(cli._record_stop_summary, [(HOST, 3)])
        self.assertEqual(code, 0)
        self.assertIn(f"Used the saved preset for {HOST}.", out)
        self.assertNotIn("Preset refreshed", out)
        presets.set_auto_learn(True, base=self.tmp)
        out, code = run_cmd(cli._record_stop_summary, [(HOST, 3)])
        self.assertIn(f"Preset refreshed for {HOST}.", out)
        self.assertIn(f"Used the saved preset for {HOST}.", out)


class PrivatePermsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_preset_files_written_0600(self):
        # Review #13: preset files hold auth shapes — owner only.
        path = presets._save_preset(
            {"name": "api.example.com", "scope": "host", "auth": {}},
            base=self.tmp)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        path = presets._save_pattern_preset(
            {"name": "sign-in", "scope": "pattern"}, base=self.tmp)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class PresetVersionTests(unittest.TestCase):
    """Item 32f: preset files carry a version — loads validate it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def _write_preset(self, name, data, scope="host"):
        path = presets.preset_path(name, scope, self.tmp)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh)
        return path

    def test_missing_version_migrates_to_1(self):
        # A v0.1/v0.2 file predates the field — it loads as v1.
        self._write_preset(HOST, {"name": HOST, "scope": "host",
                                  "endpoints": []})
        preset = presets.load_preset(HOST, base=self.tmp)
        self.assertIsNotNone(preset)
        self.assertEqual(preset["version"], presets.PRESET_VERSION)

    def test_future_version_is_refused(self):
        self._write_preset(HOST, {"name": HOST, "scope": "host",
                                  "version": 99, "endpoints": []})
        self.assertIsNone(presets.load_preset(HOST, base=self.tmp))
        self.assertEqual(presets.list_presets(base=self.tmp), [])

    def test_bad_files_are_refused(self):
        path = presets.preset_path(HOST, "host", self.tmp)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("not json")
        self.assertIsNone(presets.load_preset(HOST, base=self.tmp))
        with open(path, "w") as fh:
            json.dump(["not", "a", "dict"], fh)
        self.assertIsNone(presets.load_preset(HOST, base=self.tmp))

    def test_learned_preset_stamps_the_version(self):
        self._save_auth_flows()
        preset, _path, _created = presets.learn(HOST, base=self.tmp)
        self.assertEqual(preset["version"], presets.PRESET_VERSION)

    def _save_auth_flows(self):
        store.save_flows([
            auth_flow("f1", HOST, "/api/users/123"),
            auth_flow("f2", HOST, "/api/auth/login", method="POST",
                      status=200),
        ], base=self.tmp)


if __name__ == "__main__":
    unittest.main()
