"""Tests for asset capture — traffic (flows/HAR) + program files."""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import assets

REBLEND_T9 = os.environ.get(
    "REBLEND_FIXTURE",
    os.path.join(os.path.expanduser("~"), "workspace",
                 "reblend-v02-t9-build", "reblend"))
HAS_REBLEND = os.path.isdir(os.path.join(REBLEND_T9, "light_engine"))
ICON_PNG = os.path.join(os.path.dirname(__file__), "..", "icons",
                        "mimik-48.png")


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
        with open(ICON_PNG, "rb") as fh:
            self.png = fh.read()
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

    def test_scan_skips_symlinks(self):
        outside = os.path.join(self.tmp, "outside.png")
        with open(outside, "wb") as fh:
            fh.write(b"\x89PNG-outside")
        os.symlink(outside, os.path.join(self.root, "data", "link.png"))
        gdir, manifest = assets.scan_program_tree(self.root, "game")
        names = {e["name"] for e in manifest["assets"]}
        self.assertNotIn("link.png", names)


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


if __name__ == "__main__":
    unittest.main()
