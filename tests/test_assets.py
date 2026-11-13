"""Tests for asset capture — traffic (flows/HAR) + program files."""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import assets

REBLEND_T9 = os.environ.get(
    "REBLEND_FIXTURE",
    os.path.join(os.path.expanduser("~"), "workspace",
                 "reblend-v02-t9-build", "reblend"))
HAS_REBLEND = os.path.isdir(os.path.join(REBLEND_T9, "light_engine"))
ICON_PNG = os.path.join(os.path.dirname(__file__), "..", "icons",
                        "mimik-48.png")

_STUB_ICON_PATH = None


def _stub_png_bytes(width=48, height=48, rgb=(41, 98, 168)):
    """A valid PNG of one solid (non-gray) color, stdlib only.

    The repo holds only icons/mimik.svg — the PNGs can't be committed
    (the GitHub push path can't carry binary) — so a fresh clone has no
    icon file. Tests that need PNG bytes use this stub instead of
    hard-failing; the color is deliberately not gray so the
    average-color test still distinguishes a real read from the
    neutral-gray fallback.
    """
    import struct
    import zlib

    def chunk(ctype, data):
        out = struct.pack(">I", len(data)) + ctype + data
        return out + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def icon_png_path():
    """Path to a 48px PNG for tests: the real icon when present (zip or
    local tree), else a generated stub in the temp dir (fresh clone)."""
    global _STUB_ICON_PATH
    if os.path.exists(ICON_PNG):
        return ICON_PNG
    if _STUB_ICON_PATH is None or not os.path.exists(_STUB_ICON_PATH):
        fd, path = tempfile.mkstemp(prefix="mimik-icon-", suffix=".png")
        with os.fdopen(fd, "wb") as fh:
            fh.write(_stub_png_bytes())
        _STUB_ICON_PATH = path
    return _STUB_ICON_PATH


def icon_png_bytes():
    """PNG bytes for tests — the real icon, or the stub (see above)."""
    with open(icon_png_path(), "rb") as fh:
        return fh.read()


def read_t9(rel):
    with open(os.path.join(REBLEND_T9, rel), "rb") as fh:
        return fh.read()


def make_wav(path, seconds=0.1):
    """A tiny real .wav file (sine blip), stdlib only."""
    import math
    import struct

    rate = 8000
    frames = int(rate * seconds)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        for i in range(frames):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
            wf.writeframes(struct.pack("<h", v))


def saved_flow(fid, host, path, ctype, body):
    """A flow dict as the store saves it (body embedded)."""
    try:
        text = body.decode("utf-8")
        b64 = False
    except UnicodeDecodeError:
        text = base64.b64encode(body).decode("ascii")
        b64 = True
    flow = {
        "id": fid,
        "request": {"host": host, "method": "GET", "path": path,
                    "scheme": "https", "port": 443, "headers": []},
        "response": {"status_code": 200,
                     "headers": [["Content-Type", ctype]]},
        "_saved_response_body": text,
    }
    if b64:
        flow["_saved_response_body_b64"] = True
    return flow


class DetectTests(unittest.TestCase):
    def test_extension_wins_over_content_type(self):
        self.assertEqual(
            assets.detect_kind("https://x.com/a.glb",
                               "application/octet-stream"), "model")
        self.assertEqual(
            assets.detect_kind("https://x.com/a.obj", "text/plain"),
            "model")

    def test_content_type_fallback(self):
        self.assertEqual(
            assets.detect_kind("https://x.com/download?id=9",
                               "image/png"), "image")
        self.assertEqual(
            assets.detect_kind("https://x.com/sfx", "audio/mpeg"),
            "audio")
        self.assertEqual(
            assets.detect_kind("https://x.com/f", "font/woff2"), "font")
        self.assertEqual(
            assets.detect_kind("https://x.com/v", "video/mp4"), "video")
        self.assertEqual(
            assets.detect_kind("https://x.com/t", "image/ktx2"),
            "texture")

    def test_new_kinds(self):
        # Item 24: video, game textures, and extra model formats.
        for url, kind in [
                ("https://x.com/intro.mp4", "video"),
                ("https://x.com/clip.webm", "video"),
                ("https://x.com/diffuse.dds", "texture"),
                ("https://x.com/normal.ktx2", "texture"),
                ("https://x.com/char.fbx", "model"),
                ("https://x.com/level.dae", "model")]:
            self.assertEqual(assets.detect_kind(url), kind, url)
        self.assertEqual(assets.KIND_LABELS["video"], "Video")
        self.assertEqual(assets.KIND_LABELS["texture"], "Texture")

    def test_generic_octet_stream_is_not_an_asset(self):
        self.assertIsNone(
            assets.detect_kind("https://x.com/blob",
                               "application/octet-stream"))

    def test_json_and_html_are_not_assets(self):
        self.assertIsNone(
            assets.detect_kind("https://x.com/api/v1/users",
                               "application/json"))
        self.assertIsNone(
            assets.detect_kind("https://x.com/page.html", "text/html"))

    def test_all_kinds(self):
        for url, kind in [
                ("https://x.com/a.png", "image"),
                ("https://x.com/a.jpg", "image"),
                ("https://x.com/a.svg", "image"),
                ("https://x.com/a.gltf", "model"),
                ("https://x.com/a.wav", "audio"),
                ("https://x.com/a.ogg", "audio"),
                ("https://x.com/a.ttf", "font"),
                ("https://x.com/a.woff2", "font")]:
            self.assertEqual(assets.detect_kind(url), kind, url)


@unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
class FlowExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.glb = read_t9("examples/sample.glb")
        self.obj = read_t9("examples/house.obj")
        self.png = icon_png_bytes()
        wav_path = os.path.join(self.tmp, "sfx.wav")
        make_wav(wav_path)
        with open(wav_path, "rb") as fh:
            self.wav = fh.read()

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def _flows(self):
        return [
            saved_flow("f1", "game.example.com", "/models/crate.glb",
                       "model/gltf-binary", self.glb),
            saved_flow("f2", "game.example.com", "/models/house.obj",
                       "text/plain", self.obj),
            saved_flow("f3", "game.example.com", "/img/logo.png",
                       "image/png", self.png),
            saved_flow("f4", "game.example.com", "/sfx/jump.wav",
                       "audio/wav", self.wav),
            saved_flow("f5", "game.example.com", "/api/v1/state",
                       "application/json", b'{"ok": true}'),
        ]

    def test_assets_saved_byte_identical(self):
        gdir, manifest = assets.extract_from_flows(
            self._flows(), "game.example.com", host="game.example.com")
        self.assertIsNotNone(gdir)
        self.assertEqual(len(manifest["assets"]), 4)
        by_name = {e["name"]: e for e in manifest["assets"]}
        self.assertEqual(
            {e["kind"] for e in manifest["assets"]},
            {"model", "image", "audio"})
        for name, raw in (("crate.glb", self.glb), ("house.obj", self.obj),
                          ("logo.png", self.png), ("jump.wav", self.wav)):
            entry = by_name[name]
            self.assertEqual(entry["size"], len(raw))
            self.assertEqual(entry["sha256"],
                             hashlib.sha256(raw).hexdigest())
            self.assertEqual(entry["host"] if "host" in entry else
                             manifest["host"], "game.example.com")
            with open(os.path.join(gdir, entry["file"]), "rb") as fh:
                self.assertEqual(fh.read(), raw)
        # Manifest on disk matches what was returned.
        with open(os.path.join(gdir, "assets.json")) as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["assets"], manifest["assets"])
        self.assertEqual(on_disk["source"], "traffic")

    def test_no_assets_returns_none(self):
        gdir, manifest = assets.extract_from_flows(
            [saved_flow("f9", "api.example.com", "/v1/x",
                        "application/json", b"{}")],
            "api.example.com", host="api.example.com")
        self.assertIsNone(gdir)
        self.assertIsNone(manifest)

    def test_dedupe_same_bytes_one_file(self):
        flows = [
            saved_flow("f1", "game.example.com", "/img/a.png",
                       "image/png", self.png),
            saved_flow("f2", "game.example.com", "/img/b.png",
                       "image/png", self.png),
        ]
        gdir, manifest = assets.extract_from_flows(
            flows, "game.example.com", host="game.example.com")
        self.assertEqual(len(manifest["assets"]), 2)
        files = {e["file"] for e in manifest["assets"]}
        self.assertEqual(len(files), 1)
        self.assertTrue(manifest["assets"][1]["duplicate"])
        self.assertEqual(manifest["total_bytes"], len(self.png))

    def test_live_mitm_bytes_preferred(self):
        class FakeMitm:
            def body(self, fid, side):
                return self.png if fid == "live1" else b""

        flow = {"id": "live1",
                "request": {"host": "game.example.com", "method": "GET",
                            "path": "/img/live.png", "scheme": "https",
                            "port": 443, "headers": []},
                "response": {"status_code": 200,
                             "headers": [["Content-Type", "image/png"]]}}
        fake = FakeMitm()
        fake.png = self.png
        gdir, manifest = assets.extract_from_flows(
            [flow], "game.example.com", mitm=fake,
            host="game.example.com")
        self.assertEqual(len(manifest["assets"]), 1)
        with open(os.path.join(gdir, manifest["assets"][0]["file"]),
                  "rb") as fh:
            self.assertEqual(fh.read(), self.png)

    def test_mitm_failure_falls_back_to_saved_body(self):
        class BrokenMitm:
            def body(self, fid, side):
                raise RuntimeError("proxy gone")

        gdir, manifest = assets.extract_from_flows(
            self._flows()[:1], "game.example.com", mitm=BrokenMitm(),
            host="game.example.com")
        self.assertEqual(len(manifest["assets"]), 1)
        self.assertEqual(manifest["assets"][0]["name"], "crate.glb")


class CapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_per_file_cap_skips_with_note(self):
        group = assets.AssetGroup("h.example.com", "traffic")
        entry = group.add_bytes(b"x" * 100, "big.png", "image",
                                max_file_bytes=50)
        self.assertIsNone(entry)
        self.assertEqual(len(group.manifest["skipped"]), 1)
        skipped = group.manifest["skipped"][0]
        self.assertEqual(skipped["name"], "big.png")
        self.assertEqual(skipped["size"], 100)
        self.assertIn("cap", skipped["reason"])
        group.finish()
        self.assertEqual(group.manifest["assets"], [])

    def test_group_cap_skips_with_note(self):
        group = assets.AssetGroup("h.example.com", "traffic")
        first = group.add_bytes(b"a" * 60, "a.png", "image",
                                max_file_bytes=1000, max_group_bytes=100)
        self.assertIsNotNone(first)
        second = group.add_bytes(b"b" * 60, "b.png", "image",
                                 max_file_bytes=1000, max_group_bytes=100)
        self.assertIsNone(second)
        self.assertEqual(len(group.manifest["skipped"]), 1)
        self.assertIn("cap", group.manifest["skipped"][0]["reason"])


@unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
class HarExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.glb = read_t9("examples/sample.glb")
        self.obj = read_t9("examples/house.obj")

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def _har(self):
        def entry(url, mime, data, encoding="base64"):
            if encoding == "base64":
                text = base64.b64encode(data).decode("ascii")
            else:
                text = data.decode("utf-8")
            return {
                "request": {"method": "GET", "url": url, "headers": []},
                "response": {
                    "status": 200, "headers": [],
                    "content": {"mimeType": mime, "text": text,
                                "encoding": encoding}},
            }

        return {"log": {"entries": [
            entry("https://game.example.com/m/crate.glb",
                  "model/gltf-binary", self.glb),
            entry("https://game.example.com/m/house.obj",
                  "text/plain", self.obj, encoding="text"),
            entry("https://cdn.example.com/i/logo.png",
                  "image/png", b"\x89PNG-not-real-but-bytes"),
            entry("https://game.example.com/api/v1/x",
                  "application/json", b"{}"),
        ]}}

    def test_har_groups_per_host_byte_identical(self):
        har_path = os.path.join(self.tmp, "game.har")
        with open(har_path, "w") as fh:
            json.dump(self._har(), fh)
        groups = assets.extract_from_har(har_path)
        self.assertEqual(len(groups), 2)
        by_host = {m["host"]: (d, m) for d, m in groups}
        gdir, manifest = by_host["game.example.com"]
        self.assertEqual(len(manifest["assets"]), 2)
        self.assertEqual(manifest["source"], "har")
        by_name = {e["name"]: e for e in manifest["assets"]}
        with open(os.path.join(gdir, by_name["crate.glb"]["file"]),
                  "rb") as fh:
            self.assertEqual(fh.read(), self.glb)
        with open(os.path.join(gdir, by_name["house.obj"]["file"]),
                  "rb") as fh:
            self.assertEqual(fh.read(), self.obj)
        self.assertEqual(len(by_host["cdn.example.com"][1]["assets"]), 1)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.root = os.path.join(self.tmp, "game")
        os.makedirs(os.path.join(self.root, "data", "models"))
        os.makedirs(os.path.join(self.root, "data", "img"))
        with open(os.path.join(self.root, "data", "models", "tree.glb"),
                  "wb") as fh:
            fh.write(b"glTF-fake-bytes")
        with open(os.path.join(self.root, "data", "img", "wall.png"),
                  "wb") as fh:
            fh.write(b"\x89PNG-fake-bytes")
        with open(os.path.join(self.root, "data", "readme.txt"),
                  "w") as fh:
            fh.write("not an asset")
        # A script that would leave a marker if MIMIK ever ran it.
        self.marker = os.path.join(self.tmp, "RAN-MARKER")
        with open(os.path.join(self.root, "run.sh"), "w") as fh:
            fh.write(f"#!/bin/sh\ntouch {self.marker}\n")

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_scan_copies_assets_never_executes(self):
        gdir, manifest = assets.scan_program_tree(self.root, "game")
        self.assertIsNotNone(gdir)
        self.assertEqual(len(manifest["assets"]), 2)
        self.assertFalse(os.path.exists(self.marker))
        names = {e["name"] for e in manifest["assets"]}
        self.assertEqual(names, {"tree.glb", "wall.png"})
        paths = {e["source_path"] for e in manifest["assets"]}
        self.assertEqual(paths, {"data/models/tree.glb",
                                 "data/img/wall.png"})
        # Originals still in place (copied, never moved).
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "data", "models", "tree.glb")))
        # Bytes identical.
        entry = [e for e in manifest["assets"]
                 if e["name"] == "tree.glb"][0]
        with open(os.path.join(gdir, entry["file"]), "rb") as fh:
            self.assertEqual(fh.read(), b"glTF-fake-bytes")

    def test_scan_empty_tree_returns_none(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        gdir, manifest = assets.scan_program_tree(empty, "empty")
        self.assertIsNone(gdir)
        self.assertIsNone(manifest)

    def test_scan_keeps_new_kinds(self):
        # Item 24: a file of each new kind is detected and kept.
        files = {"intro.mp4": b"ftyp-fake",
                 "clip.webm": b"\x1a\x45\xdf\xa3-fake",
                 "diffuse.dds": b"DDS-fake",
                 "normal.ktx2": b"\xabKTX 20-fake",
                 "char.fbx": b"Kaydara-fake",
                 "level.dae": b"<?xml-fake"}
        for name, data in files.items():
            with open(os.path.join(self.root, "data", name), "wb") as fh:
                fh.write(data)
        gdir, manifest = assets.scan_program_tree(self.root, "game")
        by_name = {e["name"]: e for e in manifest["assets"]}
        self.assertEqual(by_name["intro.mp4"]["kind"], "video")
        self.assertEqual(by_name["clip.webm"]["kind"], "video")
        self.assertEqual(by_name["diffuse.dds"]["kind"], "texture")
        self.assertEqual(by_name["normal.ktx2"]["kind"], "texture")
        self.assertEqual(by_name["char.fbx"]["kind"], "model")
        self.assertEqual(by_name["level.dae"]["kind"], "model")
        # Kept: the bytes are on disk in the group dir.
        for name, data in files.items():
            with open(os.path.join(gdir, by_name[name]["file"]),
                      "rb") as fh:
                self.assertEqual(fh.read(), data, name)

    def test_scan_skips_symlinks(self):
        outside = os.path.join(self.tmp, "outside.png")
        with open(outside, "wb") as fh:
            fh.write(b"\x89PNG-outside")
        os.symlink(outside, os.path.join(self.root, "data", "link.png"))
        gdir, manifest = assets.scan_program_tree(self.root, "game")
        names = {e["name"] for e in manifest["assets"]}
        self.assertNotIn("link.png", names)

    def test_scan_respects_depth_cap(self):
        deep = self.root
        for i in range(5):
            deep = os.path.join(deep, f"level{i}")
        os.makedirs(deep)
        with open(os.path.join(deep, "deep.png"), "wb") as fh:
            fh.write(b"\x89PNG-deep")
        gdir, manifest = assets.scan_program_tree(
            self.root, "game", max_depth=2)
        names = {e["name"] for e in manifest["assets"]}
        self.assertNotIn("deep.png", names)
        # The shallow assets (data/img/wall.png, data/models/tree.glb)
        # still land — only the descent stops.
        self.assertIn("wall.png", names)
        self.assertIn("tree.glb", names)
        self.assertIn("levels", manifest.get("scan_note", ""))

    def test_scan_respects_file_count_cap(self):
        flat = os.path.join(self.tmp, "flat")
        os.makedirs(flat)
        for i in range(10):
            with open(os.path.join(flat, f"img{i:02d}.png"), "wb") as fh:
                fh.write(b"\x89PNG-%d" % i)
        gdir, manifest = assets.scan_program_tree(
            flat, "flat", max_files=3)
        self.assertEqual(len(manifest["assets"]), 3)
        self.assertIn("3-file cap", manifest.get("scan_note", ""))

    def test_scan_cancel_stops_early(self):
        flat = os.path.join(self.tmp, "flat-cancel")
        os.makedirs(flat)
        for i in range(10):
            with open(os.path.join(flat, f"img{i:02d}.png"), "wb") as fh:
                fh.write(b"\x89PNG-%d" % i)
        calls = []

        def cancelled():
            calls.append(1)
            return len(calls) > 5

        gdir, manifest = assets.scan_program_tree(
            flat, "flat", cancelled=cancelled)
        # The dirpath check is call 1; four files land before the
        # sixth call trips the cancel.
        self.assertEqual(len(manifest["assets"]), 4)
        self.assertIn("cancelled", manifest.get("scan_note", ""))

    def test_scan_skips_over_group_cap_without_reading(self):
        big = os.path.join(self.root, "data", "img", "big.png")
        with open(big, "wb") as fh:
            fh.write(b"x" * 100)
        real_open = open

        def guarded_open(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "")
            if str(path) == big and "r" in mode:
                raise AssertionError(
                    "read a file the group cap should have skipped")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", guarded_open):
            gdir, manifest = assets.scan_program_tree(
                self.root, "game", max_group_bytes=20)
        skipped = {e["name"]: e for e in manifest["skipped"]}
        self.assertIn("big.png", skipped)
        self.assertIn("group would pass", skipped["big.png"]["reason"])
        # Exactly one of the two 15-byte assets fits in the 20-byte
        # group; the other is skipped the same way, also unread.
        self.assertEqual(len(manifest["assets"]), 1)


class RecorderHookTests(unittest.TestCase):
    """ProxySession.extract_assets pulls asset bytes while mitmweb lives."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_extract_assets_from_live_flows(self):
        from mimik.recorder import ProxySession

        class FakeMitm:
            def flows(self):
                return [{
                    "id": "live1",
                    "request": {"host": "game.example.com", "method": "GET",
                                "path": "/m/crate.glb", "scheme": "https",
                                "port": 443, "headers": []},
                    "response": {
                        "status_code": 200,
                        "headers": [["Content-Type",
                                     "model/gltf-binary"]]}},
                    {"id": "live2",
                     "request": {"host": "game.example.com", "method": "GET",
                                 "path": "/api/v1/x", "scheme": "https",
                                 "port": 443, "headers": []},
                     "response": {
                         "status_code": 200,
                         "headers": [["Content-Type",
                                      "application/json"]]}},
                ]

            def body(self, fid, side):
                return b"glTF-bytes" if fid == "live1" else b"{}"

        session = ProxySession()
        session.mitm = FakeMitm()
        out = session.extract_assets()
        self.assertEqual(len(out), 1)
        host, gdir, count = out[0]
        self.assertEqual(host, "game.example.com")
        self.assertEqual(count, 1)
        with open(os.path.join(gdir, "crate.glb"), "rb") as fh:
            self.assertEqual(fh.read(), b"glTF-bytes")

    def test_extract_assets_never_raises(self):
        from mimik.recorder import ProxySession

        class BrokenMitm:
            def flows(self):
                raise RuntimeError("proxy died")

        session = ProxySession()
        session.mitm = BrokenMitm()
        self.assertEqual(session.extract_assets(), [])
        session.mitm = None
        self.assertEqual(session.extract_assets(), [])


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_list_groups_and_assets(self):
        group = assets.AssetGroup("h.example.com", "traffic",
                                  host="h.example.com")
        group.add_bytes(b"1234", "a.png", "image")
        group.finish()
        groups = assets.list_asset_groups()
        self.assertEqual(len(groups), 1)
        found = assets.list_assets(host="h.example.com")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "a.png")
        self.assertTrue(os.path.isfile(
            assets.asset_file_path(found[0])))
        self.assertEqual(assets.list_assets(host="other.example.com"),
                         [])

    def test_same_second_extractions_do_not_overwrite(self):
        # Two extractions in the same second get different dirs, so
        # the later manifest can never overwrite the earlier one.
        g1 = assets.AssetGroup("h.example.com", "traffic",
                               host="h.example.com")
        g1.add_bytes(b"1111", "a.png", "image")
        d1, _m1 = g1.finish()
        g2 = assets.AssetGroup("h.example.com", "traffic",
                               host="h.example.com")
        g2.add_bytes(b"2222", "b.png", "image")
        d2, _m2 = g2.finish()
        self.assertNotEqual(d1, d2)
        self.assertEqual(len(assets.list_asset_groups()), 2)
        self.assertEqual(len(assets.list_assets()), 2)

    def test_list_empty_when_no_assets(self):
        self.assertEqual(assets.list_asset_groups(), [])
        self.assertEqual(assets.list_assets(), [])


class DeleteGroupTests(unittest.TestCase):
    """Item 5: find/delete asset groups."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def _make_group(self, name="game.example.com"):
        group = assets.AssetGroup(name, "traffic", host=name)
        group.add_bytes(b"\x89PNG fake", "icon.png", "image")
        return group.finish()

    def test_find_groups_by_name(self):
        gdir1, _m1 = self._make_group()
        gdir2, _m2 = self._make_group()
        found = assets.find_groups("game.example.com")
        self.assertEqual({g for g, _m in found}, {gdir1, gdir2})
        self.assertEqual(assets.find_groups("nobody"), [])

    def test_delete_group_removes_dir(self):
        gdir, _m = self._make_group()
        result = assets.delete_group(gdir)
        self.assertEqual(result["deleted"], gdir)
        self.assertGreater(result["bytes_freed"], 0)
        self.assertFalse(os.path.exists(gdir))
        self.assertEqual(assets.list_asset_groups(), [])
        again = assets.delete_group(gdir)
        self.assertIsNone(again["deleted"])

    def test_delete_group_refuses_outside_paths(self):
        self.assertIsNone(
            assets.delete_group("/tmp")["deleted"])
        self.assertIsNone(
            assets.delete_group(assets.assets_dir())["deleted"])


class ModelInfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_obj_counts(self):
        path = os.path.join(self.tmp, "cube.obj")
        with open(path, "w") as fh:
            fh.write("".join(f"v {i} 0 0\n" for i in range(8)))
            fh.write("".join(f"f {i} {i + 1} {i + 2}\n" for i in range(6)))
            fh.write("vn 0 1 0\n")  # normals must not count as vertices
        info = assets.model_info(path)
        self.assertIn("format: OBJ", info)
        self.assertIn("vertices: 8", info)
        self.assertIn("faces: 6", info)

    def test_glb_meshes(self):
        path = os.path.join(self.tmp, "crate.glb")
        doc = json.dumps({
            "asset": {"version": "2.0"},
            "meshes": [{"primitives": [{}]},
                       {"primitives": [{}, {}]}],
        }).encode()
        # Pad the JSON chunk to a 4-byte boundary (glTF requires it).
        doc += b" " * (-len(doc) % 4)
        total = 12 + 8 + len(doc)
        with open(path, "wb") as fh:
            fh.write(b"glTF" + (2).to_bytes(4, "little")
                     + total.to_bytes(4, "little"))
            fh.write(len(doc).to_bytes(4, "little") + b"JSON" + doc)
        info = assets.model_info(path)
        self.assertIn("format: GLB", info)
        self.assertIn("meshes: 2", info)
        self.assertIn("primitives: 3", info)

    def test_missing_file_is_none(self):
        self.assertIsNone(assets.model_info(
            os.path.join(self.tmp, "nope.obj")))

    def test_unknown_format_gets_basics(self):
        path = os.path.join(self.tmp, "model.fbx")
        with open(path, "wb") as fh:
            fh.write(b"Kaydara FBX Binary\x00" + b"x" * 100)
        info = assets.model_info(path)
        self.assertIn("format: FBX", info)
        self.assertIn("size:", info)
        self.assertNotIn("vertices", info)


if __name__ == "__main__":
    unittest.main()
