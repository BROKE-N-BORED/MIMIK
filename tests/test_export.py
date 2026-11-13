"""Tests for pack/preset export-import (item 27)."""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # for test_presets helpers

from mimik import cli, export, presets, reblend_bridge, store
from test_presets import auth_flow, run_cmd

HOST = "game.example.com"


def _tree_bytes(root):
    """{relpath: bytes} for every file under root."""
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            full = os.path.join(dirpath, n)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = fh.read()
    return out


class ExportBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def _learn_preset(self):
        store.save_flows([
            auth_flow("f1", HOST, "/api/users/123"),
            auth_flow("f2", HOST, "/api/users/456"),
            auth_flow("f3", HOST, "/api/auth/login", method="POST",
                      status=200),
        ], base=self.tmp)
        preset, _path, _created = presets.learn(HOST, base=self.tmp)
        return preset

    def _make_pack(self, name, extra=None, built_at="2026-11-13"):
        """A fake built pack dir (pack.json + files) under packs/."""
        pdir = os.path.join(reblend_bridge.packs_dir(self.tmp), name)
        os.makedirs(os.path.join(pdir, "assets"), exist_ok=True)
        os.makedirs(os.path.join(pdir, "originals"), exist_ok=True)
        pack = {"app": "game", "host": HOST, "built_at": built_at,
                "assets": [], "materials": []}
        with open(os.path.join(pdir, "pack.json"), "w") as fh:
            json.dump(pack, fh, indent=2)
        with open(os.path.join(pdir, "assets", "a.txt"), "wb") as fh:
            fh.write(b"mesh bytes")
        with open(os.path.join(pdir, "originals", "b.bin"), "wb") as fh:
            fh.write(bytes(range(256)))
        if extra:
            for rel, data in extra.items():
                full = os.path.join(pdir, rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as fh:
                    fh.write(data)
        return pdir


class PresetExportTests(ExportBase):
    def test_preset_round_trip_exact(self):
        self._learn_preset()
        path = presets.preset_path(HOST, "host", self.tmp)
        with open(path, "rb") as fh:
            before = fh.read()
        dest = os.path.join(self.tmp, "preset.zip")
        export.export_preset(HOST, dest, self.tmp)
        self.assertTrue(os.path.isfile(dest))
        self.assertTrue(presets.delete_preset(HOST, base=self.tmp))
        self.assertFalse(os.path.isfile(path))
        result = export.import_export(dest, base=self.tmp)
        self.assertEqual(result["kind"], "preset")
        self.assertEqual(result["name"], HOST)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_pattern_preset_round_trip_exact(self):
        preset = {"name": "jwt-bearer", "scope": "pattern",
                  "display_name": "JWT Bearer",
                  "learned_from": [HOST],
                  "auth": {"type": "bearer", "header": "Authorization"}}
        presets._save_pattern_preset(preset, base=self.tmp)
        path = presets.pattern_preset_path("jwt-bearer", self.tmp)
        with open(path, "rb") as fh:
            before = fh.read()
        dest = os.path.join(self.tmp, "pattern.zip")
        export.export_preset("jwt-bearer", dest, self.tmp)
        os.remove(path)
        export.import_export(dest, base=self.tmp)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_export_missing_preset_raises(self):
        with self.assertRaises(export.ExportError):
            export.export_preset("nope.example.com",
                                 os.path.join(self.tmp, "x.zip"),
                                 self.tmp)

    def test_import_rejects_non_zip(self):
        bad = os.path.join(self.tmp, "bad.zip")
        with open(bad, "w") as fh:
            fh.write("not a zip")
        with self.assertRaises(export.ExportError):
            export.import_export(bad, base=self.tmp)

    def test_import_rejects_zip_without_marker(self):
        bad = os.path.join(self.tmp, "bad.zip")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("something.txt", "hello")
        with self.assertRaises(export.ExportError):
            export.import_export(bad, base=self.tmp)

    def test_import_rejects_traversal_entry(self):
        bad = os.path.join(self.tmp, "bad.zip")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr(export.MARKER, json.dumps(
                {"format": export.FORMAT, "version": 1,
                 "kind": "preset", "name": "evil"}))
            zf.writestr("preset/../../evil.json",
                        json.dumps({"name": "evil"}))
        with self.assertRaises(export.ExportError):
            export.import_export(bad, base=self.tmp)
        self.assertFalse(os.path.isfile(
            os.path.join(os.path.dirname(self.tmp), "evil.json")))

    def test_import_refuses_overwrite_without_force(self):
        self._learn_preset()
        dest = os.path.join(self.tmp, "preset.zip")
        export.export_preset(HOST, dest, self.tmp)
        with self.assertRaises(FileExistsError):
            export.import_export(dest, base=self.tmp)
        result = export.import_export(dest, base=self.tmp, overwrite=True)
        self.assertEqual(result["name"], HOST)

    def test_export_kind(self):
        self._learn_preset()
        pzip = os.path.join(self.tmp, "p.zip")
        export.export_preset(HOST, pzip, self.tmp)
        self.assertEqual(export.export_kind(pzip), "preset")
        self._make_pack("game_20261113-100000")
        kzip = os.path.join(self.tmp, "k.zip")
        export.export_pack("game_20261113-100000", kzip, self.tmp)
        self.assertEqual(export.export_kind(kzip), "pack")
        self.assertIsNone(export.export_kind(
            os.path.join(self.tmp, "missing.zip")))


class PackExportTests(ExportBase):
    def test_pack_round_trip_exact(self):
        pdir = self._make_pack("game_20261113-100000")
        before = _tree_bytes(pdir)
        dest = os.path.join(self.tmp, "pack.zip")
        export.export_pack("game_20261113-100000", dest, self.tmp)
        shutil.rmtree(pdir)
        result = export.import_export(dest, base=self.tmp)
        self.assertEqual(result["kind"], "pack")
        self.assertEqual(result["name"], "game_20261113-100000")
        self.assertEqual(_tree_bytes(pdir), before)

    def test_pack_export_finds_by_app_and_picks_newest(self):
        self._make_pack("game_20261113-100000", built_at="2026-11-13T10:00:00")
        self._make_pack("game_20261113-110000",
                        extra={"assets/new.txt": b"newest"},
                        built_at="2026-11-13T11:00:00")
        dest = os.path.join(self.tmp, "pack.zip")
        export.export_pack("game", dest, self.tmp)
        with zipfile.ZipFile(dest) as zf:
            marker = json.loads(zf.read(export.MARKER))
            self.assertEqual(marker["name"], "game_20261113-110000")
            self.assertIn("pack/assets/new.txt", zf.namelist())

    def test_pack_export_missing_raises(self):
        with self.assertRaises(export.ExportError):
            export.export_pack("nope", os.path.join(self.tmp, "x.zip"),
                               self.tmp)

    def test_import_rejects_pack_without_pack_json(self):
        bad = os.path.join(self.tmp, "bad.zip")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr(export.MARKER, json.dumps(
                {"format": export.FORMAT, "version": 1,
                 "kind": "pack", "name": "game_20261113-100000"}))
            zf.writestr("pack/assets/a.txt", "no manifest here")
        with self.assertRaises(export.ExportError):
            export.import_export(bad, base=self.tmp)

    def test_import_pack_force_replaces_wholesale(self):
        pdir = self._make_pack("game_20261113-100000",
                               extra={"assets/stale.txt": b"old"})
        dest = os.path.join(self.tmp, "pack.zip")
        export.export_pack("game_20261113-100000", dest, self.tmp)
        with self.assertRaises(FileExistsError):
            export.import_export(dest, base=self.tmp)
        # A second export of a slimmer pack with the same dir name
        # replaces the old one wholesale — the stale file must go.
        shutil.rmtree(pdir)
        slim = self._make_pack("game_20261113-100000")
        dest2 = os.path.join(self.tmp, "pack2.zip")
        export.export_pack("game_20261113-100000", dest2, self.tmp)
        os.makedirs(os.path.join(pdir, "assets"), exist_ok=True)
        with open(os.path.join(pdir, "assets", "stale.txt"), "wb") as fh:
            fh.write(b"stale")
        export.import_export(dest2, base=self.tmp, overwrite=True)
        self.assertFalse(os.path.isfile(
            os.path.join(pdir, "assets", "stale.txt")))
        self.assertEqual(_tree_bytes(pdir), _tree_bytes(slim))


class ExportCliTests(ExportBase):
    def test_presets_export_import_cli(self):
        self._learn_preset()
        dest = os.path.join(self.tmp, "preset.zip")
        out, code = run_cmd(cli.cmd_presets_export, argparse.Namespace(
            name=HOST, file=dest))
        self.assertEqual(code, 0)
        self.assertIn("Exported", out)
        presets.delete_preset(HOST, base=self.tmp)
        out, code = run_cmd(cli.cmd_presets_import, argparse.Namespace(
            file=dest, force=False))
        self.assertEqual(code, 0)
        self.assertIn("Imported", out)
        self.assertIsNotNone(presets.load_preset(HOST, base=self.tmp))
        # Second import without --force refuses — the refusal is the
        # SystemExit message (sys.exit(msg) carries it as the code).
        out, code = run_cmd(cli.cmd_presets_import, argparse.Namespace(
            file=dest, force=False))
        self.assertNotEqual(code, 0)
        self.assertIn("--force", str(code))
        out, code = run_cmd(cli.cmd_presets_import, argparse.Namespace(
            file=dest, force=True))
        self.assertEqual(code, 0)

    def test_pack_cli(self):
        self._make_pack("game_20261113-100000")
        out, code = run_cmd(cli.cmd_pack_list, argparse.Namespace())
        self.assertEqual(code, 0)
        self.assertIn("game_20261113-100000", out)
        dest = os.path.join(self.tmp, "pack.zip")
        out, code = run_cmd(cli.cmd_pack_export, argparse.Namespace(
            name="game", file=dest))
        self.assertEqual(code, 0)
        shutil.rmtree(os.path.join(
            reblend_bridge.packs_dir(self.tmp), "game_20261113-100000"))
        out, code = run_cmd(cli.cmd_pack_import, argparse.Namespace(
            file=dest, force=False))
        self.assertEqual(code, 0)
        self.assertIn("Imported", out)
        self.assertEqual(len(reblend_bridge.list_packs(self.tmp)), 1)


if __name__ == "__main__":
    unittest.main()
