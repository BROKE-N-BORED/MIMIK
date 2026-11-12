"""Tests for the assets + build-pack CLI commands."""
import argparse
import base64
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # for test_assets helpers

from mimik import cli, reblend_bridge
from test_assets import make_wav, read_t9, saved_flow

REBLEND_T9 = os.environ.get(
    "REBLEND_FIXTURE",
    os.path.join(os.path.expanduser("~"), "workspace",
                 "reblend-v02-t9-build", "reblend"))
HAS_REBLEND = reblend_bridge._is_reblend_dir(REBLEND_T9)
ICON_PNG = os.path.join(os.path.dirname(__file__), "..", "icons",
                        "mimik-48.png")


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


class AssetsCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def test_assets_empty_is_plain(self):
        args = argparse.Namespace(host=None, har=None, scan=None,
                                  refresh=False)
        out, code = run_cmd(cli.cmd_assets, args)
        self.assertEqual(code, 0)
        self.assertIn("no captured assets yet", out)

    @unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
    def test_assets_from_har_then_listed(self):
        har_path = os.path.join(self.tmp, "game.har")
        glb = base64.b64encode(read_t9("examples/sample.glb")).decode()
        with open(har_path, "w") as fh:
            json.dump({"log": {"entries": [
                {"request": {"method": "GET",
                            "url": "https://game.example.com/m/crate.glb",
                            "headers": []},
                 "response": {"status": 200, "headers": [],
                              "content": {"mimeType": "model/gltf-binary",
                                          "text": glb,
                                          "encoding": "base64"}}},
            ]}}, fh)
        args = argparse.Namespace(host=None, har=har_path, scan=None,
                                  refresh=False)
        out, code = run_cmd(cli.cmd_assets, args)
        self.assertEqual(code, 0)
        self.assertIn("1 asset(s) from the HAR", out)
        self.assertIn("crate.glb", out)
        self.assertIn("model", out)

    def test_assets_scan_folder(self):
        root = os.path.join(self.tmp, "game")
        os.makedirs(root)
        with open(os.path.join(root, "wall.png"), "wb") as fh:
            fh.write(b"\x89PNG-fake")
        args = argparse.Namespace(host=None, har=None, scan=root,
                                  refresh=False)
        out, code = run_cmd(cli.cmd_assets, args)
        self.assertEqual(code, 0)
        self.assertIn("1 asset(s) from", out)
        self.assertIn("wall.png", out)

    def test_assets_list_scanned_group_by_name(self):
        # Regression: a scanned program group has no host — its group
        # is named after the program, and `mimik assets <name>` must
        # still list it.
        root = os.path.join(self.tmp, "game")
        os.makedirs(root)
        with open(os.path.join(root, "wall.png"), "wb") as fh:
            fh.write(b"\x89PNG-fake")
        run_cmd(cli.cmd_assets, argparse.Namespace(
            host=None, har=None, scan=root, refresh=False))
        out, code = run_cmd(cli.cmd_assets, argparse.Namespace(
            host="game", har=None, scan=None, refresh=False))
        self.assertEqual(code, 0)
        self.assertIn("wall.png", out)
        self.assertNotIn("no captured assets", out)

    def test_assets_scan_missing_folder_exits(self):
        args = argparse.Namespace(host=None, har=None,
                                  scan=os.path.join(self.tmp, "nope"),
                                  refresh=False)
        _out, code = run_cmd(cli.cmd_assets, args)
        self.assertNotEqual(code, 0)

    @unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
    def test_assets_refresh_from_saved_captures(self):
        from mimik import store

        flows = [saved_flow("f1", "game.example.com", "/m/crate.glb",
                            "model/gltf-binary",
                            read_t9("examples/sample.glb"))]
        store.save_flows(flows)
        args = argparse.Namespace(host="game.example.com", har=None,
                                  scan=None, refresh=True)
        out, code = run_cmd(cli.cmd_assets, args)
        self.assertEqual(code, 0)
        self.assertIn("1 asset(s) from the saved captures", out)
        self.assertIn("crate.glb", out)


@unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
class BuildPackCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)
        from test_reblend_bridge import seed_game_assets

        seed_game_assets(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def test_build_pack_from_cli(self):
        args = argparse.Namespace(host="game.example.com",
                                  reblend=REBLEND_T9, out=None, scale=100.0)
        out, code = run_cmd(cli.cmd_build_pack, args)
        self.assertEqual(code, 0)
        self.assertIn("pack:", out)
        self.assertIn("models converted: 2", out)
        self.assertIn("scene:", out)
        self.assertIn("main.py --scene", out)

    def test_build_pack_scanned_group_by_name(self):
        # Regression: build-pack must accept a scanned program group's
        # name, not only a host.
        root = os.path.join(self.tmp, "mygame")
        os.makedirs(root)
        with open(os.path.join(root, "crate.glb"), "wb") as fh:
            fh.write(read_t9("examples/sample.glb"))
        out, code = run_cmd(cli.cmd_assets, argparse.Namespace(
            host=None, har=None, scan=root, refresh=False))
        self.assertEqual(code, 0)
        self.assertIn("1 asset(s) from", out)
        args = argparse.Namespace(host="mygame",
                                  reblend=REBLEND_T9, out=None, scale=100.0)
        out, code = run_cmd(cli.cmd_build_pack, args)
        self.assertEqual(code, 0)
        self.assertIn("pack:", out)
        self.assertIn("models converted: 1", out)
        self.assertIn("scene:", out)

    def test_build_pack_no_assets_exits(self):
        args = argparse.Namespace(host="nobody.example.com",
                                  reblend=REBLEND_T9, out=None, scale=100.0)
        out, code = run_cmd(cli.cmd_build_pack, args)
        # sys.exit("message") carries the text in the exception, not
        # on stdout — run_cmd returns it as the code.
        self.assertNotEqual(code, 0)
        self.assertIn("no captured assets", str(code))


if __name__ == "__main__":
    unittest.main()
