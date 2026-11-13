"""Tests for the mimik CLI — hosts/learn/gen against the fixture HAR."""
import argparse
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import cli

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mimik.har")


def run_cmd(func, args):
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            func(args)
    except SystemExit as e:
        return out.getvalue(), e.code
    return out.getvalue(), 0


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def test_hosts_from_har(self):
        args = argparse.Namespace(har=FIXTURE, curl=None)
        out, code = run_cmd(cli.cmd_hosts, args)
        self.assertEqual(code, 0)
        self.assertIn("api.example.com", out)
        self.assertIn("cdn.example.com", out)

    def test_learn_from_har(self):
        args = argparse.Namespace(host="api.example.com", har=FIXTURE,
                                  curl=None)
        out, code = run_cmd(cli.cmd_learn, args)
        self.assertEqual(code, 0)
        self.assertIn("4 endpoints", out)
        self.assertIn("POST  /v1/like", out)
        self.assertIn("GET   /v1/users", out)

    def test_learn_unknown_host_exits(self):
        args = argparse.Namespace(host="nope.example.com", har=FIXTURE,
                                  curl=None)
        out, code = run_cmd(cli.cmd_learn, args)
        self.assertNotEqual(code, 0)

    def test_gen_from_har_writes_client(self):
        out_path = os.path.join(self.tmp, "api_client.py")
        args = argparse.Namespace(host="api.example.com", har=FIXTURE,
                                  curl=None, out=out_path, model=None,
                                  polish="none", prompt_only=False)
        out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(out_path))
        with open(out_path) as f:
            src = f.read()
        compile(src, "<gen>", "exec")
        self.assertIn("class ExampleClient(App):", src)
        self.assertIn("def like(", src)
        self.assertNotIn("SECRETTOKEN123", src)
        self.assertIn("wrote", out)

    def test_gen_default_out_name(self):
        args = argparse.Namespace(host="api.example.com", har=FIXTURE,
                                  curl=None, out=None, model=None,
                                  polish="none", prompt_only=False)
        out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp,
                                        "api_example_com_client.py")))

    def test_default_out_uses_full_host(self):
        self.assertEqual(cli._default_out("api.foo.com"),
                         "api_foo_com_client.py")
        self.assertEqual(cli._default_out("api.bar.com"),
                         "api_bar_com_client.py")
        self.assertNotEqual(cli._default_out("api.foo.com"),
                            cli._default_out("api.bar.com"))
        self.assertEqual(cli._default_out("192.168.1.5"),
                         "192_168_1_5_client.py")
        self.assertEqual(cli._default_out("API.Example.COM"),
                         "api_example_com_client.py")

    def test_unique_out_numbers_on_collision(self):
        open(os.path.join(self.tmp, "api_foo_com_client.py"), "w").write("x")
        self.assertEqual(cli._unique_out("api.foo.com", self.tmp),
                         "api_foo_com_client_2.py")
        open(os.path.join(self.tmp, "api_foo_com_client_2.py"), "w").write("x")
        self.assertEqual(cli._unique_out("api.foo.com", self.tmp),
                         "api_foo_com_client_3.py")
        self.assertEqual(cli._unique_out("api.bar.com", self.tmp),
                         "api_bar_com_client.py")

    def test_gen_twice_writes_numbered_copy(self):
        args = argparse.Namespace(host="api.example.com", har=FIXTURE,
                                  curl=None, out=None, model=None,
                                  polish="none", prompt_only=False)
        out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        first = os.path.join(self.tmp, "api_example_com_client.py")
        second = os.path.join(self.tmp, "api_example_com_client_2.py")
        with open(first) as f:
            original = f.read()
        out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(second))
        with open(first) as f:
            self.assertEqual(f.read(), original)

    def test_gen_prompt_only(self):
        args = argparse.Namespace(host="api.example.com", har=FIXTURE,
                                  curl=None, out=None, model=None,
                                  polish="none", prompt_only=True)
        out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        self.assertIn("mimik.App", out)
        self.assertIn("### POST /v1/like", out)

    def test_gen_from_curl(self):
        curl_text = (
            "curl 'https://api.example.com/v1/like' -X POST "
            "-H 'Authorization: Bearer SECRETTOKEN123' "
            "--data '{\"subjectId\": \"abc\"}'"
        )
        args = argparse.Namespace(host="api.example.com", har=None,
                                  curl=curl_text, out=None, model=None,
                                  polish="none", prompt_only=False)
        out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(
            self.tmp, "api_example_com_client.py")))

    def test_hosts_from_curl(self):
        curl_text = "curl 'https://api.example.com/v1/x' -H 'A: b'"
        args = argparse.Namespace(har=None, curl=curl_text)
        out, code = run_cmd(cli.cmd_hosts, args)
        self.assertEqual(code, 0)
        self.assertIn("api.example.com", out)

    def test_clients_lists_generated_and_saved(self):
        from mimik import store

        store.save_client_copy("api_client.py", "x=1\n", base=self.tmp)
        store.save_flows([{
            "id": "1",
            "request": {"host": "api.example.com", "method": "GET",
                        "path": "/x", "scheme": "https", "port": 443,
                        "headers": []},
            "response": {"status_code": 200},
        }], base=self.tmp)
        out, code = run_cmd(cli.cmd_clients, argparse.Namespace())
        self.assertEqual(code, 0)
        self.assertIn("api_client.py", out)
        self.assertIn("api.example.com", out)

    def test_prune_with_yes_deletes_old_saves(self):
        from mimik import store

        def _flow(fid):
            return {"id": fid,
                    "request": {"host": "api.example.com", "method": "GET",
                                "path": "/x", "scheme": "https",
                                "port": 443, "headers": []},
                    "response": {"status_code": 200}}

        stamps = ["20260101-12000%d" % i for i in range(1, 4)]
        with mock.patch.object(store, "_stamp", side_effect=stamps):
            for i in range(3):
                store.save_flows([_flow(str(i))], base=self.tmp)
        args = argparse.Namespace(keep=1, yes=True)
        out, code = run_cmd(cli.cmd_prune, args)
        self.assertEqual(code, 0)
        self.assertIn("deleted 2 save(s)", out)
        self.assertEqual(len(store.load_host("api.example.com",
                                             base=self.tmp)), 1)

    def test_prune_declined_keeps_files(self):
        from mimik import store

        def _flow(fid):
            return {"id": fid,
                    "request": {"host": "api.example.com", "method": "GET",
                                "path": "/x", "scheme": "https",
                                "port": 443, "headers": []},
                    "response": {"status_code": 200}}

        stamps = ["20260101-12000%d" % i for i in range(1, 3)]
        with mock.patch.object(store, "_stamp", side_effect=stamps):
            for i in range(2):
                store.save_flows([_flow(str(i))], base=self.tmp)
        args = argparse.Namespace(keep=1, yes=False)
        with mock.patch("builtins.input", return_value="n"):
            out, code = run_cmd(cli.cmd_prune, args)
        self.assertEqual(code, 0)
        self.assertIn("kept everything", out)
        self.assertEqual(len(store.load_host("api.example.com",
                                             base=self.tmp)), 2)

    def test_prune_nothing_to_do(self):
        args = argparse.Namespace(keep=20, yes=True)
        out, code = run_cmd(cli.cmd_prune, args)
        self.assertEqual(code, 0)
        self.assertIn("nothing to prune", out)

    def test_doctor_reports_missing_proxy(self):
        with mock.patch("mimik.doctor.mitmweb_cmd", return_value=None):
            out, code = run_cmd(cli.cmd_doctor, argparse.Namespace())
        self.assertEqual(code, 1)
        self.assertIn("MIMIK setup check", out)
        self.assertIn("MISSING", out)
        self.assertIn("LAN IP", out)

    def test_doctor_passes_when_ready(self):
        fake_pyside = mock.Mock()
        with mock.patch.dict(sys.modules, {"PySide6": fake_pyside}), \
             mock.patch("mimik.doctor.mitmweb_cmd",
                               return_value=["mitmweb"]), \
             mock.patch("mimik.sources.mitm.Mitm") as mock_mitm:
            mock_mitm.return_value.flows.return_value = []
            out, code = run_cmd(cli.cmd_doctor, argparse.Namespace())
        self.assertEqual(code, 0)

    def test_record_web_prints_steps_and_exits(self):
        args = argparse.Namespace(target="web", file=None, pick=None,
                                  deb_install=False, launch=None)
        out, code = run_cmd(cli.cmd_record, args)
        self.assertEqual(code, 0)
        self.assertIn("HAR", out)
        self.assertIn("cURL", out)

    def test_record_file_needs_file(self):
        args = argparse.Namespace(target="file", file=None, pick=None,
                                  deb_install=False, launch=None)
        out, code = run_cmd(cli.cmd_record, args)
        self.assertNotEqual(code, 0)

    def test_record_windows_without_wine_exits(self):
        args = argparse.Namespace(target="windows", file=None, pick=None,
                                  deb_install=False, launch=None)
        with mock.patch("mimik.wine.check_ready",
                        return_value=(False, "wine is not installed")):
            out, code = run_cmd(cli.cmd_record, args)
        self.assertNotEqual(code, 0)
        # sys.exit(msg) carries the message as the exit code, not stdout
        self.assertIn("wine is not installed", str(code))

    def test_record_screenshot_flag_parses(self):
        with mock.patch.object(cli, "cmd_record") as record_cmd:
            cli.main(["record", "--target", "android", "--screenshot"])
        record_cmd.assert_called_once()
        self.assertTrue(record_cmd.call_args[0][0].screenshot)

    def test_record_with_screenshot_calls_capture_before_stop(self):
        # v0.4 item 15: --screenshot takes the shot in the finally,
        # before the launched program is stopped.
        args = argparse.Namespace(target="android", file=None, pick=None,
                                  deb_install=False, launch=None,
                                  screenshot=True)
        calls = []
        session = mock.Mock()
        session.run_forever.return_value = []
        with mock.patch.dict(os.environ,
                             {"MIMIK_DATA_DIR": self.tmp}), \
             mock.patch.object(cli, "ProxySession",
                               return_value=session), \
             mock.patch.object(cli.screenshot, "capture_for_session",
                               side_effect=lambda: calls.append("capture")
                               or "/tmp/shot.png"), \
             mock.patch.object(cli.launch, "stop_process",
                               side_effect=lambda p: calls.append("stop")):
            out, code = run_cmd(cli.cmd_record, args)
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["capture", "stop"])
        self.assertIn("screenshot saved: /tmp/shot.png", out)

    def test_record_without_screenshot_takes_none(self):
        args = argparse.Namespace(target="android", file=None, pick=None,
                                  deb_install=False, launch=None,
                                  screenshot=False)
        session = mock.Mock()
        session.run_forever.return_value = []
        with mock.patch.dict(os.environ,
                             {"MIMIK_DATA_DIR": self.tmp}), \
             mock.patch.object(cli, "ProxySession",
                               return_value=session), \
             mock.patch.object(cli.screenshot, "capture_for_session") \
             as capture:
            out, code = run_cmd(cli.cmd_record, args)
        self.assertEqual(code, 0)
        capture.assert_not_called()
        self.assertNotIn("screenshot", out)

    def test_main_parses_gen(self):
        with mock.patch.object(cli, "cmd_gen") as gen:
            cli.main(["gen", "api.example.com", "--har", FIXTURE,
                      "--polish", "none"])
        gen.assert_called_once()
        self.assertEqual(gen.call_args[0][0].host, "api.example.com")
        self.assertEqual(gen.call_args[0][0].polish, "none")

    def test_main_parses_diff(self):
        with mock.patch.object(cli, "cmd_diff") as diff_cmd:
            cli.main(["diff", "a.example.com", "b.example.com"])
        diff_cmd.assert_called_once()
        self.assertEqual(diff_cmd.call_args[0][0].a, "a.example.com")
        self.assertEqual(diff_cmd.call_args[0][0].b, "b.example.com")

    def test_diff_two_hosts(self):
        from mimik import store

        store.save_flows([{
            "id": "1",
            "request": {"host": "a.example.com", "method": "GET",
                        "path": "/api/old", "scheme": "https",
                        "port": 443, "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])
        store.save_flows([{
            "id": "2",
            "request": {"host": "b.example.com", "method": "GET",
                        "path": "/api/new", "scheme": "https",
                        "port": 443, "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])
        args = argparse.Namespace(a="a.example.com", b="b.example.com")
        out, code = run_cmd(cli.cmd_diff, args)
        self.assertEqual(code, 0)
        self.assertIn("Capture diff: a.example.com -> b.example.com", out)
        self.assertIn("--- New ---", out)
        self.assertIn("GET /api/new", out)
        self.assertIn("--- Removed ---", out)
        self.assertIn("GET /api/old", out)

    def test_diff_unknown_host_exits_with_message(self):
        args = argparse.Namespace(a="nobody.example.com",
                                  b="b.example.com")
        out, code = run_cmd(cli.cmd_diff, args)
        self.assertIn("no captures for host: nobody.example.com",
                      str(code))

    def test_main_parses_unpin(self):
        with mock.patch.object(cli, "cmd_unpin") as unpin_cmd:
            cli.main(["unpin", "com.example.app", "--android",
                      "--proxy-host", "192.168.1.5"])
        unpin_cmd.assert_called_once()
        args = unpin_cmd.call_args[0][0]
        self.assertEqual(args.target, "com.example.app")
        self.assertEqual(args.platform, "android")
        self.assertEqual(args.proxy_host, "192.168.1.5")

    def test_unpin_prints_the_plan(self):
        from mimik import unpin as unpin_mod

        plan = {"kind": "android", "scripts": [],
                "command": "frida -U \\\n    -f com.example.app",
                "steps": ["Run this to launch the app:",
                          "Use the app."],
                "notes": ["Own apps only."],
                "patched_apk": None, "proxy_host": "192.168.1.5"}
        args = argparse.Namespace(target="com.example.app",
                                  platform="android", ca=None,
                                  proxy_host="192.168.1.5",
                                  workdir=None)
        with mock.patch.object(unpin_mod, "prepare",
                               return_value=plan) as prep:
            out, code = run_cmd(cli.cmd_unpin, args)
        self.assertEqual(code, 0)
        prep.assert_called_once()
        self.assertIn("Android app, rooted", out)
        self.assertIn("frida -U", out)
        self.assertIn("192.168.1.5:8080", out)

    def test_unpin_error_exits_with_message(self):
        from mimik import unpin as unpin_mod

        args = argparse.Namespace(target="com.example.app",
                                  platform=None, ca=None,
                                  proxy_host=None, workdir=None)
        with mock.patch.object(unpin_mod, "prepare",
                               side_effect=unpin_mod.UnpinError(
                                   "no mitmproxy CA")):
            out, code = run_cmd(cli.cmd_unpin, args)
        self.assertIn("no mitmproxy CA", str(code))


class DeleteCliTests(unittest.TestCase):
    """Item 5: `mimik delete` + disk usage in `mimik doctor`."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def _flow(self, fid, host="api.example.com"):
        return {"id": fid,
                "request": {"host": host, "method": "GET", "path": "/x",
                            "scheme": "https", "port": 443, "headers": []},
                "response": {"status_code": 200}}

    def _args(self, **kw):
        base = {"host": None, "client": None, "group": None, "pack": None,
                "yes": True}
        base.update(kw)
        return argparse.Namespace(**base)

    def test_delete_host_with_yes(self):
        from mimik import store

        store.save_flows([self._flow("1")], base=self.tmp)
        out, code = run_cmd(cli.cmd_delete, self._args(host="api.example.com"))
        self.assertEqual(code, 0)
        self.assertIn("deleted 1 save file", out)
        self.assertIn("freed", out)
        self.assertEqual(store.load_host("api.example.com",
                                         base=self.tmp), [])

    def test_delete_host_declined_keeps_files(self):
        from mimik import store

        store.save_flows([self._flow("1")], base=self.tmp)
        with mock.patch("builtins.input", return_value="n"):
            out, code = run_cmd(
                cli.cmd_delete,
                self._args(host="api.example.com", yes=False))
        self.assertEqual(code, 0)
        self.assertIn("kept it", out)
        self.assertEqual(len(store.load_host("api.example.com",
                                             base=self.tmp)), 1)

    def test_delete_host_missing_exits(self):
        out, code = run_cmd(cli.cmd_delete,
                            self._args(host="nobody.example.com"))
        self.assertNotEqual(code, 0)
        # sys.exit(msg) carries the message as the exit code.
        self.assertIn("no saved captures", str(code))

    def test_delete_client_without_suffix(self):
        from mimik import store

        path = os.path.join(self.tmp, "x_client.py")
        with open(path, "w") as fh:
            fh.write("# client\n")
        store.save_client_copy("x_client.py", path, base=self.tmp)
        out, code = run_cmd(cli.cmd_delete, self._args(client="x"))
        self.assertEqual(code, 0)
        self.assertIn("deleted x_client.py", out)
        self.assertEqual(store.list_clients(base=self.tmp), [])

    def test_delete_group_with_yes(self):
        from mimik import assets

        group = assets.AssetGroup("game.example.com", "traffic",
                                  host="game.example.com")
        group.add_bytes(b"\x89PNG fake", "icon.png", "image")
        gdir, _m = group.finish()
        out, code = run_cmd(cli.cmd_delete,
                            self._args(group="game.example.com"))
        self.assertEqual(code, 0)
        self.assertIn("deleted 1 asset group", out)
        self.assertFalse(os.path.exists(gdir))

    def test_delete_pack_with_yes(self):
        import json
        from mimik import reblend_bridge

        pdir = os.path.join(reblend_bridge.packs_dir(),
                            "game_20260101-000000")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "pack.json"), "w") as fh:
            json.dump({"app": "game", "host": "game.example.com",
                       "group": "game", "built_at": "2026-01-01"}, fh)
        out, code = run_cmd(cli.cmd_delete, self._args(pack="game"))
        self.assertEqual(code, 0)
        self.assertIn("deleted 1 pack", out)
        self.assertFalse(os.path.exists(pdir))

    def test_doctor_shows_disk_usage(self):
        from mimik import store

        store.save_flows([self._flow("1")], base=self.tmp)
        with mock.patch("mimik.doctor.mitmweb_cmd", return_value=None):
            out, code = run_cmd(cli.cmd_doctor, argparse.Namespace())
        self.assertEqual(code, 1)  # proxy missing, but usage still prints
        self.assertIn("disk usage:", out)
        self.assertIn("captures", out)


class ImportCliTests(unittest.TestCase):
    """Item 8: `mimik import` persists HAR/cURL traffic; malformed
    input gets a plain message, never a traceback."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def _bad_har(self):
        path = os.path.join(self.tmp, "bad.har")
        with open(path, "w") as fh:
            fh.write("{not json")
        return path

    def test_import_har_persists(self):
        from mimik import store

        args = argparse.Namespace(har=FIXTURE, curl=None)
        out, code = run_cmd(cli.cmd_import, args)
        self.assertEqual(code, 0)
        self.assertIn("imported 5 flow(s)", out)
        self.assertIn("api.example.com", out)
        # After a "restart" (no --har): the hosts come from the store.
        out, code = run_cmd(cli.cmd_hosts,
                            argparse.Namespace(har=None, curl=None))
        self.assertEqual(code, 0)
        self.assertIn("api.example.com", out)
        self.assertIn("cdn.example.com", out)
        self.assertEqual(len(store.load_host("api.example.com")), 4)

    def test_import_har_twice_is_idempotent(self):
        args = argparse.Namespace(har=FIXTURE, curl=None)
        run_cmd(cli.cmd_import, args)
        out, code = run_cmd(cli.cmd_import, args)
        self.assertEqual(code, 0)
        self.assertIn("nothing new", out)

    def test_import_curl_persists(self):
        from mimik import store

        args = argparse.Namespace(
            har=None,
            curl="curl 'https://api.example.com/v1/x' "
                 "-H 'Authorization: Bearer t'")
        out, code = run_cmd(cli.cmd_import, args)
        self.assertEqual(code, 0)
        self.assertIn("imported 1 flow", out)
        self.assertEqual(len(store.load_host("api.example.com")), 1)

    def test_import_malformed_har_friendly(self):
        args = argparse.Namespace(har=self._bad_har(), curl=None)
        out, code = run_cmd(cli.cmd_import, args)
        self.assertNotEqual(code, 0)
        self.assertIn("not valid JSON", str(code))

    def test_hosts_har_malformed_friendly(self):
        out, code = run_cmd(cli.cmd_hosts,
                            argparse.Namespace(har=self._bad_har(),
                                               curl=None))
        self.assertNotEqual(code, 0)
        self.assertIn("not valid JSON", str(code))

    def test_hosts_curl_no_url_friendly(self):
        out, code = run_cmd(cli.cmd_hosts,
                            argparse.Namespace(har=None,
                                               curl="curl -H 'x: y'"))
        self.assertNotEqual(code, 0)
        self.assertIn("no URL found", str(code))


class VersionTests(unittest.TestCase):
    """Item 20: --version + one version source."""

    def test_version_flag_prints_version(self):
        from mimik import __version__

        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        # Derived from the package version, not hardcoded — a version
        # bump must not break this test.
        self.assertEqual(out.getvalue().strip(), f"mimik {__version__}")

    def test_version_matches_pyproject(self):
        # The runtime version and the packaged version must not drift
        # (__init__ still said 0.2.0 when pyproject was already 0.3.0).
        import tomllib

        from mimik import __version__

        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "pyproject.toml"), "rb") as f:
            project = tomllib.load(f)["project"]
        self.assertEqual(__version__, project["version"])


if __name__ == "__main__":
    unittest.main()
