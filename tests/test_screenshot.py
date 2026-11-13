"""Tests for mimik.screenshot — the stop-time screenshot (v0.4 item 15).

The Qt path is tested for real when Qt is available (offscreen grab
returns a real pixmap); the tool path uses a stub gnome-screenshot on
PATH. No real desktop needed either way.
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # for test_assets

from mimik import screenshot, store
from test_assets import _stub_png_bytes

try:
    from PySide6.QtWidgets import QApplication

    HAS_QT = True
except ImportError:
    HAS_QT = False


def _stub_tool(bin_dir):
    """A fake gnome-screenshot that copies a stub PNG to its -f target."""
    png = os.path.join(bin_dir, "stub.png")
    with open(png, "wb") as f:
        f.write(_stub_png_bytes())
    script = os.path.join(bin_dir, "gnome-screenshot")
    with open(script, "w") as f:
        f.write("#!/bin/sh\ncp %s \"$2\"\n" % png)
    os.chmod(script, 0o755)
    return bin_dir


class ScreenshotDirTests(unittest.TestCase):
    def test_dir_is_private_and_under_captures(self):
        tmp = tempfile.mkdtemp()
        d = screenshot.screenshots_dir(base=tmp)
        self.assertEqual(d, os.path.join(store.captures_dir(tmp),
                                         "screenshots"))
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(oct(os.stat(d).st_mode & 0o777), "0o700")


class ToolShotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = _stub_tool(tempfile.mkdtemp())

    def test_stub_tool_writes_a_png(self):
        path = os.path.join(self.tmp, "shot.png")
        with mock.patch.object(screenshot, "_qt_shot",
                               return_value=False), \
             mock.patch.dict(os.environ,
                             {"PATH": self.bin + os.pathsep + "/usr/bin"}):
            self.assertTrue(screenshot.take_screenshot(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_no_tool_no_qt_is_false(self):
        path = os.path.join(self.tmp, "shot.png")
        with mock.patch.object(screenshot, "_qt_shot",
                               return_value=False), \
             mock.patch.dict(os.environ, {"PATH": self.tmp}):
            self.assertFalse(screenshot.take_screenshot(path))
        self.assertFalse(os.path.exists(path))

    def test_failing_tool_is_false(self):
        bad = os.path.join(self.bin, "gnome-screenshot")
        with open(bad, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(bad, 0o755)
        path = os.path.join(self.tmp, "shot.png")
        with mock.patch.object(screenshot, "_qt_shot",
                               return_value=False), \
             mock.patch.dict(os.environ,
                             {"PATH": self.bin + os.pathsep + "/usr/bin"}):
            # scrot/import are not installed in the sandbox, so the
            # only candidate is the failing stub.
            self.assertFalse(screenshot.take_screenshot(path))

    def test_capture_for_session_names_and_saves(self):
        with mock.patch.object(screenshot, "_qt_shot",
                               return_value=False), \
             mock.patch.dict(os.environ,
                             {"PATH": self.bin + os.pathsep + "/usr/bin"}):
            path = screenshot.capture_for_session(base=self.tmp)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.basename(path).startswith("screenshot-"))
        self.assertTrue(path.endswith(".png"))
        self.assertIn("screenshots", path)


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class QtShotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_qt_grab_saves_a_png(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "shot.png")
        # Offscreen grab returns a real (blank) pixmap: the Qt path
        # runs end to end here. On a real desktop it holds the desktop.
        self.assertTrue(screenshot._qt_shot(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_qt_shot_off_the_gui_thread_is_false(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "shot.png")
        results = []

        def _run():
            results.append(screenshot._qt_shot(path))

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=10)
        self.assertEqual(results, [False])
        self.assertFalse(os.path.exists(path))

    def test_take_screenshot_prefers_qt(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "shot.png")
        with mock.patch.object(screenshot, "_tool_shot") as tool:
            self.assertTrue(screenshot.take_screenshot(path))
            tool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
