"""Qt GUI smoke test — launch the window offscreen, check the tabs.

Run with the Qt test env (see AGENTS.md):
  LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu \
  QT_QPA_PLATFORM=offscreen HOME=/tmp/forge-test-home \
  ~/workspace/forge-v2-testenv/bin/python -m unittest tests.test_gui
Skipped when PySide6 is not importable.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = cls.tmp
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)

    @classmethod
    def tearDownClass(cls):
        del os.environ["MIMIK_DATA_DIR"]

    def test_window_shows_with_five_tabs(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertTrue(win.isVisible())
        tabs = win.tabs
        self.assertEqual(tabs.count(), 5)
        titles = [tabs.tabText(i) for i in range(tabs.count())]
        self.assertEqual(titles, ["Record", "Hosts & Learn", "Generate",
                                  "Clients", "Doctor"])
        win.close()

    def test_record_tab_targets(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        keys = [win.target_combo.itemData(i)
                for i in range(win.target_combo.count())]
        self.assertEqual(keys, ["android", "linux-app", "file", "web",
                                "windows"])
        win.close()

    def test_doctor_tab_renders(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        text = win.doctor_text.toPlainText()
        self.assertIn("python", text)
        self.assertIn("overall:", text)
        win.close()

    def test_generate_tab_polish_options(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        values = [win.polish_combo.itemData(i)
                  for i in range(win.polish_combo.count())]
        self.assertEqual(values, ["none", "ollama", "claude", "opencode"])
        win.close()

    def test_window_screenshot(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        shot = os.path.join(self.tmp, "mimik-gui.png")
        win.grab().save(shot)
        self.assertTrue(os.path.exists(shot))
        self.assertGreater(os.path.getsize(shot), 1000)
        win.close()


if __name__ == "__main__":
    unittest.main()
