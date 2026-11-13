"""Tests for the MIMIK–Reblend bridge.

Conversion tests use the Reblend T9 tree as the fixture install
(REBLEND_FIXTURE env, else the standard build path) and verify
through Reblend's OWN loaders (load_mesh, load_scene) — a pack that
Reblend itself cannot read is a failed pack. Tests skip cleanly
when no Reblend fixture is present.
"""
import base64
import json
import os
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # for test_assets helpers

from mimik import assets, reblend_bridge
from test_assets import (icon_png_bytes, icon_png_path, make_wav, read_t9,
                         saved_flow)

REBLEND_T9 = os.environ.get(
    "REBLEND_FIXTURE",
    os.path.join(os.path.expanduser("~"), "workspace",
                 "reblend-v02-t9-build", "reblend"))
HAS_REBLEND = reblend_bridge._is_reblend_dir(REBLEND_T9)


def seed_game_assets(base):
    """A capture group for game.example.com: real .glb + .obj + PNG +
    WAV + a .gltf stub + a font stub. Returns the group dir."""
    png = icon_png_bytes()
    wav_path = os.path.join(base, "sfx.wav")
    make_wav(wav_path)
    with open(wav_path, "rb") as fh:
        wav = fh.read()
    gltf = json.dumps({"asset": {"version": "2.0"},
                       "buffers": [{"uri": "model.bin"}]}).encode("utf-8")
    flows = [
        saved_flow("f1", "game.example.com", "/m/crate.glb",
                   "model/gltf-binary", read_t9("examples/sample.glb")),
        saved_flow("f2", "game.example.com", "/m/house.obj",
                   "text/plain", read_t9("examples/house.obj")),
        saved_flow("f3", "game.example.com", "/i/logo.png",
                   "image/png", png),
        saved_flow("f4", "game.example.com", "/s/jump.wav",
                   "audio/wav", wav),
        saved_flow("f5", "game.example.com", "/m/char.gltf",
                   "model/gltf+json", gltf),
        saved_flow("f6", "game.example.com", "/f/ui.woff2",
                   "font/woff2", b"wOF2-fake-font-bytes"),
    ]
    gdir, _m = assets.extract_from_flows(
        flows, "game.example.com", host="game.example.com", base=base)
    return gdir


class FindReblendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self._env = dict(os.environ)
        os.environ.pop("REBLEND_PATH", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        del os.environ["MIMIK_DATA_DIR"]

    def test_explicit_valid_and_invalid(self):
        if HAS_REBLEND:
            self.assertEqual(reblend_bridge.find_reblend(REBLEND_T9),
                             os.path.abspath(REBLEND_T9))
        self.assertIsNone(reblend_bridge.find_reblend(self.tmp))

    def test_env_override(self):
        if not HAS_REBLEND:
            self.skipTest("no Reblend fixture")
        os.environ["REBLEND_PATH"] = REBLEND_T9
        self.assertEqual(reblend_bridge.find_reblend(),
                         os.path.abspath(REBLEND_T9))

    def test_saved_setting_round_trip(self):
        if not HAS_REBLEND:
            self.skipTest("no Reblend fixture")
        ok, _msg = reblend_bridge.set_reblend_path(REBLEND_T9)
        self.assertTrue(ok)
        self.assertEqual(reblend_bridge.get_saved_path(),
                         os.path.abspath(REBLEND_T9))
        self.assertEqual(reblend_bridge.find_reblend(),
                         os.path.abspath(REBLEND_T9))

    def test_set_invalid_path_refused(self):
        ok, msg = reblend_bridge.set_reblend_path(self.tmp)
        self.assertFalse(ok)
        self.assertIn("does not look like a Reblend folder", msg)

    def test_missing_install_returns_none(self):
        with mock.patch.object(reblend_bridge, "_AUTO_GLOBS", []):
            self.assertIsNone(reblend_bridge.find_reblend())


@unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
class LoadTests(unittest.TestCase):
    def test_load_light_engine(self):
        le = reblend_bridge.load_light_engine(REBLEND_T9)
        self.assertEqual(le.version, "0.2.0")
        self.assertTrue(hasattr(le.meshio, "import_mesh"))
        self.assertTrue(hasattr(le.meshfile, "save_mesh"))
        self.assertTrue(hasattr(le.scene, "load_scene"))
        # Second load returns the same modules, no re-import.
        self.assertIs(reblend_bridge.load_light_engine(REBLEND_T9), le)


@unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
class ConvertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.le = reblend_bridge.load_light_engine(REBLEND_T9)

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_glb_converts_and_loads(self):
        out = os.path.join(self.tmp, "assets")
        os.makedirs(out)
        result = reblend_bridge.convert_model(
            os.path.join(REBLEND_T9, "examples", "sample.glb"),
            out, 100.0, self.le, set())
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["objects"]), 2)
        self.assertEqual(result["triangles"], 18)
        for obj in result["objects"]:
            mesh = self.le.meshfile.load_mesh(
                os.path.join(out, os.path.basename(obj["mesh_file"])))
            self.assertGreater(mesh["triangle_count"], 0)

    def test_obj_converts_and_loads(self):
        out = os.path.join(self.tmp, "assets")
        os.makedirs(out)
        result = reblend_bridge.convert_model(
            os.path.join(REBLEND_T9, "examples", "house.obj"),
            out, 100.0, self.le, set())
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["objects"]), 2)
        names = {o["name"] for o in result["objects"]}
        self.assertEqual(names, {"Walls", "Roof"})
        for obj in result["objects"]:
            self.le.meshfile.load_mesh(
                os.path.join(out, os.path.basename(obj["mesh_file"])))

    def test_bad_model_is_a_recorded_failure(self):
        bad = os.path.join(self.tmp, "bad.glb")
        with open(bad, "wb") as fh:
            fh.write(b"not a glb at all")
        out = os.path.join(self.tmp, "assets")
        os.makedirs(out)
        result = reblend_bridge.convert_model(bad, out, 100.0,
                                              self.le, set())
        self.assertFalse(result["ok"])
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["objects"], [])

    def test_heavy_mesh_warns_never_refuses(self):
        tiny_mesh = {"positions": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
                                   0.0, 1.0, 0.0],
                     "normals": [0.0, 0.0, 1.0] * 3,
                     "uvs": [0.0, 0.0] * 3,
                     "indices": [0, 1, 2]}
        fake_meshio = types.SimpleNamespace(
            import_mesh=lambda path, scale=100.0: (
                [{"name": "Heavy", "mesh": tiny_mesh,
                  "material": {"base_color": "#ff0000"}}],
                {"objects": 1, "triangles": 2_000_000, "warnings": []}),
            is_heavy=lambda n: n > 1_000_000,
            unique_asset_name=self.le.meshio.unique_asset_name)
        fake_le = types.SimpleNamespace(meshio=fake_meshio,
                                        meshfile=self.le.meshfile)
        out = os.path.join(self.tmp, "assets")
        os.makedirs(out)
        src = os.path.join(self.tmp, "heavy.glb")
        with open(src, "wb") as fh:
            fh.write(b"fake")
        result = reblend_bridge.convert_model(src, out, 100.0, fake_le,
                                              set())
        self.assertTrue(result["ok"])
        self.assertTrue(any("heavy" in w for w in result["warnings"]))


