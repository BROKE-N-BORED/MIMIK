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


def _ep_flow(fid, method, path):
    """One minimal flow dict for endpoint-table tests (v0.6 T6):
    no bodies — the table only needs method, path, status."""
    return {
        "id": fid,
        "request": {"host": "ep.example.com", "method": method,
                    "path": path, "scheme": "https", "port": 443,
                    "headers": []},
        "response": {"status_code": 200, "headers": []},
    }


class GuiSourceTests(unittest.TestCase):
    """Static guards on mimik/gui.py — no Qt needed, runs everywhere."""

    def test_no_lambda_signal_slots(self):
        # Lambdas connected to Qt signals corrupt the heap at teardown
        # (proven: ~50% malloc abort with two lambda slots, 0/20
        # without them). Slots must be bound methods. The regex
        # tolerates whitespace/newlines: a lambda on the line after
        # .connect( is the same bug (v0.6 T13's first version dodged
        # the plain-substring check exactly that way).
        path = os.path.join(os.path.dirname(__file__), "..",
                            "mimik", "gui.py")
        with open(path) as f:
            src = f.read()
        self.assertNotRegex(src, r"\.connect\(\s*lambda")
        self.assertNotRegex(src, r"\.connect\(\s*functools\.partial")

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

    def test_stylesheets_have_identity_states(self):
        # v0.6 T1: both stylesheets carry pressable buttons (a
        # :pressed state + rounded corners), a raised dialog surface,
        # and the type-hierarchy labels (section + wordmark).
        path = os.path.join(os.path.dirname(__file__), "..",
                            "mimik", "gui.py")
        with open(path) as f:
            src = f.read()
        dark = src.split('STYLESHEET = """')[1].split('"""')[0]
        light = src.split('LIGHT_STYLESHEET = """')[1].split('"""')[0]
        for name, sheet in (("dark", dark), ("light", light)):
            for marker in (":pressed", "border-radius: 6px",
                           "QDialog {", "QLabel#section",
                           "QLabel#wordmark"):
                self.assertIn(marker, sheet, f"{name}: {marker}")


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
        # v0.6 T8: the Record tab's remembered setup lives in the
        # real QSettings — drop it after every test so a target,
        # file, or launch command picked by one test never leaks
        # into the next test's fresh window.
        from mimik.gui import _settings
        _settings().remove("record")
        _settings().remove("access")
        # v0.7: the startup update-check toggle lives in the real
        # QSettings too — drop it so one test's toggle never leaks
        # into the next test's fresh window.
        _settings().remove("update")
        self.app.processEvents()
        gc.collect()

    def test_window_shows_with_eight_tabs(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertTrue(win.isVisible())
        tabs = win.tabs
        self.assertEqual(tabs.count(), 8)
        titles = [tabs.tabText(i) for i in range(tabs.count())]
        self.assertEqual(titles, ["Record", "Hosts and Learn", "Generate",
                                  "Assets", "Clients", "Binary", "Doctor",
                                  "Presets"])
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

    def test_record_tab_identity_header(self):
        # v0.6 T1: the Record tab carries the MIMIK mark + wordmark
        # inside the window (it used to appear only in the title bar).
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertEqual(win.identity_wordmark.text(), "MIMIK")
        pix = win.identity_icon.pixmap()
        self.assertIsNotNone(pix)
        self.assertFalse(pix.isNull())
        win.close()

    def test_hosts_list_pending_before_first_load(self):
        # v0.6 T4: no false empty before the load answers.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertEqual(win.hosts_list.count(), 1)
        self.assertIn("Loading", win.hosts_list.item(0).text())
        win.close()

    def test_gen_combo_pending_before_first_load(self):
        # v0.6 T4: the host combo is pending, not falsely empty.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertIn("Loading",
                      win.gen_host.lineEdit().placeholderText())
        win.close()

    def test_hosts_empty_state_after_load_answers(self):
        # v0.6 T4: an empty store shows the honest empty state only
        # once the load has answered. Own data dir: the shared class
        # dir may hold hosts from other tests.
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            try:
                self.assertTrue(win._refresh_hosts(refresh=False))
                _wait_for(self.app, lambda: win._task is None)
                self.assertEqual(win.hosts_list.count(), 1)
                self.assertIn("No captures yet",
                              win.hosts_list.item(0).text())
            finally:
                win.close()

    def test_hosts_load_emits_real_counts(self):
        # v0.6 T4: the load worker reports real counts through the
        # progress signal (flows read, hosts found).
        from mimik import sample
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            sample.load_sample()
            win = MainWindow()
            win.show()
            try:
                seen = []
                self.assertTrue(win._refresh_hosts(refresh=False))
                win._task.progress.connect(seen.append)
                _wait_for(self.app, lambda: win._task is None)
                joined = " ".join(seen)
                self.assertIn("flows", joined)
                self.assertIn("hosts found", joined)
            finally:
                win.close()

    def test_empty_state_action_buttons_disabled(self):
        # v0.8 B3: every action that needs a selection or a host is
        # disabled in the empty state (the audit found them all
        # clickable, erroring or doing nothing). Actions that need
        # nothing (Refresh, Import, Scan, Analyze) stay enabled.
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            self.app.processEvents()
            try:
                for b in (win.host_delete_btn, win.host_gen_btn,
                          win.host_link_btn, win.gen_btn,
                          win.asset_delete_group_btn,
                          win.asset_preview_btn, win.asset_reveal_btn,
                          win.build_pack_btn, win.asset_delete_pack_btn,
                          win.asset_export_pack_btn, win.smoke_btn,
                          win.client_delete_btn, win.client_open_btn,
                          win.client_reveal_btn, win.learn_btn,
                          win.preset_delete_btn, win.preset_export_btn):
                    self.assertFalse(b.isEnabled(), b.text())
                # v0.8 B4: the Binary list wraps instead of clipping.
                self.assertTrue(win.binaries_list.wordWrap())
            finally:
                win.close()

    def test_sample_state_enables_actions_and_autoselects(self):
        # v0.8 B3/G9: with the sample loaded, the lone host selects
        # itself on Hosts (endpoints load, actions enable), the lone
        # preset selects itself on Presets, the Assets combo shows a
        # short label, and picking the asset row enables Preview.
        from PySide6.QtCore import Qt

        from mimik import sample
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            sample.load_sample()
            win = MainWindow()
            win.show()
            try:
                self.assertTrue(win._refresh_hosts(refresh=False))
                _wait_for(self.app, lambda: win._task is None)
                # Auto-select fires the endpoints load as a 2nd task.
                _wait_for(self.app, lambda: win._task is None
                          and win.ep_table.rowCount() > 0)
                item = win.hosts_list.currentItem()
                self.assertIsNotNone(item)
                self.assertEqual(item.data(Qt.UserRole),
                                 sample.SAMPLE_HOST)
                self.assertTrue(win.host_delete_btn.isEnabled())
                self.assertTrue(win.host_gen_btn.isEnabled())
                win._refresh_presets()
                self.app.processEvents()
                self.assertTrue(win.learn_btn.isEnabled())
                self.assertTrue(win.preset_delete_btn.isEnabled())
                self.assertTrue(win.preset_export_btn.isEnabled())
                win._refresh_asset_groups()
                self.app.processEvents()
                self.assertEqual(win.asset_group.currentText(),
                                 f"{sample.SAMPLE_HOST} (1 asset)")
                self.assertTrue(win.asset_delete_group_btn.isEnabled())
                self.assertTrue(win.build_pack_btn.isEnabled())
                self.assertFalse(win.asset_preview_btn.isEnabled())
                win.asset_table.setCurrentCell(0, 0)
                self.app.processEvents()
                self.assertTrue(win.asset_preview_btn.isEnabled())
                self.assertTrue(win.asset_reveal_btn.isEnabled())
            finally:
                win.close()

    def test_encryption_actions_follow_store(self):
        # v0.8 G1: File-menu encryption actions + the status bar
        # follow the store — locked reads as "locked", never as an
        # empty store.
        from mimik import crypto, sample, store
        if not crypto.available():
            self.skipTest("crypto packages missing")
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            self.app.processEvents()
            try:
                self.assertFalse(win._unlock_action.isVisible())
                self.assertEqual(win._encrypt_action.text(),
                                 "Encrypt captures…")
            finally:
                win.close()
            sample.load_sample()
            store.enable_encryption("pw123")
            store.lock()
            win = MainWindow()
            win.show()
            self.app.processEvents()
            try:
                self.assertTrue(win._unlock_action.isVisible())
                self.assertEqual(win._encrypt_action.text(),
                                 "Turn off capture encryption…")
                self.assertIn("locked", win.status_flows.text())
                with mock.patch.object(win, "_ask_passphrase",
                                       return_value="pw123"):
                    win._unlock_captures()
                self.assertTrue(store.unlocked())
                self.assertFalse(win._unlock_action.isVisible())
                self.assertNotIn("locked", win.status_flows.text())
            finally:
                win.close()
                store.lock()

    def test_accessible_names_cover_controls(self):
        # v0.8 G8: buttons, checkboxes and tab pages carry
        # accessible names for screen readers.
        from PySide6.QtWidgets import QPushButton
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            self.app.processEvents()
            try:
                named = [b for b in win.findChildren(QPushButton)
                         if b.accessibleName()]
                self.assertGreater(len(named), 10)
                self.assertEqual(win.tabs.accessibleName(),
                                 "Main sections")
                for i in range(win.tabs.count()):
                    self.assertTrue(
                        win.tabs.widget(i).accessibleName())
            finally:
                win.close()

    def test_doctor_pending_shows_check_names(self):
        # v0.6 T4: the Doctor tab shows the check names with a
        # pending state while probing, never a blank box.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        text = win.doctor_text.toPlainText()
        self.assertIn("requests library", text)
        self.assertIn("…", text)
        win.close()

    def test_doctor_rerun_shows_pending_then_results(self):
        # v0.6 T4: Re-check re-shows pending while probing; the real
        # results replace it when the probe finishes.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            self.assertTrue(win._run_doctor())
            # Synchronous: the worker's done only fires via the loop.
            text = win.doctor_text.toPlainText()
            self.assertIn("requests library", text)
            self.assertIn("…", text)
            _wait_for(self.app, lambda: "overall:" in
                      win.doctor_text.toPlainText())
        finally:
            win.close()

    def test_snapshot_restore_list_roundtrip(self):
        # v0.6 T5: the list snapshot helpers keep selection +
        # scroll across a clear + refill.
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QListWidget, QListWidgetItem
        from mimik.gui import _restore_list, _snapshot_list

        w = QListWidget()
        for i in range(50):
            item = QListWidgetItem(f"host-{i}")
            item.setData(Qt.UserRole, f"host-{i}")
            w.addItem(item)
        w.resize(200, 100)
        w.show()
        self.app.processEvents()
        w.setCurrentRow(30)
        # ScrollPerItem is Qt's default: the scrollbar counts items,
        # so 10 is inside the range and 120 would clamp.
        w.verticalScrollBar().setValue(10)
        snap = _snapshot_list(w)
        self.assertEqual(snap, ("host-30", 10))
        w.clear()
        for i in range(50):
            item = QListWidgetItem(f"host-{i}")
            item.setData(Qt.UserRole, f"host-{i}")
            w.addItem(item)
        _restore_list(w, snap)
        self.assertEqual(w.currentRow(), 30)
        self.assertEqual(w.verticalScrollBar().value(), 10)
        w.close()

    def test_snapshot_restore_table_roundtrip(self):
        # v0.6 T5: the table snapshot helpers keep selection +
        # scroll across a refill, keyed on the row identity.
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
        from mimik.gui import _restore_table, _snapshot_table

        w = QTableWidget()
        w.setColumnCount(1)
        w.setRowCount(50)
        for i in range(50):
            item = QTableWidgetItem(f"asset-{i}")
            item.setData(Qt.UserRole, {"path": f"/a/{i}.png"})
            w.setItem(i, 0, item)
        w.resize(200, 100)
        w.show()
        self.app.processEvents()
        w.setCurrentCell(30, 0)
        # ScrollPerItem is Qt's default: the scrollbar counts rows,
        # so 10 is inside the range and 120 would clamp.
        w.verticalScrollBar().setValue(10)
        snap = _snapshot_table(w)
        w.setRowCount(0)
        w.setRowCount(50)
        for i in range(50):
            item = QTableWidgetItem(f"asset-{i}")
            item.setData(Qt.UserRole, {"path": f"/a/{i}.png"})
            w.setItem(i, 0, item)
        _restore_table(w, snap, key="path")
        self.assertEqual(w.currentRow(), 30)
        self.assertEqual(w.verticalScrollBar().value(), 10)
        w.close()

    def test_hosts_refresh_keeps_selection_and_endpoints(self):
        # v0.6 T5: Refresh while viewing a host's endpoints keeps
        # the host selected and the endpoints view intact.
        from PySide6.QtCore import Qt
        from mimik import sample
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            sample.load_sample()
            win = MainWindow()
            win.show()
            try:
                win.tabs.setCurrentIndex(1)  # Hosts tab loads
                _wait_for(self.app, lambda: win._task is None)
                self.assertEqual(win.hosts_list.count(), 1)
                win.hosts_list.setCurrentRow(0)
                _wait_for(self.app, lambda: win._task is None
                          and win.ep_table.rowCount() > 0)
                self.assertGreater(win.ep_table.rowCount(), 0)
                win._refresh_hosts()
                _wait_for(self.app, lambda: win._task is None
                          and win.ep_table.rowCount() > 0)
                self.assertEqual(
                    win.hosts_list.currentItem().data(Qt.UserRole),
                    sample.SAMPLE_HOST)
                self.assertGreater(win.ep_table.rowCount(), 0)
            finally:
                win.close()

    def test_presets_refresh_keeps_selection(self):
        # v0.6 T5: refreshing the Presets tab keeps the selected
        # preset. Own data dir: the preset must not leak into the
        # shared class dir.
        from PySide6.QtCore import Qt
        from mimik import presets, store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
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
            try:
                win.tabs.setCurrentIndex(7)  # Presets tab loads
                self.app.processEvents()
                self.assertEqual(win.presets_list.count(), 1)
                win.presets_list.setCurrentRow(0)
                self.app.processEvents()
                win._refresh_presets()
                self.app.processEvents()
                self.assertEqual(win.presets_list.currentRow(), 0)
                self.assertEqual(
                    win.presets_list.currentItem().data(Qt.UserRole),
                    "game.example.com")
            finally:
                win.close()

    def test_clients_refresh_keeps_selection(self):
        # v0.6 T5: refreshing the Clients tab keeps the selected
        # client. Own data dir: the fake client file must not leak.
        from PySide6.QtCore import Qt
        from mimik import store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            cdir = store.clients_dir(empty)
            os.makedirs(cdir, exist_ok=True)
            path = os.path.join(cdir, "demo_client.py")
            with open(path, "w") as f:
                f.write("# generated client\n")
            win = MainWindow()
            win.show()
            try:
                win.tabs.setCurrentIndex(4)  # Clients tab loads
                self.app.processEvents()
                self.assertEqual(win.clients_list.count(), 1)
                win.clients_list.setCurrentRow(0)
                win._refresh_clients()
                self.app.processEvents()
                self.assertEqual(win.clients_list.currentRow(), 0)
                self.assertEqual(
                    win.clients_list.currentItem().data(Qt.UserRole),
                    path)
            finally:
                win.close()

    def test_endpoints_table_has_seen_column(self):
        # v0.6 T6: the endpoints table shows how many times each
        # endpoint was seen.
        from mimik import store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            flows = [_ep_flow(f"a{i}", "GET", "/api/a")
                     for i in range(3)]
            flows.append(_ep_flow("b0", "GET", "/api/b"))
            store.save_flows(flows)
            win = MainWindow()
            win.show()
            try:
                win.tabs.setCurrentIndex(1)  # Hosts tab loads
                _wait_for(self.app, lambda: win._task is None)
                win.hosts_list.setCurrentRow(0)
                _wait_for(self.app, lambda: win._task is None
                          and win.ep_table.rowCount() > 0)
                self.assertEqual(win.ep_table.columnCount(), 4)
                self.assertEqual(
                    win.ep_table.horizontalHeaderItem(3).text(),
                    "Seen")
                seen = {}
                for row in range(win.ep_table.rowCount()):
                    path = win.ep_table.item(row, 1).text()
                    seen[path] = win.ep_table.item(row, 3).text()
                self.assertEqual(seen["/api/a"], "3")
                self.assertEqual(seen["/api/b"], "1")
            finally:
                win.close()

    def test_endpoints_seen_sorts_numerically(self):
        # v0.6 T6: sorting by Seen is numeric (10 above 3), not
        # text (where "3" would sort above "10").
        from PySide6.QtCore import Qt
        from mimik import store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            flows = ([_ep_flow(f"a{i}", "GET", "/api/a")
                      for i in range(2)]
                     + [_ep_flow(f"b{i}", "GET", "/api/b")
                        for i in range(10)]
                     + [_ep_flow(f"c{i}", "GET", "/api/c")
                        for i in range(3)])
            store.save_flows(flows)
            win = MainWindow()
            win.show()
            try:
                win.tabs.setCurrentIndex(1)  # Hosts tab loads
                _wait_for(self.app, lambda: win._task is None)
                win.hosts_list.setCurrentRow(0)
                _wait_for(self.app, lambda: win._task is None
                          and win.ep_table.rowCount() == 3)
                win.ep_table.sortItems(3, Qt.DescendingOrder)
                seen = [int(win.ep_table.item(row, 3).text())
                        for row in range(3)]
                self.assertEqual(seen, [10, 3, 2])
            finally:
                win.close()

    def test_gen_host_unselected_after_first_load(self):
        # v0.6 T7: the host combo starts unselected ("pick a host")
        # on its first load instead of landing on the first host.
        from mimik import sample
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            sample.load_sample()
            win = MainWindow()
            win.show()
            try:
                win.tabs.setCurrentIndex(2)  # Generate tab loads
                _wait_for(self.app, lambda: win._task is None)
                self.assertEqual(win.gen_host.currentIndex(), -1)
                self.assertEqual(
                    win.gen_host.lineEdit().placeholderText(),
                    "pick a host")
            finally:
                win.close()

    def test_gen_preview_shows_endpoints_and_auth(self):
        # v0.6 T7: picking a host previews what Generate will make
        # — endpoint count from the cached flows, auth shape only.
        from mimik import sample
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            sample.load_sample()
            win = MainWindow()
            win.show()
            try:
                win.tabs.setCurrentIndex(2)  # Generate tab loads
                _wait_for(self.app, lambda: win._task is None)
                self.assertEqual(win.gen_preview.text(), "")
                win.gen_host.setCurrentText(sample.SAMPLE_HOST)
                self.assertEqual(
                    win.gen_preview.text(),
                    "6 endpoints → 6 methods, auth: bearer")
            finally:
                win.close()

    def test_gen_preview_empty_for_unknown_host(self):
        # v0.6 T7: a typed host with no flows gets no preview.
        from mimik import sample
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            sample.load_sample()
            win = MainWindow()
            win.show()
            try:
                win.tabs.setCurrentIndex(2)  # Generate tab loads
                _wait_for(self.app, lambda: win._task is None)
                win.gen_host.setCurrentText("nope.example.com")
                self.assertEqual(win.gen_preview.text(), "")
            finally:
                win.close()

    def test_record_remembers_last_target(self):
        # v0.6 T8: the last capture target comes back at startup.
        from mimik.gui import MainWindow, _settings

        _settings().remove("record")
        _settings().setValue("record/last_target", "linux-app")
        win = MainWindow()
        win.show()
        try:
            self.app.processEvents()
            self.assertEqual(win._current_target, "linux-app")
            self.assertEqual(win.pads["linux-app"].property("padState"),
                             "selected")
        finally:
            win.close()

    def test_record_remembers_launch_command(self):
        # v0.6 T8: the last launch command comes back at startup.
        from mimik.gui import MainWindow, _settings

        _settings().remove("record")
        _settings().setValue("record/last_launch_cmd", "firefox")
        win = MainWindow()
        win.show()
        try:
            self.assertEqual(win.launch_edit.text(), "firefox")
        finally:
            win.close()

    def test_record_remembers_file(self):
        # v0.6 T8: the last chosen file comes back at startup, and
        # its launch plan builds when the file target is selected.
        from mimik.gui import MainWindow, _settings

        script = os.path.join(tempfile.mkdtemp(), "demo.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho hi\n")
        os.chmod(script, 0o755)
        _settings().remove("record")
        _settings().setValue("record/last_file", script)
        win = MainWindow()
        win.show()
        try:
            self.assertEqual(win.file_path, script)
            self.assertEqual(win.file_label.text(), script)
            self.assertIsNone(win.plan)  # builds on select, not boot
            win._select_target("file")
            self.assertIsNotNone(win.plan)
            self.assertTrue(win.plan.command)
        finally:
            win.close()

    def test_filter_choice_is_per_target(self):
        # v0.6 T8: the filter checkbox follows the target — each
        # target keeps its own choice, toggles save to the target.
        from mimik import presets
        from mimik.gui import MainWindow, _settings

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            presets.set_filtering_enabled(False)
            presets.set_filtering_enabled(True, target="android")
            _settings().remove("record")
            win = MainWindow()
            win.show()
            try:
                # Android (the startup target) has filtering on.
                self.assertTrue(win.filter_check.isChecked())
                win._select_target("linux-app")
                # Linux app falls back to the global off.
                self.assertFalse(win.filter_check.isChecked())
                win.filter_check.setChecked(True)
                self.assertTrue(presets.filtering_enabled(
                    target="linux-app"))
                win._select_target("android")
                self.assertTrue(win.filter_check.isChecked())
                win._select_target("linux-app")
                self.assertTrue(win.filter_check.isChecked())
            finally:
                win.close()

    def test_dropped_label_shows_count(self):
        # v0.6 T9: the live strip quietly shows what the filter
        # dropped, and hides again when nothing is dropped.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            win.live_strip.setVisible(True)  # as if recording
            self.assertFalse(win.live_dropped.isVisible())
            win._dropped_updated(["google-analytics.com", "sentry.io"])
            self.assertTrue(win.live_dropped.isVisible())
            self.assertEqual(win.live_dropped.text(),
                             "telemetry dropped: 2 hosts")
            win._dropped_updated([])
            self.assertFalse(win.live_dropped.isVisible())
        finally:
            win.close()

    def test_excluded_dialog_shows_dropped_hosts(self):
        # v0.6 T9: the Excluded hosts dialog names the hosts
        # actually dropped in this session, one tap away.
        from PySide6.QtWidgets import QDialog, QLabel
        from mimik.gui import MainWindow

        win = MainWindow()
        try:
            win._dropped_hosts = ["google-analytics.com", "sentry.io"]
            with mock.patch.object(QDialog, "exec", return_value=1):
                win._show_excluded_hosts()
            dlg = win._excluded_dialog
            labels = [lbl.text() for lbl in dlg.findChildren(QLabel)]
            self.assertTrue(any(
                "google-analytics.com" in t and "sentry.io" in t
                for t in labels), labels)
        finally:
            win.close()

    def test_section_labels_use_hierarchy(self):
        # v0.6 T1: section labels carry the section object name, so
        # the stylesheet renders them larger/bolder than body text.
        from PySide6.QtWidgets import QLabel
        from mimik.gui import MainWindow

        win = MainWindow()
        sections = [lbl for lbl in win.findChildren(QLabel)
                    if lbl.objectName() == "section"]
        texts = [lbl.text() for lbl in sections]
        self.assertIn("Live hosts while recording:", texts)
        self.assertIn("Endpoints for the selected host:", texts)
        win.close()

    def test_tray_pixmap_states(self):
        # v0.6 T2: the tray dot is grey idle, red recording.
        from mimik.gui import _tray_pixmap

        idle = _tray_pixmap("idle")
        rec = _tray_pixmap("recording")
        self.assertFalse(idle.isNull())
        self.assertFalse(rec.isNull())
        self.assertEqual(idle.size().width(), 32)
        self.assertEqual(idle.toImage().pixelColor(16, 16).name(),
                         "#9a9aa2")
        self.assertEqual(rec.toImage().pixelColor(16, 16).name(),
                         "#e06c6c")

    def test_status_chip_follows_recording(self):
        # v0.6 T2: with no system tray (offscreen), the chip shows
        # while recording and hides when idle.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertIsNone(win.tray_icon)
        self.assertIsNotNone(win.status_chip)
        self.assertFalse(win.status_chip.isVisible())
        win.worker = object()
        win._record_started = time.monotonic()
        win._flow_count = 3
        win._host_count = 1
        win._update_status_bar()
        self.app.processEvents()
        self.assertTrue(win.status_chip.isVisible())
        self.assertIn("Recording", win._chip_label.text())
        self.assertIn("3 flows", win._chip_label.text())
        win.worker = None
        win._record_started = None
        win._update_status_bar()
        self.app.processEvents()
        self.assertFalse(win.status_chip.isVisible())
        win.close()

    def test_tray_click_stops_or_shows(self):
        # v0.6 T2: a tray click stops a recording, or shows the
        # window when idle.
        from PySide6.QtWidgets import QSystemTrayIcon
        from mimik.gui import MainWindow

        win = MainWindow()
        with mock.patch.object(win, "_stop_recording") as stop:
            win.worker = object()
            win._tray_activated(QSystemTrayIcon.Trigger)
            stop.assert_called_once()
        with mock.patch.object(win, "bring_to_front") as show:
            win.worker = None
            win._tray_activated(QSystemTrayIcon.DoubleClick)
            show.assert_called_once()
        win.close()

    def test_tray_icon_created_when_available(self):
        # v0.6 T2: when the desktop has a tray, the window owns a
        # tray icon (offscreen has none, so force availability).
        from PySide6.QtWidgets import QSystemTrayIcon
        from mimik.gui import MainWindow

        with mock.patch.object(
                QSystemTrayIcon, "isSystemTrayAvailable",
                return_value=True):
            win = MainWindow()
        self.assertIsNotNone(win.tray_icon)
        self.assertIsNone(win.status_chip)
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
        from mimik.gui import PAD_TARGETS, MainWindow

        win = MainWindow()
        for key, _title, _sub in PAD_TARGETS:
            win._select_target(key)
            self.app.processEvents()
            text = win.steps.toPlainText()
            self.assertNotIn("mimik hosts", text)
            self.assertNotIn("mimik gen", text)
            self.assertNotIn("mimik record", text)
            self.assertNotIn("mimik --", text)
        win._select_target("android")
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

    def test_copy_debug_report_button(self):
        # v0.5 item 15 (U12/T21): the Doctor tab's Copy debug report
        # button puts the scrubbed report on the clipboard.
        from PySide6.QtWidgets import QApplication, QPushButton
        from mimik import __version__
        from mimik import log as mimik_log
        from mimik.gui import MainWindow

        mimik_log.log_info("test", "debug-report-marker-321")
        win = MainWindow()
        btns = [b for b in win.findChildren(QPushButton)
                if b.text() == "Copy debug report"]
        self.assertEqual(len(btns), 1)
        btns[0].click()
        text = QApplication.clipboard().text()
        self.assertIn(f"MIMIK {__version__}", text)
        self.assertIn("debug-report-marker-321", text)
        self.assertIn("copied to the clipboard",
                      win.doctor_text.toPlainText())
        win.close()

    def test_howto_menu_opens_index(self):
        # v0.5 item 13 (U22): Help menu "How to Use…" opens its own
        # window with a topic list; picking a topic shows its text.
        from PySide6.QtWidgets import QDialog
        from mimik.gui import MainWindow

        win = MainWindow()
        menus = {a.text(): a.menu()
                 for a in win.menuBar().actions()}
        help_items = [a.text() for a in menus["Help"].actions()]
        self.assertIn("How to Use…", help_items)
        with mock.patch.object(QDialog, "exec", return_value=1):
            win._howto_action.trigger()
        dlg = win._howto_dialog
        self.assertEqual(dlg.windowTitle(), "How to Use MIMIK")
        self.assertEqual(win._howto_list.count(), 10)
        titles = [win._howto_list.item(i).text()
                  for i in range(win._howto_list.count())]
        self.assertIn("Binary tab", titles)
        # The first topic (Start here) shows on open.
        self.assertIn("Record the app", win._howto_text.toPlainText())
        for i, title in enumerate(titles):
            if title == "Binary tab":
                win._howto_list.setCurrentRow(i)
                break
        self.app.processEvents()
        self.assertIn("never runs it",
                      win._howto_text.toPlainText())
        win.close()

    def test_ghidra_setup_button_shows_steps(self):
        # v0.5 item 11: the Doctor tab's Ghidra button shows the
        # setup steps in a dialog.
        from PySide6.QtWidgets import QDialog, QPushButton, QTextEdit
        from mimik import ghidra as ghidra_mod
        from mimik.gui import MainWindow

        win = MainWindow()
        # Scoped to the Doctor tab: the Binary tab has three more
        # buttons with the same text (one per Ghidra view).
        doctor_tab = win.tabs.widget(6)
        btns = [b for b in doctor_tab.findChildren(QPushButton)
                if b.text() == "Ghidra setup steps…"]
        self.assertEqual(len(btns), 1)
        with mock.patch.object(QDialog, "exec", return_value=1):
            btns[0].click()
        dlg = win._ghidra_setup_dialog
        self.assertEqual(dlg.windowTitle(), "Ghidra setup steps")
        text = dlg.findChild(QTextEdit).toPlainText()
        self.assertIn(ghidra_mod.GHIDRA_RELEASE_URL, text)
        self.assertIn("JDK 25", text)
        win.close()

    def test_filter_checkbox_toggles_setting(self):
        # Item 26: the Record tab's filter checkbox is the per-record
        # control — it reflects and persists the setting. v0.6 T8:
        # the setting it toggles is the current target's own choice;
        # the global value is untouched.
        from mimik import presets
        from mimik.gui import MainWindow

        presets.set_filtering_enabled(True)
        win = MainWindow()
        try:
            target = win._current_target
            self.assertIsNotNone(target)
            self.assertTrue(win.filter_check.isChecked())
            win.filter_check.setChecked(False)
            self.app.processEvents()
            self.assertFalse(
                presets.filtering_enabled(target=target))
            self.assertTrue(presets.filtering_enabled())
            win.filter_check.setChecked(True)
            self.app.processEvents()
            self.assertTrue(
                presets.filtering_enabled(target=target))
        finally:
            presets.set_filtering_enabled(True)
            presets.set_filtering_enabled(
                True, target=win._current_target)
            win.close()

    def test_record_tab_toy_surface(self):
        # v0.5 item 16 (U34), v0.6 T16: tap a pad to capture, Stop
        # to end it — nothing else visible by default. Advanced
        # options live behind one collapsible "More options"
        # section.
        from PySide6.QtCore import Qt
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertFalse(win.more_btn.isChecked())
        self.assertTrue(win.more_box.isHidden())
        self.assertEqual(win.more_btn.arrowType(), Qt.RightArrow)
        # The toy surface is all there: five pads, Stop, steps.
        self.assertEqual(len(win.pads), 5)
        for pad in win.pads.values():
            self.assertFalse(pad.isHidden())
        self.assertFalse(hasattr(win, "start_btn"))
        self.assertFalse(win.stop_btn.isHidden())
        self.assertFalse(win.steps.isHidden())
        # One tap opens it; the options are all still there (U1:
        # reorganize only, nothing removed).
        win.more_btn.click()
        self.app.processEvents()
        self.assertTrue(win.more_btn.isChecked())
        self.assertFalse(win.more_box.isHidden())
        self.assertEqual(win.more_btn.arrowType(), Qt.DownArrow)
        self.assertFalse(win.filter_check.isHidden())
        self.assertFalse(win.screenshot_check.isHidden())
        self.assertFalse(win.lan_label.isHidden())
        win.more_btn.click()
        self.app.processEvents()
        self.assertTrue(win.more_box.isHidden())
        win.close()

    def test_record_tab_more_options_auto_expands(self):
        # v0.5 item 16: the file picker and the launch box live
        # inside "More options", so picking those targets expands
        # it automatically — never a dead end. It never
        # auto-collapses.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win._select_target("file")
        self.app.processEvents()
        self.assertTrue(win.more_btn.isChecked())
        self.assertFalse(win.more_box.isHidden())
        self.assertFalse(win.file_box.isHidden())
        win._select_target("linux-app")
        self.app.processEvents()
        self.assertFalse(win.app_box.isHidden())
        self.assertTrue(win.file_box.isHidden())
        win._select_target("android")
        self.app.processEvents()
        self.assertTrue(win.more_btn.isChecked())
        self.assertTrue(win.file_box.isHidden())
        self.assertTrue(win.app_box.isHidden())
        win.close()

    def test_record_and_generate_fields_explain_themselves(self):
        # v0.5 item 16 (U20): every field explains itself in plain
        # words directly underneath it. v0.6 T16: the target combo
        # became pads, so the combo's explain line is gone — each
        # pad explains itself with a plain-words subtitle instead.
        from PySide6.QtWidgets import QLabel
        from mimik.gui import MainWindow

        win = MainWindow()
        record_tab = win.tabs.widget(0)
        explains = [w for w in record_tab.findChildren(QLabel)
                    if w.objectName() == "explain"]
        self.assertGreaterEqual(len(explains), 3)
        pads = [win.pads[key] for key in
                ("android", "linux-app", "file", "web", "windows")]
        self.assertEqual(len(pads), 5)
        for pad in pads:
            self.assertTrue(pad.title_label.text())
            self.assertTrue(pad.subtitle_label.text())
        labels = [w.text() for w in record_tab.findChildren(QLabel)]
        self.assertIn("What do you want to capture?", labels)
        generate_tab = win.tabs.widget(2)
        gen_explains = [w for w in generate_tab.findChildren(QLabel)
                        if w.objectName() == "explain"]
        # v0.6 T10: the polish picker gained its own plain-words
        # line, so the Generate tab has two explain labels now.
        self.assertEqual(len(gen_explains), 2)
        gen_text = " ".join(w.text() for w in gen_explains)
        self.assertIn("Usually the one with JSON", gen_text)
        self.assertIn("Polish rewrites the generated client", gen_text)
        win.close()

    def test_live_strip_hidden_when_idle(self):
        # v0.5 item 19 (U37): the live strip shows only while
        # recording; idle, the Record tab looks as it did.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        self.assertTrue(win.live_strip.isHidden())
        self.assertEqual(win.live_text.text(), "")
        win.close()

    def test_live_strip_shows_recording_numbers(self):
        # v0.5 item 19: flows, hosts, bytes, elapsed — real numbers,
        # updated by the status tick. Simulated recording state (no
        # real proxy): the worker + start time are what the updater
        # keys on.
        import time

        from mimik import store
        from mimik.gui import MainWindow, _dir_size

        win = MainWindow()
        win.show()
        win.worker = object()
        win._record_started = time.monotonic()
        win._flow_count = 3
        win._host_count = 2
        win._record_bytes_start = _dir_size(store.captures_dir())
        win._update_status_bar()
        self.app.processEvents()
        self.assertFalse(win.live_strip.isHidden())
        self.assertEqual(win.live_dot.text(), "●")
        self.assertIn("3 flows", win.live_text.text())
        self.assertIn("2 hosts", win.live_text.text())
        self.assertIn("0 B", win.live_text.text())
        self.assertIn("00:00", win.live_text.text())
        # Back to idle: the strip hides again.
        win.worker = None
        win._record_started = None
        win._update_status_bar()
        self.app.processEvents()
        self.assertTrue(win.live_strip.isHidden())
        win.close()

    def test_fmt_bytes_and_dir_size(self):
        # v0.5 item 19 helpers.
        import os
        import tempfile

        from mimik.gui import _dir_size, _fmt_bytes

        self.assertEqual(_fmt_bytes(0), "0 B")
        self.assertEqual(_fmt_bytes(512), "512 B")
        self.assertEqual(_fmt_bytes(1024), "1 KB")
        self.assertEqual(_fmt_bytes(1536), "1.5 KB")
        self.assertEqual(_fmt_bytes(1048576), "1 MB")
        self.assertEqual(_fmt_bytes(2621440), "2.5 MB")
        self.assertEqual(_fmt_bytes(-5), "0 B")
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.bin"), "wb") as fh:
                fh.write(b"x" * 100)
            os.mkdir(os.path.join(tmp, "sub"))
            with open(os.path.join(tmp, "sub", "b.bin"), "wb") as fh:
                fh.write(b"y" * 50)
            self.assertEqual(_dir_size(tmp), 150)
        self.assertEqual(_dir_size("/no/such/dir/at/all"), 0)

    def test_stop_button_dips_on_press(self):
        # v0.5 item 20, v0.6 T16: the Stop button dips to 97% over
        # 150 ms on press, then settles back to its exact rect.
        # (The Start button is gone — the pads start a capture.)
        # The wait is a wait-for-stop, not a fixed sleep: under
        # load the 150 ms animation can still be running at 250 ms.
        from PySide6.QtCore import QAbstractAnimation
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        button = win.stop_btn
        original = button.geometry()
        button.pressed.emit()
        anim = win._dip_anims.get(button)
        self.assertIsNotNone(anim)
        self.assertEqual(anim.duration(), 150)
        self.assertEqual(anim.state(), QAbstractAnimation.Running)
        self.assertTrue(_wait_for(
            self.app,
            lambda: anim.state() == QAbstractAnimation.Stopped,
            timeout=5))
        self.assertEqual(button.geometry(), original)
        win.close()

    def test_recording_finish_shows_completion_status(self):
        # v0.5 item 20: a quiet settled status when a capture
        # finishes — no modal, just the line, until the next
        # recording starts.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win._flow_count = 17
        win._host_count = 3
        win._recording_finished()
        self.assertEqual(win.status_proxy.text(),
                         "✓ Recorded 17 flows from 3 hosts")
        win._update_status_bar()
        self.assertEqual(win.status_proxy.text(),
                         "✓ Recorded 17 flows from 3 hosts")
        win._done_text = None
        win._update_status_bar()
        self.assertEqual(win.status_proxy.text(), "Proxy stopped")
        win.close()

    def test_generate_finish_shows_completion_status(self):
        # v0.5 item 20: the settled status names the client that
        # was written.
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
            self.assertTrue(win.status_proxy.text().startswith(
                "✓ Client written: "))
            self.assertIn("game", win.status_proxy.text())
            win.close()
        finally:
            os.chdir(old_cwd)

    def test_live_count_ticks_when_flows_land(self):
        # v0.5 item 20: the live count ticks as flows land — each
        # host poll re-renders the strip with the new numbers.
        import time

        from mimik import store
        from mimik.gui import MainWindow, _dir_size

        win = MainWindow()
        win.show()
        win.worker = object()
        win._record_started = time.monotonic()
        win._record_bytes_start = _dir_size(store.captures_dir())
        win._show_hosts([("a.example.com", 5), ("b.example.com", 2)])
        self.assertIn("7 flows", win.live_text.text())
        self.assertIn("2 hosts", win.live_text.text())
        win._show_hosts([("a.example.com", 9), ("b.example.com", 2)])
        self.assertIn("11 flows", win.live_text.text())
        win.worker = None
        win._record_started = None
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
        win.tabs.setCurrentIndex(7)  # Presets tab loads on first show
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
        win.tabs.setCurrentIndex(7)  # Presets tab
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

    def test_first_run_preloads_sample_and_shows_checklist(self):
        """v0.6 T3: a fresh data dir pre-loads the sample (no modal)
        and shows the checklist with step 1 ticked. The sample does
        not tick steps 2-3: it is pre-loaded, not the user's own
        recording."""
        from mimik import presets, sample, store
        from mimik.gui import MainWindow, _settings

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            _settings().remove("checklist")
            try:
                win = MainWindow()
                win.show()
                win._maybe_first_run()
                self.app.processEvents()
                self.assertEqual(len(store.load_host(sample.SAMPLE_HOST)),
                                 6)
                self.assertIsNotNone(
                    presets.load_preset(sample.SAMPLE_HOST))
                self.assertTrue(win.checklist_box.isVisible())
                labels = win._checklist_labels
                self.assertTrue(labels["picked"].text().startswith("✓"))
                self.assertTrue(labels["started"].text().startswith("○"))
                self.assertTrue(labels["host_seen"].text().startswith("○"))
                self.assertTrue(labels["generated"].text().startswith("○"))
                win.close()
            finally:
                _settings().remove("checklist")

    def test_checklist_dismiss_never_shows_again(self):
        """v0.6 T3: dismissing the checklist hides it for good —
        _maybe_first_run never shows it again."""
        from PySide6.QtWidgets import QPushButton
        from mimik.gui import MainWindow, _settings

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            _settings().remove("checklist")
            try:
                win = MainWindow()
                win.show()
                win._maybe_first_run()
                self.assertTrue(win.checklist_box.isVisible())
                dismiss = [b for b in
                           win.checklist_box.findChildren(QPushButton)
                           if b.text() == "Done with this"]
                self.assertEqual(len(dismiss), 1)
                dismiss[0].click()
                self.assertFalse(win.checklist_box.isVisible())
                win._maybe_first_run()
                self.assertFalse(win.checklist_box.isVisible())
                win.close()
            finally:
                _settings().remove("checklist")

    def test_checklist_ticks_on_events(self):
        """v0.6 T3: the steps tick as they really happen — live
        hosts tick step 3, a generated client ticks step 4, and the
        box goes away when every step is done."""
        from mimik.gui import MainWindow, _settings

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            _settings().remove("checklist")
            try:
                win = MainWindow()
                win.show()
                win._maybe_first_run()
                labels = win._checklist_labels
                self.assertTrue(labels["host_seen"].text().startswith("○"))
                win._show_hosts([("app.example.com", 5)])
                self.assertTrue(labels["host_seen"].text().startswith("✓"))
                self.assertTrue(labels["generated"].text().startswith("○"))
                win._checklist_tick("generated")
                self.assertTrue(labels["generated"].text().startswith("✓"))
                win._checklist_tick("started")
                self.assertFalse(win.checklist_box.isVisible())
                win.close()
            finally:
                _settings().remove("checklist")

    def test_help_menu_shows_checklist(self):
        """v0.6 T3: Help's checklist action brings the dismissed
        checklist back."""
        from mimik.gui import MainWindow, _settings

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            _settings().remove("checklist")
            try:
                win = MainWindow()
                win.show()
                win._maybe_first_run()
                self.assertTrue(win.checklist_box.isVisible())
                win._checklist_dismiss()
                self.assertFalse(win.checklist_box.isVisible())
                win._checklist_action.trigger()
                self.assertTrue(win.checklist_box.isVisible())
                win.close()
            finally:
                _settings().remove("checklist")

    def test_doctor_sample_button_loads(self):
        """v0.6 T3: the Doctor tab's Load sample capture button
        loads the sample and reports into the Doctor text — no
        modal (the old guide dialog is gone)."""
        from PySide6.QtWidgets import QPushButton
        from mimik import presets, sample, store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            win.tabs.setCurrentIndex(6)  # Doctor tab
            self.app.processEvents()
            btns = [b for b in win.findChildren(QPushButton)
                    if b.text() == "Load sample capture"]
            self.assertEqual(len(btns), 1)
            btns[0].click()
            self.assertEqual(len(store.load_host(sample.SAMPLE_HOST)),
                             6)
            self.assertIsNotNone(
                presets.load_preset(sample.SAMPLE_HOST))
            self.assertIn("Sample capture loaded",
                          win.doctor_text.toPlainText())
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
            # v0.5 item 21: five menus — Record + Generate join
            # File / View / Help.
            self.assertEqual(
                set(menus),
                {"File", "Record", "Generate", "View", "Help"})
            file_items = [a.text() for a in menus["File"].actions()
                          if not a.isSeparator()]
            self.assertIn("Import HAR…", file_items)
            self.assertIn("Import cURL…", file_items)
            self.assertIn("Scan a program folder…", file_items)
            self.assertIn("Analyze a binary…", file_items)
            self.assertIn("Ghidra setup steps…", file_items)
            self.assertIn("Load sample capture", file_items)
            self.assertIn("Quit", file_items)
            self.assertEqual(win._quit_action.shortcut().toString(),
                             "Ctrl+Q")
            record_items = [a.text() for a in menus["Record"].actions()
                            if not a.isSeparator()]
            self.assertIn("Start recording", record_items)
            self.assertIn("Stop recording", record_items)
            self.assertIn("Exclude telemetry + analytics hosts",
                          record_items)
            self.assertIn(
                "Screenshot the desktop when recording stops",
                record_items)
            self.assertEqual(win._menu_start_act.shortcut().toString(),
                             "Ctrl+R")
            gen_items = [a.text() for a in menus["Generate"].actions()
                         if not a.isSeparator()]
            self.assertEqual(gen_items, ["Generate client",
                                         "Build asset pack",
                                         "Learn preset"])
            view_items = [a.text() for a in menus["View"].actions()
                          if not a.isSeparator()]
            self.assertEqual(view_items[:8],
                             [win.tabs.tabText(i) for i in range(8)])
            self.assertIn("Refresh", view_items)
            self.assertEqual(win._refresh_action.shortcut().toString(),
                             "F5")
            for i, act in enumerate(win._tab_actions):
                self.assertEqual(act.shortcut().toString(),
                                 f"Ctrl+{i + 1}")
            help_items = [a.text() for a in menus["Help"].actions()]
            self.assertIn("About MIMIK", help_items)
            self.assertIn("View log", help_items)
            self.assertIn("Copy debug report", help_items)
        finally:
            win.close()

    def test_record_menu_toggles_mirror_checkboxes(self):
        # v0.5 item 21: the Record menu's two toggles mirror the
        # Record tab checkboxes, both directions.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            self.assertEqual(win._menu_filter_act.isChecked(),
                             win.filter_check.isChecked())
            win.filter_check.setChecked(
                not win.filter_check.isChecked())
            self.app.processEvents()
            self.assertEqual(win._menu_filter_act.isChecked(),
                             win.filter_check.isChecked())
            win._menu_filter_act.trigger()
            self.app.processEvents()
            self.assertEqual(win._menu_filter_act.isChecked(),
                             win.filter_check.isChecked())
            self.assertEqual(win._menu_shot_act.isChecked(),
                             win.screenshot_check.isChecked())
            win.screenshot_check.setChecked(
                not win.screenshot_check.isChecked())
            self.app.processEvents()
            self.assertEqual(win._menu_shot_act.isChecked(),
                             win.screenshot_check.isChecked())
            win._menu_shot_act.trigger()
            self.app.processEvents()
            self.assertEqual(win._menu_shot_act.isChecked(),
                             win.screenshot_check.isChecked())
        finally:
            win.close()

    def test_record_menu_start_stop_mirror_buttons(self):
        # v0.5 item 21, v0.6 T16: the Record menu's Start/Stop
        # mirror the pads' enabled state (the pads start a
        # capture now; there is no Start button).
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            pads_on = lambda: all(p.isEnabled()
                                  for p in win.pads.values())
            self.assertTrue(pads_on())
            self.assertTrue(win._menu_start_act.isEnabled())
            self.assertFalse(win.stop_btn.isEnabled())
            self.assertFalse(win._menu_stop_act.isEnabled())
            win.worker = mock.Mock()
            win._stop_recording()
            self.assertFalse(win.stop_btn.isEnabled())
            self.assertFalse(win._menu_stop_act.isEnabled())
            win._recording_finished()
            self.assertTrue(pads_on())
            self.assertTrue(win._menu_start_act.isEnabled())
            self.assertFalse(win.stop_btn.isEnabled())
            self.assertFalse(win._menu_stop_act.isEnabled())
        finally:
            win.close()

    def test_generate_menu_lands_on_tabs_without_inputs(self):
        # v0.5 item 21: with nothing picked, the Generate menu
        # lands on the tab whose guidance says what to pick — it
        # never warns about an input the tab is still loading.
        # Runs in its own empty data dir: "nothing picked" is only
        # true on an empty dir. In the shared class dir the
        # Presets tab's source combo defaults to its first item
        # (an asset group from the pack test), so Learn preset
        # learned a second preset in a worker and broke
        # test_presets_tab_lists_seeded_preset's count.
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            try:
                win._menu_generate_client()
                self.app.processEvents()
                self.assertEqual(win.tabs.currentIndex(), 2)
                self.assertEqual(win.gen_log.toPlainText(), "")
                win._menu_build_pack()
                self.app.processEvents()
                self.assertEqual(win.tabs.currentIndex(), 3)
                win._menu_learn_preset()
                self.app.processEvents()
                self.assertEqual(win.tabs.currentIndex(), 7)
            finally:
                win.close()

    def test_generate_menu_generates_when_host_picked(self):
        # v0.5 item 21: with a host picked, the Generate menu runs
        # the generation itself.
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
            win._menu_generate_client()
            self.assertTrue(_wait_for(
                self.app,
                lambda: "wrote" in win.gen_log.toPlainText()))
            self.assertTrue(os.path.exists(win.gen_out))
            win.close()
        finally:
            os.chdir(old_cwd)

    def test_file_menu_load_sample(self):
        # v0.5 item 21: File's Load sample capture goes to the
        # Doctor tab and loads the sample (no modal since v0.6 T3).
        # Runs in its own data dir like the other sample tests —
        # the sample's preset must not leak into the shared class
        # dir (it broke test_presets_tab_lists_seeded_preset's
        # count).
        from mimik import sample, store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            try:
                win._menu_load_sample()
                self.app.processEvents()
                self.assertEqual(win.tabs.currentIndex(), 6)
                self.assertEqual(len(store.load_host(sample.SAMPLE_HOST)),
                                 6)
            finally:
                win.close()

    def test_help_menu_copy_debug_report(self):
        # v0.5 item 21: Help's Copy debug report is the same action
        # as the Doctor tab's button.
        from PySide6.QtWidgets import QApplication
        from mimik import __version__
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            win._debug_action.trigger()
            text = QApplication.clipboard().text()
            self.assertIn(f"MIMIK {__version__}", text)
            self.assertIn("copied to the clipboard",
                          win.doctor_text.toPlainText())
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
                    self.app, lambda: win._update_task is None))
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
                    self.app, lambda: win._update_task is None))
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
                    self.app, lambda: win._update_task is None))
                warn.assert_called_once()
                self.assertIn("no route to host", warn.call_args[0][2])
        finally:
            win.close()

    def test_startup_check_toggle_in_help_menu(self):
        """v0.7: Help has a checkable "Check for updates on startup",
        on by default; toggling it writes the setting."""
        from mimik.gui import MainWindow, _settings

        win = MainWindow()
        try:
            help_menu = [a.menu() for a in win.menuBar().actions()
                         if a.text() == "Help"][0]
            items = [a.text() for a in help_menu.actions()]
            self.assertIn("Check for updates on startup", items)
            action = win._startup_update_action
            self.assertTrue(action.isCheckable())
            self.assertTrue(action.isChecked())
            self.assertTrue(win._startup_update_check_enabled())
            action.trigger()
            self.assertFalse(action.isChecked())
            self.assertFalse(_settings().value(
                "update/check_on_startup", True, type=bool))
            self.assertFalse(win._startup_update_check_enabled())
            action.trigger()
            self.assertTrue(_settings().value(
                "update/check_on_startup", True, type=bool))
        finally:
            win.close()

    def test_startup_check_silent_when_current(self):
        """v0.7: the launch check says nothing when current, and runs
        only once per launch."""
        from PySide6.QtWidgets import QMessageBox
        from mimik import update
        from mimik.gui import MainWindow

        current = {"current": "0.6.0", "latest": "0.6.0",
                   "update_available": False, "url": None, "error": None}
        win = MainWindow()
        win.show()
        try:
            with mock.patch.object(update, "check_for_update",
                                   return_value=current) as check, \
                 mock.patch.object(QMessageBox,
                                   "information") as info, \
                 mock.patch.object(QMessageBox, "warning") as warn:
                win._maybe_startup_update_check()
                self.assertTrue(_wait_for(
                    self.app, lambda: win._update_task is None))
                info.assert_not_called()
                warn.assert_not_called()
                self.assertTrue(win._update_check_ran)
                win._maybe_startup_update_check()
                self.assertEqual(check.call_count, 1)
        finally:
            win.close()

    def test_startup_check_silent_when_failed(self):
        """v0.7: a failed launch check is a status-bar line, never a
        modal on startup."""
        from PySide6.QtWidgets import QMessageBox
        from mimik import update
        from mimik.gui import MainWindow

        error = {"current": "0.6.0", "latest": None,
                 "update_available": False, "url": None,
                 "error": "no route to host"}
        win = MainWindow()
        win.show()
        try:
            with mock.patch.object(update, "check_for_update",
                                   return_value=error), \
                 mock.patch.object(QMessageBox, "warning") as warn:
                win._maybe_startup_update_check()
                self.assertTrue(_wait_for(
                    self.app, lambda: win._update_task is None))
                warn.assert_not_called()
                self.assertIn("could not check for updates",
                              win.statusBar().currentMessage())
        finally:
            win.close()

    def test_startup_check_disabled_runs_nothing(self):
        """v0.7: with the startup check unticked, the launch step
        makes no check at all."""
        from mimik import update
        from mimik.gui import MainWindow, _settings

        _settings().setValue("update/check_on_startup", False)
        win = MainWindow()
        win.show()
        try:
            with mock.patch.object(update,
                                   "check_for_update") as check:
                win._maybe_startup_update_check()
                self.app.processEvents()
                check.assert_not_called()
                self.assertIsNone(win._update_task)
        finally:
            win.close()

    def test_second_check_while_running_refused(self):
        """v0.7: a second check while one runs is refused plainly."""
        import time

        from PySide6.QtWidgets import QMessageBox
        from mimik import update
        from mimik.gui import MainWindow

        def slow_check():
            time.sleep(0.5)
            return {"current": "0.6.0", "latest": "0.6.0",
                    "update_available": False, "url": None,
                    "error": None}

        win = MainWindow()
        win.show()
        try:
            with mock.patch.object(update, "check_for_update",
                                   side_effect=slow_check), \
                 mock.patch.object(QMessageBox,
                                   "information") as info:
                self.assertTrue(win._start_update_check(silent=False))
                self.assertFalse(win._start_update_check(silent=False))
                info.assert_called_once()
                self.assertIn("Already checking",
                              info.call_args[0][2])
                self.assertTrue(_wait_for(
                    self.app, lambda: win._update_task is None))
        finally:
            win.close()

    def _newer_result(self):
        return {"current": "0.6.0", "latest": "0.7.0",
                "update_available": True,
                "url": "https://github.com/x/releases/tag/v0.7",
                "zip_url": "https://dl/mimik-v0.7.zip",
                "zip_name": "mimik-v0.7.zip",
                "sha_url": "https://dl/mimik-v0.7.zip.sha256",
                "sha_name": "mimik-v0.7.zip.sha256",
                "error": None}

    def test_update_offer_downloads_and_restarts(self):
        """v0.7: Yes to the offer downloads and verifies, Yes to the
        restart prompt stages the update and launches the updater."""
        import tempfile

        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QMessageBox
        from mimik import update
        from mimik.gui import MainWindow

        data_dir = tempfile.mkdtemp()
        staged = os.path.join(update.pending_dir(base=data_dir),
                              "mimik-v0.7.zip")
        os.makedirs(update.pending_dir(base=data_dir), exist_ok=True)
        with open(staged, "wb") as f:
            f.write(b"zip bytes")
        download = {"ok": True, "path": staged,
                    "sha256": "ab" * 32, "size": 9}
        win = MainWindow()
        win.show()
        try:
            with mock.patch.dict(os.environ,
                                 {"MIMIK_DATA_DIR": data_dir}), \
                 mock.patch.object(update, "check_for_update",
                                   return_value=self._newer_result()), \
                 mock.patch.object(update, "is_installed",
                                   return_value=True), \
                 mock.patch.object(update, "download_update",
                                   return_value=download) as dl, \
                 mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.Yes) as q, \
                 mock.patch.object(QProcess,
                                   "startDetached") as launch:
                win._check_for_updates()
                self.assertTrue(_wait_for(
                    self.app, lambda: win._update_task is None))
                dl.assert_called_once()
                self.assertEqual(q.call_count, 2)
                self.assertIn("Download and install",
                              q.call_args_list[0][0][2])
                self.assertIn("Restart MIMIK now",
                              q.call_args_list[1][0][2])
                marker = update.read_pending(base=data_dir)
                self.assertEqual(marker["version"], "0.7.0")
                self.assertEqual(marker["zip"], "mimik-v0.7.zip")
                launch.assert_called_once()
                script = launch.call_args[0][0]
                self.assertTrue(script.endswith("update.sh"))
                self.assertTrue(os.path.isfile(script))
                self.assertFalse(win.isVisible())
        finally:
            win.close()

    def test_update_offer_no_restart_leaves_staged(self):
        """v0.7: No to the restart prompt leaves the update staged
        for the next launch and does not relaunch."""
        import tempfile

        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QMessageBox
        from mimik import update
        from mimik.gui import MainWindow

        data_dir = tempfile.mkdtemp()
        staged = os.path.join(update.pending_dir(base=data_dir),
                              "mimik-v0.7.zip")
        os.makedirs(update.pending_dir(base=data_dir), exist_ok=True)
        with open(staged, "wb") as f:
            f.write(b"zip bytes")
        download = {"ok": True, "path": staged,
                    "sha256": "ab" * 32, "size": 9}
        win = MainWindow()
        win.show()
        try:
            with mock.patch.dict(os.environ,
                                 {"MIMIK_DATA_DIR": data_dir}), \
                 mock.patch.object(update, "check_for_update",
                                   return_value=self._newer_result()), \
                 mock.patch.object(update, "is_installed",
                                   return_value=True), \
                 mock.patch.object(update, "download_update",
                                   return_value=download), \
                 mock.patch.object(
                     QMessageBox, "question",
                     side_effect=[QMessageBox.Yes,
                                  QMessageBox.No]) as q, \
                 mock.patch.object(QProcess,
                                   "startDetached") as launch:
                win._check_for_updates()
                self.assertTrue(_wait_for(
                    self.app, lambda: win._update_task is None))
                self.assertEqual(q.call_count, 2)
                marker = update.read_pending(base=data_dir)
                self.assertEqual(marker["version"], "0.7.0")
                launch.assert_not_called()
                self.assertTrue(win.isVisible())
                self.assertIn("will install when MIMIK restarts",
                              win.statusBar().currentMessage())
        finally:
            win.close()

    def test_download_failure_warns_and_stages_nothing(self):
        """v0.7: a refused download (no sidecar, bad checksum, or a
        failed fetch) warns in plain words and stages nothing."""
        import tempfile

        from PySide6.QtWidgets import QMessageBox
        from mimik import update
        from mimik.gui import MainWindow

        data_dir = tempfile.mkdtemp()
        download = {"ok": False, "error":
                    "the release has no checksum file, so MIMIK will "
                    "not install it blind"}
        win = MainWindow()
        win.show()
        try:
            with mock.patch.dict(os.environ,
                                 {"MIMIK_DATA_DIR": data_dir}), \
                 mock.patch.object(update, "check_for_update",
                                   return_value=self._newer_result()), \
                 mock.patch.object(update, "is_installed",
                                   return_value=True), \
                 mock.patch.object(update, "download_update",
                                   return_value=download), \
                 mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.Yes) as q, \
                 mock.patch.object(QMessageBox, "warning") as warn:
                win._check_for_updates()
                self.assertTrue(_wait_for(
                    self.app, lambda: win._update_task is None))
                self.assertEqual(q.call_count, 1)
                warn.assert_called_once()
                self.assertIn("no checksum file", warn.call_args[0][2])
                self.assertIsNone(update.read_pending(base=data_dir))
        finally:
            win.close()

    def test_startup_update_note_shown_once(self):
        """v0.7: a note main() left (a failed install) shows once as
        a dialog; a plain note shows once in the status bar."""
        from PySide6.QtWidgets import QMessageBox
        from mimik.gui import MainWindow, _settings

        _settings().setValue("update/check_on_startup", False)
        win = MainWindow()
        win.show()
        try:
            win._startup_update_note = "The update did not install: x"
            win._startup_update_failed = True
            with mock.patch.object(QMessageBox,
                                   "information") as info:
                win._maybe_startup_update_check()
                info.assert_called_once()
                self.assertIn("did not install", info.call_args[0][2])
                self.assertIsNone(win._startup_update_note)
        finally:
            win.close()
        _settings().setValue("update/check_on_startup", False)
        win = MainWindow()
        win.show()
        try:
            win._startup_update_note = "The staged update was thrown away."
            with mock.patch.object(QMessageBox,
                                   "information") as info:
                win._maybe_startup_update_check()
                info.assert_not_called()
                self.assertIn("thrown away",
                              win.statusBar().currentMessage())
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
        win.tabs.setCurrentIndex(7)  # Presets tab loads on first show
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
        # v0.6 T16: the targets are pads now, in PAD_TARGETS order.
        from mimik.gui import PAD_TARGETS, MainWindow

        win = MainWindow()
        self.assertEqual(list(win.pads),
                         [key for key, _t, _s in PAD_TARGETS])
        self.assertEqual(list(win.pads),
                         ["android", "linux-app", "file", "web",
                          "windows"])
        win.close()

    def test_record_pads_have_titles_and_subtitles(self):
        # v0.6 T16: every pad names its target in plain words.
        from mimik.gui import PAD_TARGETS, MainWindow

        win = MainWindow()
        try:
            for key, title, subtitle in PAD_TARGETS:
                pad = win.pads[key]
                self.assertEqual(pad.title_label.text(), title)
                self.assertEqual(pad.subtitle_label.text(), subtitle)
        finally:
            win.close()

    def test_tap_android_pad_starts_recording(self):
        # v0.6 T16 (entry 024): tap a pad, it starts.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            with mock.patch.object(win, "_start_recording") as started:
                QTest.mouseClick(win.pads["android"], Qt.LeftButton)
                self.app.processEvents()
                started.assert_called_once()
        finally:
            win.close()

    def test_tap_web_pad_selects_without_starting(self):
        # v0.6 T16: the web pad has no proxy to start — it only
        # selects, so the steps text says what to do instead.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            with mock.patch.object(win, "_start_recording") as started:
                QTest.mouseClick(win.pads["web"], Qt.LeftButton)
                self.app.processEvents()
                started.assert_not_called()
            self.assertEqual(win._current_target, "web")
            self.assertEqual(win.pads["web"].property("padState"),
                             "selected")
        finally:
            win.close()

    def test_pads_say_what_a_tap_does(self):
        # v0.8 B5: the pads are the Start button (T16) but read as
        # selectors — the hint line and every pad subtitle must say
        # what tapping does, and the Web pad must own up that it
        # starts no proxy.
        from mimik.gui import PAD_TARGETS, MainWindow

        win = MainWindow()
        win.show()
        try:
            self.assertIn("Tap a card to start recording",
                          win.pads_hint.text())
            subs = {key: sub for key, _t, sub in PAD_TARGETS}
            self.assertIn("Tap to start", subs["android"])
            self.assertIn("Tap to start", subs["linux-app"])
            self.assertIn("Tap to start", subs["windows"])
            self.assertIn("Tap to choose", subs["file"])
            self.assertIn("no proxy", subs["web"])
            self.assertGreaterEqual(win.steps.minimumHeight(), 140)
        finally:
            win.close()

    def test_tap_file_pad_without_file_opens_picker(self):
        # v0.6 T16: the file pad with no file opens the picker
        # instead of starting — no dead end, no silent no-op.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            self.assertIsNone(win.plan)
            with mock.patch.object(win, "_choose_file") as choose, \
                 mock.patch.object(win, "_start_recording") as started:
                QTest.mouseClick(win.pads["file"], Qt.LeftButton)
                self.app.processEvents()
                choose.assert_called_once()
                started.assert_not_called()
        finally:
            win.close()

    def test_tap_recording_pad_stops(self):
        # v0.6 T16 (entry 024): tapping the recording pad stops
        # it, like tapping a playing clip.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            win.worker = mock.Mock()
            win._set_pads_enabled(False)
            win._select_target("android")
            self.assertEqual(win.pads["android"].property("padState"),
                             "recording")
            self.assertTrue(win.pads["android"].isEnabled())
            with mock.patch.object(win, "_stop_recording") as stopped:
                QTest.mouseClick(win.pads["android"], Qt.LeftButton)
                self.app.processEvents()
                stopped.assert_called_once()
        finally:
            win.worker = None  # the mock is not a real thread
            win.close()

    def test_tap_other_pad_while_recording_is_ignored(self):
        # v0.6 T16: one recording at a time — tapping another pad
        # mid-recording starts nothing and stops nothing. Called
        # directly: the other pads are greyed out, so a real click
        # never reaches them; this tests _tap_pad's own guard.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            win.worker = mock.Mock()
            win._set_pads_enabled(False)
            win._select_target("android")
            self.assertFalse(win.pads["web"].isEnabled())
            with mock.patch.object(win, "_start_recording") as started, \
                 mock.patch.object(win, "_stop_recording") as stopped:
                win._tap_pad("web")
                started.assert_not_called()
                stopped.assert_not_called()
            self.assertEqual(win._current_target, "android")
        finally:
            win.worker = None
            win.close()

    def test_pad_states_follow_selection_and_recording(self):
        # v0.6 T16 (entry 024): every pad has a clear state.
        from mimik.gui import MainWindow

        win = MainWindow()
        try:
            states = lambda: {k: p.property("padState")
                              for k, p in win.pads.items()}
            win._select_target("linux-app")
            self.assertEqual(states()["linux-app"], "selected")
            self.assertEqual(states()["android"], "idle")
            win.worker = mock.Mock()
            win._select_target("linux-app")
            self.assertEqual(states()["linux-app"], "recording")
            win.worker = None
            win._select_target("linux-app")
            self.assertEqual(states()["linux-app"], "selected")
        finally:
            win.worker = None
            win.close()

    def test_file_pad_subtitle_names_chosen_file(self):
        # v0.6 T16: once a file is prepared, the pad says so.
        from mimik.gui import MainWindow

        win = MainWindow()
        try:
            win._select_target("file")
            self.assertEqual(win.pads["file"].subtitle_label.text(),
                             "Tap to choose a file")
            win.file_path = "/tmp/some-program.AppImage"
            win.plan = mock.Mock()
            win.plan.command = ["some-program.AppImage"]
            win._refresh_file_pad()
            self.assertEqual(win.pads["file"].subtitle_label.text(),
                             "Ready: some-program.AppImage, tap to record")
        finally:
            win.close()

    def test_pad_keyboard_space_starts(self):
        # v0.6 T16: pads are real buttons — Space starts, like a
        # click. (Full keyboard/focus work is T11.)
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            pad = win.pads["android"]
            pad.setFocus()
            self.app.processEvents()
            with mock.patch.object(win, "_start_recording") as started:
                QTest.keyClick(pad, Qt.Key_Space)
                self.app.processEvents()
                started.assert_called_once()
        finally:
            win.close()

    def test_doctor_tab_renders(self):
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win.tabs.setCurrentIndex(6)  # Doctor tab loads on first show
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

    def test_polish_combo_shows_real_availability(self):
        # v0.6 T10: the probe's real labels replace the "checking…"
        # placeholders, and the plain-words line says what polish
        # does. Keys stay the same.
        from mimik.gui import MainWindow

        engines = [
            {"key": "ollama", "ok": True,
             "label": "ollama (running, 2 models)"},
            {"key": "claude", "ok": True,
             "label": "claude (installed)"},
            {"key": "opencode", "ok": False,
             "label": "opencode (not installed)"},
        ]
        win = MainWindow()
        try:
            self.assertIn("rewrites the generated client",
                          win.polish_explain.text())
            with mock.patch("mimik.gui.doctor.polish_engines",
                            return_value=engines):
                win._refresh_polish()
                _wait_for(self.app, lambda: win._polish_task is None)
            labels = [win.polish_combo.itemText(i)
                      for i in range(win.polish_combo.count())]
            self.assertEqual(labels, ["none (deterministic, free)",
                                      "ollama (running, 2 models)",
                                      "claude (installed)",
                                      "opencode (not installed)"])
            values = [win.polish_combo.itemData(i)
                      for i in range(win.polish_combo.count())]
            self.assertEqual(values, ["none", "ollama", "claude",
                                      "opencode"])
        finally:
            win.close()

    def test_polish_refresh_keeps_selection(self):
        # v0.6 T10: rebuilding the labels must not move the pick.
        from mimik.gui import MainWindow

        engines = [
            {"key": "ollama", "ok": False,
             "label": "ollama (not running)"},
            {"key": "claude", "ok": True,
             "label": "claude (installed)"},
            {"key": "opencode", "ok": False,
             "label": "opencode (not installed)"},
        ]
        win = MainWindow()
        try:
            win.polish_combo.setCurrentIndex(
                win.polish_combo.findData("claude"))
            with mock.patch("mimik.gui.doctor.polish_engines",
                            return_value=engines):
                win._refresh_polish()
                _wait_for(self.app, lambda: win._polish_task is None)
            self.assertEqual(win.polish_combo.currentData(), "claude")
        finally:
            win.close()

    def test_focus_ring_rules_in_both_themes(self):
        # v0.6 T11 (a): a designed focus ring in both themes — a
        # violet border on focused controls, and a pad ring that
        # never reads as selection (violet) or recording (red).
        from mimik.gui import LIGHT_STYLESHEET, STYLESHEET

        for sheet, violet in ((STYLESHEET, "#b48fdd"),
                              (LIGHT_STYLESHEET, "#6b3fa0")):
            self.assertIn("QPushButton:focus", sheet)
            self.assertIn(f"border: 2px solid {violet}", sheet)
            self.assertIn("QFrame#pad:focus", sheet)

    def test_tab_switch_focuses_primary_action(self):
        # v0.6 T11 (b): switching tabs lands focus on the tab's
        # primary action, so each tab works from the keyboard.
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            targets = {
                0: win.pads[win._current_target],
                1: win.hosts_list,
                2: win.gen_host,
                3: win.asset_group,
                4: win.clients_list,
                5: win.binary_analyze_btn,
                6: win.doctor_recheck_btn,
                7: win.presets_list,
            }
            # Record last: switching to the tab already shown fires
            # no signal, so it must be reached by a real switch.
            for index in (1, 2, 3, 4, 5, 6, 7, 0):
                win.tabs.setCurrentIndex(index)
                self.app.processEvents()
                self.assertIs(win.focusWidget(), targets[index],
                              f"tab {index}")
        finally:
            win.close()

    def test_f9_toggles_recording(self):
        # v0.6 T11 (c): F9 starts when idle, stops when recording.
        # The real start/stop stay mocked — this pins the wiring.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win.activateWindow()
        self.app.processEvents()
        try:
            with mock.patch.object(
                    win, "_start_recording") as start, \
                    mock.patch.object(win, "_stop_recording") as stop:
                QTest.keyClick(win, Qt.Key_F9)
                self.app.processEvents()
                start.assert_called_once_with()
                stop.assert_not_called()
                win.worker = mock.Mock()
                QTest.keyClick(win, Qt.Key_F9)
                self.app.processEvents()
                stop.assert_called_once_with()
        finally:
            win.worker = None
            win.close()

    def test_pad_space_key_starts_recording(self):
        # v0.6 T11 (b): a focused pad starts on Space, like a
        # button — the keyboard path into a capture.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        win.activateWindow()
        self.app.processEvents()
        try:
            pad = win.pads["android"]
            pad.setFocus()
            with mock.patch.object(win, "_start_recording") as start:
                QTest.keyClick(pad, Qt.Key_Space)
                self.app.processEvents()
                start.assert_called_once_with()
        finally:
            win.close()

    def test_client_preview_shows_host_methods_date(self):
        # v0.6 T12: selecting a client shows its host, method list,
        # and generated date in the preview pane.
        from datetime import date
        from mimik import codegen, store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            src = codegen.generate_deterministic("demo.example.com", [
                {"method": "GET", "path": "/api/items", "status": 200,
                 "query": "", "request_body": "",
                 "response_body": ""}])
            store.save_client_copy("demo_client.py", src)
            win = MainWindow()
            win.show()
            try:
                win.tabs.setCurrentIndex(4)  # Clients tab
                self.assertTrue(_wait_for(
                    self.app, lambda: any(
                        "demo_client.py"
                        in win.clients_list.item(i).text()
                        for i in range(win.clients_list.count()))))
                self.assertTrue(_select_client(win, "demo_client.py"))
                self.app.processEvents()
                text = win.client_preview.toPlainText()
                self.assertIn("Host: demo.example.com", text)
                self.assertIn("get_items", text)
                self.assertIn(f"Generated: {date.today().isoformat()}",
                              text)
            finally:
                win.close()

    def test_client_preview_empty_when_no_client(self):
        # v0.6 T12: with no clients, the preview pane stays empty.
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            try:
                win.tabs.setCurrentIndex(4)  # Clients tab
                self.app.processEvents()
                self.assertEqual(win.client_preview.toPlainText(), "")
            finally:
                win.close()

    def test_big_text_grows_body_type(self):
        # v0.6 T13: the Big text preset grows body type (14 to 18,
        # explain 12 to 15) and leaves the theme alone.
        from mimik.gui import MainWindow

        win = MainWindow()
        try:
            win._set_access("big_text", True)
            sheet = win.styleSheet()
            self.assertIn("#17171c", sheet)
            self.assertIn("font-size: 18px", sheet)
            self.assertIn("font-size: 15px", sheet)
            self.assertNotIn("font-size: 14px", sheet)
            self.assertNotIn("font-size: 12px", sheet)
            win._set_access("big_text", False)
            self.assertIn("font-size: 14px", win.styleSheet())
        finally:
            win.close()

    def test_high_contrast_sheet(self):
        # v0.6 T13: the High contrast preset swaps in the black /
        # white / yellow sheet, and turning it off restores the
        # saved theme. Big text still applies on top of it.
        from mimik.gui import MainWindow

        win = MainWindow()
        try:
            win._set_access("high_contrast", True)
            sheet = win.styleSheet()
            self.assertIn("#000000", sheet)
            self.assertIn("#ffff00", sheet)
            self.assertNotIn("#17171c", sheet)
            win._set_access("big_text", True)
            self.assertIn("font-size: 18px", win.styleSheet())
            self.assertIn("#000000", win.styleSheet())
            win._set_access("big_text", False)
            win._set_access("high_contrast", False)
            self.assertIn("#17171c", win.styleSheet())
        finally:
            win.close()

    def test_reduced_motion_skips_dip(self):
        # v0.6 T13: Reduced motion is the off-switch for the press
        # dip — no animation object is even created.
        from mimik.gui import MainWindow

        win = MainWindow()
        try:
            win._set_access("reduced_motion", True)
            win._dip_button(win.stop_btn)
            self.assertEqual(win._dip_anims, {})
            win._set_access("reduced_motion", False)
            win._dip_button(win.stop_btn)
            self.assertEqual(len(win._dip_anims), 1)
        finally:
            win.close()

    def test_one_touch_hides_advanced_controls(self):
        # v0.6 T13: One-touch capture leaves the Record tab as pads
        # and Stop only; turning it off restores every control.
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            win = MainWindow()
            win.show()
            try:
                self.assertTrue(win.more_btn.isVisible())
                self.assertTrue(win.steps.isVisible())
                self.assertTrue(win.host_list.isVisible())
                win._set_access("one_touch", True)
                self.app.processEvents()
                self.assertFalse(win.more_btn.isVisible())
                self.assertFalse(win.more_box.isVisible())
                self.assertFalse(win.steps.isVisible())
                self.assertFalse(win.live_hosts_label.isVisible())
                self.assertFalse(win.host_list.isVisible())
                self.assertTrue(win.pads["android"].isVisible())
                self.assertTrue(win.stop_btn.isVisible())
                win._set_access("one_touch", False)
                self.app.processEvents()
                self.assertTrue(win.more_btn.isVisible())
                self.assertTrue(win.steps.isVisible())
                self.assertTrue(win.host_list.isVisible())
                self.assertTrue(win.more_box.isHidden())
            finally:
                win.close()

    def test_one_touch_applies_defaults(self):
        # v0.6 T13: turning One-touch on sets the current target's
        # capture choices to the defaults (filter on, screenshot
        # off) — single-tap start with defaults.
        from mimik import presets
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            presets.set_filtering_enabled(False, target="android")
            presets.set_screenshot_enabled(True, target="android")
            win = MainWindow()
            try:
                self.assertFalse(win.filter_check.isChecked())
                self.assertTrue(win.screenshot_check.isChecked())
                win._set_access("one_touch", True)
                self.assertTrue(
                    presets.filtering_enabled(target="android"))
                self.assertFalse(
                    presets.screenshot_enabled(target="android"))
                self.assertTrue(win.filter_check.isChecked())
                self.assertFalse(win.screenshot_check.isChecked())
            finally:
                win.close()

    def test_view_menu_has_accessibility_presets(self):
        # v0.6 T13: the View menu's Accessibility submenu holds all
        # four presets, and triggering one applies it.
        from mimik.gui import MainWindow, access_setting

        win = MainWindow()
        try:
            self.assertEqual(set(win._access_actions),
                             {"big_text", "high_contrast",
                              "reduced_motion", "one_touch"})
            for act in win._access_actions.values():
                self.assertTrue(act.isCheckable())
            win._access_actions["big_text"].trigger()
            self.assertTrue(access_setting("big_text"))
            self.assertIn("font-size: 18px", win.styleSheet())
            win._access_actions["big_text"].trigger()
            self.assertFalse(access_setting("big_text"))
        finally:
            win.close()

    def test_checklist_offers_comfort_presets(self):
        # v0.6 T13: the first-run checklist offers the same four
        # presets, and the menu action and the checkbox stay in
        # sync both ways.
        from mimik.gui import MainWindow, access_setting

        win = MainWindow()
        try:
            self.assertEqual(set(win._access_checks),
                             {"big_text", "high_contrast",
                              "reduced_motion", "one_touch"})
            win._access_checks["reduced_motion"].setChecked(True)
            self.assertTrue(access_setting("reduced_motion"))
            self.assertTrue(
                win._access_actions["reduced_motion"].isChecked())
            win._access_actions["reduced_motion"].trigger()
            self.assertFalse(access_setting("reduced_motion"))
            self.assertFalse(
                win._access_checks["reduced_motion"].isChecked())
        finally:
            win.close()

    def test_access_presets_persist_across_restarts(self):
        # v0.6 T13: a preset survives a restart — the next window
        # opens with it applied and both controls showing it.
        from mimik.gui import MainWindow, access_setting

        win1 = MainWindow()
        win1._set_access("big_text", True)
        win1.close()
        self.app.processEvents()
        self.assertTrue(access_setting("big_text"))
        win2 = MainWindow()
        try:
            self.assertIn("font-size: 18px", win2.styleSheet())
            self.assertTrue(win2._access_actions["big_text"].isChecked())
            self.assertTrue(win2._access_checks["big_text"].isChecked())
        finally:
            win2.close()

    def test_timeline_scrubs_endpoints(self):
        # v0.6 T14: dragging the timeline filters the endpoints to
        # the calls made up to that moment; far right shows all.
        from mimik import store
        from mimik.gui import MainWindow

        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": empty}):
            flows = [_ep_flow("t1", "GET", "/api/a"),
                     _ep_flow("t2", "GET", "/api/b"),
                     _ep_flow("t3", "GET", "/api/c")]
            flows[0]["request"]["timestamp_start"] = 1000.0
            flows[1]["request"]["timestamp_start"] = 1010.0
            flows[2]["request"]["timestamp_start"] = 1020.0
            store.save_flows(flows, base=empty)
            win = MainWindow()
            win.show()
            try:
                win.tabs.setCurrentIndex(1)  # Hosts tab
                self.assertTrue(_wait_for(
                    self.app, lambda: any(
                        "ep.example.com" in win.hosts_list.item(i).text()
                        for i in range(win.hosts_list.count()))))
                win.hosts_list.setCurrentRow(0)
                self.assertTrue(_wait_for(
                    self.app, lambda: win.ep_table.rowCount() == 3))
                self.assertTrue(win.timeline_slider.isEnabled())
                self.assertEqual(win.timeline_label.text(),
                                 "all 3 endpoints")
                win.timeline_slider.setValue(0)
                self.app.processEvents()
                self.assertFalse(win.ep_table.isRowHidden(0))
                self.assertTrue(win.ep_table.isRowHidden(1))
                self.assertTrue(win.ep_table.isRowHidden(2))
                self.assertIn("1 of 3", win.timeline_label.text())
                win.timeline_slider.setValue(500)
                self.app.processEvents()
                self.assertFalse(win.ep_table.isRowHidden(1))
                self.assertTrue(win.ep_table.isRowHidden(2))
                self.assertIn("2 of 3", win.timeline_label.text())
                win.timeline_slider.setValue(1000)
                self.app.processEvents()
                self.assertFalse(win.ep_table.isRowHidden(2))
                self.assertEqual(win.timeline_label.text(),
                                 "all 3 endpoints")
            finally:
                win.close()

    def test_timeline_disabled_without_times(self):
        # v0.6 T14: endpoints with no times leave the timeline
        # disabled and saying so; a single shared moment says that
        # instead of pretending to scrub.
        from mimik.gui import MainWindow

        win = MainWindow()
        try:
            win._setup_timeline([{"first_seen": None},
                                 {"first_seen": None}])
            self.assertFalse(win.timeline_slider.isEnabled())
            self.assertEqual(win.timeline_label.text(),
                             "no times recorded yet")
            win._setup_timeline([{"first_seen": 1000.0},
                                 {"first_seen": 1000.0}])
            self.assertFalse(win.timeline_slider.isEnabled())
            self.assertEqual(win.timeline_label.text(),
                             "all calls at one moment")
        finally:
            win.close()

    def test_assets_tab_is_a_bento(self):
        # v0.6 T15: hero asset table on the left, Reblend + pack as
        # side tiles stacked on the right — three distinct tiles,
        # hero biggest (entry 005: size is the hierarchy).
        from PySide6.QtWidgets import QFrame

        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        try:
            # The tab must be shown before its tiles lay out — a
            # hidden tab page reports unlaid-out geometry.
            win.tabs.setCurrentIndex(3)  # Assets tab
            self.app.processEvents()
            tiles = [c for c in win.findChildren(QFrame)
                     if c.objectName() == "tile"]
            self.assertEqual(len(tiles), 3)
            hero = win.asset_table.parentWidget()
            reblend = win.reblend_label.parentWidget()
            pack = win.build_pack_btn.parentWidget()
            self.assertEqual(len({hero, reblend, pack}), 3)
            self.assertGreater(hero.width(), reblend.width())
            self.assertGreater(hero.width(), pack.width())
            self.assertGreater(hero.height(), reblend.height())
            self.assertGreater(hero.height(), pack.height())
        finally:
            win.close()

    def test_tile_rules_in_all_themes(self):
        # v0.6 T15: the tile rule (16 px corners, entry 005's
        # 12-24 px band) exists in every stylesheet.
        from mimik.gui import (HIGH_CONTRAST_STYLESHEET,
                               LIGHT_STYLESHEET, STYLESHEET)

        for sheet in (STYLESHEET, LIGHT_STYLESHEET,
                      HIGH_CONTRAST_STYLESHEET):
            self.assertIn("QFrame#tile", sheet)
            self.assertIn("border-radius: 16px", sheet)
            # Labels on a tile must not paint the window color
            # behind their text (the generic QWidget rule would).
            self.assertIn("QFrame#tile QLabel { background: transparent; }",
                          sheet)

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
            second = os.path.join(tmp, "game_example_com_2_client.py")
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
                os.path.join(tmp, "game_example_com_2_client.py")))
        finally:
            win.close()

    def test_generate_overwrite_cancel(self):
        win, tmp, original = self._generate_with_existing(None)
        try:
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "game_example_com_2_client.py")))
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
                win._select_target("file")
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
                win.tabs.setCurrentIndex(6)  # Doctor tab
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
        win.tabs.setCurrentIndex(6)  # Doctor tab
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

            # Binary: empty list + summary placeholder.
            win.tabs.setCurrentIndex(5)
            self.app.processEvents()
            self.assertEqual(win.binaries_list.count(), 1)
            self.assertIn("No binaries analyzed",
                          win.binaries_list.item(0).text())
            self.assertIn("Select a binary",
                          win.binary_summary.placeholderText())

            # Doctor: the check still runs (placeholder pre-load).
            win.tabs.setCurrentIndex(6)
            self.assertTrue(_wait_for(
                self.app,
                lambda: "overall:" in win.doctor_text.toPlainText()))
            self.assertIn("Running the setup check",
                          win.doctor_text.placeholderText())

            # Presets: empty source + both empty lists.
            win.tabs.setCurrentIndex(7)
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
        presets_tab = win.tabs.widget(7)
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
        status bar with the new saved total. v0.5 item 20: the proxy
        side settles on the completion state, not "Proxy stopped"."""
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
        self.assertEqual(win.status_proxy.text(),
                         "✓ Recorded 0 flows from 0 hosts")
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
        # v0.6 T8: the checkbox toggles the current target's own
        # screenshot choice; the global value is untouched.
        from mimik import presets
        from mimik.gui import MainWindow

        win = MainWindow()
        win.show()
        target = win._current_target
        self.assertIsNotNone(target)
        self.assertFalse(win.screenshot_check.isChecked())
        self.assertFalse(presets.screenshot_enabled())
        win.screenshot_check.setChecked(True)
        self.app.processEvents()
        self.assertTrue(presets.screenshot_enabled(target=target))
        self.assertFalse(presets.screenshot_enabled())
        win.screenshot_check.setChecked(False)
        self.app.processEvents()
        self.assertFalse(presets.screenshot_enabled(target=target))
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


@unittest.skipUnless(HAS_QT, "PySide6 not installed")
class BinaryTabTests(unittest.TestCase):
    """v0.5 items 7, 8, 10: the Binary tab + the Hosts link."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        from tests.test_binary import build_tiny_elf
        from mimik import binary as binary_mod

        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.elf_path = os.path.join(self.tmp, "tiny.elf")
        with open(self.elf_path, "wb") as f:
            f.write(build_tiny_elf())
        self.analysis = binary_mod.analyze(self.elf_path)
        self.sha = self.analysis["sha256"]
        from mimik.gui import MainWindow

        self.win = MainWindow()
        self.win.show()

    def tearDown(self):
        self.win.close()
        del os.environ["MIMIK_DATA_DIR"]
        self.app.processEvents()
        gc.collect()

    def _open_binary_tab(self):
        self.win.tabs.setCurrentIndex(5)  # Binary tab
        self.app.processEvents()

    def _select_binary(self):
        win = self.win
        for i in range(win.binaries_list.count()):
            if "tiny.elf" in win.binaries_list.item(i).text():
                win.binaries_list.setCurrentRow(i)
                self.app.processEvents()
                return True
        return False

    def _attach_fake_ghidra(self):
        """A fake Ghidra result + per-function files, as if the pass
        had run. The xref address is Ghidra-padded (0x0040100b) to
        exercise the address normalization."""
        from mimik import binary as binary_mod

        result = {
            "ok": True, "cancelled": False,
            "functions": [
                {"address": "0x401000", "name": "main", "size": 42,
                 "decompiled": True, "disassembly": True},
                {"address": "0x401020", "name": "helper", "size": 10,
                 "decompiled": True, "disassembly": True},
            ],
            "callgraph": {"edges": [["0x401000", "0x401020"]],
                          "names": {"0x401000": "main",
                                    "0x401020": "helper"}},
            "xrefs": {"strings": [
                {"address": "0x0040100b", "text": "HelloMIMIK",
                 "referenced_by": ["0x401000"]}]},
            "log_path": os.path.join(self.tmp, "x.log"),
            "heap": "2G", "version": "12.2",
        }
        binary_mod.update_ghidra(self.sha, result)
        funcs = os.path.join(binary_mod.analysis_dir(self.sha),
                             "functions")
        os.makedirs(funcs, exist_ok=True)
        with open(os.path.join(funcs, "401000_main.c"), "w") as f:
            f.write("int main(void) { return 42; }\n")
        with open(os.path.join(funcs, "401000_main.asm"), "w") as f:
            f.write("0x401000: ret\n")

    def _save_capture(self, host, launched=""):
        from mimik import store

        flows = [{
            "id": "1",
            "request": {"host": host, "method": "GET", "path": "/v1/x",
                        "scheme": "https", "port": 443, "headers": []},
            "response": {"status_code": 200},
        }]
        store.save_flows(flows, meta={"launched_file": launched})

    def _select_host(self, host):
        from PySide6.QtCore import Qt

        win = self.win
        win.tabs.setCurrentIndex(1)  # Hosts tab
        self.assertTrue(_wait_for(
            self.app, lambda: win._task is None))
        for i in range(win.hosts_list.count()):
            item = win.hosts_list.item(i)
            if item.data(Qt.UserRole) == host:
                win.hosts_list.setCurrentRow(i)
                self.app.processEvents()
                return True
        return False

    def test_binary_tab_lists_analyzed(self):
        self._open_binary_tab()
        texts = [self.win.binaries_list.item(i).text()
                 for i in range(self.win.binaries_list.count())]
        self.assertTrue(any("tiny.elf" in t for t in texts), texts)
        self.assertTrue(any("ghidra: no" in t for t in texts), texts)

    def test_binaries_refresh_keeps_selection(self):
        # v0.6 T5: refreshing the Binary tab keeps the selected
        # binary (and its views) instead of dropping to nothing.
        # No re-select after the refresh: the selection itself is
        # the thing under test.
        self._open_binary_tab()
        self.assertTrue(self._select_binary())
        self.assertIn("ELF", self.win.binary_summary.toPlainText())
        self.win._refresh_binaries()
        self.app.processEvents()
        current = self.win.binaries_list.currentItem()
        self.assertIsNotNone(current)
        self.assertIn("tiny.elf", current.text())
        self.assertIn("ELF", self.win.binary_summary.toPlainText())

    def test_binary_tab_shows_summary_and_strings(self):
        self._open_binary_tab()
        self.assertTrue(self._select_binary())
        summary = self.win.binary_summary.toPlainText()
        self.assertIn("ELF", summary)
        self.assertIn("printf", summary)
        self.assertIn("myfunc", summary)
        table = self.win.binary_str_table
        found = False
        for row in range(table.rowCount()):
            if table.item(row, 1).text() == "HelloMIMIK":
                found = True
                self.assertEqual(table.item(row, 0).text(), "0x100b")
                # Item 7: without Ghidra the column says so honestly.
                self.assertEqual(table.item(row, 2).text(),
                                 "needs Ghidra")
        self.assertTrue(found, "HelloMIMIK string not in the table")

    def test_binary_tab_ghidra_honest_state(self):
        self._open_binary_tab()
        self.assertTrue(self._select_binary())
        self.assertIn("Ghidra",
                      self.win.binary_decomp_state.text())
        # isHidden, not isVisible: the Decompiled page is not the
        # current inner tab, so isVisible() is False either way.
        self.assertFalse(self.win.binary_decomp_btn.isHidden())
        self.assertFalse(self.win.binary_decomp_state.isHidden())
        self.assertTrue(self.win.binary_decomp_text.isHidden())
        funcs = [self.win.binary_func_list.item(i).text()
                 for i in range(self.win.binary_func_list.count())]
        self.assertTrue(any("Ghidra pass" in t for t in funcs), funcs)

    def test_binary_tab_with_ghidra_data(self):
        self._attach_fake_ghidra()
        self._open_binary_tab()
        self.assertTrue(self._select_binary())
        # Functions list, decompiled C, disassembly.
        funcs = [self.win.binary_func_list.item(i).text()
                 for i in range(self.win.binary_func_list.count())]
        self.assertTrue(any("main" in t for t in funcs), funcs)
        for i in range(self.win.binary_func_list.count()):
            if "main" in self.win.binary_func_list.item(i).text():
                self.win.binary_func_list.setCurrentRow(i)
                break
        self.app.processEvents()
        self.assertIn("return 42",
                      self.win.binary_decomp_text.toPlainText())
        self.assertIn("ret", self.win.binary_asm_text.toPlainText())
        # Call graph: main calls helper.
        tree = self.win.binary_call_tree
        root = tree.topLevelItem(0)
        self.assertEqual(root.text(0), "main")
        self.assertEqual(root.child(0).text(0), "Calls (1)")
        self.assertEqual(root.child(0).child(0).text(0), "helper")
        self.assertEqual(root.child(1).text(0), "Called by (0)")
        # Strings: the xref now names the referencing function.
        table = self.win.binary_str_table
        for row in range(table.rowCount()):
            if table.item(row, 1).text() == "HelloMIMIK":
                self.assertEqual(table.item(row, 2).text(), "main")
                break
        else:
            self.fail("HelloMIMIK string not in the table")

    def test_hosts_link_binary_dialog(self):
        from PySide6.QtWidgets import QDialog, QPushButton

        self._save_capture("app.example.com")
        self.assertTrue(self._select_host("app.example.com"))
        self.assertIn("none linked",
                      self.win.host_binary_label.text())
        btns = [b for b in self.win.findChildren(QPushButton)
                if b.text() == "Link binary…"]
        self.assertEqual(len(btns), 1)
        with mock.patch.object(QDialog, "exec", return_value=1):
            btns[0].click()
        dlg = self.win._link_dialog
        self.assertEqual(self.win._link_list.count(), 1)
        self.assertIn("tiny.elf",
                      self.win._link_list.item(0).text())
        self.win._link_list.setCurrentRow(0)
        link = [b for b in dlg.findChildren(QPushButton)
                if b.text() == "Link"]
        self.assertEqual(len(link), 1)
        link[0].click()
        self.assertIn("tiny.elf",
                      self.win.host_binary_label.text())
        self.assertTrue(self.win.host_binary_jump.isEnabled())

    def test_hosts_linked_label_auto_match_and_jump(self):
        self._save_capture("app.example.com",
                           launched=self.elf_path)
        self.assertTrue(self._select_host("app.example.com"))
        self.assertIn("tiny.elf",
                      self.win.host_binary_label.text())
        self.assertTrue(self.win.host_binary_jump.isEnabled())
        self.win.host_binary_jump.click()
        self.app.processEvents()
        self.assertEqual(self.win.tabs.currentIndex(), 5)
        current = self.win.binaries_list.currentItem()
        self.assertIsNotNone(current)
        self.assertIn("tiny.elf", current.text())

    def test_binary_delete(self):
        from PySide6.QtWidgets import QMessageBox

        self._open_binary_tab()
        self.assertTrue(self._select_binary())
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.Yes):
            self.win.binary_delete_btn.click()
        self.app.processEvents()
        texts = [self.win.binaries_list.item(i).text()
                 for i in range(self.win.binaries_list.count())]
        self.assertEqual(len(texts), 1)
        self.assertIn("No binaries analyzed", texts[0])
        # The source file is untouched.
        self.assertTrue(os.path.isfile(self.elf_path))

    def test_analyze_file_button(self):
        from PySide6.QtWidgets import QFileDialog
        from mimik import binary as binary_mod

        binary_mod.delete_binary(self.sha)
        self._open_binary_tab()
        with mock.patch.object(
                QFileDialog, "getOpenFileName",
                return_value=(self.elf_path, "")):
            self.win.binary_analyze_btn.click()
        self.assertTrue(_wait_for(
            self.app, lambda: self.win._task is None))
        texts = [self.win.binaries_list.item(i).text()
                 for i in range(self.win.binaries_list.count())]
        self.assertTrue(any("tiny.elf" in t for t in texts), texts)


if __name__ == "__main__":
    unittest.main()
