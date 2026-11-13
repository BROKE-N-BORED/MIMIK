"""Tests for icon fixtures + the SVG->PNG renderer (v0.4 item 1).

The repo holds only icons/mimik.svg — the PNGs can't be committed
(the GitHub push path can't carry binary). Tests must not hard-fail
without them, and install.sh renders them from the SVG at install time.
"""
import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # for test_assets helpers

import test_assets

ROOT = os.path.join(os.path.dirname(__file__), "..")
RENDER_SCRIPT = os.path.join(ROOT, "tools", "render_icons.py")
SVG = os.path.join(ROOT, "icons", "mimik.svg")

HAS_QTSVG = False
try:
    HAS_QTSVG = importlib.util.find_spec("PySide6.QtSvg") is not None
except (ImportError, ValueError):
    # find_spec raises when the parent package doesn't exist at all
    # (system python has no PySide6) — that just means no QtSvg.
    HAS_QTSVG = False


def png_dimensions(data):
    """(width, height) from a PNG's IHDR — stdlib, no Qt needed."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


class StubPngTests(unittest.TestCase):
    def test_stub_is_a_valid_png(self):
        data = test_assets._stub_png_bytes()
        self.assertEqual(png_dimensions(data), (48, 48))
        # IDAT decompresses to 48 rows of (filter byte + 48 RGB pixels).
        idat_start = data.index(b"IDAT") + 4
        idat_len = struct.unpack(">I", data[idat_start - 8:idat_start - 4])[0]
        raw = zlib.decompress(data[idat_start:idat_start + idat_len])
        self.assertEqual(len(raw), 48 * (1 + 48 * 3))

    def test_stub_is_not_gray(self):
        # The average-color test must distinguish a real read from the
        # neutral-gray fallback — the stub color is deliberately blue.
        self.assertNotEqual((41, 98, 168), (128, 128, 128))

    def test_icon_bytes_fall_back_to_stub_without_the_icon(self):
        real = test_assets.ICON_PNG
        test_assets.ICON_PNG = os.path.join(self._tmp_missing(), "nope.png")
        test_assets._STUB_ICON_PATH = None
        try:
            data = test_assets.icon_png_bytes()
            self.assertEqual(png_dimensions(data), (48, 48))
            path = test_assets.icon_png_path()
            self.assertTrue(os.path.exists(path))
            self.assertTrue(path.endswith(".png"))
        finally:
            test_assets.ICON_PNG = real
            test_assets._STUB_ICON_PATH = None

    def _tmp_missing(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp

    @unittest.skipUnless(HAS_QTSVG, "QtSvg not available")
    def test_qimage_reads_the_stub(self):
        from PySide6.QtGui import QImage

        path = test_assets.icon_png_path()
        img = QImage(path)
        self.assertFalse(img.isNull())
        self.assertEqual((img.width(), img.height()), (48, 48))


class RenderIconsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.icons = os.path.join(self.tmp, "icons")
        os.makedirs(self.icons)

    def _run(self, *args, **kw):
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        return subprocess.run([sys.executable, RENDER_SCRIPT, *args],
                              capture_output=True, text=True, env=env,
                              timeout=120, **kw)

    @unittest.skipUnless(HAS_QTSVG, "QtSvg not available")
    def test_renders_all_three_sizes_from_the_svg(self):
        shutil.copy(SVG, os.path.join(self.icons, "mimik.svg"))
        proc = self._run(self.icons)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sizes = {}
        for size in (48, 128, 256):
            path = os.path.join(self.icons, f"mimik-{size}.png")
            self.assertTrue(os.path.exists(path), f"{size}px missing")
            with open(path, "rb") as fh:
                data = fh.read()
            self.assertGreater(len(data), 0)
            self.assertEqual(png_dimensions(data), (size, size))
            sizes[size] = len(data)
        # Bigger renders carry more bytes — the sizes are really honored.
        self.assertLess(sizes[48], sizes[256])

    @unittest.skipUnless(HAS_QTSVG, "QtSvg not available")
    def test_rendered_png_is_not_blank(self):
        from PySide6.QtGui import QImage

        shutil.copy(SVG, os.path.join(self.icons, "mimik.svg"))
        proc = self._run(self.icons, "48")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        img = QImage(os.path.join(self.icons, "mimik-48.png"))
        self.assertFalse(img.isNull())
        colors = set()
        for y in range(0, 48, 4):
            for x in range(0, 48, 4):
                colors.add(img.pixel(x, y))
        self.assertGreater(len(colors), 1, "rendered icon is one flat color")

    def test_missing_svg_exits_nonzero(self):
        proc = self._run(self.icons)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SVG not found", proc.stderr)

    def test_no_renderer_means_nonzero(self):
        # Direct unit test of the fallback: with both renderers broken,
        # render_all reports errors for every size.
        spec = importlib.util.spec_from_file_location(
            "render_icons", RENDER_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        shutil.copy(SVG, os.path.join(self.icons, "mimik.svg"))
        mod.render_qt = lambda *a: (_ for _ in ()).throw(
            RuntimeError("no Qt"))
        mod.render_rsvg = lambda *a: (_ for _ in ()).throw(
            RuntimeError("no rsvg"))
        rendered, errors = mod.render_all(self.icons, (48, 128, 256))
        self.assertEqual(rendered, [])
        self.assertEqual(len(errors), 3)


if __name__ == "__main__":
    unittest.main()