class ConvertFailureTests(unittest.TestCase):
    """convert_model with a fake light_engine — no Reblend fixture."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_unexpected_import_error_is_a_recorded_failure(self):
        # Review #20: a malformed .glb can raise struct.error /
        # KeyError / IndexError from the importer — the contract is
        # "a failed import is a recorded failure, never an exception".
        for exc in (struct.error("bad chunk"),
                    KeyError("chunk"), IndexError("chunk")):
            fake_meshio = types.SimpleNamespace(
                import_mesh=mock.Mock(side_effect=exc))
            fake_le = types.SimpleNamespace(meshio=fake_meshio,
                                            meshfile=None)
            out = os.path.join(self.tmp, "assets")
            os.makedirs(out, exist_ok=True)
            result = reblend_bridge.convert_model(
                os.path.join(self.tmp, "bad.glb"), out, 100.0, fake_le,
                set())
            self.assertFalse(result["ok"])
            self.assertIsNotNone(result["error"])
            self.assertEqual(result["objects"], [])


class ColorTests(unittest.TestCase):
    def test_svg_falls_back_to_gray(self):
        color, note = reblend_bridge.average_color("x.svg")
        self.assertEqual(color, reblend_bridge.NEUTRAL_GRAY)
        self.assertIn("SVG", note)

    def test_missing_file_falls_back_to_gray(self):
        color, note = reblend_bridge.average_color("/no/such/file.png")
        self.assertEqual(color, reblend_bridge.NEUTRAL_GRAY)
        self.assertIsNotNone(note)

    @unittest.skipUnless(
        os.environ.get("MIMIK_TEST_QIMAGE") == "1",
        "QImage path tested under the Qt env only")
    def test_qimage_average_color(self):
        color, note = reblend_bridge.average_color(icon_png_path())
        self.assertIsNone(note)
        self.assertRegex(color, r"^#[0-9a-f]{6}$")
        self.assertNotEqual(color, reblend_bridge.NEUTRAL_GRAY)


@unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self._env = dict(os.environ)
        os.environ.pop("REBLEND_PATH", None)
        seed_game_assets(self.tmp)
        self.le = reblend_bridge.load_light_engine(REBLEND_T9)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        del os.environ["MIMIK_DATA_DIR"]

    def test_full_pack_end_to_end(self):
        result = reblend_bridge.build_pack(host="game.example.com",
                                           reblend=REBLEND_T9)
        pack_dir = result["pack_dir"]
        self.assertEqual(result["models_converted"], 2)
        self.assertEqual(result["counts"]["model"], 3)  # glb + obj + gltf
        self.assertEqual(result["counts"]["image"], 1)
        self.assertEqual(result["counts"]["audio"], 1)
        self.assertEqual(result["counts"]["font"], 1)

        # Scene loads through Reblend's own loader, with real meshes.
        self.assertIsNotNone(result["scene_path"])
        scene = self.le.scene.load_scene(result["scene_path"])
        self.assertEqual(scene["version"], 2)
        meshes = [o for o in scene["objects"]
                  if o.get("shape") == "mesh"]
        self.assertEqual(len(meshes), 4)  # Crate + Pyramid + Walls + Roof
        for obj in meshes:
            mesh_path = os.path.join(pack_dir, obj["mesh_file"])
            self.assertTrue(os.path.isfile(mesh_path), obj["mesh_file"])
            loaded = self.le.meshfile.load_mesh(mesh_path)
            self.assertGreater(loaded["triangle_count"], 0)
        names = {o["name"] for o in meshes}
        self.assertEqual(names, {"Crate", "Pyramid", "Walls", "Roof"})
        # The .glb node transform rides on the object, as in Reblend.
        crate = [o for o in meshes if o["name"] == "Crate"][0]
        self.assertNotEqual(crate["position"], [0.0, 0.0, 0.0])

        # Images: original in textures/ + color-only material in pack.json.
        self.assertEqual(len(result["materials"]), 1)
        mat = result["materials"][0]
        self.assertRegex(mat["base_color"], r"^#[0-9a-f]{6}$")
        self.assertIn("color only", mat["note"])
        self.assertTrue(os.path.isfile(
            os.path.join(pack_dir, mat["source"])))

        # pack.json tells the truth about the rest.
        with open(os.path.join(pack_dir, "pack.json")) as fh:
            pack = json.load(fh)
        self.assertEqual(pack["reblend_version"], "0.2.0")
        by_name = {a["name"]: a for a in pack["assets"]}
        self.assertIn("not wired into a Reblend scene",
                      by_name["jump.wav"]["note"])
        self.assertIn("not wired into a Reblend scene",
                      by_name["ui.woff2"]["note"])
        self.assertIn("needs .glb", by_name["char.gltf"]["note"])
        self.assertTrue(os.path.isfile(
            os.path.join(pack_dir, by_name["jump.wav"]["original"])))
        self.assertTrue(os.path.isfile(
            os.path.join(pack_dir, by_name["char.gltf"]["original"])))

    def test_pack_with_malformed_model_completes(self):
        # Review #20: an importer raising struct.error (not OSError /
        # ValueError) must not escape build_pack — the model lands as
        # a recorded failure and the pack still completes.
        group = assets.AssetGroup("badgame", "traffic",
                                  host="bad.example.com")
        group.add_bytes(b"not a glb at all", "bad.glb", "model")
        group.finish()
        fake_meshio = types.SimpleNamespace(
            import_mesh=mock.Mock(
                side_effect=struct.error("bad chunk")))
        fake_le = types.SimpleNamespace(meshio=fake_meshio,
                                        meshfile=None,
                                        version="fake-0.0")
        with mock.patch.object(reblend_bridge, "load_light_engine",
                               return_value=fake_le):
            result = reblend_bridge.build_pack(host="bad.example.com",
                                               reblend=REBLEND_T9)
        self.assertEqual(result["models_converted"], 0)
        by_name = {a["name"]: a for a in result["pack"]["assets"]}
        self.assertFalse(by_name["bad.glb"]["ok"])
        self.assertIsNotNone(by_name["bad.glb"]["error"])
        self.assertTrue(os.path.isfile(
            os.path.join(result["pack_dir"], "pack.json")))

    def test_pack_keeps_new_kinds_as_originals(self):
        # Item 24: video + texture + fbx/dae land in the pack as kept
        # originals with honest notes — the pack still completes.
        group = assets.AssetGroup("newkinds", "traffic",
                                  host="newkinds.example.com")
        group.add_bytes(b"ftyp-fake", "intro.mp4", "video")
        group.add_bytes(b"DDS-fake", "diffuse.dds", "texture")
        group.add_bytes(b"Kaydara-fake", "char.fbx", "model")
        group.add_bytes(b"<?xml-fake", "level.dae", "model")
        group.finish()
        result = reblend_bridge.build_pack(host="newkinds.example.com",
                                           reblend=REBLEND_T9)
        self.assertEqual(result["counts"]["video"], 1)
        self.assertEqual(result["counts"]["texture"], 1)
        self.assertEqual(result["counts"]["model"], 2)
        by_name = {a["name"]: a for a in result["pack"]["assets"]}
        self.assertIn("no video support", by_name["intro.mp4"]["note"])
        self.assertIn("convert to PNG", by_name["diffuse.dds"]["note"])
        self.assertIn("needs .glb", by_name["char.fbx"]["note"])
        self.assertIn("needs .glb", by_name["level.dae"]["note"])
        for name in ("intro.mp4", "diffuse.dds", "char.fbx", "level.dae"):
            self.assertTrue(os.path.isfile(
                os.path.join(result["pack_dir"],
                             by_name[name]["original"])),
                name)

    def test_pack_without_reblend_keeps_originals(self):
        with mock.patch.object(reblend_bridge, "_AUTO_GLOBS", []):
            result = reblend_bridge.build_pack(host="game.example.com")
        self.assertIsNone(result["reblend_path"])
        self.assertIsNone(result["scene_path"])
        self.assertEqual(result["models_converted"], 0)
        self.assertTrue(any("Reblend not found" in n
                            for n in result["notes"]))
        # Originals still assembled.
        self.assertTrue(os.path.isdir(
            os.path.join(result["pack_dir"], "originals")))
        self.assertTrue(os.path.isdir(
            os.path.join(result["pack_dir"], "textures")))
        with open(os.path.join(result["pack_dir"], "pack.json")) as fh:
            pack = json.load(fh)
        self.assertIsNone(pack["scene"])
        self.assertIsNone(pack["reblend_version"])

    def test_pack_dedupes_same_bytes(self):
        # Capture the same .glb a second time under another URL.
        flows = [saved_flow("f7", "game.example.com", "/m/crate2.glb",
                            "model/gltf-binary",
                            read_t9("examples/sample.glb"))]
        assets.extract_from_flows(flows, "game.example.com",
                                  host="game.example.com")
        result = reblend_bridge.build_pack(host="game.example.com",
                                           reblend=REBLEND_T9)
        self.assertEqual(result["models_converted"], 2)
        scene = self.le.scene.load_scene(result["scene_path"])
        meshes = [o for o in scene["objects"]
                  if o.get("shape") == "mesh"]
        self.assertEqual(len(meshes), 4)

    def test_list_packs(self):
        reblend_bridge.build_pack(host="game.example.com",
                                  reblend=REBLEND_T9)
        packs = reblend_bridge.list_packs()
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0][1]["host"], "game.example.com")


class LaunchArgsTests(unittest.TestCase):
    def test_args_with_main_py(self):
        if not HAS_REBLEND:
            self.skipTest("no Reblend fixture")
        args = reblend_bridge.reblend_launch_args(
            REBLEND_T9, "/tmp/pack/game.json")
        self.assertEqual(args, [sys.executable,
                                os.path.join(REBLEND_T9, "main.py"),
                                "--scene", "/tmp/pack/game.json"])

    def test_args_without_main_py(self):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "light_engine"))
        self.assertIsNone(
            reblend_bridge.reblend_launch_args(tmp, "/tmp/pack/x.json"))
        self.assertIsNone(
            reblend_bridge.reblend_launch_args(None, "/tmp/pack/x.json"))

    def test_open_in_reblend_launches(self):
        if not HAS_REBLEND:
            self.skipTest("no Reblend fixture")
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value.poll.return_value = None  # still running
            ok, msg = reblend_bridge.open_in_reblend(
                REBLEND_T9, "/tmp/pack/game.json", settle=0)
        self.assertTrue(ok)
        popen.assert_called_once()
        called = popen.call_args[0][0]
        self.assertEqual(called[-2:], ["--scene", "/tmp/pack/game.json"])

    def test_args_prefers_reblend_venv_python(self):
        # Review #14: Reblend's own .venv has its dependencies;
        # MIMIK's Python may not.
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "main.py"), "w") as fh:
            fh.write("pass")
        venv_py = os.path.join(tmp, ".venv", "bin", "python")
        os.makedirs(os.path.dirname(venv_py))
        with open(venv_py, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(venv_py, 0o755)
        args = reblend_bridge.reblend_launch_args(tmp, "/tmp/pack/x.json")
        self.assertEqual(args[0], venv_py)

    def test_open_in_reblend_reports_instant_failure(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "main.py"), "w") as fh:
            fh.write("import sys; print('no PySide6 here', file=sys.stderr); "
                     "sys.exit(1)")
        ok, msg = reblend_bridge.open_in_reblend(
            tmp, "/tmp/pack/x.json", settle=0.3)
        self.assertFalse(ok)
        self.assertIn("exit code 1", msg)
        self.assertIn("no PySide6 here", msg)
        self.assertNotIn("opening the pack", msg)

    def test_open_without_reblend_is_plain(self):
        ok, msg = reblend_bridge.open_in_reblend(None, "/tmp/x.json")
        self.assertFalse(ok)
        self.assertIn("File menu", msg)


class DeletePackTests(unittest.TestCase):
    """Item 5: find/delete packs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def _make_pack(self, name="game", host="game.example.com"):
        pdir = os.path.join(reblend_bridge.packs_dir(),
                            f"{name}_20260101-000000")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "pack.json"), "w") as fh:
            json.dump({"app": name, "host": host, "group": name,
                       "built_at": "2026-01-01T00:00:00"}, fh)
        with open(os.path.join(pdir, "scene.json"), "w") as fh:
            fh.write("{}")
        return pdir

    def test_find_packs_by_app_host_and_dir(self):
        pdir = self._make_pack()
        self.assertEqual([p for p, _d in
                          reblend_bridge.find_packs("game")], [pdir])
        self.assertEqual([p for p, _d in reblend_bridge.find_packs(
            "game.example.com")], [pdir])
        self.assertEqual([p for p, _d in reblend_bridge.find_packs(
            os.path.basename(pdir))], [pdir])
        self.assertEqual(reblend_bridge.find_packs("nobody"), [])

    def test_delete_pack_removes_dir(self):
        pdir = self._make_pack()
        result = reblend_bridge.delete_pack(pdir)
        self.assertEqual(result["deleted"], pdir)
        self.assertGreater(result["bytes_freed"], 0)
        self.assertFalse(os.path.exists(pdir))
        self.assertEqual(reblend_bridge.list_packs(), [])
        again = reblend_bridge.delete_pack(pdir)
        self.assertIsNone(again["deleted"])

    def test_delete_pack_refuses_outside_paths(self):
        self.assertIsNone(
            reblend_bridge.delete_pack("/tmp")["deleted"])
        self.assertIsNone(
            reblend_bridge.delete_pack(
                reblend_bridge.packs_dir())["deleted"])


if __name__ == "__main__":
    unittest.main()
