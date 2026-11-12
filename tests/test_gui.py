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
sys.path.insert(0, os.path.dirname(__file__))  # for test_assets helpers

from test_assets import read_t9

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:
    HAS_QT = False

REBLEND_T9 = os.environ.get(
    "REBLEND_FIXTURE",
    os.path.join(os.path.expanduser("~"), "workspace",
                 "reblend-v02-t9-build", "reblend"))


def _has_reblend():
    from mimik import reblend_bridge

    return reblend_bridge._is_reblend_dir(REBLEND_T9)


HAS_REBLEND = _has_reblend()


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

    def test_window_shows_with_seven_tabs(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertTrue(win.isVisible())
        tabs = win.tabs
        self.assertEqual(tabs.count(), 7)
        titles = [tabs.tabText(i) for i in range(tabs.count())]
        self.assertEqual(titles, ["Record", "Hosts and Learn", "Generate",
                                  "Assets", "Clients", "Doctor", "Presets"])
        # Qt eats a bare & in visible widget text (mnemonic marker — it
        # rendered as "_" in the tab bar while tabText() still returned the
        # raw string, so the old assertion stayed green on a wrong label).
        for t in titles:
            self.assertNotIn("&", t)
        win.close()

    def test_presets_tab_lists_seeded_preset(self):
        from mimik import presets, store
        from mimik.gui import MainWindow

        store.save_flows([{
            "id": "p1",
            "request": {"host": "game.example.com", "method": "GET",
                        "path": "/api/users/123", "scheme": "https",
                        "port": 443,
                        "headers": [["Authorization",
                                     "Bearer SECRET-TOKEN-1"]]},
            "response": {"status_code": 200, "headers": []},
        }])
        presets.learn("game.example.com")

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertEqual(win.presets_list.count(), 1)
        item = win.presets_list.item(0)
        self.assertIn("game.example.com", item.text())
        self.assertIn("(host)", item.text())
        self.assertFalse(win.auto_learn_check.isChecked())
        win.presets_list.setCurrentRow(0)
        self.app.processEvents()
        detail = win.preset_detail.toPlainText()
        self.assertIn("How this app signs in:", detail)
        self.assertIn("header: Authorization", detail)
        self.assertIn("GET /api/users/{id}", detail)
        self.assertNotIn("SECRET-TOKEN-1", detail)
        win.close()

    def test_assets_tab_lists_fixture_assets(self):
        from mimik import assets
        from mimik.gui import MainWindow

        group = assets.AssetGroup("game.example.com", "traffic",
                                  host="game.example.com")
        group.add_bytes(b"\x89PNG-fake", "logo.png", "image")
        group.add_bytes(b"glTF-fake", "crate.glb", "model")
        group.finish()

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertEqual(win.asset_group.count(), 1)
        self.assertEqual(win.asset_table.rowCount(), 2)
        names = {win.asset_table.item(r, 0).text()
                 for r in range(win.asset_table.rowCount())}
        self.assertEqual(names, {"logo.png", "crate.glb"})
        kinds = {win.asset_table.item(r, 1).text()
                 for r in range(win.asset_table.rowCount())}
        self.assertEqual(kinds, {"Image", "3D model"})
        statuses = {win.asset_table.item(r, 4).text()
                    for r in range(win.asset_table.rowCount())}
        self.assertEqual(statuses, {"saved"})
        self.assertIn("Reblend", win.reblend_label.text())
        self.assertEqual(win.scale_spin.value(), 100)
        # No pack built yet — the Open buttons stay disabled.
        self.assertFalse(win.open_pack_btn.isEnabled())
        self.assertFalse(win.open_reblend_btn.isEnabled())
        win.close()

    @unittest.skipUnless(HAS_REBLEND, "Reblend T9 fixture not available")
    def test_assets_tab_opens_pack_from_earlier_session(self):
        from mimik import assets, reblend_bridge
        from mimik.gui import MainWindow

        group = assets.AssetGroup("game.example.com", "traffic",
                                  host="game.example.com")
        group.add_bytes(read_t9("examples/sample.glb"), "crate.glb",
                        "model")
        group.finish()
        # Pack built "earlier" (not through this window).
        reblend_bridge.build_pack(host="game.example.com",
                                  reblend=REBLEND_T9)
        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertTrue(win.open_pack_btn.isEnabled())
        self.assertTrue(win.open_reblend_btn.isEnabled())
        self.assertIn("game.example.com", win.pack_dir)
        self.assertTrue(win.pack_scene.endswith(".json"))
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
