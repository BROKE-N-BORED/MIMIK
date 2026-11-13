"""Qt GUI smoke test — launch the window offscreen, check the tabs.

Run with the Qt test env (see AGENTS.md):
  LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu \
  QT_QPA_PLATFORM=offscreen HOME=/tmp/forge-test-home \
  ~/workspace/forge-v2-testenv/bin/python -m unittest tests.test_gui
Skipped when PySide6 is not importable.
"""
import gc
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

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


def _wait_for(app, condition, timeout=15):
    """Pump the event loop until condition() is true (background tasks
    finish on the GUI thread). Returns the final condition() value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.05)
    app.processEvents()
    return condition()


def _select_client(win, name):
    """Select a client by name in the Clients list."""
    for i in range(win.clients_list.count()):
        if name in win.clients_list.item(i).text():
            win.clients_list.setCurrentRow(i)
            return True
    return False


class GuiSourceTests(unittest.TestCase):
    """Static guards on mimik/gui.py — no Qt needed, runs everywhere."""

    def test_no_lambda_signal_slots(self):
        # Lambdas connected to Qt signals corrupt the heap at teardown
        # (proven: ~50% malloc abort with two lambda slots, 0/20
        # without them). Slots must be bound methods.
        path = os.path.join(os.path.dirname(__file__), "..",
                            "mimik", "gui.py")
        with open(path) as f:
            src = f.read()
        self.assertNotIn(".connect(lambda", src)
        self.assertNotIn(".connect(functools.partial", src)

    def test_no_em_dash_in_app_strings(self):
        # U33 (design-rules.md, immutable): no em-dashes in visible UI
        # text. Enforced on every string constant in every module under
        # mimik/ except docstrings — comments never reach the AST, and
        # a code string is one refactor from being displayed (GUI
        # status bar, dialogs, logs, CLI output, generated clients).
        # v0.4 swept 55 gui.py strings, then the verifier swept 108
        # more package-wide (Doctor tab, scan notes, CLI, generated
        # -client templates); this guard keeps them out.
        import ast

        root = os.path.join(os.path.dirname(__file__), "..", "mimik")
        bad = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                docstrings = set()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Module, ast.ClassDef,
                                         ast.FunctionDef,
                                         ast.AsyncFunctionDef)):
                        first = node.body[0] if node.body else None
                        if (isinstance(first, ast.Expr)
                                and isinstance(first.value, ast.Constant)
                                and isinstance(first.value.value, str)):
                            docstrings.add(id(first.value))
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Constant)
                            and isinstance(node.value, str)
                            and id(node) not in docstrings
                            and "—" in node.value):
                        bad.append((os.path.relpath(path, root),
                                    node.lineno, node.value[:60]))
        self.assertEqual(bad, [])


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

    def tearDown(self):
        # Deterministic cleanup: each test abandons a MainWindow whose
        # signal-connection cycles are only freed by the cyclic GC. Left
        # to accumulate, the GC fires mid-task (triggered by a worker
        # thread's allocations) and frees QObject wrappers while a worker
        # runs — a segfault in the next worker-thread requests call
        # (proven: GC disabled, or collected here, runs clean 3/3;
        # without it the suite segfaults). Collect here, when no worker
        # thread is running.
        self.app.processEvents()
        gc.collect()

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

    def test_title_shows_version(self):
        # Item 20: the version is in the title bar.
        from mimik import __version__
        from mimik.gui import MainWindow

        win = MainWindow()
        self.assertTrue(win.windowTitle().startswith("MIMIK "))
        self.assertIn(__version__, win.windowTitle())
        win.close()

    def test_about_button_opens_about_dialog(self):
        # Item 20: the Doctor tab's About button opens the About
        # dialog with the version and the mimic attribution.
        from PySide6.QtWidgets import QMessageBox, QPushButton
        from mimik import __version__
        from mimik.gui import MainWindow

        win = MainWindow()
        btns = [b for b in win.findChildren(QPushButton)
                if b.text() == "About MIMIK"]
        self.assertEqual(len(btns), 1)
        with mock.patch.object(QMessageBox, "about") as about:
            btns[0].click()
        about.assert_called_once()
        _parent, title, text = about.call_args.args
        self.assertEqual(title, "About MIMIK")
        self.assertIn(__version__, text)
        self.assertIn("littledivy/mimic", text)
        win.close()

    def test_record_steps_point_at_tabs_not_terminal(self):
        # Item 21: no `mimik ...` commands in the Record instructions
        # for any target; the steps point at GUI tabs and buttons.
        from mimik.gui import MainWindow

        win = MainWindow()
        for i in range(win.target_combo.count()):
            win.target_combo.setCurrentIndex(i)
            self.app.processEvents()
            text = win.steps.toPlainText()
            self.assertNotIn("mimik hosts", text)
            self.assertNotIn("mimik gen", text)
            self.assertNotIn("mimik record", text)
            self.assertNotIn("mimik --", text)
        win.target_combo.setCurrentIndex(0)  # Android
        self.assertIn("Hosts and Learn", win.steps.toPlainText())
        win.close()

    def test_doctor_hint_points_at_record_tab(self):
        # Item 21, same defect on the Doctor tab: the mitmweb hint
        # sends the user to the Record tab's Start proxy button,
        # not to the terminal.
        from mimik.gui import MainWindow

        win = MainWindow()
        result = {"required": [], "optional": [], "info": [],
                  "ok": True, "mitmweb_running": False}
        html = win._render_doctor(result)
        self.assertIn("Start proxy", html)
        self.assertNotIn("mimik record", html)
        win.close()

    def test_crash_handler_logs_and_shows_dialog(self):
        # Item 23: an uncaught exception is logged and the user is
        # told where the log is.
        from PySide6.QtWidgets import QMessageBox
        from mimik import gui as gui_mod
        from mimik import log as mimik_log
        from mimik.gui import MainWindow

        win = MainWindow()
        with mock.patch.object(QMessageBox, "critical") as critical, \
                mock.patch.object(sys, "__excepthook__"):
            gui_mod._handle_uncaught(ValueError, ValueError("boom-test"),
                                     None)
        critical.assert_called_once()
        _parent, title, text = critical.call_args.args
        self.assertIn("something went wrong", title)
        self.assertIn("boom-test", text)
        self.assertIn(mimik_log.log_path(), text)
        self.assertIn("boom-test", mimik_log.read_tail())
        win.close()

    def test_view_log_shows_log_tail(self):
        # Item 23: the Doctor tab's View log button shows the log's
        # tail in a dialog.
        from PySide6.QtWidgets import QDialog, QPushButton, QTextEdit
        from mimik import log as mimik_log
        from mimik.gui import MainWindow

        mimik_log.log_info("test", "view-log-marker-123")
        win = MainWindow()
        btns = [b for b in win.findChildren(QPushButton)
                if b.text() == "View log"]
        self.assertEqual(len(btns), 1)
        with mock.patch.object(QDialog, "exec", return_value=1):
            btns[0].click()
        dlg = win._log_dialog
        self.assertEqual(dlg.windowTitle(), "MIMIK log")
        text = dlg.findChild(QTextEdit).toPlainText()
        self.assertIn("view-log-marker-123", text)
        win.close()

    def test_filter_checkbox_toggles_setting(self):
        # Item 26: the Record tab's filter checkbox is the per-record
        # control — it reflects and persists the setting.
        from mimik import presets
        from mimik.gui import MainWindow

        presets.set_filtering_enabled(True)
        win = MainWindow()
        try:
            self.assertTrue(win.filter_check.isChecked())
            win.filter_check.setChecked(False)
            self.app.processEvents()
            self.assertFalse(presets.filtering_enabled())
            win.filter_check.setChecked(True)
            self.app.processEvents()
            self.assertTrue(presets.filtering_enabled())
        finally:
            presets.set_filtering_enabled(True)
            win.close()

    def test_excluded_hosts_dialog_edits_list(self):
        # Item 26: the Manage dialog adds, removes, and restores the
        # exclusion list.
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QDialog, QListWidget, QPushButton
        from mimik import filters, presets
        from mimik.gui import MainWindow

        presets.set_excluded_hosts(list(filters.DEFAULT_EXCLUDED))
        win = MainWindow()
        try:
            btns = [b for b in win.findChildren(QPushButton)
                    if b.text() == "Manage excluded hosts…"]
            self.assertEqual(len(btns), 1)
            with mock.patch.object(QDialog, "exec", return_value=1):
                btns[0].click()
            dlg = win._excluded_dialog
            self.assertEqual(dlg.windowTitle(), "Excluded hosts")
            lst = dlg.findChild(QListWidget)
            self.assertEqual(lst.count(), len(filters.DEFAULT_EXCLUDED))

            def _dlg_button(text):
                found = [b for b in dlg.findChildren(QPushButton)
                         if b.text() == text]
                self.assertEqual(len(found), 1, text)
                return found[0]

            # Add a host.
            win._excluded_edit.setText("telemetry.example.com")
            _dlg_button("Add").click()
            self.assertIn("telemetry.example.com",
                          presets.excluded_hosts())
            self.assertEqual(lst.count(), len(filters.DEFAULT_EXCLUDED) + 1)
            # Remove it.
            items = lst.findItems("telemetry.example.com",
                                  Qt.MatchExactly)
            self.assertEqual(len(items), 1)
            lst.setCurrentItem(items[0])
            _dlg_button("Remove selected").click()
            self.assertNotIn("telemetry.example.com",
                             presets.excluded_hosts())
            # Restore defaults.
            presets.set_excluded_hosts(["only.example.com"])
            _dlg_button("Restore defaults").click()
            self.assertEqual(presets.excluded_hosts(),
                             list(filters.DEFAULT_EXCLUDED))
            self.assertEqual(lst.count(), len(filters.DEFAULT_EXCLUDED))
        finally:
            presets.set_excluded_hosts(list(filters.DEFAULT_EXCLUDED))
            win.close()

    def test_export_import_preset_round_trip(self):
        """Item 27: export -> delete -> import restores the preset
        exactly, through the Presets tab buttons."""
        from PySide6.QtWidgets import QFileDialog
        from mimik import export, presets, store
        from mimik.gui import MainWindow

        store.save_flows([{
            "id": "p1",
            "request": {"host": "game.example.com", "method": "GET",
                        "path": "/api/users/123", "scheme": "https",
                        "port": 443,
                        "headers": [["Authorization",
                                     "Bearer <redacted>"]]},
            "response": {"status_code": 200, "headers": []},
        }])
        presets.learn("game.example.com")
        path = presets.preset_path("game.example.com", "host")
        with open(path, "rb") as fh:
            before = fh.read()
        dest = os.path.join(tempfile.mkdtemp(), "preset.zip")

        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(6)  # Presets tab loads on first show
        self.app.processEvents()
        win.presets_list.setCurrentRow(0)
        self.app.processEvents()
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(dest, "")):
            win._export_preset()
        self.assertTrue(os.path.isfile(dest))
        self.assertEqual(export.export_kind(dest), "preset")
        self.assertIn("Exported", win.presets_log.text())
        presets.delete_preset("game.example.com")
        self.assertFalse(os.path.isfile(path))
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(dest, "")):
            win._import_preset()
        self.assertIn("Imported", win.presets_log.text())
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(win.presets_list.count(), 1)
        win.close()

    def test_import_preset_conflict_asks_before_replacing(self):
        """Item 27: importing over an existing preset asks first —
        No keeps the current file, Yes restores the export."""
        import json
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from mimik import export, presets, store
        from mimik.gui import MainWindow

        store.save_flows([{
            "id": "p1",
            "request": {"host": "game.example.com", "method": "GET",
                        "path": "/api/users/123", "scheme": "https",
                        "port": 443, "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])
        presets.learn("game.example.com")
        dest = os.path.join(tempfile.mkdtemp(), "preset.zip")
        export.export_preset("game.example.com", dest)
        path = presets.preset_path("game.example.com", "host")
        with open(path, "rb") as fh:
            exported = fh.read()
        # Change the saved preset so Yes/No are distinguishable.
        data = json.loads(exported)
        data["endpoints"] = []
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)

        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(6)
        self.app.processEvents()
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(dest, "")), \
             mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.No):
            win._import_preset()
        with open(path, "rb") as fh:
            self.assertNotEqual(fh.read(), exported)
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(dest, "")), \
             mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.Yes):
            win._import_preset()
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), exported)
        win.close()

    def test_export_import_pack_round_trip(self):
        """Item 27: export -> delete -> import restores the pack
        exactly, through the Assets tab buttons."""
        import json
        from PySide6.QtWidgets import QFileDialog
        from mimik import assets, export, reblend_bridge
        from mimik.gui import MainWindow

        group = assets.AssetGroup("exp-pack.example.com", "traffic",
                                  host="exp-pack.example.com")
        group.add_bytes(b"\\x89PNG fake", "icon.png", "image")
        gdir, _m = group.finish()
        pdir = os.path.join(reblend_bridge.packs_dir(),
                            "exp-pack_20260101-000000")
        os.makedirs(os.path.join(pdir, "originals"))
        with open(os.path.join(pdir, "pack.json"), "w") as fh:
            json.dump({"app": "exp-pack", "host": "exp-pack.example.com",
                       "group": "exp-pack.example.com",
                       "built_at": "2026-01-01T00:00:00"}, fh)
        with open(os.path.join(pdir, "originals", "icon.png"),
                  "wb") as fh:
            fh.write(b"\\x89PNG fake")
        dest = os.path.join(tempfile.mkdtemp(), "pack.zip")

        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(3)  # Assets tab
        self.assertTrue(_wait_for(
            self.app, lambda: win.asset_group.findData(gdir) >= 0))
        win.asset_group.setCurrentIndex(win.asset_group.findData(gdir))
        self.assertTrue(win.open_pack_btn.isEnabled())
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(dest, "")):
            win._export_pack()
        self.assertTrue(os.path.isfile(dest))
        self.assertEqual(export.export_kind(dest), "pack")
        self.assertIn("Exported", win.assets_log.toPlainText())
        reblend_bridge.delete_pack(pdir)
        self.assertFalse(os.path.exists(pdir))
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(dest, "")):
            win._import_pack()
        self.assertTrue(os.path.isfile(
            os.path.join(pdir, "pack.json")))
        self.assertTrue(os.path.isfile(
            os.path.join(pdir, "originals", "icon.png")))
        self.assertIn("Imported", win.assets_log.toPlainText())
        win.close()

    def test_sample_offer_loads_and_guides(self):
        """Item 28: a fresh data dir offers the sample; accepting
        loads it and the guide jumps to the Generate tab with the
        sample host pre-selected."""
        from PySide6.QtWidgets import QDialog, QPushButton
        from mimik import presets, sample, store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            with mock.patch.object(QDialog, "exec", return_value=1):
                win._maybe_offer_sample()
            dlg = win._sample_dialog
            self.assertEqual(dlg.windowTitle(), "Welcome to MIMIK")
            load = [b for b in dlg.findChildren(QPushButton)
                    if b.text() == "Load the sample capture"]
            self.assertEqual(len(load), 1)
            with mock.patch.object(QDialog, "exec", return_value=1):
                load[0].click()
            self.assertEqual(len(store.load_host(sample.SAMPLE_HOST)),
                             6)
            self.assertIsNotNone(
                presets.load_preset(sample.SAMPLE_HOST))
            guide = win._sample_guide_dialog
            self.assertEqual(guide.windowTitle(), "Sample loaded")
            go = [b for b in guide.findChildren(QPushButton)
                  if b.text() == "Take me to Generate"]
            self.assertEqual(len(go), 1)
            go[0].click()
            self.assertEqual(win.tabs.currentIndex(), 2)
            self.assertTrue(_wait_for(
                self.app,
                lambda: win.gen_host.currentText() ==
                sample.SAMPLE_HOST))
            win.close()

    def test_sample_offer_decline_never_asks_again(self):
        """Item 28: declining the sample marks it offered — the
        offer never pops a second time."""
        from PySide6.QtWidgets import QDialog, QPushButton
        from mimik import presets
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            with mock.patch.object(QDialog, "exec", return_value=1):
                win._maybe_offer_sample()
            dlg = win._sample_dialog
            no = [b for b in dlg.findChildren(QPushButton)
                  if b.text() == "No thanks"]
            self.assertEqual(len(no), 1)
            no[0].click()
            self.assertTrue(presets.sample_offered())
            with mock.patch.object(QDialog, "exec") as exec_mock:
                win._maybe_offer_sample()
            exec_mock.assert_not_called()
            win.close()

    def test_doctor_sample_button_loads(self):
        """Item 28: the Doctor tab's Load sample capture button
        loads the sample without the first-run offer."""
        from PySide6.QtWidgets import QDialog, QPushButton
        from mimik import presets, sample, store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            win.tabs.setCurrentIndex(5)  # Doctor tab
            self.app.processEvents()
            btns = [b for b in win.findChildren(QPushButton)
                    if b.text() == "Load sample capture"]
            self.assertEqual(len(btns), 1)
            with mock.patch.object(QDialog, "exec", return_value=1):
                btns[0].click()
            guide = win._sample_guide_dialog
            explore = [b for b in guide.findChildren(QPushButton)
                       if b.text() == "I'll explore"]
            self.assertEqual(len(explore), 1)
            explore[0].click()
            self.assertEqual(len(store.load_host(sample.SAMPLE_HOST)),
                             6)
            self.assertIsNotNone(
                presets.load_preset(sample.SAMPLE_HOST))
            win.close()

    def test_smoke_test_refuses_foreign_file(self):
        """Item 29: a foreign file in clients/ is NOT executed when
        the confirmation is declined — the marker file it would
        write on import never appears."""
        from PySide6.QtWidgets import QMessageBox
        from mimik import store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            cdir = store.clients_dir()
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(cdir, "foreign_client.py"), "w") as fh:
                fh.write("open('pwned.txt', 'w').write('ran')\n")
            marker = os.path.join(cdir, "pwned.txt")
            win = MainWindow()
            win.show()
            win.tabs.setCurrentIndex(4)  # Clients tab
            self.assertTrue(_wait_for(
                self.app, lambda: any(
                    "foreign_client.py" in win.clients_list.item(i).text()
                    for i in range(win.clients_list.count()))))
            self.assertTrue(_select_client(win, "foreign_client.py"))
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.No):
                win._smoke_test()
            self.assertFalse(os.path.isfile(marker))
            self.assertIn("refused", win.smoke_log.toPlainText())
            win.close()

    def test_smoke_test_runs_foreign_file_on_confirm(self):
        """Item 29: the same foreign file DOES run when the user
        explicitly confirms — the marker file appears."""
        from PySide6.QtWidgets import QMessageBox
        from mimik import store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            cdir = store.clients_dir()
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(cdir, "foreign_client.py"), "w") as fh:
                fh.write("open('pwned.txt', 'w').write('ran')\n")
            marker = os.path.join(cdir, "pwned.txt")
            win = MainWindow()
            win.show()
            win.tabs.setCurrentIndex(4)  # Clients tab
            self.assertTrue(_wait_for(
                self.app, lambda: any(
                    "foreign_client.py" in win.clients_list.item(i).text()
                    for i in range(win.clients_list.count()))))
            self.assertTrue(_select_client(win, "foreign_client.py"))
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.Yes):
                win._smoke_test()
            self.assertTrue(_wait_for(
                self.app, lambda: win._task is None))
            self.assertTrue(os.path.isfile(marker))
            win.close()

    def test_smoke_test_runs_generated_client_without_asking(self):
        """Item 29: a generated client smoke-tests with no
        confirmation dialog — the marker is the trust."""
        from PySide6.QtWidgets import QMessageBox
        from mimik import codegen, store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                            ".."))
        env = {"MIMIK_DATA_DIR": empty,
               "PYTHONPATH": repo + os.pathsep +
               os.environ.get("PYTHONPATH", "")}
        with mock.patch.dict(os.environ, env):
            src = codegen.generate_deterministic("demo.example.com", [
                {"method": "GET", "path": "/api/items", "status": 200,
                 "query": "", "request_body": "",
                 "response_body": ""}])
            store.save_client_copy("demo_client.py", src)
            win = MainWindow()
            win.show()
            win.tabs.setCurrentIndex(4)  # Clients tab
            self.assertTrue(_wait_for(
                self.app, lambda: any(
                    "demo_client.py" in win.clients_list.item(i).text()
                    for i in range(win.clients_list.count()))))
            self.assertTrue(_select_client(win, "demo_client.py"))
            with mock.patch.object(QMessageBox, "question") as asked:
                win._smoke_test()
                asked.assert_not_called()
            self.assertTrue(_wait_for(
                self.app, lambda: win._task is None))
            self.assertIn("class: DemoClient", win.smoke_log.toPlainText())
            self.assertIn("get_items", win.smoke_log.toPlainText())
            win.close()

    def test_menu_bar_present(self):
        """Item 30: File / View / Help menus exist, with the Quit,
        Refresh, and tab-jump shortcuts wired."""
        from mimik.gui import MainWindow

        win = MainWindow()
        try:
            menus = {a.text(): a.menu()
                     for a in win.menuBar().actions()}
            self.assertEqual(set(menus), {"File", "View", "Help"})
            file_items = [a.text() for a in menus["File"].actions()
                          if not a.isSeparator()]
            self.assertIn("Import HAR…", file_items)
            self.assertIn("Import cURL…", file_items)
            self.assertIn("Quit", file_items)
            self.assertEqual(win._quit_action.shortcut().toString(),
                             "Ctrl+Q")
            view_items = [a.text() for a in menus["View"].actions()
                          if not a.isSeparator()]
            self.assertEqual(view_items[:7],
                             [win.tabs.tabText(i) for i in range(7)])
            self.assertIn("Refresh", view_items)
            self.assertEqual(win._refresh_action.shortcut().toString(),
                             "F5")
            for i, act in enumerate(win._tab_actions):
                self.assertEqual(act.shortcut().toString(),
                                 f"Ctrl+{i + 1}")
            help_items = [a.text() for a in menus["Help"].actions()]
            self.assertIn("About MIMIK", help_items)
            self.assertIn("View log", help_items)
        finally:
            win.close()

    def test_tab_shortcut_jumps(self):
        """Item 30: a View-menu tab action switches to that tab."""
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            win._tab_actions[2].trigger()  # Generate
            self.app.processEvents()
            self.assertEqual(win.tabs.currentIndex(), 2)
            win._tab_actions[5].trigger()  # Doctor
            self.app.processEvents()
            self.assertEqual(win.tabs.currentIndex(), 5)
        finally:
            win.close()

    def test_f5_refreshes_current_tab(self):
        """Item 30: F5 re-reads the current tab — a client saved
        after the tab loaded appears after a refresh."""
        from mimik import store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            store.save_client_copy("f5_alpha_client.py", "# alpha\n")
            win = MainWindow()
            win.show()
            win.tabs.setCurrentIndex(4)  # Clients tab
            self.assertTrue(_wait_for(
                self.app, lambda: any(
                    "f5_alpha_client.py" in win.clients_list.item(i).text()
                    for i in range(win.clients_list.count()))))
            store.save_client_copy("f5_beta_client.py", "# beta\n")
            win._refresh_action.trigger()
            self.app.processEvents()
            self.assertTrue(any(
                "f5_beta_client.py" in win.clients_list.item(i).text()
                for i in range(win.clients_list.count())))
            win.close()

    def test_help_about_opens_dialog(self):
        """Item 30: Help > About opens the About dialog (item 20)."""
        from PySide6.QtWidgets import QMessageBox
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            with mock.patch.object(QMessageBox, "about") as about:
                win._about_action.trigger()
                about.assert_called_once()
        finally:
            win.close()

    def test_ctrl_q_quits(self):
        """Item 30: the Quit action closes the window."""
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertTrue(win.isVisible())
        win._quit_action.trigger()
        self.app.processEvents()
        self.assertFalse(win.isVisible())

    def test_help_menu_has_update_check(self):
        """Item 31: Help > Check for updates… exists."""
        from mimik.gui import MainWindow

        win = MainWindow()
        try:
            help_menu = [a.menu() for a in win.menuBar().actions()
                         if a.text() == "Help"][0]
            items = [a.text() for a in help_menu.actions()]
            self.assertIn("Check for updates…", items)
            self.assertEqual(win._update_action.text(),
                             "Check for updates…")
        finally:
            win.close()

    def test_update_check_available_offers_releases_page(self):
        """Item 31: a newer release asks to open the releases page —
        Yes opens the URL."""
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import QMessageBox
        from mimik import update
        from mimik.gui import MainWindow

        newer = {"current": "0.3.0", "latest": "0.4.0",
                 "update_available": True,
                 "url": "https://github.com/x/releases/tag/v0.4.0",
                 "error": None}
        win = MainWindow()
        win.show()
        try:
            with mock.patch.object(update, "check_for_update",
                                   return_value=newer), \
                 mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.Yes) as q, \
                 mock.patch.object(QDesktopServices,
                                   "openUrl") as opener:
                win._check_for_updates()
                self.assertTrue(_wait_for(
                    self.app, lambda: win._task is None))
                q.assert_called_once()
                self.assertIn("0.4.0", q.call_args[0][2])
                opener.assert_called_once()
                self.assertEqual(
                    opener.call_args[0][0].toString(),
                    "https://github.com/x/releases/tag/v0.4.0")
        finally:
            win.close()

    def test_update_check_current_reports_latest(self):
        """Item 31: no newer release reports up to date."""
        from PySide6.QtWidgets import QMessageBox
        from mimik import update
        from mimik.gui import MainWindow

        current = {"current": "0.3.0", "latest": "0.3.0",
                   "update_available": False, "url": None, "error": None}
        win = MainWindow()
        win.show()
        try:
            with mock.patch.object(update, "check_for_update",
                                   return_value=current), \
                 mock.patch.object(QMessageBox,
                                   "information") as info:
                win._check_for_updates()
                self.assertTrue(_wait_for(
                    self.app, lambda: win._task is None))
                info.assert_called_once()
                self.assertIn("latest version", info.call_args[0][2])
        finally:
            win.close()

    def test_update_check_error_warns(self):
        """Item 31: a failed check warns instead of crashing."""
        from PySide6.QtWidgets import QMessageBox
        from mimik import update
        from mimik.gui import MainWindow

        error = {"current": "0.3.0", "latest": None,
                 "update_available": False, "url": None,
                 "error": "no route to host"}
        win = MainWindow()
        win.show()
        try:
            with mock.patch.object(update, "check_for_update",
                                   return_value=error), \
                 mock.patch.object(QMessageBox, "warning") as warn:
                win._check_for_updates()
                self.assertTrue(_wait_for(
                    self.app, lambda: win._task is None))
                warn.assert_called_once()
                self.assertIn("no route to host", warn.call_args[0][2])
        finally:
            win.close()

    def test_launch_edit_is_single_line(self):
        """Item 32d: the launch command box is a QLineEdit — a
        one-line shell command, not a multi-line editor."""
        from PySide6.QtWidgets import QLineEdit
        from mimik.gui import MainWindow

        win = MainWindow()
        try:
            self.assertIsInstance(win.launch_edit, QLineEdit)
            win.launch_edit.setText("firefox --private")
            self.assertEqual(win.launch_edit.text(), "firefox --private")
        finally:
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
        win.tabs.setCurrentIndex(6)  # Presets tab loads on first show
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
        win.tabs.setCurrentIndex(3)  # Assets tab loads on first show
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
        win.tabs.setCurrentIndex(3)  # Assets tab loads on first show
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
        win.show()
        win.tabs.setCurrentIndex(5)  # Doctor tab loads on first show
        self.assertTrue(_wait_for(
            self.app,
            lambda: "overall:" in win.doctor_text.toPlainText()))
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

    # ---- item 3: closeEvent + stale Wine state -------------------------------

    def test_close_without_recording_accepts(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertTrue(win.close())
        self.assertFalse(win.isVisible())

    def test_close_mid_record_keep_recording(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        worker = mock.Mock()
        worker.isRunning.return_value = True
        win.worker = worker
        win.show()
        self.app.processEvents()
        with mock.patch.object(win, "_confirm_stop_recording",
                               return_value=False):
            self.assertFalse(win.close())
        self.assertTrue(win.isVisible())
        worker.force_stop.assert_not_called()
        win.worker = None
        win.close()

    def test_close_mid_record_stop_and_close(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        worker = mock.Mock()
        worker.isRunning.return_value = True
        win.worker = worker
        win.show()
        self.app.processEvents()
        with mock.patch.object(win, "_confirm_stop_recording",
                               return_value=True):
            self.assertTrue(win.close())
        worker.force_stop.assert_called_once_with()
        self.assertFalse(win.isVisible())

    def test_force_stop_terminates_stuck_worker(self):
        import time

        from PySide6.QtWidgets import QApplication
        from mimik.gui import RecordWorker

        class StuckWorker(RecordWorker):
            def run(self):
                time.sleep(60)  # ignores _stop on purpose

        worker = StuckWorker("android")
        worker.start()
        self.assertTrue(worker.isRunning())
        worker.force_stop(timeout_ms=100)
        self.assertTrue(worker.isFinished())
        QApplication.instance().processEvents()

    def test_stale_wine_state_none_prompts_nothing(self):
        from PySide6.QtWidgets import QMessageBox
        from mimik import wine
        from mimik.gui import MainWindow

        win = MainWindow()
        with mock.patch.object(wine, "has_saved_state",
                               return_value=False), \
             mock.patch.object(QMessageBox, "question") as question:
            win.check_stale_wine_state()
        question.assert_not_called()
        win.close()

    def test_stale_wine_state_yes_restores(self):
        from PySide6.QtWidgets import QMessageBox
        from mimik import wine
        from mimik.gui import MainWindow

        win = MainWindow()
        with mock.patch.object(wine, "has_saved_state",
                               return_value=True), \
             mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.Yes), \
             mock.patch.object(wine, "revert_proxy") as revert, \
             mock.patch.object(QMessageBox, "information"):
            win.check_stale_wine_state()
        revert.assert_called_once_with()
        win.close()

    def test_stale_wine_state_no_leaves_it(self):
        from PySide6.QtWidgets import QMessageBox
        from mimik import wine
        from mimik.gui import MainWindow

        win = MainWindow()
        with mock.patch.object(wine, "has_saved_state",
                               return_value=True), \
             mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.No), \
             mock.patch.object(wine, "revert_proxy") as revert:
            win.check_stale_wine_state()
        revert.assert_not_called()
        win.close()

    # ---- item 4: worker threads, _flows_cache, lazy tabs --------------------

    def test_startup_fetches_no_flows(self):
        from mimik.gui import MainWindow
        from mimik.sources import mitm as mitm_mod
        from mimik import store

        def _boom(*args, **kwargs):
            raise AssertionError("startup fetched flows")

        with mock.patch.object(mitm_mod.Mitm, "flows", _boom), \
             mock.patch.object(store, "load_all", _boom):
            win = MainWindow()
            win.show()
            self.app.processEvents()
        win.close()

    def test_flows_cache_reused_until_invalidated(self):
        from mimik.gui import MainWindow
        from mimik.sources import mitm as mitm_mod

        win = MainWindow()
        mitm = mock.Mock()
        mitm.flows.return_value = []
        with mock.patch.object(mitm_mod, "Mitm", return_value=mitm):
            win._flows()
            win._flows()
            self.assertEqual(mitm.flows.call_count, 1)
            win._invalidate_flows()
            win._flows()
            self.assertEqual(mitm.flows.call_count, 2)
        win.close()

    def test_host_select_uses_cache_and_fills_table(self):
        from mimik import store
        from mimik.gui import MainWindow

        store.save_flows([{
            "id": "h1",
            "request": {"host": "game.example.com", "method": "GET",
                        "path": "/api/users/123", "scheme": "https",
                        "port": 443, "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])
        real_load_all = store.load_all
        calls = []

        def counting_load_all(*args, **kwargs):
            calls.append(1)
            return real_load_all(*args, **kwargs)

        win = MainWindow()
        win.show()
        with mock.patch.object(store, "load_all", counting_load_all):
            win.tabs.setCurrentIndex(1)  # Hosts tab loads on first show
            self.assertTrue(_wait_for(
                self.app, lambda: self._hosts_item(
                    win, "game.example.com") is not None))
            win.hosts_list.setCurrentItem(
                self._hosts_item(win, "game.example.com"))
            self.assertTrue(_wait_for(
                self.app, lambda: win.ep_table.rowCount() == 1))
        # One fetch for the tab load; host-select reused the cache.
        self.assertEqual(len(calls), 1)
        self.assertEqual(win.ep_table.item(0, 1).text(), "/api/users/123")
        win.close()

    def test_task_stays_responsive_and_cancel_discards(self):
        from PySide6.QtCore import QTimer
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        fired = []
        QTimer.singleShot(0, lambda: fired.append(True))

        def slow(worker):
            for _ in range(40):
                if worker.cancelled:
                    return "cancelled-early"
                time.sleep(0.05)
            return "finished"

        on_done = mock.Mock()
        self.assertTrue(win._run_task("slow work…", slow, on_done))
        self.assertTrue(win.progress_bar.isVisible())
        # The GUI thread is free while the task runs: the timer fires.
        self.assertTrue(_wait_for(self.app, lambda: bool(fired)))
        win._cancel_task()
        self.assertTrue(_wait_for(
            self.app, lambda: win._task is None, timeout=10))
        self.assertFalse(win.progress_bar.isVisible())
        on_done.assert_not_called()
        win.close()

    def test_folder_scan_cancel_stops_the_scan(self):
        from mimik import assets
        from mimik.gui import MainWindow

        # A folder big enough that the scan is still running when
        # Cancel lands (each file is read + hashed + copied).
        big = os.path.join(self.tmp, "big-folder")
        os.makedirs(big)
        for i in range(2000):
            with open(os.path.join(big, f"img{i:04d}.png"), "wb") as fh:
                fh.write(b"\x89PNG" + bytes([i % 256]) * 4096)
        win = MainWindow()
        win.show()

        def _scan_started():
            # The scan copies files into the group dir as it goes —
            # wait until at least one has landed before cancelling, so
            # the cancel provably interrupts a scan in flight. (A bare
            # event-loop turn doesn't prove the worker thread started:
            # under load the cancel used to land before the first
            # file, and the scan correctly returned no group at all.)
            base = os.path.join(self.tmp, "assets")
            if not os.path.isdir(base):
                return False
            for d in os.listdir(base):
                if not d.startswith("big_"):
                    continue
                gdir = os.path.join(base, d)
                if (os.path.isdir(gdir)
                        and any(os.path.isfile(os.path.join(gdir, f))
                                for f in os.listdir(gdir))):
                    return True
            return False

        seen = []

        def work(worker):
            gdir, manifest = assets.scan_program_tree(
                big, "big", cancelled=lambda: worker.cancelled)
            seen.append(manifest)
            return gdir, manifest

        on_done = mock.Mock()
        self.assertTrue(win._run_task("scanning…", work, on_done))
        self.assertTrue(_wait_for(self.app, _scan_started, timeout=15))
        win._cancel_task()
        self.assertTrue(_wait_for(
            self.app, lambda: win._task is None, timeout=10))
        on_done.assert_not_called()
        # The scan stopped mid-folder: partial results with the note.
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])
        self.assertIn("cancelled", seen[0].get("scan_note", ""))
        self.assertLess(len(seen[0]["assets"]), 2000)
        win.close()

    def test_close_mid_fast_task_closes(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()

        def quick(worker):
            time.sleep(0.2)
            return "done"

        done = []
        win._run_task("quick work…", quick, lambda ok, v: done.append(v))
        # Task still running, but it settles inside the close wait.
        self.assertTrue(win.close())
        self.assertFalse(win.isVisible())

    def test_close_mid_blocking_task_defers_until_task_ends(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()

        def blocking(worker):
            # Ignores cancel on purpose (like a blocking Ollama POST).
            time.sleep(3)
            return "done"

        done = []
        win._run_task("blocking work…", blocking,
                      lambda ok, v: done.append(v))
        # The 2 s close wait expires — the close defers, window stays.
        self.assertFalse(win.close())
        self.assertTrue(win.isVisible())
        self.assertTrue(win._close_pending)
        # The task ends on its own; the window closes by itself.
        self.assertTrue(_wait_for(
            self.app, lambda: not win.isVisible(), timeout=10))
        self.assertIsNone(win._task)
        # Cancelled by the close, so the result was discarded.
        self.assertEqual(done, [])

    def test_generate_runs_off_thread(self):
        from mimik import store
        from mimik.gui import MainWindow

        store.save_flows([{
            "id": "g1",
            "request": {"host": "game.example.com", "method": "GET",
                        "path": "/api/users/123", "scheme": "https",
                        "port": 443, "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])
        tmp = tempfile.mkdtemp()
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            win = MainWindow()
            win.show()
            win.tabs.setCurrentIndex(2)  # Generate tab
            self.assertTrue(_wait_for(
                self.app,
                lambda: win.gen_host.findText("game.example.com") >= 0))
            win.gen_host.setCurrentText("game.example.com")
            win._generate()
            self.assertTrue(_wait_for(
                self.app,
                lambda: "wrote" in win.gen_log.toPlainText()))
            self.assertTrue(os.path.exists(win.gen_out))
            self.assertTrue(win.open_btn.isEnabled())
            self.assertTrue(win.copy_btn.isEnabled())
            win.close()
        finally:
            os.chdir(old_cwd)

    def _generate_with_existing(self, choice):
        """Seed game.example.com flows, pre-create its client file in a
        fresh cwd, run Generate with _ask_overwrite answering `choice`.
        Returns (win, tmp, original_content)."""
        from mimik import store
        from mimik.gui import MainWindow

        store.save_flows([{
            "id": "g1",
            "request": {"host": "game.example.com", "method": "GET",
                        "path": "/api/users/123", "scheme": "https",
                        "port": 443, "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])
        tmp = tempfile.mkdtemp()
        old_cwd = os.getcwd()
        os.chdir(tmp)
        original = "# the user's old client — must survive a re-generate\n"
        with open(os.path.join(tmp, "game_example_com_client.py"),
                  "w") as f:
            f.write(original)
        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(2)  # Generate tab
        self.assertTrue(_wait_for(
            self.app,
            lambda: win.gen_host.findText("game.example.com") >= 0))
        win.gen_host.setCurrentText("game.example.com")
        with mock.patch.object(win, "_ask_overwrite",
                               return_value=choice) as patched:
            win._generate()
            if choice is None:
                self.assertTrue(_wait_for(
                    self.app,
                    lambda: "not saved" in win.gen_log.toPlainText()))
            else:
                self.assertTrue(_wait_for(
                    self.app,
                    lambda: "wrote" in win.gen_log.toPlainText()))
            patched.assert_called_once()
        os.chdir(old_cwd)
        return win, tmp, original

    def test_generate_overwrite_numbered(self):
        win, tmp, original = self._generate_with_existing("numbered")
        try:
            second = os.path.join(tmp, "game_example_com_client_2.py")
            self.assertTrue(os.path.exists(second))
            self.assertEqual(win.gen_out, second)
            with open(os.path.join(tmp, "game_example_com_client.py")) as f:
                self.assertEqual(f.read(), original)
        finally:
            win.close()

    def test_generate_overwrite_accepts(self):
        win, tmp, original = self._generate_with_existing("overwrite")
        try:
            with open(os.path.join(tmp, "game_example_com_client.py")) as f:
                self.assertNotEqual(f.read(), original)
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "game_example_com_client_2.py")))
        finally:
            win.close()

    def test_generate_overwrite_cancel(self):
        win, tmp, original = self._generate_with_existing(None)
        try:
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "game_example_com_client_2.py")))
            with open(os.path.join(tmp, "game_example_com_client.py")) as f:
                self.assertEqual(f.read(), original)
            self.assertFalse(win.open_btn.isEnabled())
            self.assertIn("class", win.gen_log.toPlainText())
        finally:
            win.close()

    def _seed_preview_group(self, name, files):
        """Seed an asset group `name` with [(filename, bytes, kind)].
        Returns the group dir."""
        from mimik import assets

        group = assets.AssetGroup(name, "traffic", host=name)
        for filename, data, kind in files:
            group.add_bytes(data, filename, kind)
        gdir, _m = group.finish()
        return gdir

    def _select_asset_group(self, win, gdir):
        """Show the Assets tab and select the group at `gdir`."""
        win.tabs.setCurrentIndex(3)  # Assets tab
        self.assertTrue(_wait_for(
            self.app, lambda: win.asset_group.findData(gdir) >= 0))
        win.asset_group.setCurrentIndex(win.asset_group.findData(gdir))
        self.app.processEvents()

    def test_preview_image_double_click(self):
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication, QDialog, QLabel
        from mimik.gui import MainWindow

        img = QImage(64, 48, QImage.Format_RGB32)
        img.fill(0xFFFF0000)  # opaque red
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        gdir = self._seed_preview_group(
            "preview-image.example.com",
            [("test.png", bytes(buf.data()), "image")])
        win = MainWindow()
        win.show()
        try:
            self._select_asset_group(win, gdir)
            self.assertEqual(win.asset_table.rowCount(), 1)
            # Double-click the Kind column — any column previews.
            win.asset_table.itemDoubleClicked.emit(
                win.asset_table.item(0, 1))
            self.app.processEvents()
            dialogs = [w for w in QApplication.topLevelWidgets()
                       if isinstance(w, QDialog) and w.isVisible()]
            self.assertEqual(len(dialogs), 1)
            dlg = dialogs[0]
            self.assertEqual(dlg.windowTitle(), "test.png")
            shown = [c for c in dlg.findChildren(QLabel)
                     if c.pixmap() is not None and not c.pixmap().isNull()]
            self.assertEqual(len(shown), 1)
            info = " ".join(c.text() for c in dlg.findChildren(QLabel))
            self.assertIn("64 × 48 px", info)
            dlg.close()
        finally:
            win.close()

    def test_preview_model_double_click(self):
        from PySide6.QtWidgets import QApplication, QDialog, QTextEdit
        from mimik.gui import MainWindow

        obj = "".join(f"v {i} 0 0\n" for i in range(8)) + "f 1 2 3\n"
        gdir = self._seed_preview_group(
            "preview-model.example.com",
            [("cube.obj", obj.encode(), "model")])
        win = MainWindow()
        win.show()
        try:
            self._select_asset_group(win, gdir)
            win.asset_table.itemDoubleClicked.emit(
                win.asset_table.item(0, 0))
            self.app.processEvents()
            dialogs = [w for w in QApplication.topLevelWidgets()
                       if isinstance(w, QDialog) and w.isVisible()]
            self.assertEqual(len(dialogs), 1)
            texts = dialogs[0].findChildren(QTextEdit)
            self.assertEqual(len(texts), 1)
            body = texts[0].toPlainText()
            self.assertIn("vertices: 8", body)
            self.assertIn("faces: 1", body)
            self.assertIn("cube.obj", body)
            dialogs[0].close()
        finally:
            win.close()

    def test_preview_audio_opens_player(self):
        from mimik.gui import MainWindow

        gdir = self._seed_preview_group(
            "preview-audio.example.com",
            [("song.mp3", b"ID3-fake", "audio")])
        win = MainWindow()
        win.show()
        try:
            self._select_asset_group(win, gdir)
            with mock.patch("mimik.gui.QDesktopServices") as desktop:
                win.asset_table.itemDoubleClicked.emit(
                    win.asset_table.item(0, 0))
                self.app.processEvents()
            desktop.openUrl.assert_called_once()
            url = desktop.openUrl.call_args[0][0]
            self.assertTrue(url.toLocalFile().endswith("song.mp3"))
        finally:
            win.close()

    def test_reveal_asset_opens_folder(self):
        from mimik.gui import MainWindow

        gdir = self._seed_preview_group(
            "preview-reveal.example.com",
            [("icon.png", b"\\x89PNG-fake", "image")])
        win = MainWindow()
        win.show()
        try:
            self._select_asset_group(win, gdir)
            win.asset_table.setCurrentCell(0, 0)
            with mock.patch("mimik.gui.QDesktopServices") as desktop:
                win._reveal_asset()
            desktop.openUrl.assert_called_once()
            url = desktop.openUrl.call_args[0][0]
            self.assertEqual(url.toLocalFile(), gdir)
        finally:
            win.close()

    def test_open_client_double_click(self):
        from PySide6.QtCore import Qt
        from mimik import store
        from mimik.gui import MainWindow

        path = store.save_client_copy("previewme_client.py", "# client\n")
        win = MainWindow()
        win.show()
        try:
            win.tabs.setCurrentIndex(4)  # Clients tab
            self.assertTrue(_wait_for(
                self.app, lambda: win.clients_list.count() >= 1))
            found = None
            for i in range(win.clients_list.count()):
                item = win.clients_list.item(i)
                if item.data(Qt.UserRole) == path:
                    found = item
                    break
            self.assertIsNotNone(found)
            with mock.patch("mimik.gui.QDesktopServices") as desktop:
                win.clients_list.itemDoubleClicked.emit(found)
                self.app.processEvents()
            desktop.openUrl.assert_called_once()
            url = desktop.openUrl.call_args[0][0]
            self.assertEqual(url.toLocalFile(), path)
        finally:
            win.close()

    def test_reveal_client_opens_folder(self):
        from PySide6.QtCore import Qt
        from mimik import store
        from mimik.gui import MainWindow

        path = store.save_client_copy("revealme_client.py", "# client\n")
        win = MainWindow()
        win.show()
        try:
            win.tabs.setCurrentIndex(4)  # Clients tab
            self.assertTrue(_wait_for(
                self.app, lambda: win.clients_list.count() >= 1))
            found = None
            for i in range(win.clients_list.count()):
                item = win.clients_list.item(i)
                if item.data(Qt.UserRole) == path:
                    found = item
                    break
            self.assertIsNotNone(found)
            win.clients_list.setCurrentItem(found)
            with mock.patch("mimik.gui.QDesktopServices") as desktop:
                win._reveal_client()
            desktop.openUrl.assert_called_once()
            url = desktop.openUrl.call_args[0][0]
            self.assertEqual(url.toLocalFile(), os.path.dirname(path))
        finally:
            win.close()

    def test_generate_for_host_jumps(self):
        from PySide6.QtWidgets import QPushButton
        from mimik import store
        from mimik.gui import MainWindow

        store.save_flows([{
            "id": "g1",
            "request": {"host": "game.example.com", "method": "GET",
                        "path": "/api/users/123", "scheme": "https",
                        "port": 443, "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])
        win = MainWindow()
        win.show()
        try:
            win.tabs.setCurrentIndex(1)  # Hosts tab
            self.assertTrue(_wait_for(
                self.app,
                lambda: self._hosts_item(win, "game.example.com")
                is not None))
            win.hosts_list.setCurrentItem(
                self._hosts_item(win, "game.example.com"))
            btn = next(b for b in win.findChildren(QPushButton)
                       if b.text() == "Generate client…")
            btn.click()
            self.assertTrue(_wait_for(
                self.app,
                lambda: win.tabs.currentIndex() == 2
                and win.gen_host.currentText() == "game.example.com"))
        finally:
            win.close()

    def _make_test_zip(self):
        import zipfile

        zpath = os.path.join(tempfile.mkdtemp(), "app.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("myapp", bytes([0x7F]) + b"ELF" + bytes(100))
        return zpath

    def test_worker_uses_passed_plan_without_repreparing(self):
        from mimik import launch
        from mimik.gui import RecordWorker
        from mimik.sources import mitm as mitm_mod

        cache = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_CACHE_DIR": cache}):
            plan = launch.prepare_file(self._make_test_zip())
            self.assertIsNotNone(plan.command)
            proxy = mock.Mock()
            proxy.start.side_effect = mitm_mod.MitmError("no proxy here")
            worker = RecordWorker("file", plan=plan)
            with mock.patch("mimik.gui.ProxySession",
                            return_value=proxy), \
                 mock.patch("mimik.gui.launch.prepare_file",
                            side_effect=AssertionError(
                                "must not re-prepare")):
                worker.run()
            self.assertIs(worker._plan, plan)

    def test_worker_prepares_when_no_plan(self):
        from mimik import launch
        from mimik.gui import RecordWorker
        from mimik.sources import mitm as mitm_mod

        cache = tempfile.mkdtemp()
        zpath = self._make_test_zip()
        with mock.patch.dict(os.environ, {"MIMIK_CACHE_DIR": cache}):
            proxy = mock.Mock()
            proxy.start.side_effect = mitm_mod.MitmError("no proxy here")
            worker = RecordWorker("file", file_path=zpath)
            with mock.patch("mimik.gui.ProxySession",
                            return_value=proxy), \
                 mock.patch("mimik.gui.launch.prepare_file",
                            wraps=launch.prepare_file) as prepared:
                worker.run()
            prepared.assert_called_once()
            self.assertIsNotNone(worker._plan.command)

    def test_start_recording_passes_plan_to_worker(self):
        from mimik.gui import MainWindow

        cache = tempfile.mkdtemp()
        zpath = self._make_test_zip()
        win = MainWindow()
        win.show()
        try:
            with mock.patch.dict(os.environ, {"MIMIK_CACHE_DIR": cache}):
                win.target_combo.setCurrentIndex(
                    win.target_combo.findData("file"))
                win.file_path = zpath
                win._prepare_file()
                self.assertIsNotNone(win.plan)
                self.assertIsNotNone(win.plan.command)
                with mock.patch("mimik.gui.RecordWorker") as worker_cls:
                    win._start_recording()
                worker_cls.assert_called_once()
                _args, kwargs = worker_cls.call_args
                self.assertIs(kwargs["plan"], win.plan)
                win.worker = None  # the mock is not a real thread —
                # don't let closeEvent ask about it
        finally:
            win.close()

    def _hosts_item(self, win, host):
        from PySide6.QtCore import Qt

        for i in range(win.hosts_list.count()):
            item = win.hosts_list.item(i)
            if item.data(Qt.UserRole) == host:
                return item
        return None

    def test_filter_endpoints_table(self):
        # Item 22: typing in the endpoints filter hides non-matching
        # rows live; clearing it shows them all again.
        from mimik import store
        from mimik.gui import MainWindow

        host = "filter-ep.example.com"
        store.save_flows([{
            "id": f"e{i}",
            "request": {"host": host, "method": "GET",
                        "path": path, "scheme": "https",
                        "port": 443, "headers": []},
            "response": {"status_code": 200, "headers": []},
        } for i, path in enumerate(
            ["/api/users/123", "/api/posts/456", "/api/widgets/789"])])
        win = MainWindow()
        win.show()
        try:
            win.tabs.setCurrentIndex(1)  # Hosts tab
            self.assertTrue(_wait_for(
                self.app, lambda: self._hosts_item(win, host) is not None))
            win.hosts_list.setCurrentItem(self._hosts_item(win, host))
            self.assertTrue(_wait_for(
                self.app, lambda: win.ep_table.rowCount() == 3))
            win.ep_filter.setText("users")
            self.app.processEvents()
            visible = [win.ep_table.item(r, 1).text()
                       for r in range(win.ep_table.rowCount())
                       if not win.ep_table.isRowHidden(r)]
            self.assertEqual(visible, ["/api/users/123"])
            self.assertEqual(win.ep_filter_info.text(), "1 of 3")
            win.ep_filter.setText("")
            self.app.processEvents()
            visible = [r for r in range(win.ep_table.rowCount())
                       if not win.ep_table.isRowHidden(r)]
            self.assertEqual(visible, [0, 1, 2])
            self.assertEqual(win.ep_filter_info.text(), "")
        finally:
            win.close()

    def test_filter_assets_table(self):
        # Item 22: the same live filter on the Assets table,
        # case-insensitive.
        from mimik.gui import MainWindow

        gdir = self._seed_preview_group(
            "filter-assets.example.com",
            [("logo.png", b"\x89PNG-fake", "image"),
             ("crate.glb", b"glTF-fake", "model")])
        win = MainWindow()
        win.show()
        try:
            self._select_asset_group(win, gdir)
            self.assertEqual(win.asset_table.rowCount(), 2)
            win.asset_filter.setText("logo")
            self.app.processEvents()
            visible = [win.asset_table.item(r, 0).text()
                       for r in range(win.asset_table.rowCount())
                       if not win.asset_table.isRowHidden(r)]
            self.assertEqual(visible, ["logo.png"])
            self.assertEqual(win.asset_filter_info.text(), "1 of 2")
            win.asset_filter.setText("GLB")
            self.app.processEvents()
            visible = [win.asset_table.item(r, 0).text()
                       for r in range(win.asset_table.rowCount())
                       if not win.asset_table.isRowHidden(r)]
            self.assertEqual(visible, ["crate.glb"])
            win.asset_filter.setText("")
            self.app.processEvents()
            visible = [r for r in range(win.asset_table.rowCount())
                       if not win.asset_table.isRowHidden(r)]
            self.assertEqual(visible, [0, 1])
            self.assertEqual(win.asset_filter_info.text(), "")
        finally:
            win.close()

    def test_filter_clients_list(self):
        # Item 22: the same live filter on the Clients list.
        from mimik import store
        from mimik.gui import MainWindow

        store.save_client_copy("filter_alpha_client.py", "# alpha\n")
        store.save_client_copy("filter_beta_client.py", "# beta\n")
        win = MainWindow()
        win.show()
        try:
            win.tabs.setCurrentIndex(4)  # Clients tab
            self.assertTrue(_wait_for(
                self.app, lambda: any(
                    "filter_alpha_client.py" in
                    win.clients_list.item(i).text()
                    for i in range(win.clients_list.count()))))
            win.clients_filter.setText("alpha")
            self.app.processEvents()
            visible = [win.clients_list.item(i).text()
                       for i in range(win.clients_list.count())
                       if not win.clients_list.item(i).isHidden()]
            self.assertEqual(len(visible), 1)
            self.assertIn("filter_alpha_client.py", visible[0])
            self.assertTrue(
                win.clients_filter_info.text().startswith("1 of "))
            win.clients_filter.setText("")
            self.app.processEvents()
            visible = [win.clients_list.item(i).text()
                       for i in range(win.clients_list.count())
                       if not win.clients_list.item(i).isHidden()]
            self.assertTrue(any("filter_alpha_client.py" in t
                                for t in visible))
            self.assertTrue(any("filter_beta_client.py" in t
                                for t in visible))
            self.assertEqual(win.clients_filter_info.text(), "")
        finally:
            win.close()

    def test_hosts_tab_shows_sizes(self):
        from mimik import store
        from mimik.gui import MainWindow

        store.save_flows([{
            "id": "s1",
            "request": {"host": "sized.example.com", "method": "GET",
                        "path": "/x", "scheme": "https", "port": 443,
                        "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])
        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(1)  # Hosts tab
        self.assertTrue(_wait_for(
            self.app,
            lambda: self._hosts_item(win, "sized.example.com") is not None))
        text = self._hosts_item(win, "sized.example.com").text()
        self.assertIn("sized.example.com", text)
        self.assertIn("(", text)
        self.assertIn("B)", text)  # e.g. "(512 B)" or "(1.2 KB)"
        win.close()

    def test_delete_host_removes_captures_and_updates_list(self):
        from PySide6.QtWidgets import QMessageBox
        from mimik import store
        from mimik.gui import MainWindow

        store.save_flows([{
            "id": "d1",
            "request": {"host": "delete-me.example.com", "method": "GET",
                        "path": "/x", "scheme": "https", "port": 443,
                        "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])
        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(1)  # Hosts tab
        self.assertTrue(_wait_for(
            self.app, lambda: self._hosts_item(
                win, "delete-me.example.com") is not None))
        item = self._hosts_item(win, "delete-me.example.com")
        win.hosts_list.setCurrentItem(item)
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.Yes):
            win._delete_host()
        self.assertTrue(_wait_for(
            self.app, lambda: self._hosts_item(
                win, "delete-me.example.com") is None))
        self.assertEqual(store.load_host("delete-me.example.com"), [])
        win.close()

    def test_delete_client_removes_copy(self):
        from PySide6.QtWidgets import QMessageBox
        from mimik import store
        from mimik.gui import MainWindow

        path = os.path.join(self.tmp, "delme_client.py")
        with open(path, "w") as fh:
            fh.write("# client\n")
        kept = store.save_client_copy("delme_client.py", path)
        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(4)  # Clients tab
        self.assertTrue(_wait_for(
            self.app, lambda: win.clients_list.count() >= 1))
        found = None
        from PySide6.QtCore import Qt
        for i in range(win.clients_list.count()):
            item = win.clients_list.item(i)
            if item.data(Qt.UserRole) == kept:
                found = item
                break
        self.assertIsNotNone(found)
        win.clients_list.setCurrentItem(found)
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.Yes):
            win._delete_client()
        self.assertFalse(os.path.exists(kept))
        self.assertEqual(store.list_clients(), [])
        win.close()

    def test_delete_group_removes_dir(self):
        from PySide6.QtWidgets import QMessageBox
        from mimik import assets
        from mimik.gui import MainWindow

        group = assets.AssetGroup("del-group.example.com", "traffic",
                                  host="del-group.example.com")
        group.add_bytes(b"\x89PNG fake", "icon.png", "image")
        gdir, _m = group.finish()
        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(3)  # Assets tab
        self.assertTrue(_wait_for(
            self.app, lambda: win.asset_group.findData(gdir) >= 0))
        win.asset_group.setCurrentIndex(win.asset_group.findData(gdir))
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.Yes):
            win._delete_group()
        self.assertFalse(os.path.exists(gdir))
        self.assertEqual(win.asset_group.findData(gdir), -1)
        win.close()

    def test_delete_pack_removes_dir(self):
        import json
        from PySide6.QtWidgets import QMessageBox
        from mimik import assets, reblend_bridge
        from mimik.gui import MainWindow

        group = assets.AssetGroup("del-pack.example.com", "traffic",
                                  host="del-pack.example.com")
        group.add_bytes(b"\x89PNG fake", "icon.png", "image")
        gdir, _m = group.finish()
        pdir = os.path.join(reblend_bridge.packs_dir(),
                            "del-pack_20260101-000000")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "pack.json"), "w") as fh:
            json.dump({"app": "del-pack", "host": "del-pack.example.com",
                       "group": "del-pack.example.com",
                       "built_at": "2026-01-01T00:00:00",
                       "scene": "scene.json"}, fh)
        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(3)  # Assets tab
        self.assertTrue(_wait_for(
            self.app, lambda: win.asset_group.findData(gdir) >= 0))
        win.asset_group.setCurrentIndex(win.asset_group.findData(gdir))
        self.assertTrue(win.open_pack_btn.isEnabled())
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.Yes):
            win._delete_pack()
        self.assertFalse(os.path.exists(pdir))
        self.assertFalse(win.open_pack_btn.isEnabled())
        win.close()

    def test_doctor_tab_colors_lines(self):
        """Item 12: the Doctor tab renders the shared check in color —
        green for passing, red for missing (the header's promise)."""
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            # Force the proxy check red so both colors are present
            # whatever the machine has installed.
            with mock.patch("mimik.doctor.mitmweb_cmd",
                            return_value=None):
                win.tabs.setCurrentIndex(5)  # Doctor tab
                self.assertTrue(_wait_for(
                    self.app,
                    lambda: "overall:" in win.doctor_text.toPlainText()))
            text = win.doctor_text.toPlainText()
            for name in ("python", "requests library",
                         "proxy (mitmweb or uvx)", "Qt GUI (PySide6)",
                         "mitmweb running + reachable",
                         "Ollama (optional polish)", "mitmproxy CA cert",
                         "wine (Windows capture target)",
                         "WinLinx (Windows capture target)",
                         "Reblend (asset packs)"):
                self.assertIn(name, text)
            html = win.doctor_text.toHtml()
            self.assertIn("#66bb6a", html)  # green: passing checks
            self.assertIn("#ef5350", html)  # red: the missing proxy
            self.assertIn("overall: SOMETHING IS MISSING", text)
        finally:
            win.close()

    def test_doctor_tab_shows_disk_usage(self):
        from mimik import store
        from mimik.gui import MainWindow

        store.save_flows([{
            "id": "u1",
            "request": {"host": "usage.example.com", "method": "GET",
                        "path": "/x", "scheme": "https", "port": 443,
                        "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])
        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(5)  # Doctor tab
        self.assertTrue(_wait_for(
            self.app, lambda: "overall:" in win.doctor_text.toPlainText()))
        text = win.doctor_text.toPlainText()
        self.assertIn("disk usage:", text)
        self.assertIn("captures", text)
        win.close()

    def test_empty_states_show_guidance(self):
        """Item 6: with an empty data dir every tab shows guidance,
        not blank black boxes."""
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()

            # Record: start-here order + live-hosts empty state.
            self.assertIn("Record", win.start_here.text())
            self.assertIn("Hosts", win.start_here.text())
            self.assertIn("Generate", win.start_here.text())
            self.assertEqual(win.host_list.count(), 1)
            self.assertIn("No live hosts yet",
                          win.host_list.item(0).text())

            # Hosts: empty list + "select a host" endpoints table.
            win.tabs.setCurrentIndex(1)
            self.assertTrue(_wait_for(
                self.app, lambda: win._task is None))
            self.assertEqual(win.hosts_list.count(), 1)
            self.assertIn("No captures yet",
                          win.hosts_list.item(0).text())
            self.assertIn("Select a host",
                          win.ep_table.item(0, 0).text())

            # Generate: empty combo placeholder + log placeholder.
            win.tabs.setCurrentIndex(2)
            self.assertTrue(_wait_for(
                self.app, lambda: win._task is None))
            self.assertEqual(win.gen_host.count(), 0)
            self.assertIn("No hosts yet",
                          win.gen_host.lineEdit().placeholderText())
            self.assertIn("Generate a client",
                          win.gen_log.placeholderText())

            # Assets: empty group combo + empty table.
            win.tabs.setCurrentIndex(3)
            self.app.processEvents()
            self.assertIn("No captures yet",
                          win.asset_group.currentText())
            self.assertIn("No assets yet",
                          win.asset_table.item(0, 0).text())

            # Clients: empty list.
            win.tabs.setCurrentIndex(4)
            self.app.processEvents()
            self.assertEqual(win.clients_list.count(), 1)
            self.assertIn("No clients yet",
                          win.clients_list.item(0).text())

            # Doctor: the check still runs (placeholder pre-load).
            win.tabs.setCurrentIndex(5)
            self.assertTrue(_wait_for(
                self.app,
                lambda: "overall:" in win.doctor_text.toPlainText()))
            self.assertIn("Running the setup check",
                          win.doctor_text.placeholderText())

            # Presets: empty source + both empty lists.
            win.tabs.setCurrentIndex(6)
            self.app.processEvents()
            self.assertEqual(win.preset_source.currentText(),
                             "No captures yet")
            self.assertIn("No presets yet",
                          win.presets_list.item(0).text())
            self.assertIn("No pattern presets yet",
                          win.pattern_presets_list.item(0).text())
            self.assertIn("Select a preset",
                          win.preset_detail.placeholderText())
            win.close()

    def test_presets_intro_is_short(self):
        """Item 6: the Presets wall of text is one short line now."""
        from mimik.gui import MainWindow

        win = MainWindow()
        presets_tab = win.tabs.widget(6)
        intro = presets_tab.layout().itemAt(0).widget()
        self.assertLess(len(intro.text()), 200)
        self.assertIn("never hold passwords", intro.text())
        win.close()

    def test_status_bar_shows_idle_state(self):
        """Item 7: idle shows proxy stopped + the saved-flows total."""
        from mimik import store
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.assertEqual(win.status_proxy.text(), "Proxy stopped")
        saved = sum(n for _h, n, _t in store.list_captures())
        noun = "flow" if saved == 1 else "flows"
        self.assertEqual(win.status_flows.text(), f"{saved} saved {noun}")
        self.assertFalse(win._status_timer.isActive())
        win.close()

    def test_status_bar_shows_recording_state(self):
        """Item 7: recording shows elapsed time + live flows/hosts."""
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win.worker = mock.Mock()
        win._record_started = time.monotonic() - 125  # 2:05 ago
        win._flow_count = 142
        win._host_count = 3
        win._update_status_bar()
        self.assertIn("Recording", win.status_proxy.text())
        self.assertIn("02:05", win.status_proxy.text())
        self.assertEqual(win.status_flows.text(), "142 flows · 3 hosts")
        win.worker = None
        win.close()

    def test_show_hosts_updates_flow_count(self):
        """Item 7: live host updates feed the status bar's flow count."""
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win.worker = mock.Mock()
        win._record_started = time.monotonic()
        win._show_hosts([("a.example.com", 5), ("b.example.com", 7)])
        self.assertEqual(win.status_flows.text(), "12 flows · 2 hosts")
        self.assertEqual(win.host_list.count(), 2)
        win.worker = None
        win.close()

    def test_recording_finished_returns_to_idle(self):
        """Item 7: record-stop stops the timer and restores the idle
        status bar with the new saved total."""
        from mimik import store
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win.worker = mock.Mock()
        win._record_started = time.monotonic() - 30
        win._status_timer.start()
        win._recording_finished()
        self.assertIsNone(win.worker)
        self.assertFalse(win._status_timer.isActive())
        self.assertEqual(win.status_proxy.text(), "Proxy stopped")
        saved = sum(n for _h, n, _t in store.list_captures())
        noun = "flow" if saved == 1 else "flows"
        self.assertEqual(win.status_flows.text(), f"{saved} saved {noun}")
        win.close()

    def test_import_har_saves_to_store(self):
        """Item 8: the Generate tab's HAR import persists the flows —
        the host shows on the Hosts tab afterwards, like a capture."""
        from PySide6.QtWidgets import QFileDialog
        from mimik import store
        from mimik.gui import MainWindow
        from mimik.sources import mitm as mitm_mod

        fixture = os.path.join(os.path.dirname(__file__), "fixtures",
                               "mimik.har")
        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            win.tabs.setCurrentIndex(2)  # Generate tab
            self.assertTrue(_wait_for(
                self.app, lambda: win._task is None))
            with mock.patch.object(
                    QFileDialog, "getOpenFileName",
                    return_value=(fixture, "")):
                win._import_har()
            self.assertTrue(_wait_for(
                self.app,
                lambda: win.gen_host.findText("api.example.com") >= 0))
            self.assertIn("flows saved", win.import_label.text())
            hosts = [h for h, _n in mitm_mod.hosts(store.load_all())]
            self.assertIn("api.example.com", hosts)
            self.assertIn("cdn.example.com", hosts)
            # The Hosts tab picks the imported host up on next visit.
            win.tabs.setCurrentIndex(1)
            self.assertTrue(_wait_for(
                self.app, lambda: self._hosts_item(
                    win, "api.example.com") is not None))
            win.close()

    def test_import_har_malformed_shows_warning(self):
        """Item 8: a malformed HAR warns instead of dying silently."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from mimik.gui import MainWindow

        bad = os.path.join(self.tmp, "bad.har")
        with open(bad, "w") as fh:
            fh.write("{not json")
        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(2)  # Generate tab
        self.assertTrue(_wait_for(
            self.app, lambda: win._task is None))
        with mock.patch.object(
                QFileDialog, "getOpenFileName",
                return_value=(bad, "")), \
             mock.patch.object(QMessageBox, "warning") as warn:
            win._import_har()
            self.assertTrue(_wait_for(
                self.app, lambda: win._task is None))
        self.assertTrue(warn.called)
        self.assertIn("not valid JSON", warn.call_args[0][2])
        win.close()

    def test_import_curl_malformed_shows_warning(self):
        """Item 8: a cURL paste without a URL warns (was silent)."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from mimik.gui import MainWindow

        bad = os.path.join(self.tmp, "bad.curl")
        with open(bad, "w") as fh:
            fh.write("curl -H 'x: y'")
        win = MainWindow()
        win.show()
        with mock.patch.object(
                QFileDialog, "getOpenFileName",
                return_value=(bad, "")), \
             mock.patch.object(QMessageBox, "warning") as warn:
            win._import_curl()
        self.assertTrue(warn.called)
        self.assertIn("no URL found", warn.call_args[0][2])
        win.close()


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class WindowIconTests(unittest.TestCase):
    """v0.4 item 7: the window carries the MIMIK icon in code."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = cls.tmp
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)

    @classmethod
    def tearDownClass(cls):
        del os.environ["MIMIK_DATA_DIR"]

    def tearDown(self):
        self.app.processEvents()
        gc.collect()

    def test_main_window_has_a_non_null_icon(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        self.assertFalse(win.windowIcon().isNull())
        win.close()

    def test_window_icon_is_non_null(self):
        from mimik import gui

        # The tree always holds at least the SVG, so the icon is never
        # null — PNG when present, SVG fallback otherwise.
        self.assertFalse(gui.window_icon().isNull())

    def test_window_icon_carries_png_sizes_when_pngs_exist(self):
        from PySide6.QtCore import QSize

        from mimik import gui

        if not any(s is not None for _p, s in gui._icon_files()):
            self.skipTest("no PNG icon in this tree (fresh clone: SVG only)")
        sizes = gui.window_icon().availableSizes()
        self.assertIn(QSize(256, 256), sizes)
        self.assertIn(QSize(48, 48), sizes)

    def test_svg_fallback_when_no_png_exists(self):
        from mimik import gui

        svg = os.path.join(os.path.dirname(__file__), "..", "icons",
                           "mimik.svg")
        with mock.patch.object(gui, "_icon_files",
                               return_value=[(svg, None)]):
            icon = gui.window_icon()
        self.assertFalse(icon.isNull())

    def test_no_icon_files_means_null_icon(self):
        from mimik import gui

        with mock.patch.object(gui, "_icon_files", return_value=[]):
            icon = gui.window_icon()
        self.assertTrue(icon.isNull())


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class GeometryTests(unittest.TestCase):
    """v0.4 item 8: the window reopens where it was left."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.settings_dir = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = cls.tmp
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)
        # Redirect QSettings at the config root so the tests never
        # touch the real ~/.config — and restore it afterwards.
        from PySide6.QtCore import QSettings

        from mimik.gui import SETTINGS_APP, SETTINGS_ORG

        probe = QSettings(SETTINGS_ORG, SETTINGS_APP)
        cls._orig_base = os.path.dirname(
            os.path.dirname(probe.fileName()))
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                          cls.settings_dir)

    @classmethod
    def tearDownClass(cls):
        from PySide6.QtCore import QSettings

        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                          cls._orig_base)
        del os.environ["MIMIK_DATA_DIR"]

    def tearDown(self):
        self.app.processEvents()
        gc.collect()

    def _clear_settings(self):
        from PySide6.QtCore import QSettings

        from mimik.gui import SETTINGS_APP, SETTINGS_ORG

        QSettings(SETTINGS_ORG, SETTINGS_APP).clear()

    def test_default_geometry_when_nothing_saved(self):
        from mimik.gui import MainWindow

        self._clear_settings()
        win = MainWindow()
        self.assertEqual(win.width(), 900)
        self.assertEqual(win.height(), 650)
        win.close()

    def test_geometry_restores_after_restart(self):
        from mimik.gui import MainWindow

        self._clear_settings()
        win1 = MainWindow()
        win1.show()
        # The layout minimum is 796x640, so a 700x500 resize clamps up
        # to it; the saved frame must fit the 800x800 offscreen screen
        # or Qt shifts it on restore (its own on-screen adjustment).
        win1.resize(700, 500)
        win1.move(0, 60)
        self.app.processEvents()
        saved = win1.frameGeometry().getRect()
        win1.close()  # closeEvent saves the geometry
        self.app.processEvents()

        win2 = MainWindow()
        win2.show()  # the frame isn't realized until show()
        self.app.processEvents()
        self.assertEqual(win2.frameGeometry().getRect(), saved)
        win2.close()

    def test_offscreen_geometry_falls_back_to_a_visible_spot(self):
        from PySide6.QtCore import QSettings

        from mimik.gui import MainWindow, SETTINGS_APP, SETTINGS_ORG

        self._clear_settings()
        win1 = MainWindow()
        win1.show()
        win1.move(9999, 9999)
        self.app.processEvents()
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(
            "geometry", win1.saveGeometry())
        win1.close()

        win2 = MainWindow()
        screen = self.app.primaryScreen()
        available = screen.availableGeometry()
        self.assertTrue(available.intersects(win2.frameGeometry()),
                        f"restored {win2.frameGeometry().getRect()} "
                        f"outside {available.getRect()}")
        win2.close()


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class ThemeTests(unittest.TestCase):
    """v0.4 item 9: light/dark toggle, persisted."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.settings_dir = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = cls.tmp
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)
        # Redirect QSettings at the config root so the tests never
        # touch the real ~/.config — and restore it afterwards.
        from PySide6.QtCore import QSettings

        from mimik.gui import SETTINGS_APP, SETTINGS_ORG

        probe = QSettings(SETTINGS_ORG, SETTINGS_APP)
        cls._orig_base = os.path.dirname(
            os.path.dirname(probe.fileName()))
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                          cls.settings_dir)

    @classmethod
    def tearDownClass(cls):
        from PySide6.QtCore import QSettings

        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                          cls._orig_base)
        del os.environ["MIMIK_DATA_DIR"]

    def tearDown(self):
        self.app.processEvents()
        gc.collect()

    def _clear_settings(self):
        from PySide6.QtCore import QSettings

        from mimik.gui import SETTINGS_APP, SETTINGS_ORG

        QSettings(SETTINGS_ORG, SETTINGS_APP).clear()

    def test_default_theme_is_dark(self):
        from mimik.gui import MainWindow

        self._clear_settings()
        win = MainWindow()
        self.assertIn("#17171c", win.styleSheet())
        self.assertTrue(win._dark_theme_action.isChecked())
        win.close()

    def test_toggle_switches_the_theme(self):
        from mimik.gui import MainWindow

        self._clear_settings()
        win = MainWindow()
        win.set_theme("light")
        self.assertIn("#f2f2f4", win.styleSheet())
        self.assertNotIn("#17171c", win.styleSheet())
        self.assertFalse(win._dark_theme_action.isChecked())
        win.set_theme("dark")
        self.assertIn("#17171c", win.styleSheet())
        self.assertTrue(win._dark_theme_action.isChecked())
        win.close()

    def test_menu_action_toggles_the_theme(self):
        from mimik.gui import MainWindow

        self._clear_settings()
        win = MainWindow()
        win._dark_theme_action.trigger()  # unchecks: dark -> light
        self.assertIn("#f2f2f4", win.styleSheet())
        win._dark_theme_action.trigger()  # checks: light -> dark
        self.assertIn("#17171c", win.styleSheet())
        win.close()

    def test_theme_persists_across_restarts(self):
        from mimik.gui import MainWindow

        self._clear_settings()
        win1 = MainWindow()
        win1.set_theme("light")
        win1.close()
        self.app.processEvents()

        win2 = MainWindow()
        self.assertIn("#f2f2f4", win2.styleSheet())
        self.assertFalse(win2._dark_theme_action.isChecked())
        win2.close()


class _ReplayStub(BaseHTTPRequestHandler):
    """Local stub for the GUI replay tests — answers one fixed body."""

    body = b'{"hello": "from-stub"}'

    def _handle(self):
        payload = _ReplayStub.body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args):
        pass


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class ReplayTests(unittest.TestCase):
    """v0.4 item 10: the Replay button re-sends a flow, shows the reply."""

    @classmethod
    def setUpClass(cls):
        from test_websocket import EchoServer

        cls.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = cls.tmp
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _ReplayStub)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.ws_server = EchoServer()
        cls.ws_server.__enter__()
        cls.ws_port = cls.ws_server.port

    @classmethod
    def tearDownClass(cls):
        cls.ws_server.__exit__(None, None, None)
        cls.server.shutdown()
        cls.server.server_close()
        del os.environ["MIMIK_DATA_DIR"]

    def tearDown(self):
        self.app.processEvents()
        gc.collect()

    def _seed_flow(self):
        from mimik import store

        store.save_flows([{
            "id": "r1",
            "request": {"host": "127.0.0.1", "method": "GET",
                        "path": "/api/hello", "scheme": "http",
                        "port": self.port, "headers": []},
            "response": {"status_code": 200, "headers": []},
        }])

    def _seed_ws_flow(self):
        from mimik import store

        store.save_flows([{
            "id": "w1",
            "request": {"host": "127.0.0.1", "method": "GET",
                        "path": "/chat", "scheme": "http",
                        "port": self.ws_port, "headers": []},
            "response": {"status_code": 101, "headers": []},
            "websocket": {"messages_meta": {"count": 2}},
            "_saved_ws_messages": [
                {"from_client": True, "timestamp": 1.0,
                 "text": "hello-ws"},
                {"from_client": False, "timestamp": 1.1,
                 "text": "hello-ws"},
            ],
        }])

    def _open_hosts_tab(self, win):
        win.tabs.setCurrentIndex(1)  # Hosts tab loads on first show
        self.assertTrue(_wait_for(
            self.app,
            lambda: win.hosts_list.count() == 1
            and "127.0.0.1" in win.hosts_list.item(0).text()))

    def test_replay_button_disabled_without_a_selection(self):
        from mimik.gui import MainWindow

        self._seed_flow()
        win = MainWindow()
        win.show()
        self._open_hosts_tab(win)
        # Hosts loaded, no endpoint selected — Replay stays disabled.
        self.assertFalse(win.replay_btn.isEnabled())
        win.close()

    def test_replay_shows_the_stub_response(self):
        from mimik.gui import MainWindow

        self._seed_flow()
        win = MainWindow()
        win.show()
        self._open_hosts_tab(win)
        win.hosts_list.setCurrentRow(0)
        self.assertTrue(_wait_for(
            self.app, lambda: win.ep_table.rowCount() == 1))
        self.assertEqual(win.ep_table.item(0, 1).text(), "/api/hello")
        win.ep_table.selectRow(0)
        self.app.processEvents()
        self.assertTrue(win.replay_btn.isEnabled())

        win._replay_selected()
        self.assertTrue(_wait_for(
            self.app,
            lambda: getattr(win, "_replay_dialog", None) is not None))
        text = win._replay_text.toPlainText()
        self.assertIn("Replay: 200 OK", text)
        self.assertIn("Original: 200", text)
        self.assertIn("from-stub", text)
        self.assertIn(f"http://127.0.0.1:{self.port}/api/hello", text)
        win._replay_dialog.close()
        win.close()

    def test_ws_replay_shows_the_transcript(self):
        """v0.4 item 13: a WS endpoint replays its client messages."""
        from mimik.gui import MainWindow

        self._seed_flow()
        self._seed_ws_flow()
        win = MainWindow()
        win.show()
        self._open_hosts_tab(win)
        win.hosts_list.setCurrentRow(0)
        # The HTTP flow may already be seeded — find the /chat row by
        # path, whatever row it lands on.
        self.assertTrue(_wait_for(
            self.app,
            lambda: any(win.ep_table.item(r, 1)
                        and win.ep_table.item(r, 1).text() == "/chat"
                        for r in range(win.ep_table.rowCount()))))
        row = next(r for r in range(win.ep_table.rowCount())
                   if win.ep_table.item(r, 1).text() == "/chat")
        self.assertEqual(win.ep_table.item(row, 2).text(), "101 (WS)")
        win.ep_table.selectRow(row)
        self.app.processEvents()
        self.assertTrue(win.replay_btn.isEnabled())

        win._replay_selected()
        self.assertTrue(_wait_for(
            self.app,
            lambda: getattr(win, "_replay_dialog", None) is not None))
        text = win._replay_text.toPlainText()
        self.assertIn("WebSocket replay", text)
        self.assertIn(f"ws://127.0.0.1:{self.ws_port}/chat", text)
        self.assertIn("Sent 1 message(s), received 1.", text)
        self.assertIn("hello-ws", text)
        win._replay_dialog.close()
        win.close()


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class DiffTests(unittest.TestCase):
    """v0.4 item 12: the Compare captures dialog diffs two hosts."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = cls.tmp
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)

    @classmethod
    def tearDownClass(cls):
        del os.environ["MIMIK_DATA_DIR"]

    def tearDown(self):
        self.app.processEvents()
        gc.collect()

    def _seed_hosts(self):
        from mimik import store

        store.save_flows([
            {"id": "a1",
             "request": {"host": "a.example.com", "method": "GET",
                         "path": "/api/old", "scheme": "https",
                         "port": 443, "headers": []},
             "response": {"status_code": 200, "headers": []}},
            {"id": "a2",
             "request": {"host": "a.example.com", "method": "GET",
                         "path": "/api/users", "scheme": "https",
                         "port": 443, "headers": []},
             "response": {"status_code": 200, "headers": []},
             "_saved_response_body": '{"users": []}'},
        ])
        store.save_flows([
            {"id": "b1",
             "request": {"host": "b.example.com", "method": "GET",
                         "path": "/api/new", "scheme": "https",
                         "port": 443, "headers": []},
             "response": {"status_code": 200, "headers": []}},
            {"id": "b2",
             "request": {"host": "b.example.com", "method": "GET",
                         "path": "/api/users", "scheme": "https",
                         "port": 443, "headers": []},
             "response": {"status_code": 500, "headers": []},
             "_saved_response_body": '{"users": [], "error": "x"}'},
        ])

    def test_dialog_diffs_two_hosts(self):
        from mimik.gui import MainWindow

        self._seed_hosts()
        win = MainWindow()
        win.show()
        win._compare_captures()
        dlg = win._diff_dialog
        self.assertTrue(dlg.isVisible())
        win._diff_combo_a.setCurrentIndex(
            win._diff_combo_a.findText("a.example.com"))
        win._diff_combo_b.setCurrentIndex(
            win._diff_combo_b.findText("b.example.com"))
        win._run_diff_dialog()
        self.assertTrue(_wait_for(
            self.app, lambda: bool(win._diff_text.toPlainText())))
        text = win._diff_text.toPlainText()
        self.assertIn("Capture diff: a.example.com -> b.example.com",
                      text)
        self.assertIn("--- New ---", text)
        self.assertIn("GET /api/new", text)
        self.assertIn("--- Removed ---", text)
        self.assertIn("GET /api/old", text)
        self.assertIn("--- Changed ---", text)
        self.assertIn("GET /api/users: status 200 -> 500", text)
        self.assertIn("+error", text)
        dlg.close()
        win.close()


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class UnpinTests(unittest.TestCase):
    """v0.4 item 14: the Unpin dialog prepares a Frida run."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = cls.tmp
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)

    @classmethod
    def tearDownClass(cls):
        del os.environ["MIMIK_DATA_DIR"]

    def tearDown(self):
        self.app.processEvents()
        gc.collect()

    def _plan(self):
        return {"kind": "android", "scripts": [],
                "command": "frida -U \\\n    -f com.example.app",
                "steps": ["Run this to launch the app:",
                          "Use the app."],
                "notes": ["Own apps only."],
                "patched_apk": None, "proxy_host": "192.168.1.5"}

    def test_menu_action_opens_the_dialog(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.assertEqual(win._unpin_action.text(),
                         "Unpin a pinned app…")
        win._unpin_action.trigger()
        dlg = win._unpin_dialog_ref
        self.assertTrue(dlg.isVisible())
        self.assertEqual(dlg.windowTitle(), "Unpin a pinned app")
        dlg.close()
        win.close()

    def test_dialog_shows_the_prepared_plan(self):
        from mimik import unpin as unpin_mod
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win._unpin_dialog()
        dlg = win._unpin_dialog_ref
        win._unpin_target.setText("com.example.app")
        with mock.patch.object(unpin_mod, "prepare",
                               return_value=self._plan()) as prep:
            win._run_unpin_dialog()
            self.assertTrue(_wait_for(
                self.app,
                lambda: bool(win._unpin_text.toPlainText())))
        prep.assert_called_once()
        self.assertEqual(prep.call_args[1]["platform"], None)
        text = win._unpin_text.toPlainText()
        self.assertIn("Android app, rooted", text)
        self.assertIn("frida -U", text)
        self.assertIn("192.168.1.5:8080", text)
        dlg.close()
        win.close()

    def test_dialog_shows_a_failure_in_place(self):
        from mimik import unpin as unpin_mod
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win._unpin_dialog()
        dlg = win._unpin_dialog_ref
        win._unpin_target.setText("com.example.app")
        with mock.patch.object(unpin_mod, "prepare",
                               side_effect=unpin_mod.UnpinError(
                                   "no mitmproxy CA")):
            win._run_unpin_dialog()
            self.assertTrue(_wait_for(
                self.app,
                lambda: bool(win._unpin_text.toPlainText())))
        text = win._unpin_text.toPlainText()
        self.assertIn("Could not prepare:", text)
        self.assertIn("no mitmproxy CA", text)
        dlg.close()
        win.close()


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class ScreenshotTests(unittest.TestCase):
    """v0.4 item 15: the stop-time screenshot checkbox + Stop wiring."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = cls.tmp
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)

    @classmethod
    def tearDownClass(cls):
        del os.environ["MIMIK_DATA_DIR"]

    def tearDown(self):
        self.app.processEvents()
        gc.collect()

    def test_checkbox_default_off_and_persists(self):
        from mimik import presets
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.assertFalse(win.screenshot_check.isChecked())
        self.assertFalse(presets.screenshot_enabled())
        win.screenshot_check.setChecked(True)
        self.app.processEvents()
        self.assertTrue(presets.screenshot_enabled())
        win.screenshot_check.setChecked(False)
        self.app.processEvents()
        self.assertFalse(presets.screenshot_enabled())
        win.close()

    def test_stop_takes_the_screenshot_first(self):
        from mimik import screenshot as shot_mod
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win.screenshot_check.setChecked(True)
        self.app.processEvents()
        calls = []
        worker = mock.Mock()
        worker.request_stop.side_effect = lambda: calls.append("stop")
        win.worker = worker
        with mock.patch.object(
                shot_mod, "capture_for_session",
                side_effect=lambda: calls.append("capture")
                or "/tmp/shot.png"):
            win._stop_recording()
        self.assertEqual(calls, ["capture", "stop"])
        self.assertIn("screenshot saved: /tmp/shot.png",
                      win.steps.toPlainText())
        win.worker = None
        win.close()

    def test_stop_without_the_option_takes_none(self):
        from mimik import screenshot as shot_mod
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win.screenshot_check.setChecked(False)
        self.app.processEvents()
        worker = mock.Mock()
        win.worker = worker
        with mock.patch.object(shot_mod, "capture_for_session") as cap:
            win._stop_recording()
        cap.assert_not_called()
        worker.request_stop.assert_called_once()
        win.worker = None
        win.close()


if __name__ == "__main__":
    unittest.main()
