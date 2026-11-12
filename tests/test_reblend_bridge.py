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
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # for test_assets helpers

from mimik import assets, reblend_bridge
from test_assets import make_wav, read_t9, saved_flow

REBLEND_T9 = os.environ.get(
    "REBLEND_FIXTURE",
    os.path.join(os.path.expanduser("~"), "workspace",
                 "reblend-v02-t9-build", "reblend"))
HAS_REBLEND = reblend_bridge._is_reblend_dir(REBLEND_T9)
ICON_PNG = os.path.join(os.path.dirname(__file__), "..", "icons",
                        "mimik-48.png")


def seed_game_assets(base):
    """A capture group for game.example.com: real .glb + .obj + PNG +
    WAV + a .gltf stub + a font stub. Returns the group dir."""
    with open(ICON_PNG, "rb") as fh:
        png = fh.read()
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
        color, note = reblend_bridge.average_color(ICON_PNG)
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
            ok, msg = reblend_bridge.open_in_reblend(
                REBLEND_T9, "/tmp/pack/game.json")
        self.assertTrue(ok)
        popen.assert_called_once()
        called = popen.call_args[0][0]
        self.assertEqual(called[-2:], ["--scene", "/tmp/pack/game.json"])

    def test_open_without_reblend_is_plain(self):
        ok, msg = reblend_bridge.open_in_reblend(None, "/tmp/x.json")
        self.assertFalse(ok)
        self.assertIn("File menu", msg)


if __name__ == "__main__":
    unittest.main()
