"""MIMIK desktop GUI — a native Qt window, never a web page.

One window, seven tabs: Record | Hosts and Learn | Generate | Assets |
Clients | Doctor | Presets. Dark, plain, big readable text. Accent
violet #b48fdd only where needed.
"""
import html
import os
import subprocess
import sys
import threading
import time

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from . import assets, codegen, doctor, export, filters, launch, log, presets, reblend_bridge, sample, store, update, wine
from . import __version__
from .flows import fetch_flows
from .recorder import ProxySession
from .sources import har
from .sources import mitm as mitm_mod

ACCENT = "#b48fdd"

STYLESHEET = """
QMainWindow, QWidget { background: #17171c; color: #e8e8ea; font-size: 14px; }
QTabWidget::pane { border: 1px solid #33333a; }
QTabBar::tab { background: #222228; padding: 10px 18px; margin-right: 2px; }
QTabBar::tab:selected { background: #2c2c33; color: #b48fdd; }
QPushButton { background: #2c2c33; border: 1px solid #44444c; padding: 10px 16px; }
QPushButton:hover { border-color: #b48fdd; }
QPushButton:disabled { color: #66666e; }
QPushButton#big { font-size: 16px; padding: 14px; background: #3a2f4a; }
QListWidget, QTableWidget, QTextEdit, QComboBox {
    background: #1e1e24; border: 1px solid #33333a; }
QComboBox { padding: 6px; }
QGroupBox { border: 1px solid #33333a; margin-top: 12px; padding-top: 8px; }
QGroupBox::title { color: #b48fdd; }
"""

# Record-tab instructions, worded for the GUI (item 21): they point at
# tabs and buttons, never at terminal commands. The CLI keeps its own
# wording in cli.py (ANDROID_STEPS etc.) — a terminal user IS in a
# terminal, so `mimik hosts` is right there and wrong here.
GUI_ANDROID_STEPS = """
Android phone capture — do this once, then just reopen the app to add traffic:

  1. Phone → Settings → Wi-Fi → your network → Proxy → Manual
        Hostname: {ip}      Port: 8080
  2. Phone browser → http://mitm.it → download the Android certificate
  3. Settings → Security → Install a certificate → CA certificate → pick it
     (on newer Android: Settings → Security → Encryption & credentials →
      Install a certificate → CA certificate)
  4. open the target app and use it normally
  5. back here: press Stop, then open the Hosts and Learn tab to pick
     the app's API host, then press Generate client…

  mitmweb dashboard: http://127.0.0.1:8081   (MIMIK reads flows from here)

  Android 7+ apps only trust user-installed CAs if the app opts in — some
  apps show no traffic because of that, and pinned apps (banks, Instagram)
  show none at all. If the Hosts and Learn tab shows the app's API host,
  you're good.
"""

GUI_LINUX_APP_STEPS = """
Linux app capture — run the app through the proxy:

  Launch it yourself with the proxy env vars, for example:

      http_proxy=http://127.0.0.1:8080 https_proxy=http://127.0.0.1:8080 <app>

  Or let MIMIK launch it: type the app's command in the
  "Linux app to launch" box above, then press Start proxy.

  The app's own CA trust matters: apps using the system store need the
  mitmproxy CA installed (see README); apps with their own store may refuse.
  If the Hosts and Learn tab shows the app's API host, you're good.
"""

GUI_WEB_STEPS = """
Web app capture — no proxy needed:

  1. Browser → DevTools (F12) → Network → use the web app normally
  2. Right-click the network log → Save all as HAR → save session.har
  3. On the Generate tab, press Import HAR… and pick session.har —
     then pick the host and press Generate client.

  Or copy one request as cURL (right-click → Copy → Copy as cURL)
  and press Import cURL… on the Generate tab.
"""

GUI_WINDOWS_STEPS = """
Windows program capture (via WinLinx) — MIMIK captures, WinLinx runs:

  1. Launch the Windows program in WinLinx (it owns running Windows programs)
  2. MIMIK points its Wine prefix at this proxy and installs the mitmproxy
     CA into the prefix, so the program's HTTPS traffic can be captured
  3. Use the program normally — its API traffic lands in MIMIK
  4. Stop here restores the prefix's original proxy settings

  Some Windows programs ignore the system proxy or pin their certificates —
  those show no traffic, same as pinned phone apps.
"""

TARGETS = [
    ("android", "Android phone (same Wi-Fi)"),
    ("linux-app", "Linux app (installed)"),
    ("file", "Program from file (AppImage / archive / ISO / .deb)"),
    ("web", "Web app (HAR / cURL — no proxy)"),
    ("windows", "Windows program (via WinLinx — capture only)"),
]


def _fmt_elapsed(seconds):
    """03:24 for under an hour, 1:03:24 beyond it."""
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _empty_item(text):
    """A non-selectable placeholder row for an empty QListWidget —
    guidance instead of a blank black box. NoItemFlags means it can
    never become current, so selection handlers never see it."""
    item = QListWidgetItem(text)
    item.setFlags(Qt.NoItemFlags)
    return item


def _empty_table_row(table, text):
    """One spanned, non-selectable row for an empty QTableWidget.
    Fill paths must call table.clearSpans() before writing real rows,
    or the span survives and breaks the column layout."""
    table.clearSpans()
    table.setRowCount(1)
    table.setSpan(0, 0, 1, table.columnCount())
    item = QTableWidgetItem(text)
    item.setFlags(Qt.NoItemFlags)
    table.setItem(0, 0, item)


class RecordWorker(QThread):
    """Runs the proxy (+ launched program / Wine proxy) off the UI thread."""

    hosts_updated = Signal(list)
    status = Signal(str)
    finished = Signal()

    def __init__(self, target, file_path=None, pick=None, plan=None,
                 launch_cmd=None, deb_install=False):
        super().__init__()
        self.target = target
        self.file_path = file_path
        self.pick = pick
        # A plan prepared by the GUI (item 11): the worker launches from
        # it directly instead of preparing the file a second time (which
        # re-extracted the archive or re-mounted the ISO and leaked the
        # first mount). When no plan is passed, the worker prepares the
        # file itself, once.
        self.plan = plan
        self.launch_cmd = launch_cmd
        self.deb_install = deb_install
        self._stop = False
        self._last_rows = []
        # Live children, mirrored here so force_stop() can clean up from
        # the GUI thread after terminating a stuck worker (a terminated
        # thread never runs its finally block).
        self._session = None
        self._app_proc = None
        self._plan = None
        self._prefix = None
        self._cleanup_lock = threading.Lock()
        self._cleaned_up = False

    def request_stop(self):
        self._stop = True

    def force_stop(self, timeout_ms=10000):
        """Stop from the GUI thread (window close): ask nicely and wait,
        then terminate the thread and clean up from here. Without the
        second half, a terminated thread would strand the proxy (ports
        8080/8081), the launched program, the prefix proxy, and any
        mounted image."""
        self.request_stop()
        if not self.wait(timeout_ms):
            self.terminate()
            self.wait(2000)
            self._cleanup()

    def _cleanup(self):
        """Stop the launched program, unmount, restore the prefix, stop
        the proxy. Idempotent (guarded) — safe to call from run()'s
        finally block AND from force_stop() after a terminate()."""
        with self._cleanup_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
        launch.stop_process(self._app_proc)
        if self._plan is not None and self._plan.mount is not None:
            try:
                self._plan.mount.unmount()
            except Exception as e:
                log.log_error("record cleanup unmount", e)
            else:
                self.status.emit("unmounted the disk image")
        if self._prefix is not None:
            try:
                wine.revert_proxy(self._prefix)
                self.status.emit("prefix proxy settings restored")
            except Exception as e:
                self.status.emit(
                    f"could not restore the prefix proxy: {e}")
        if self._session is not None:
            try:
                self._session.stop()
            except Exception as e:
                log.log_error("record cleanup session stop", e)

    def _hosts_updated(self, rows):
        self._last_rows = rows
        self.hosts_updated.emit(rows)

    def _wait(self, seconds):
        waited = 0.0
        while waited < seconds and not self._stop:
            self.msleep(200)
            waited += 0.2

    def run(self):
        session = None
        app_proc = None
        plan = None
        prefix = None
        try:
            meta = {"target": self.target}
            if self.target == "windows":
                ok, msg = wine.check_ready()
                if not ok:
                    self.status.emit(msg)
                    return
                prefix = wine.wine_prefix()
                self._prefix = prefix
                meta["prefix"] = prefix
            elif self.target == "file":
                if self.plan is not None:
                    plan = self.plan
                else:
                    try:
                        plan = launch.prepare_file(
                            self.file_path, pick=self.pick,
                            deb_install=self.deb_install)
                    except launch.LaunchError as e:
                        self.status.emit(str(e))
                        return
                self._plan = plan
                if plan.needs_pick:
                    self.status.emit("pick the program to run first")
                    return
                meta.update({"launched_file": self.file_path,
                             "kind": plan.kind})
                self.status.emit(f"will run: {plan.command_display}")
                # Assets inside the program's own files: copy them out
                # now, while the extraction / mount is live.
                if plan.work_dir:
                    try:
                        program = os.path.basename(
                            launch.run_dir_for(self.file_path))
                        gdir, manifest = assets.scan_program_tree(
                            plan.work_dir, program,
                            cancelled=lambda: self._stop)
                        if gdir:
                            note = manifest.get("scan_note") or ""
                            self.status.emit(
                                f"found {len(manifest['assets'])} asset "
                                "file(s) in the program — saved for the "
                                "asset pack"
                                + (f" ({note})" if note else ""))
                    except Exception as e:
                        self.status.emit(f"asset scan skipped: {e}")

            session = ProxySession(
                meta=meta,
                on_update=self._hosts_updated,
            )
            self._session = session
            try:
                session.start()
            except mitm_mod.MitmError as e:
                self.status.emit(str(e))
                return
            self.status.emit("proxy up — capturing")

            if self.target == "windows":
                self.status.emit("pointing the Wine prefix at the proxy…")
                try:
                    wine.apply_proxy(prefix)
                    self.status.emit("prefix now points at the proxy")
                except Exception as e:
                    self.status.emit(f"could not set the prefix proxy: {e}")
                try:
                    wine.install_ca(prefix=prefix)
                    self.status.emit("mitmproxy CA installed in the prefix")
                except Exception as e:
                    self.status.emit(
                        f"could not install the CA in the prefix: {e}")
                self.status.emit(
                    "now launch the Windows program in WinLinx")
            elif plan is not None:
                try:
                    app_proc = launch.launch_process(plan)
                    self._app_proc = app_proc
                    self.status.emit(f"launched: {plan.command_display}")
                except Exception as e:
                    self.status.emit(f"could not launch the program: {e}")
            elif self.target == "linux-app" and self.launch_cmd:
                import shlex

                env = dict(os.environ)
                env.update(launch.proxy_env())
                try:
                    app_proc = launch.launch_checked(
                        shlex.split(self.launch_cmd), env=env,
                        label=self.launch_cmd)
                    self._app_proc = app_proc
                    self.status.emit(f"launched: {self.launch_cmd}")
                except Exception as e:
                    self.status.emit(f"could not launch: {e}")

            while not self._stop:
                try:
                    session.poll()
                except Exception as e:
                    self.status.emit(f"proxy read failed: {e}")
                self._wait(5)
        finally:
            self._cleanup()
            # Presets: refresh when auto-learn is on, and say which
            # saved presets apply to the hosts just captured — app
            # presets, and pattern presets the traffic matched.
            try:
                for host, refreshed, has, matched in presets.after_record(
                        [h for h, _n in self._last_rows]):
                    if refreshed:
                        self.status.emit(f"Preset refreshed for {host}")
                    if has:
                        self.status.emit(f"Used the saved preset for {host}")
                    if matched:
                        self.status.emit(
                            f"Matched patterns: {', '.join(matched)}")
            except Exception as e:
                log.log_error("record-stop after_record", e)
            self.status.emit("capture saved")
            self.finished.emit()


class TaskWorker(QThread):
    """Runs one callable off the UI thread.

    fn(worker) runs in the thread; result carries (ok, value) — ok False
    means fn raised, and value is the exception. A long fn should check
    worker.cancelled in its loop and return early; a blocking call (an
    Ollama POST, a subprocess) can't be interrupted, but its result is
    discarded when it returns, so Cancel always leaves the UI free.
    Progress text goes through the progress signal.
    """

    result = Signal(bool, object)
    progress = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self._cancel = False

    def request_cancel(self):
        self._cancel = True

    @property
    def cancelled(self):
        return self._cancel

    def run(self):
        try:
            value = self._fn(self)
        except Exception as e:
            self.result.emit(False, e)
        else:
            self.result.emit(True, value)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"MIMIK {__version__} — any app, as a Python library")
        self.setStyleSheet(STYLESHEET)  # dark theme, even without main()
        self.resize(900, 650)  # 620 + room for the status bar (item 7)
        self.worker = None
        self.plan = None
        self.file_path = None
        self.har_path = None
        self.curl_path = None
        self.gen_out = None
        self.pack_dir = None
        self.pack_scene = None
        self.pack_reblend = None
        # Background-task state (item 4): one task at a time, cached
        # flows, tabs that load their data on first show.
        self._flows_cache = None
        self._task = None
        self._task_on_done = None
        self._task_disable = []
        self._pending_host = None
        self._loaded = set()
        self._close_pending = False
        # Hosts → Generate jump (item 10): the host to pick once the
        # Generate tab's host combo has loaded.
        self._gen_jump_host = None
        # The latest asset preview dialog (item 10).
        self._preview_dialog = None

        tabs = QTabWidget()
        tabs.addTab(self._record_tab(), "Record")
        tabs.addTab(self._hosts_tab(), "Hosts and Learn")
        tabs.addTab(self._generate_tab(), "Generate")
        tabs.addTab(self._assets_tab(), "Assets")
        tabs.addTab(self._clients_tab(), "Clients")
        tabs.addTab(self._doctor_tab(), "Doctor")
        tabs.addTab(self._presets_tab(), "Presets")
        self.tabs = tabs
        tabs.currentChanged.connect(self._tab_shown)
        self._build_menus()

        # Progress row: visible only while a background task runs.
        self.progress_bar = QWidget()
        prow = QHBoxLayout(self.progress_bar)
        prow.setContentsMargins(6, 2, 6, 2)
        self.progress_label = QLabel("")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel_task)
        prow.addWidget(self.progress_label, 1)
        prow.addWidget(self.cancel_btn)
        self.progress_bar.setVisible(False)

        central = QWidget()
        clay = QVBoxLayout(central)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.addWidget(tabs)
        clay.addWidget(self.progress_bar)
        self.setCentralWidget(central)

        # Status bar (item 7): proxy state + elapsed time + flow count,
        # visible on every tab. The timer ticks only while recording.
        self._record_started = None
        self._flow_count = 0
        self._host_count = 0
        self._saved_flows = 0
        bar = self.statusBar()
        self.status_proxy = QLabel()
        bar.addWidget(self.status_proxy, 1)
        self.status_flows = QLabel()
        bar.addPermanentWidget(self.status_flows)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._update_status_bar)
        self._refresh_saved_flows()
        self._update_status_bar()

    def _refresh_saved_flows(self):
        """Re-count saved flows for the idle status bar. Runs at
        startup, record-stop, host delete, and sample load — the only
        points the total changes. A corrupt store reads as 0, never a
        crash."""
        try:
            self._saved_flows = sum(
                n for _h, n, _t in store.list_captures())
        except Exception:
            self._saved_flows = 0

    def _update_status_bar(self):
        """Proxy state + elapsed time + flow count. Recording shows
        elapsed time since record start and live flows/hosts; idle
        shows the saved-flows total."""
        if self.worker is not None and self._record_started is not None:
            elapsed = time.monotonic() - self._record_started
            self.status_proxy.setText(
                f"● Recording — {_fmt_elapsed(elapsed)}")
            flows = "flow" if self._flow_count == 1 else "flows"
            hosts = "host" if self._host_count == 1 else "hosts"
            self.status_flows.setText(
                f"{self._flow_count} {flows} · {self._host_count} {hosts}")
        else:
            self.status_proxy.setText("Proxy stopped")
            flows = "flow" if self._saved_flows == 1 else "flows"
            self.status_flows.setText(
                f"{self._saved_flows} saved {flows}")

    # ---- close + startup safety ---------------------------------------------

    def _confirm_stop_recording(self):
        """Ask whether a running recording should stop so the window can
        close. Returns True for "Stop and close"."""
        box = QMessageBox(self)
        box.setWindowTitle("MIMIK")
        box.setText("A recording is in progress.")
        box.setInformativeText(
            "Stop the recording and close, or keep recording?")
        stop_btn = box.addButton("Stop and close",
                                 QMessageBox.AcceptRole)
        box.addButton("Keep recording", QMessageBox.RejectRole)
        box.setDefaultButton(stop_btn)
        box.exec()
        return box.clickedButton() is stop_btn

    def closeEvent(self, event):
        """Never strand a recording: closing mid-record stops the worker
        (proxy, launched program, prefix, mount) before the window goes
        away. Before v0.3 there was no closeEvent at all — closing the
        window left mitmweb orphaned on ports 8080/8081.

        A background task must not outlive the window either — its
        QThread would be deleted while still running. Cancel it and wait
        briefly (fast tasks settle at once); a blocking task defers the
        close until it ends (_task_finished closes then).
        """
        if self._task is not None:
            self._cancel_task()
            task = self._task
            if task is not None and not task.wait(2000):
                self._close_pending = True
                event.ignore()
                return
            # Thread done — flush its queued result through
            # _task_finished so the task is fully settled first.
            QApplication.instance().processEvents()
            if self._task is not None:
                # The retry started another task — defer the close.
                self._close_pending = True
                event.ignore()
                return
        worker = self.worker
        if worker is not None and worker.isRunning():
            if not self._confirm_stop_recording():
                event.ignore()
                return
            worker.force_stop()
        event.accept()

    def check_stale_wine_state(self):
        """A saved Wine proxy state means a capture ended without
        restoring the prefix — warn and offer to restore it now."""
        if not wine.has_saved_state():
            return
        answer = QMessageBox.question(
            self, "MIMIK — Wine proxy settings",
            "MIMIK saved your Wine proxy settings during a capture that "
            "didn't finish cleanly, so your Wine prefix may still be "
            "pointing at MIMIK's proxy.\n\n"
            "Restore your original proxy settings now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        try:
            wine.revert_proxy()
        except Exception as e:
            QMessageBox.warning(
                self, "MIMIK",
                f"Could not restore the Wine proxy settings:\n{e}")
            return
        QMessageBox.information(
            self, "MIMIK", "Wine proxy settings restored.")

    # ---- background tasks + lazy tabs ----------------------------------------

    _TAB_KEYS = ("record", "hosts", "generate", "assets", "clients",
                 "doctor", "presets")

    def _run_task(self, label, fn, on_done, disable=()):
        """Run fn(worker) off the UI thread; on_done(ok, value) runs back
        on the UI thread when it finishes. One task at a time — returns
        False if a task is already running. Buttons in `disable` are
        greyed out until the task finishes (or is cancelled)."""
        if self._task is not None:
            return False
        worker = TaskWorker(fn)
        self._task = worker
        self._task_on_done = on_done
        self._task_disable = list(disable)
        for button in self._task_disable:
            button.setEnabled(False)
        self.progress_label.setText(label)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")
        self.progress_bar.setVisible(True)
        worker.progress.connect(self.progress_label.setText)
        worker.result.connect(self._task_finished)
        worker.start()
        return True

    def _cancel_task(self):
        """Cancel the running task. A fn that checks worker.cancelled
        stops early; a blocking call runs out, but its result is
        discarded (see _task_finished), so Cancel always frees the UI."""
        if self._task is not None:
            self._task.request_cancel()
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling…")
            self.progress_label.setText(
                self.progress_label.text() + " — cancelling…")

    def _task_finished(self, ok, value):
        worker = self._task
        self._task = None
        self.progress_bar.setVisible(False)
        for button in self._task_disable:
            button.setEnabled(True)
        self._task_disable = []
        on_done = self._task_on_done
        self._task_on_done = None
        if worker is not None:
            # Join before dropping the reference: the result signal is
            # emitted at the end of run(), so the thread may still be
            # winding down when this slot runs. Never deleteLater() a
            # worker here — the Python wrapper's GC would free the C++
            # object while the deleteLater event is still pending, a
            # double-delete that corrupts the heap (seen as a
            # malloc_consolidate abort at teardown).
            worker.wait()
        cancelled = worker is not None and worker.cancelled
        if not cancelled and on_done is not None:
            on_done(ok, value)
        if self._close_pending:
            # The close was deferred until this task ended — close now
            # instead of starting any retry work.
            self._close_pending = False
            self.close()
            return
        if cancelled:
            return  # result discarded — the user cancelled
        # A tab whose first load was refused while busy, or a host
        # clicked while a task ran, gets its turn now.
        if self._task is None:
            self._tab_shown(self.tabs.currentIndex())
            if self._pending_host is not None:
                host = self._pending_host
                self._pending_host = None
                self._start_host_task(host)

    # ---- menu bar + shortcuts (item 30) ---------------------------------------

    def _build_menus(self):
        """File / View / Help menus + shortcuts. All actions connect
        to bound methods — no lambdas on signals (heap rule)."""
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        import_har = QAction("Import HAR…", self)
        import_har.triggered.connect(self._import_har)
        file_menu.addAction(import_har)
        import_curl = QAction("Import cURL…", self)
        import_curl.triggered.connect(self._import_curl)
        file_menu.addAction(import_curl)
        file_menu.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)
        self._quit_action = quit_act

        view_menu = menubar.addMenu("View")
        self._tab_actions = []
        for index in range(self.tabs.count()):
            act = QAction(self.tabs.tabText(index), self)
            act.setShortcut(f"Ctrl+{index + 1}")
            act.setData(index)
            act.triggered.connect(self._menu_go_to_tab)
            view_menu.addAction(act)
            self._tab_actions.append(act)
        view_menu.addSeparator()
        refresh_act = QAction("Refresh", self)
        refresh_act.setShortcut("F5")
        refresh_act.triggered.connect(self._menu_refresh_current_tab)
        view_menu.addAction(refresh_act)
        self._refresh_action = refresh_act

        help_menu = menubar.addMenu("Help")
        about_act = QAction("About MIMIK", self)
        about_act.triggered.connect(self._show_about)
        help_menu.addAction(about_act)
        self._about_action = about_act
        log_act = QAction("View log", self)
        log_act.triggered.connect(self._show_log)
        help_menu.addAction(log_act)
        update_act = QAction("Check for updates…", self)
        update_act.triggered.connect(self._check_for_updates)
        help_menu.addAction(update_act)
        self._update_action = update_act

    def _menu_go_to_tab(self):
        """View menu tab jump — the action carries its tab index."""
        action = self.sender()
        if action is None:
            return
        index = action.data()
        if isinstance(index, int) and 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)

    def _menu_refresh_current_tab(self):
        """F5: refresh whichever tab is showing."""
        key = self._TAB_KEYS[self.tabs.currentIndex()]
        if key == "hosts":
            self._refresh_hosts()
        elif key == "generate":
            self._refresh_gen_hosts()
        elif key == "assets":
            self._refresh_reblend_status()
            self._refresh_asset_groups()
            self._asset_group_selected()
        elif key == "clients":
            self._refresh_clients()
        elif key == "doctor":
            self._run_doctor()
        elif key == "presets":
            self._refresh_presets()
        # record: nothing to refresh — the tab is live by construction.
        self._refresh_saved_flows()
        self._update_status_bar()

    def _tab_shown(self, index):
        """Lazy-load a tab's data on first show — startup fetches
        nothing for tabs the user never opens."""
        if index < 0 or index >= len(self._TAB_KEYS):
            return
        key = self._TAB_KEYS[index]
        if key in self._loaded or key == "record":
            return
        if self._load_tab(key):
            self._loaded.add(key)

    def _load_tab(self, key):
        """Load one tab's data. Returns False if a background task is
        running and the load must wait (it retries when the task ends)."""
        if key == "hosts":
            return self._refresh_hosts(refresh=False)
        if key == "generate":
            return self._refresh_gen_hosts(refresh=False)
        if key == "assets":
            self._refresh_reblend_status()
            self._refresh_asset_groups()
            return True
        if key == "clients":
            self._refresh_clients()
            return True
        if key == "doctor":
            return self._run_doctor()
        if key == "presets":
            self._refresh_presets()
            return True
        return True

    # ---- Record tab ----------------------------------------------------------

    def _record_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        lay.addWidget(QLabel(
            "Capture your own app's traffic, then turn it into a Python "
            "library. Own accounts only."))
        self.start_here = QLabel(
            "Start here: 1 Record an app → 2 pick a host on Hosts and "
            "Learn → 3 Generate a client.")
        self.start_here.setWordWrap(True)
        lay.addWidget(self.start_here)
        row = QHBoxLayout()
        row.addWidget(QLabel("Capture target:"))
        self.target_combo = QComboBox()
        for key, label in TARGETS:
            self.target_combo.addItem(label, key)
        self.target_combo.currentIndexChanged.connect(self._target_changed)
        row.addWidget(self.target_combo, 1)
        lay.addLayout(row)

        # Capture filter (item 26): per-record control over the
        # telemetry/analytics exclusion list.
        frow = QHBoxLayout()
        self.filter_check = QCheckBox(
            "Exclude telemetry + analytics hosts")
        self.filter_check.setChecked(presets.filtering_enabled())
        self.filter_check.stateChanged.connect(self._filter_toggled)
        frow.addWidget(self.filter_check, 1)
        manage_btn = QPushButton("Manage excluded hosts…")
        manage_btn.clicked.connect(self._show_excluded_hosts)
        frow.addWidget(manage_btn)
        lay.addLayout(frow)

        self.lan_label = QLabel(f"LAN IP for the phone proxy: {doctor.lan_ip()}:8080")
        lay.addWidget(self.lan_label)

        # File picker (file target only)
        self.file_box = QGroupBox("Program from file")
        flay = QVBoxLayout(self.file_box)
        frow = QHBoxLayout()
        self.file_label = QLabel("no file chosen")
        pick_btn = QPushButton("Choose file…")
        pick_btn.clicked.connect(self._choose_file)
        frow.addWidget(self.file_label, 1)
        frow.addWidget(pick_btn)
        flay.addLayout(frow)
        self.candidate_list = QListWidget()
        self.candidate_list.setMaximumHeight(90)
        self.candidate_list.currentRowChanged.connect(self._candidate_changed)
        self.candidate_list.addItem(_empty_item(
            "Choose a file to see its launch options."))
        flay.addWidget(self.candidate_list)
        self.cmd_label = QLabel("")
        self.cmd_label.setWordWrap(True)
        flay.addWidget(self.cmd_label)
        self.deb_install_check = QPushButton("Install .deb with apt instead")
        self.deb_install_check.setCheckable(True)
        flay.addWidget(self.deb_install_check)
        clear_btn = QPushButton("Clear temp extractions (~/.cache/mimik/runs)")
        clear_btn.clicked.connect(self._clear_runs)
        flay.addWidget(clear_btn)
        lay.addWidget(self.file_box)

        # Linux-app launch command — a one-line shell command, so a
        # QLineEdit (item 32d), not the QTextEdit it used to be.
        self.app_box = QGroupBox("Linux app to launch (optional)")
        alay = QHBoxLayout(self.app_box)
        self.launch_edit = QLineEdit()
        self.launch_edit.setPlaceholderText("e.g. firefox   (leave blank to "
                                            "launch it yourself)")
        alay.addWidget(self.launch_edit, 1)
        lay.addWidget(self.app_box)

        self.steps = QTextEdit()
        self.steps.setReadOnly(True)
        lay.addWidget(self.steps, 1)

        self.host_list = QListWidget()
        self.host_list.setMaximumHeight(110)
        self.host_list.addItem(_empty_item(
            "No live hosts yet — start the proxy and use the app."))
        lay.addWidget(QLabel("Live hosts while recording:"))
        lay.addWidget(self.host_list)

        brow = QHBoxLayout()
        self.start_btn = QPushButton("Start proxy")
        self.start_btn.setObjectName("big")
        self.start_btn.clicked.connect(self._start_recording)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("big")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_recording)
        brow.addWidget(self.start_btn, 1)
        brow.addWidget(self.stop_btn, 1)
        lay.addLayout(brow)

        self._target_changed()
        return w

    def _target_changed(self):
        key = self.target_combo.currentData()
        self.file_box.setVisible(key == "file")
        self.app_box.setVisible(key == "linux-app")

        if key == "android":
            self.steps.setText(GUI_ANDROID_STEPS.format(ip=doctor.lan_ip()))
            self.start_btn.setEnabled(True)
            self.start_btn.setText("Start proxy")
        elif key == "linux-app":
            self.steps.setText(GUI_LINUX_APP_STEPS)
            self.start_btn.setEnabled(True)
            self.start_btn.setText("Start proxy")
        elif key == "file":
            self.steps.setText(
                "Pick a program file: AppImage, executable, script, archive\n"
                "(.zip / .tar.gz), disk image (.iso / .img), or .deb.\n\n"
                "Archives are extracted to ~/.cache/mimik/runs/ and the main\n"
                "program is auto-picked (change it in the list). Disk images\n"
                "are mounted READ-ONLY and you pick the program inside.\n\n"
                "Windows .exe/.msi are WinLinx's job — MIMIK never runs them.\n"
                "To capture one, run it in WinLinx and use the Windows target.")
            self.start_btn.setEnabled(self.plan is not None
                                      and self.plan.command is not None)
            self.start_btn.setText("Start proxy + launch program")
        elif key == "web":
            self.steps.setText(GUI_WEB_STEPS)
            self.start_btn.setEnabled(False)
            self.start_btn.setText("No proxy needed — import a HAR instead")
        elif key == "windows":
            self.steps.setText(GUI_WINDOWS_STEPS + "\nCommands MIMIK will run:\n"
                "  wine regedit /s <proxy.reg>\n"
                "  wine certutil -addstore -f -user Root "
                "~/.mitmproxy/mitmproxy-ca-cert.pem\n"
                "Stop restores the prefix's original proxy settings.")
            self.start_btn.setEnabled(True)
            self.start_btn.setText("Start proxy (configure prefix)")

    def _filter_toggled(self, _state=None):
        """The Record tab's filter checkbox — a persistent setting,
        like auto-learn."""
        presets.set_filtering_enabled(self.filter_check.isChecked())

    def _show_excluded_hosts(self):
        """The exclusion-list editor: the effective list, add/remove,
        restore defaults (item 26)."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Excluded hosts")
        dlg.resize(500, 400)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "Hosts never captured — their traffic is dropped before\n"
            "it reaches the store or the host list. Subdomains of a\n"
            "listed host are excluded too."))
        lst = QListWidget()
        self._excluded_list = lst
        lay.addWidget(lst, 1)
        add_row = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText(
            "host to exclude, e.g. telemetry.example.com")
        self._excluded_edit = edit
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_excluded_host)
        add_row.addWidget(edit, 1)
        add_row.addWidget(add_btn)
        lay.addLayout(add_row)
        row = QHBoxLayout()
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_excluded_host)
        restore_btn = QPushButton("Restore defaults")
        restore_btn.clicked.connect(self._restore_excluded_defaults)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        row.addWidget(remove_btn)
        row.addWidget(restore_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        lay.addLayout(row)
        self._refresh_excluded_list()
        self._excluded_dialog = dlg
        dlg.exec()

    def _refresh_excluded_list(self):
        self._excluded_list.clear()
        for host in presets.excluded_hosts():
            self._excluded_list.addItem(host)

    def _add_excluded_host(self, _checked=False):
        host = self._excluded_edit.text().strip().lower()
        if not host:
            return
        hosts = presets.excluded_hosts()
        if host not in hosts:
            hosts.append(host)
            presets.set_excluded_hosts(hosts)
        self._excluded_edit.clear()
        self._refresh_excluded_list()

    def _remove_excluded_host(self, _checked=False):
        item = self._excluded_list.currentItem()
        if item is None:
            return
        hosts = [h for h in presets.excluded_hosts()
                 if h != item.text()]
        presets.set_excluded_hosts(hosts)
        self._refresh_excluded_list()

    def _restore_excluded_defaults(self, _checked=False):
        from . import filters

        presets.set_excluded_hosts(list(filters.DEFAULT_EXCLUDED))
        self._refresh_excluded_list()

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a program file", os.path.expanduser("~"),
            "Programs (*.AppImage *.iso *.img *.deb *.zip *.tar.gz *.tgz "
            "*.sh *.py);;All files (*)")
        if not path:
            return
        self.file_path = path
        self.file_label.setText(path)
        self._prepare_file()

    def _prepare_file(self, pick=None):
        self.candidate_list.clear()
        self.plan = None
        try:
            plan = launch.prepare_file(
                self.file_path, pick=pick,
                deb_install=self.deb_install_check.isChecked())
        except launch.LaunchError as e:
            self.cmd_label.setText(str(e))
            self.start_btn.setEnabled(False)
            return
        self.plan = plan
        for c in plan.candidates:
            self.candidate_list.addItem(c)
        if plan.command:
            for i, c in enumerate(plan.candidates):
                if plan.command[-1] == c or plan.command == [c]:
                    self.candidate_list.setCurrentRow(i)
                    break
            self.cmd_label.setText(f"will run:  {plan.command_display}\n"
                                   f"{plan.message or ''}")
            self.start_btn.setEnabled(True)
        else:
            self.cmd_label.setText("pick the program to run from the list")
            self.start_btn.setEnabled(False)

    def _candidate_changed(self, row):
        if self.plan is None or row < 0:
            return
        exe = self.plan.candidates[row]
        self.plan.command = launch.build_launch_command(exe)
        self.cmd_label.setText(f"will run:  {self.plan.command_display}")
        self.start_btn.setEnabled(True)

    def _clear_runs(self):
        n = launch.clear_runs()
        QMessageBox.information(self, "MIMIK", f"cleared {n} temp extraction(s)")

    def _start_recording(self):
        key = self.target_combo.currentData()
        plan = None
        if key == "file":
            plan = self.plan
            if plan is not None and plan.is_stale:
                # A previous record unmounted the image (or the
                # extraction was cleared) — prepare once more, with the
                # user's pick, instead of launching from dead files.
                row = self.candidate_list.currentRow()
                pick = plan.candidates[row] if row >= 0 else None
                self._prepare_file(pick=pick)
                plan = self.plan
                if plan is None or not plan.command:
                    return  # prepare failed, or still needs a pick
        self.worker = RecordWorker(
            key, file_path=self.file_path, plan=plan,
            launch_cmd=self.launch_edit.text().strip() or None,
            deb_install=self.deb_install_check.isChecked())
        self.worker.hosts_updated.connect(self._show_hosts)
        self.worker.status.connect(self._append_step)
        self.worker.finished.connect(self._recording_finished)
        self.worker.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.target_combo.setEnabled(False)
        self.host_list.clear()
        self._record_started = time.monotonic()
        self._flow_count = 0
        self._host_count = 0
        self._status_timer.start()
        self._update_status_bar()

    def _append_step(self, text):
        """RecordWorker status line -> Record tab log. A bound method,
        not a lambda: lambdas connected to Qt signals corrupt the heap
        at teardown (proven: ~50% malloc abort with two lambda slots,
        0/20 without them). Never connect a lambda/closure/partial
        to a Qt signal in this file."""
        self.steps.append(text)

    def _show_hosts(self, rows):
        self.host_list.clear()
        self._flow_count = sum(n for _h, n in rows)
        self._host_count = len(rows)
        self._update_status_bar()
        if not rows:
            self.host_list.addItem(_empty_item(
                "Listening — use the app and its hosts appear here."))
            return
        for host, n in rows:
            self.host_list.addItem(f"{n:>6}  {host}")

    def _stop_recording(self):
        if self.worker is not None:
            self.worker.request_stop()
            self.stop_btn.setEnabled(False)
            self.steps.append("stopping…")

    def _recording_finished(self):
        self.worker = None
        self._record_started = None
        self._status_timer.stop()
        self._refresh_saved_flows()
        self._update_status_bar()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.target_combo.setEnabled(True)
        self._target_changed()
        self._invalidate_flows()
        # Flow-backed tabs that already loaded go stale — reload the one
        # on screen now; the others reload when next shown.
        for key in ("hosts", "generate"):
            self._loaded.discard(key)
        self._tab_shown(self.tabs.currentIndex())

    # ---- Hosts & Learn tab ---------------------------------------------------

    def _hosts_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Captured hosts (live proxy, else saved captures)."))
        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_hosts_clicked)
        row.addWidget(refresh)
        delete_btn = QPushButton("Delete selected host…")
        delete_btn.clicked.connect(self._delete_host)
        row.addWidget(delete_btn)
        gen_btn = QPushButton("Generate client…")
        gen_btn.clicked.connect(self._generate_for_host)
        row.addWidget(gen_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.hosts_list = QListWidget()
        self.hosts_list.currentItemChanged.connect(self._host_selected)
        self.hosts_list.addItem(_empty_item(
            "No captures yet — record an app, or import a HAR on the "
            "Generate tab."))
        lay.addWidget(self.hosts_list, 1)
        lay.addWidget(QLabel("Endpoints for the selected host:"))
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Filter:"))
        self.ep_filter = QLineEdit()
        self.ep_filter.setPlaceholderText("filter endpoints…")
        self.ep_filter.textChanged.connect(self._filter_ep_table)
        frow.addWidget(self.ep_filter, 1)
        self.ep_filter_info = QLabel("")
        frow.addWidget(self.ep_filter_info)
        lay.addLayout(frow)
        self.ep_table = QTableWidget(0, 3)
        self.ep_table.setHorizontalHeaderLabels(["Method", "Path", "Status"])
        self.ep_table.horizontalHeader().setStretchLastSection(True)
        _empty_table_row(self.ep_table,
                         "Select a host to see its endpoints.")
        lay.addWidget(self.ep_table, 2)
        # No fetch here — the tab loads on first show (_tab_shown).
        return w

    def _invalidate_flows(self):
        """Drop the cached flows — call on Refresh and record-stop."""
        self._flows_cache = None

    def _flows(self, refresh=False):
        """(mitm, flows, source) — live proxy first, saved captures if
        the proxy is down. Cached: repeated reads (host-select, Generate,
        tab switches) reuse the last fetch instead of re-pulling every
        flow and re-saving; Refresh buttons and record-stop invalidate
        the cache explicitly."""
        if not refresh and self._flows_cache is not None:
            return self._flows_cache
        # The fetch itself lives in mimik.flows (item 32b) — the GUI
        # only adds the cache on top.
        result = fetch_flows(where="gui _flows")
        self._flows_cache = result
        return result

    def _refresh_hosts_clicked(self, _checked=False):
        """Refresh button -> full refresh (clicked's bool must not leak
        into _refresh_hosts' refresh flag)."""
        self._refresh_hosts()

    def _refresh_hosts(self, refresh=True):
        """Reload the host list — the flow fetch runs off the UI thread.
        Returns False if a task is already running (the load retries
        when it ends)."""
        def work(worker):
            _m, flows, src = self._flows(refresh=refresh)
            rows = []
            for host, n in mitm_mod.hosts(flows):
                stats = store.capture_stats(host)
                rows.append((host, n,
                             src == "saved" and stats["truncated"],
                             stats["bytes"]))
            return rows

        def done(ok, rows):
            if not ok:
                return
            self.hosts_list.clear()
            if not rows:
                self.hosts_list.addItem(_empty_item(
                    "No captures yet — record an app, or import a HAR "
                    "on the Generate tab."))
                return
            for host, n, truncated, size in rows:
                text = f"{n:>6}  {host}"
                if size:
                    text += f"  ({store.format_size(size)})"
                if truncated:
                    text += ("  (truncated — showing newest %d)"
                             % store.MAX_FLOWS_PER_HOST)
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, host)
                self.hosts_list.addItem(item)

        return self._run_task("loading hosts…", work, done)

    def _delete_host(self):
        """Delete the selected host's saved captures (after asking).
        The flow cache is dropped and both host lists reload, so no
        stale reference to the deleted host remains."""
        item = self.hosts_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "MIMIK", "select a host first")
            return
        host = item.data(Qt.UserRole)
        stats = store.capture_stats(host)
        answer = QMessageBox.question(
            self, "MIMIK",
            f"Delete all {stats['flows']} saved flow(s) for {host} "
            f"({stats['files']} save file(s), "
            f"{store.format_size(stats['bytes'])})?\n\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        store.delete_host(host)
        self._invalidate_flows()
        self._refresh_saved_flows()
        self._update_status_bar()
        self.ep_table.setRowCount(0)
        # Discard both host lists so they reload even if a task is
        # running (the retry after it ends re-runs the load).
        self._loaded.discard("hosts")
        self._loaded.discard("generate")
        self._refresh_hosts()

    def _generate_for_host(self):
        """Jump to the Generate tab with the selected host picked, so
        the user doesn't have to re-pick it there."""
        item = self.hosts_list.currentItem()
        if item is None or not item.data(Qt.UserRole):
            QMessageBox.warning(self, "MIMIK", "select a host first")
            return
        host = item.data(Qt.UserRole)
        self._gen_jump_host = host
        self.tabs.setCurrentIndex(2)  # Generate tab
        if "generate" in self._loaded:
            idx = self.gen_host.findText(host)
            if idx >= 0:
                self.gen_host.setCurrentIndex(idx)
                self._gen_jump_host = None

    def _host_selected(self, current, _prev):
        self.ep_table.setRowCount(0)
        if current is None:
            return
        self._pending_host = current.data(Qt.UserRole)
        if self._task is None:
            host = self._pending_host
            self._pending_host = None
            self._start_host_task(host)

    def _start_host_task(self, host):
        """Endpoints for one host, off the UI thread (building them from
        1,200 saved flows took ~0.9 s — long enough to freeze the UI)."""
        def work(worker):
            m, flows, source = self._flows()
            if source == "live":
                try:
                    return mitm_mod.endpoints(m, flows, host)
                except Exception as e:
                    log.log_error("gui live endpoints", e)
            return mitm_mod.endpoints_from_saved(flows, host)

        def done(ok, eps):
            if not ok:
                return
            # Only fill the table if this host is still selected.
            current = self.hosts_list.currentItem()
            if current is None or current.data(Qt.UserRole) != host:
                return
            self.ep_table.clearSpans()
            if not eps:
                _empty_table_row(self.ep_table,
                                 "No endpoints for this host.")
                return
            self.ep_table.setRowCount(len(eps))
            for i, e in enumerate(eps):
                self.ep_table.setItem(i, 0, QTableWidgetItem(e["method"]))
                self.ep_table.setItem(i, 1, QTableWidgetItem(e["path"]))
                self.ep_table.setItem(
                    i, 2, QTableWidgetItem(str(e["status"])))
            self._filter_ep_table()

        return self._run_task(f"loading endpoints for {host}…", work, done)

    # ---- list filtering (item 22) -------------------------------------------

    def _apply_table_filter(self, table, query):
        """Hide table rows whose cells don't contain the query
        (case-insensitive substring). Empty-state rows (spanned) stay
        visible so the list never goes silently blank. Returns
        (visible, total) over real rows."""
        q = query.strip().lower()
        visible = total = 0
        for r in range(table.rowCount()):
            if table.columnSpan(r, 0) > 1:
                table.setRowHidden(r, False)
                continue
            total += 1
            text = " ".join(
                (table.item(r, c).text() if table.item(r, c) else "")
                for c in range(table.columnCount())).lower()
            hidden = bool(q) and q not in text
            table.setRowHidden(r, hidden)
            if not hidden:
                visible += 1
        return visible, total

    def _apply_list_filter(self, widget, query):
        """Hide list items whose text doesn't contain the query.
        Empty-state items (no UserRole data) stay visible. Returns
        (visible, total) over real items."""
        q = query.strip().lower()
        visible = total = 0
        for i in range(widget.count()):
            item = widget.item(i)
            if item.data(Qt.UserRole) is None:
                item.setHidden(False)
                continue
            total += 1
            hidden = bool(q) and q not in item.text().lower()
            item.setHidden(hidden)
            if not hidden:
                visible += 1
        return visible, total

    def _filter_info(self, label, query, visible, total):
        """The "N of M" note beside a filter box — shown only while a
        filter is active, so an empty box leaves a clean row."""
        if query.strip():
            label.setText(f"{visible} of {total}")
        else:
            label.setText("")

    def _filter_ep_table(self, _text=None):
        visible, total = self._apply_table_filter(
            self.ep_table, self.ep_filter.text())
        self._filter_info(self.ep_filter_info, self.ep_filter.text(),
                          visible, total)

    def _filter_asset_table(self, _text=None):
        visible, total = self._apply_table_filter(
            self.asset_table, self.asset_filter.text())
        self._filter_info(self.asset_filter_info,
                          self.asset_filter.text(), visible, total)

    def _filter_clients_list(self, _text=None):
        visible, total = self._apply_list_filter(
            self.clients_list, self.clients_filter.text())
        self._filter_info(self.clients_filter_info,
                          self.clients_filter.text(), visible, total)

    # ---- Generate tab ---------------------------------------------------------

    def _generate_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Generate a Python client from captured traffic — "
            "deterministic and free, no LLM."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Host:"))
        self.gen_host = QComboBox()
        self.gen_host.setEditable(True)
        # Set it on the line edit, not the combo: QComboBox's own
        # placeholderText stores the value but never forwards it to
        # the line edit in this Qt build, so nothing renders.
        self.gen_host.lineEdit().setPlaceholderText(
            "No hosts yet — record an app or import a HAR")
        row.addWidget(self.gen_host, 1)
        refresh = QPushButton("Refresh hosts")
        refresh.clicked.connect(self._refresh_gen_hosts_clicked)
        row.addWidget(refresh)
        lay.addLayout(row)

        row = QHBoxLayout()
        har_btn = QPushButton("Import HAR…")
        har_btn.clicked.connect(self._import_har)
        self.gen_har_btn = har_btn
        curl_btn = QPushButton("Import cURL…")
        curl_btn.clicked.connect(self._import_curl)
        self.import_label = QLabel("source: live proxy / saved captures")
        row.addWidget(har_btn)
        row.addWidget(curl_btn)
        row.addWidget(self.import_label, 1)
        lay.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Polish:"))
        self.polish_combo = QComboBox()
        self.polish_combo.addItem("none (deterministic, free)", "none")
        self.polish_combo.addItem("ollama (local model, if running)", "ollama")
        self.polish_combo.addItem("claude (CLI, if installed)", "claude")
        self.polish_combo.addItem("opencode (CLI, if installed)", "opencode")
        row.addWidget(self.polish_combo, 1)
        lay.addLayout(row)

        gen_btn = QPushButton("Generate client")
        gen_btn.setObjectName("big")
        gen_btn.clicked.connect(self._generate)
        self.gen_btn = gen_btn
        lay.addWidget(gen_btn)

        self.gen_log = QTextEdit()
        self.gen_log.setReadOnly(True)
        self.gen_log.setPlaceholderText(
            "Generate a client to see the result here.")
        lay.addWidget(self.gen_log, 1)

        row = QHBoxLayout()
        self.open_btn = QPushButton("Open in editor")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open_generated)
        copy_btn = QPushButton("Copy import snippet")
        copy_btn.setEnabled(False)
        copy_btn.clicked.connect(self._copy_snippet)
        self.copy_btn = copy_btn
        row.addWidget(self.open_btn)
        row.addWidget(copy_btn)
        row.addStretch(1)
        lay.addLayout(row)

        # No fetch here — the tab loads on first show (_tab_shown).
        return w

    def _refresh_gen_hosts_clicked(self, _checked=False):
        """Refresh hosts button -> full refresh (clicked's bool must
        not leak into _refresh_gen_hosts' refresh flag)."""
        self._refresh_gen_hosts()

    def _refresh_gen_hosts(self, refresh=True):
        """Reload the host combo — the flow fetch runs off the UI
        thread. Returns False if a task is already running."""
        def work(worker):
            _m, flows, _src = self._flows(refresh=refresh)
            return [h for h, _n in mitm_mod.hosts(flows)]

        def done(ok, hosts):
            if not ok:
                return
            current = self.gen_host.currentText()
            self.gen_host.clear()
            self.gen_host.addItems(hosts)
            if current:
                self.gen_host.setCurrentText(current)
            if self._gen_jump_host:
                # A Hosts-tab "Generate client…" jump is waiting — the
                # jump host wins over the restored selection.
                idx = self.gen_host.findText(self._gen_jump_host)
                if idx >= 0:
                    self.gen_host.setCurrentIndex(idx)
                self._gen_jump_host = None

        return self._run_task("loading hosts…", work, done)

    def _import_har(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a HAR file", os.path.expanduser("~"),
            "HAR files (*.har);;All files (*)")
        if not path:
            return

        def work(worker):
            data = har.parse(path)  # parse once, share across the import
            flows = har.load(data)
            store.save_flows(flows,
                             meta={"source": "import", "from": path})
            return ([h for h, _n in mitm_mod.hosts(flows)], len(flows))

        def done(ok, result):
            if not ok:
                QMessageBox.warning(
                    self, "MIMIK",
                    f"could not read that HAR file: {result}")
                return
            hosts, n = result
            self.har_path = path
            self.curl_path = None
            self.import_label.setText(
                f"source: HAR {path} ({n} flows saved)")
            self.gen_host.clear()
            self.gen_host.addItems(hosts)
            # The import changed the store — the flow cache is stale,
            # the Hosts tab reloads with the new host on next visit,
            # and the status bar's saved total moves.
            self._invalidate_flows()
            self._loaded.discard("hosts")
            self._refresh_saved_flows()
            self._update_status_bar()

        self._run_task("reading HAR file…", work, done,
                       disable=(self.gen_har_btn,))

    def _import_curl(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a file with a cURL command",
            os.path.expanduser("~"), "All files (*)")
        if path:
            self.curl_path = path
            self.har_path = None
            self.import_label.setText(f"source: cURL {path}")
            from .sources import curl

            try:
                flows = curl.load(path)
            except ValueError as e:
                QMessageBox.warning(
                    self, "MIMIK",
                    f"couldn't read that cURL file: {e}")
                return
            hosts = [h for h, _n in mitm_mod.hosts(flows)]
            self.gen_host.clear()
            self.gen_host.addItems(hosts)

    def _generate(self):
        host = self.gen_host.currentText().strip()
        if not host:
            QMessageBox.warning(self, "MIMIK", "pick a host first")
            return
        har_path, curl_path = self.har_path, self.curl_path
        polish = self.polish_combo.currentData()

        def work(worker):
            if har_path:
                eps = har.endpoints(har_path, host)
            elif curl_path:
                from .sources import curl

                eps = mitm_mod.endpoints_from_saved(curl.load(curl_path),
                                                    host)
            else:
                m, flows, source = self._flows()
                if source == "live":
                    try:
                        eps = mitm_mod.endpoints(m, flows, host)
                    except Exception:
                        eps = mitm_mod.endpoints_from_saved(flows, host)
                else:
                    eps = mitm_mod.endpoints_from_saved(flows, host)
            if not eps:
                raise ValueError(f"no requests to {host} found")
            preset, applied, _pids, auth_label = presets.resolve_for_gen(
                host)
            src = codegen.generate(host, eps, polish=polish, preset=preset,
                                   preset_label=auth_label, applied=applied)
            return src, applied

        def done(ok, result):
            if not ok:
                QMessageBox.warning(self, "MIMIK",
                                    f"could not generate: {result}")
                return
            src, applied = result
            from .cli import _default_out, _unique_out

            out = os.path.join(os.getcwd(), _default_out(host))
            if os.path.exists(out):
                numbered = _unique_out(host, os.getcwd())
                choice = self._ask_overwrite(os.path.basename(out),
                                             numbered)
                if choice is None:
                    # Cancelled — the generated source is still shown,
                    # so the work isn't lost.
                    self.gen_log.setText(
                        f"not saved — {os.path.basename(out)} already "
                        f"exists (generation cancelled)\n\n{src[:3000]}")
                    return
                if choice == "numbered":
                    out = os.path.join(os.getcwd(), numbered)
            with open(out, "w") as f:
                f.write(src)
            store.save_client_copy(os.path.basename(out), src)
            self.gen_out = out
            self.gen_log.setText(
                (f"presets applied: {'; '.join(applied)}\n\n"
                 if applied else "")
                + f"wrote {out}\n\n{src[:3000]}"
                + ("\n…(truncated in this preview)"
                   if len(src) > 3000 else ""))
            self.open_btn.setEnabled(True)
            self.copy_btn.setEnabled(True)
            self._refresh_clients()

        # Endpoint building + generation (and up to 300 s of Ollama
        # polish) run off the UI thread — the window stays responsive
        # and Cancel drops the result.
        self._run_task(f"generating a client for {host}…", work, done,
                       disable=(self.gen_btn,))

    def _ask_overwrite(self, name, numbered):
        """The generated file already exists — overwrite it, save as
        the numbered copy, or cancel. Returns "overwrite", "numbered",
        or None."""
        box = QMessageBox(self)
        box.setWindowTitle("MIMIK")
        box.setText(f"{name} already exists.")
        box.setInformativeText("Overwrite it, or save as a numbered copy?")
        overwrite = box.addButton("Overwrite", QMessageBox.AcceptRole)
        numbered_btn = box.addButton(f"Save as {numbered}",
                                     QMessageBox.ActionRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(numbered_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is overwrite:
            return "overwrite"
        if clicked is numbered_btn:
            return "numbered"
        return None

    def _open_generated(self):
        if self.gen_out:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.gen_out))

    def _copy_snippet(self):
        if not self.gen_out:
            return
        from .cli import _class_name

        with open(self.gen_out) as f:
            cls = _class_name(f.read()) or "Client"
        mod = os.path.splitext(os.path.basename(self.gen_out))[0]
        QApplication.clipboard().setText(
            f"from {mod} import {cls}\nacc = {cls}()\n")
        self.gen_log.append("\n(import snippet copied to clipboard)")

    # ---- Assets tab -----------------------------------------------------------

    def _assets_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Assets captured from your apps — images, models, audio, "
            "fonts. Build them into a Reblend pack."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Capture:"))
        self.asset_group = QComboBox()
        self.asset_group.currentIndexChanged.connect(
            self._asset_group_selected)
        row.addWidget(self.asset_group, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_asset_groups)
        row.addWidget(refresh)
        delete_group_btn = QPushButton("Delete group…")
        delete_group_btn.clicked.connect(self._delete_group)
        row.addWidget(delete_group_btn)
        lay.addLayout(row)

        self.asset_table = QTableWidget(0, 5)
        self.asset_table.setHorizontalHeaderLabels(
            ["Name", "Kind", "Size", "Source", "Status"])
        self.asset_table.horizontalHeader().setStretchLastSection(True)
        self.asset_table.itemDoubleClicked.connect(self._preview_asset_item)
        _empty_table_row(self.asset_table,
                         "No assets yet — capture an app or import a HAR.")
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Filter:"))
        self.asset_filter = QLineEdit()
        self.asset_filter.setPlaceholderText("filter assets…")
        self.asset_filter.textChanged.connect(self._filter_asset_table)
        frow.addWidget(self.asset_filter, 1)
        self.asset_filter_info = QLabel("")
        frow.addWidget(self.asset_filter_info)
        lay.addLayout(frow)
        lay.addWidget(self.asset_table, 2)

        row = QHBoxLayout()
        har_btn = QPushButton("Import HAR…")
        har_btn.clicked.connect(self._import_assets_har)
        self.assets_har_btn = har_btn
        scan_btn = QPushButton("Scan a program folder…")
        scan_btn.clicked.connect(self._scan_assets_folder)
        self.scan_btn = scan_btn
        preview_btn = QPushButton("Preview selected…")
        preview_btn.clicked.connect(self._preview_asset_button)
        reveal_btn = QPushButton("Reveal in folder")
        reveal_btn.clicked.connect(self._reveal_asset)
        row.addWidget(har_btn)
        row.addWidget(scan_btn)
        row.addWidget(preview_btn)
        row.addWidget(reveal_btn)
        row.addStretch(1)
        lay.addLayout(row)

        # Reblend location
        rbox = QGroupBox("Reblend (for converting models into a scene)")
        rlay = QVBoxLayout(rbox)
        rrow = QHBoxLayout()
        self.reblend_label = QLabel("")
        pick_btn = QPushButton("Choose Reblend folder…")
        pick_btn.clicked.connect(self._choose_reblend)
        rrow.addWidget(self.reblend_label, 1)
        rrow.addWidget(pick_btn)
        rlay.addLayout(rrow)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Model scale (cm per unit):"))
        self.scale_spin = QSpinBox()
        self.scale_spin.setRange(1, 10000)
        self.scale_spin.setValue(100)
        srow.addWidget(self.scale_spin)
        srow.addStretch(1)
        rlay.addLayout(srow)
        lay.addWidget(rbox)

        build_btn = QPushButton("Build asset pack")
        build_btn.setObjectName("big")
        build_btn.clicked.connect(self._build_pack)
        self.build_pack_btn = build_btn
        lay.addWidget(build_btn)

        row = QHBoxLayout()
        self.open_pack_btn = QPushButton("Open pack folder")
        self.open_pack_btn.setEnabled(False)
        self.open_pack_btn.clicked.connect(self._open_pack_folder)
        self.open_reblend_btn = QPushButton("Open in Reblend")
        self.open_reblend_btn.setEnabled(False)
        self.open_reblend_btn.clicked.connect(self._open_in_reblend)
        delete_pack_btn = QPushButton("Delete pack…")
        delete_pack_btn.clicked.connect(self._delete_pack)
        export_pack_btn = QPushButton("Export pack…")
        export_pack_btn.clicked.connect(self._export_pack)
        import_pack_btn = QPushButton("Import pack…")
        import_pack_btn.clicked.connect(self._import_pack)
        row.addWidget(self.open_pack_btn)
        row.addWidget(self.open_reblend_btn)
        row.addWidget(delete_pack_btn)
        row.addWidget(export_pack_btn)
        row.addWidget(import_pack_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.assets_log = QTextEdit()
        self.assets_log.setReadOnly(True)
        self.assets_log.setMaximumHeight(110)
        self.assets_log.setPlaceholderText(
            "Build a pack to see the result here.")
        lay.addWidget(self.assets_log, 1)

        # No fetch here — the tab loads on first show (_tab_shown).
        return w

    def _pack_status_map(self):
        """sha256 -> short conversion status from built packs (newest first)."""
        status = {}
        for _pdir, pack in reblend_bridge.list_packs():
            for rec in pack.get("assets", []):
                digest = rec.get("sha256")
                if not digest or digest in status:
                    continue
                if rec.get("ok"):
                    status[digest] = "converted to mesh"
                elif rec.get("note"):
                    status[digest] = rec["note"][:60]
                else:
                    status[digest] = "kept as original"
            for mat in pack.get("materials", []):
                digest = mat.get("sha256")
                if digest and digest not in status:
                    status[digest] = "color material in pack"
        return status

    def _refresh_asset_groups(self):
        groups = assets.list_asset_groups()
        current = self.asset_group.currentData()
        self.asset_group.blockSignals(True)
        self.asset_group.clear()
        for gdir, manifest in groups:
            n = len(manifest.get("assets", []))
            label = (f"{manifest.get('group')} — "
                     f"{manifest.get('captured_at', '')} ({n} assets)")
            self.asset_group.addItem(label, gdir)
        if not groups:
            # No data — a placeholder item (no UserRole data, so
            # _asset_group_selected treats it as "nothing selected").
            self.asset_group.addItem(
                "No captures yet — record an app or import a HAR")
        self.asset_group.blockSignals(False)
        if current:
            idx = self.asset_group.findData(current)
            if idx >= 0:
                self.asset_group.setCurrentIndex(idx)
        self._asset_group_selected()

    def _asset_group_selected(self, _idx=None):
        self.asset_table.setRowCount(0)
        gdir = self.asset_group.currentData()
        if not gdir:
            _empty_table_row(self.asset_table,
                             "No assets yet — capture an app or import "
                             "a HAR.")
            return
        status_map = self._pack_status_map()
        entries = []
        host = None
        for gd, manifest in assets.list_asset_groups():
            if gd == gdir:
                entries = manifest.get("assets", [])
                host = manifest.get("host")
                break
        self._wire_pack_buttons(host)
        self._apply_preset_scale(host, gdir)
        self.asset_table.clearSpans()
        if not entries:
            _empty_table_row(self.asset_table,
                             "No assets in this group.")
            return
        self.asset_table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            size = e.get("size", 0)
            if size >= 1024 * 1024:
                size_text = f"{size / (1024 * 1024):.1f} MB"
            elif size >= 1024:
                size_text = f"{size / 1024:.1f} KB"
            else:
                size_text = f"{size} B"
            source = e.get("source") or ""
            if e.get("url"):
                source = f"traffic: {e['url'][:60]}"
            elif e.get("source_path"):
                source = f"program file: {e['source_path'][:60]}"
            self.asset_table.setItem(i, 0, QTableWidgetItem(e.get("name", "")))
            self.asset_table.item(i, 0).setData(Qt.UserRole, {
                "path": os.path.join(gdir, e.get("file", "")),
                "kind": e.get("kind"),
                "name": e.get("name", ""),
                "size": e.get("size", 0),
            })
            self.asset_table.setItem(
                i, 1, QTableWidgetItem(
                    assets.KIND_LABELS.get(e.get("kind"), e.get("kind", ""))))
            self.asset_table.setItem(i, 2, QTableWidgetItem(size_text))
            self.asset_table.setItem(i, 3, QTableWidgetItem(source))
            self.asset_table.setItem(
                i, 4, QTableWidgetItem(
                    status_map.get(e.get("sha256"), "saved")))
        self._filter_asset_table()

    # ---- Asset preview (item 10) ------------------------------------------

    def _selected_asset(self):
        """The selected asset row's data dict, or None."""
        row = self.asset_table.currentRow()
        if row < 0:
            return None
        item = self.asset_table.item(row, 0)
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _preview_asset_item(self, item):
        """Double-click on an asset row — preview it (any column)."""
        if item is None:
            return
        name_item = self.asset_table.item(item.row(), 0)
        data = name_item.data(Qt.UserRole) if name_item is not None else None
        if data:
            self._preview_asset(data)

    def _preview_asset_button(self):
        data = self._selected_asset()
        if data is None:
            QMessageBox.warning(self, "MIMIK", "select an asset first")
            return
        self._preview_asset(data)

    def _preview_asset(self, data):
        """Preview by kind: images in a viewer, audio in the default
        player, models as an info dialog, everything else as info."""
        path = data["path"]
        if not os.path.isfile(path):
            QMessageBox.warning(self, "MIMIK",
                                f"{data['name']} is no longer on disk.")
            return
        kind = data.get("kind")
        if kind == "image":
            self._show_image_dialog(path, data["name"])
        elif kind in ("audio", "video"):
            # Play it in the user's default player.
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif kind == "model":
            self._show_info_dialog(data["name"],
                                   assets.model_info(path) or "", path)
        else:
            info = (f"kind: {assets.KIND_LABELS.get(kind, kind or 'unknown')}\n"
                    f"size: {store.format_size(data.get('size', 0))}")
            self._show_info_dialog(data["name"], info, path)

    def _show_image_dialog(self, path, name):
        pix = QPixmap(path)
        if pix.isNull():
            self._show_info_dialog(
                name, f"{name} could not be read as an image.", path)
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(name)
        dlg._preview_path = path
        lay = QVBoxLayout(dlg)
        label = QLabel()
        label.setPixmap(pix.scaled(640, 480, Qt.KeepAspectRatio,
                                   Qt.SmoothTransformation))
        label.setAlignment(Qt.AlignCenter)
        lay.addWidget(label)
        lay.addWidget(QLabel(
            f"{pix.width()} × {pix.height()} px — "
            f"{store.format_size(os.path.getsize(path))}"))
        row = QHBoxLayout()
        row.addStretch(1)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(self._open_preview_file)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        row.addWidget(open_btn)
        row.addWidget(close_btn)
        lay.addLayout(row)
        self._preview_dialog = dlg
        dlg.show()

    def _show_info_dialog(self, title, body, path):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg._preview_path = path
        lay = QVBoxLayout(dlg)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(body + f"\nfile: {path}")
        lay.addWidget(text)
        row = QHBoxLayout()
        row.addStretch(1)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(self._open_preview_file)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        row.addWidget(open_btn)
        row.addWidget(close_btn)
        lay.addLayout(row)
        self._preview_dialog = dlg
        dlg.show()

    def _open_preview_file(self):
        """Open the file the preview dialog is showing, in its default
        app. The path lives on the dialog itself (read via the
        button's parent), so several open previews each open their
        own file."""
        sender = self.sender()
        dlg = sender.parent() if sender is not None else None
        path = getattr(dlg, "_preview_path", None) if dlg is not None else None
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _reveal_asset(self):
        data = self._selected_asset()
        if data is None:
            QMessageBox.warning(self, "MIMIK", "select an asset first")
            return
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.dirname(data["path"])))

    def _wire_pack_buttons(self, host):
        """Point Open pack folder / Open in Reblend at the newest pack
        for this host, so a pack built in an earlier session is still
        openable without rebuilding."""
        self.pack_dir = None
        self.pack_scene = None
        self.pack_reblend = None
        if host:
            for pdir, manifest in reblend_bridge.list_packs():
                if manifest.get("host") != host:
                    continue
                self.pack_dir = pdir
                scene = manifest.get("scene")
                if scene:
                    self.pack_scene = os.path.join(pdir, scene)
                self.pack_reblend = (manifest.get("reblend_path")
                                     or reblend_bridge.find_reblend())
                break
        self.open_pack_btn.setEnabled(self.pack_dir is not None)
        self.open_reblend_btn.setEnabled(
            self.pack_scene is not None and self.pack_reblend is not None)

    def _apply_preset_scale(self, host, gdir):
        """Point the scale spinbox at the saved preset's scale, if the
        selected capture has one — app preset first, then a matching
        pattern preset. The user can still change it."""
        scale, _app, _patterns = presets.resolve_pack_scale(
            host=host,
            group=None if host else os.path.basename(gdir or ""))
        if scale is None:
            return
        self.scale_spin.blockSignals(True)
        self.scale_spin.setValue(int(scale))
        self.scale_spin.blockSignals(False)

    def _refresh_reblend_status(self):
        path = reblend_bridge.find_reblend()
        if path:
            self.reblend_label.setText(f"Reblend found at {path}")
        else:
            self.reblend_label.setText(
                "Reblend not found — pick your Reblend folder to convert "
                "models (capture and listing still work without it)")

    def _choose_reblend(self):
        path = QFileDialog.getExistingDirectory(
            self, "Choose your Reblend folder", os.path.expanduser("~"))
        if not path:
            return
        ok, msg = reblend_bridge.set_reblend_path(path)
        if not ok:
            QMessageBox.warning(self, "MIMIK", msg)
            return
        self._refresh_reblend_status()
        self.assets_log.setText(msg)

    def _import_assets_har(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a HAR file", os.path.expanduser("~"),
            "HAR files (*.har);;All files (*)")
        if not path:
            return

        def work(worker):
            return assets.extract_from_har(path)

        def done(ok, groups):
            if not ok:
                QMessageBox.warning(
                    self, "MIMIK",
                    f"could not read that HAR file: {groups}")
                return
            total = sum(len(m["assets"]) for _d, m in groups)
            self.assets_log.setText(
                f"imported {total} asset(s) from {path}" if total
                else "no assets in that HAR file")
            self._refresh_asset_groups()

        self._run_task("importing assets from HAR…", work, done,
                       disable=(self.assets_har_btn,))

    def _scan_assets_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Choose a program folder to scan",
            os.path.expanduser("~"))
        if not path:
            return
        program = os.path.basename(os.path.abspath(path)) or "folder"

        def work(worker):
            return assets.scan_program_tree(
                path, program, cancelled=lambda: worker.cancelled)

        def done(ok, result):
            if not ok:
                self.assets_log.setText(f"folder scan failed: {result}")
                return
            gdir, manifest = result
            if not gdir:
                self.assets_log.setText(f"no asset files in {path}")
            else:
                note = manifest.get("scan_note") or ""
                self.assets_log.setText(
                    f"saved {len(manifest['assets'])} asset(s) from {path}"
                    + (f" ({note})" if note else ""))
            self._refresh_asset_groups()

        self._run_task(f"scanning {path}…", work, done,
                       disable=(self.scan_btn,))

    def _build_pack(self):
        gdir = self.asset_group.currentData()
        if not gdir:
            QMessageBox.warning(self, "MIMIK", "no captured assets yet — "
                                "record an app or import a HAR first")
            return
        host = None
        group = os.path.basename(gdir)
        for gd, manifest in assets.list_asset_groups():
            if gd == gdir:
                host = manifest.get("host")
                break
        scale = float(self.scale_spin.value())
        resolved, _app, _patterns = presets.resolve_pack_scale(
            host=host, group=None if host else group)
        if resolved is not None and scale == float(resolved):
            # The spinbox still holds the preset's value — let the
            # pack use the preset itself (and say so in the log).
            scale = None

        def work(worker):
            return reblend_bridge.build_pack(
                host=host, group=None if host else group,
                scale=scale)

        def done(ok, result):
            if not ok:
                QMessageBox.warning(self, "MIMIK",
                                    f"pack build failed: {result}")
                return
            self.pack_dir = result["pack_dir"]
            self.pack_scene = result["scene_path"]
            self.pack_reblend = result["reblend_path"]
            lines = [f"pack: {result['pack_dir']}"]
            if result.get("preset_applied"):
                lines.append(
                    f"preset: used the saved scale {result['scale']}")
            if result.get("pattern_presets_applied"):
                lines.append(
                    f"patterns: used the saved scale {result['scale']} from "
                    f"{', '.join(result['pattern_presets_applied'])}")
            if result["scene_path"]:
                lines.append(f"scene: {result['scene_path']}")
                lines.append(
                    f"models converted: {result['models_converted']}")
            for note in result["notes"]:
                lines.append(f"note: {note}")
            for w in result["warnings"]:
                lines.append(f"warning: {w}")
            self.assets_log.setText("\n".join(lines))
            self.open_pack_btn.setEnabled(True)
            self.open_reblend_btn.setEnabled(
                result["scene_path"] is not None
                and result["reblend_path"] is not None)
            self._asset_group_selected()

        self._run_task("building asset pack…", work, done,
                       disable=(self.build_pack_btn,))

    def _delete_group(self):
        """Delete the selected asset group (after asking). Packs built
        from it are self-contained, so no pack breaks."""
        gdir = self.asset_group.currentData()
        if not gdir:
            QMessageBox.warning(self, "MIMIK", "no asset group selected")
            return
        manifest = {}
        for gd, m in assets.list_asset_groups():
            if gd == gdir:
                manifest = m
                break
        name = manifest.get("group") or os.path.basename(gdir)
        n = len(manifest.get("assets", []))
        answer = QMessageBox.question(
            self, "MIMIK",
            f"Delete the asset group {name} ({n} asset(s), "
            f"{store.format_size(store.dir_size(gdir))})?\n\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        assets.delete_group(gdir)
        self._refresh_asset_groups()

    def _delete_pack(self):
        """Delete the current pack (after asking). The source asset
        group stays — only the built pack goes. Re-selecting the group
        re-wires the Open buttons to the next-newest pack, if any."""
        if not self.pack_dir:
            QMessageBox.warning(self, "MIMIK",
                                "no pack built for this group yet")
            return
        answer = QMessageBox.question(
            self, "MIMIK",
            f"Delete the pack {os.path.basename(self.pack_dir)} "
            f"({store.format_size(store.dir_size(self.pack_dir))})?\n\n"
            "The source assets stay — only the built pack goes. "
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        reblend_bridge.delete_pack(self.pack_dir)
        self._asset_group_selected()

    def _export_pack(self):
        if not self.pack_dir:
            QMessageBox.warning(self, "MIMIK",
                                "no pack built for this group yet")
            return
        name = os.path.basename(self.pack_dir)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export pack", f"{name}.zip", "MIMIK export (*.zip)")
        if not path:
            return
        try:
            export.export_pack(name, path)
        except export.ExportError as e:
            QMessageBox.warning(self, "MIMIK", str(e))
            return
        self.assets_log.setText(f"Exported the pack {name} to {path}.")

    def _import_pack(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import pack", "", "MIMIK export (*.zip)")
        if not path:
            return
        try:
            result = export.import_export(path)
        except export.ExportError as e:
            QMessageBox.warning(self, "MIMIK", str(e))
            return
        except FileExistsError:
            answer = QMessageBox.question(
                self, "MIMIK",
                "That pack already exists. Replace it?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            result = export.import_export(path, overwrite=True)
        self._refresh_asset_groups()
        self._asset_group_selected()
        self.assets_log.setText(
            f"Imported the pack {result['name']}.")

    def _open_pack_folder(self):
        if self.pack_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.pack_dir))

    def _open_in_reblend(self):
        if not self.pack_scene or not self.pack_reblend:
            return
        ok, msg = reblend_bridge.open_in_reblend(self.pack_reblend,
                                                 self.pack_scene)
        if not ok:
            QMessageBox.warning(self, "MIMIK", msg)
            if self.pack_dir:
                QDesktopServices.openUrl(QUrl.fromLocalFile(self.pack_dir))
        else:
            self.assets_log.append(msg)

    # ---- Presets tab --------------------------------------------------------

    def _presets_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Presets are rules MIMIK learns from your captures — sign-in, "
            "endpoints, assets. They never hold passwords or tokens."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Learn from:"))
        self.preset_source = QComboBox()
        row.addWidget(self.preset_source, 1)
        learn_btn = QPushButton("Learn preset")
        learn_btn.clicked.connect(self._learn_preset)
        self.learn_btn = learn_btn
        row.addWidget(learn_btn)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_presets)
        row.addWidget(refresh)
        lay.addLayout(row)

        self.auto_learn_check = QCheckBox(
            "Learn presets from captures (refresh when a capture stops)")
        self.auto_learn_check.setChecked(presets.auto_learn_enabled())
        self.auto_learn_check.toggled.connect(self._toggle_auto_learn)
        lay.addWidget(self.auto_learn_check)

        lay.addWidget(QLabel("Saved presets:"))
        self.presets_list = QListWidget()
        self.presets_list.currentItemChanged.connect(self._preset_selected)
        lay.addWidget(self.presets_list, 1)

        lay.addWidget(QLabel("Pattern presets:"))
        self.pattern_presets_list = QListWidget()
        self.pattern_presets_list.currentItemChanged.connect(
            self._pattern_preset_selected)
        lay.addWidget(self.pattern_presets_list, 1)

        self.preset_detail = QTextEdit()
        self.preset_detail.setReadOnly(True)
        self.preset_detail.setPlaceholderText(
            "Select a preset to see its details.")
        lay.addWidget(self.preset_detail, 2)

        row = QHBoxLayout()
        delete_btn = QPushButton("Delete preset")
        delete_btn.clicked.connect(self._delete_preset)
        row.addWidget(delete_btn)
        export_btn = QPushButton("Export selected…")
        export_btn.clicked.connect(self._export_preset)
        row.addWidget(export_btn)
        import_btn = QPushButton("Import…")
        import_btn.clicked.connect(self._import_preset)
        row.addWidget(import_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.presets_log = QLabel("")
        self.presets_log.setWordWrap(True)
        lay.addWidget(self.presets_log)

        # No fetch here — the tab loads on first show (_tab_shown).
        return w

    def _refresh_presets(self):
        sources = set()
        for host, _n, _t in store.list_captures():
            sources.add(host)
        for _gdir, manifest in assets.list_asset_groups():
            group = manifest.get("group")
            if group:
                sources.add(group)
        current = self.preset_source.currentText()
        self.preset_source.blockSignals(True)
        self.preset_source.clear()
        for name in sorted(sources):
            self.preset_source.addItem(name, name)
        if not sources:
            # No data — a placeholder item with no UserRole data, so
            # _learn_preset treats it as "nothing selected".
            self.preset_source.addItem("No captures yet")
        self.preset_source.blockSignals(False)
        if current:
            self.preset_source.setCurrentText(current)
        self.presets_list.clear()
        saved = presets.list_presets()
        if not saved:
            self.presets_list.addItem(_empty_item(
                "No presets yet — learn one from a capture above."))
        for p in saved:
            item = QListWidgetItem(
                f"{p['name']} ({p['scope']}) — {presets.summary(p)}")
            item.setData(Qt.UserRole, p["name"])
            self.presets_list.addItem(item)
        self.pattern_presets_list.clear()
        patterns = presets.list_pattern_presets()
        if not patterns:
            self.pattern_presets_list.addItem(_empty_item(
                "No pattern presets yet — learn one from a capture "
                "above."))
        for p in patterns:
            learned_from = p.get("learned_from") or []
            item = QListWidgetItem(
                f"{p.get('display_name') or p['name']} — learned from "
                f"{len(learned_from)} "
                f"app{'s' if len(learned_from) != 1 else ''}")
            item.setData(Qt.UserRole, p["name"])
            self.pattern_presets_list.addItem(item)
        self._preset_selected()

    def _preset_selected(self, _current=None, _prev=None):
        item = self.presets_list.currentItem()
        if item is None:
            if self.pattern_presets_list.currentItem() is None:
                self.preset_detail.setText("")
            return
        self.pattern_presets_list.blockSignals(True)
        self.pattern_presets_list.setCurrentRow(-1)
        self.pattern_presets_list.blockSignals(False)
        preset = presets.load_preset(item.data(Qt.UserRole))
        if preset is None:
            self.preset_detail.setText("")
            return
        self.preset_detail.setText("\n".join(presets.describe(preset)))

    def _pattern_preset_selected(self, _current=None, _prev=None):
        item = self.pattern_presets_list.currentItem()
        if item is None:
            if self.presets_list.currentItem() is None:
                self.preset_detail.setText("")
            return
        self.presets_list.blockSignals(True)
        self.presets_list.setCurrentRow(-1)
        self.presets_list.blockSignals(False)
        preset = presets.load_pattern_preset(item.data(Qt.UserRole))
        if preset is None:
            self.preset_detail.setText("")
            return
        self.preset_detail.setText("\n".join(presets.describe(preset)))

    def _learn_preset(self):
        # The source combo carries the name as UserRole data; the
        # "No captures yet" placeholder has none, so it can't be
        # learned from by mistake.
        name = self.preset_source.currentData() or ""
        if not name:
            QMessageBox.warning(self, "MIMIK",
                                "pick a capture to learn from first")
            return

        def work(worker):
            return presets.learn_all(name)

        def done(ok, value):
            if not ok:
                self.presets_log.setText(f"could not learn: {value}")
                return
            result, patterns, _matched = value
            if result is None:
                self.presets_log.setText(
                    f"nothing to learn from for {name} yet — "
                    "capture it first")
                return
            preset, _path, created = result
            verb = "Learned" if created else "Refreshed"
            log = f"{verb} a preset for {name}: {presets.summary(preset)}"
            if patterns:
                names = ", ".join(p["name"] for p, _c in patterns)
                log += f" Patterns learned/updated: {names}"
            self.presets_log.setText(log)
            self._refresh_presets()

        self._run_task(f"learning a preset from {name}…", work, done,
                       disable=(self.learn_btn,))

    def _selected_preset(self):
        """(is_pattern, name) for the selected preset, or (None, None).

        The two lists clear each other's selection on change, so at
        most one has a current item; the pattern list wins when both
        somehow do (same as delete used to). Empty-state placeholder
        items carry no UserRole data, so they read as no selection.
        """
        pattern_item = self.pattern_presets_list.currentItem()
        if pattern_item is not None:
            fid = pattern_item.data(Qt.UserRole)
            if fid:
                return True, fid
        item = self.presets_list.currentItem()
        if item is not None:
            name = item.data(Qt.UserRole)
            if name:
                return False, name
        return None, None

    def _delete_preset(self):
        is_pattern, name = self._selected_preset()
        if name is None:
            QMessageBox.warning(self, "MIMIK", "select a preset first")
            return
        if is_pattern:
            answer = QMessageBox.question(
                self, "MIMIK", f"Delete the pattern preset {name}?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            presets.delete_pattern_preset(name)
            self.presets_log.setText(
                f"Deleted the pattern preset {name}.")
            self._refresh_presets()
            return
        answer = QMessageBox.question(
            self, "MIMIK", f"Delete the preset for {name}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        presets.delete_preset(name)
        self.presets_log.setText(f"Deleted the preset for {name}.")
        self._refresh_presets()

    def _export_preset(self):
        is_pattern, name = self._selected_preset()
        if name is None:
            QMessageBox.warning(self, "MIMIK", "select a preset first")
            return
        default = f"{name}-preset.zip"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export preset", default, "MIMIK export (*.zip)")
        if not path:
            return
        try:
            export.export_preset(name, path)
        except export.ExportError as e:
            QMessageBox.warning(self, "MIMIK", str(e))
            return
        kind = "pattern preset" if is_pattern else "preset"
        self.presets_log.setText(
            f"Exported the {kind} {name} to {path}.")

    def _import_preset(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import preset", "", "MIMIK export (*.zip)")
        if not path:
            return
        try:
            result = export.import_export(path)
        except export.ExportError as e:
            QMessageBox.warning(self, "MIMIK", str(e))
            return
        except FileExistsError:
            answer = QMessageBox.question(
                self, "MIMIK",
                "That preset already exists. Replace it?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            result = export.import_export(path, overwrite=True)
        self._refresh_presets()
        self.presets_log.setText(
            f"Imported the {result['kind']} {result['name']}.")

    def _toggle_auto_learn(self, checked):
        presets.set_auto_learn(checked)
        self.presets_log.setText(
            "Presets will refresh when a capture stops."
            if checked else
            "Presets only learn when you press Learn preset.")

    # ---- Clients tab ----------------------------------------------------------

    def _clients_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Generated clients (copies kept under "
                             "~/.local/share/mimik/clients/)."))
        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_clients)
        row.addWidget(refresh)
        row.addStretch(1)
        lay.addLayout(row)
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Filter:"))
        self.clients_filter = QLineEdit()
        self.clients_filter.setPlaceholderText("filter clients…")
        self.clients_filter.textChanged.connect(self._filter_clients_list)
        frow.addWidget(self.clients_filter, 1)
        self.clients_filter_info = QLabel("")
        frow.addWidget(self.clients_filter_info)
        lay.addLayout(frow)
        self.clients_list = QListWidget()
        self.clients_list.itemDoubleClicked.connect(self._open_client_item)
        lay.addWidget(self.clients_list, 1)
        smoke = QPushButton("Smoke-test selected client")
        smoke.clicked.connect(self._smoke_test)
        self.smoke_btn = smoke
        delete_btn = QPushButton("Delete selected client…")
        delete_btn.clicked.connect(self._delete_client)
        open_btn = QPushButton("Open selected…")
        open_btn.clicked.connect(self._open_client)
        reveal_btn = QPushButton("Reveal in folder")
        reveal_btn.clicked.connect(self._reveal_client)
        row2 = QHBoxLayout()
        row2.addWidget(smoke)
        row2.addWidget(delete_btn)
        row2.addWidget(open_btn)
        row2.addWidget(reveal_btn)
        lay.addLayout(row2)
        self.smoke_log = QTextEdit()
        self.smoke_log.setReadOnly(True)
        self.smoke_log.setPlaceholderText(
            "Smoke-test a client to see the result here.")
        lay.addWidget(self.smoke_log, 1)
        # No fetch here — the tab loads on first show (_tab_shown).
        return w

    def _refresh_clients(self):
        self.clients_list.clear()
        clients = store.list_clients()
        if not clients:
            self.clients_list.addItem(_empty_item(
                "No clients yet — generate one on the Generate tab."))
            return
        for name, path, size in clients:
            item = QListWidgetItem(f"{name}  ({size} bytes)")
            item.setData(Qt.UserRole, path)
            self.clients_list.addItem(item)
        self._filter_clients_list()

    def _open_client_item(self, item):
        """Double-click on a client — open it in the default editor."""
        if item is None:
            return
        path = item.data(Qt.UserRole)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_client(self):
        item = self.clients_list.currentItem()
        if item is None or not item.data(Qt.UserRole):
            QMessageBox.warning(self, "MIMIK", "select a client first")
            return
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(item.data(Qt.UserRole)))

    def _reveal_client(self):
        item = self.clients_list.currentItem()
        if item is None or not item.data(Qt.UserRole):
            QMessageBox.warning(self, "MIMIK", "select a client first")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(
            os.path.dirname(item.data(Qt.UserRole))))

    def _delete_client(self):
        """Delete the selected generated client copy (after asking)."""
        item = self.clients_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "MIMIK", "select a client first")
            return
        path = item.data(Qt.UserRole)
        name = os.path.basename(path)
        answer = QMessageBox.question(
            self, "MIMIK",
            f"Delete the generated client {name}?\n\n"
            "This deletes the kept copy — the client file in the "
            "folder it was generated into (if any) stays. "
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        store.delete_client(name)
        self._refresh_clients()

    def _smoke_test(self):
        item = self.clients_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "MIMIK", "select a client first")
            return
        path = item.data(Qt.UserRole)
        if not codegen.is_generated_client(path):
            # Item 29: the smoke test executes the file — a foreign
            # file dropped into clients/ must not run on one click.
            # A generated client carries the marker; anything else
            # needs an explicit yes (covers user-edited clients that
            # lost their header too).
            answer = QMessageBox.question(
                self, "MIMIK",
                f"{os.path.basename(path)} was not generated by "
                f"MIMIK.\n\nThe smoke test runs the file's code — "
                f"only run it if you trust where it came from. "
                f"Run it anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self.smoke_log.setText(
                    f"smoke test refused: {os.path.basename(path)} was "
                    f"not generated by MIMIK")
                return
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('c', {path!r})\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "from mimik import App\n"
            "for name in dir(mod):\n"
            "    obj = getattr(mod, name)\n"
            "    if isinstance(obj, type) and issubclass(obj, App) "
            "and obj is not App:\n"
            "        print('class:', name, '| HOST:', obj.HOST)\n"
            "        print('methods:', ', '.join(\n"
            "            m for m in dir(obj)\n"
            "            if not m.startswith('_') and callable(getattr(obj, m))\n"
            "            and m not in ('get', 'post', 'put', 'patch', 'delete',\n"
            "                          'head', 'options', 'request')))\n"
        )

        def work(worker):
            # Popen + poll (not subprocess.run): Cancel kills the child
            # instead of waiting out the 30 s timeout.
            proc = subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=os.path.dirname(path))
            deadline = time.time() + 30
            while proc.poll() is None:
                if worker.cancelled:
                    proc.kill()
                    proc.wait()
                    return None  # discarded — see _task_finished
                if time.time() > deadline:
                    proc.kill()
                    proc.wait()
                    raise TimeoutError(
                        "smoke test timed out after 30 s")
                time.sleep(0.1)
            out, _ = proc.communicate()
            return out or "(no output)"

        def done(ok, out):
            if not ok:
                self.smoke_log.setText(f"smoke test failed: {out}")
            else:
                self.smoke_log.setText(out)

        self._run_task("smoke-testing…", work, done,
                       disable=(self.smoke_btn,))

    # ---- Doctor tab -----------------------------------------------------------

    def _doctor_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Setup check — green is good, red has a fix."))
        refresh = QPushButton("Re-check")
        refresh.clicked.connect(self._run_doctor)
        about = QPushButton("About MIMIK")
        about.clicked.connect(self._show_about)
        view_log = QPushButton("View log")
        view_log.clicked.connect(self._show_log)
        sample_btn = QPushButton("Load sample capture")
        sample_btn.clicked.connect(self._load_sample_clicked)
        update_btn = QPushButton("Check for updates…")
        update_btn.clicked.connect(self._check_for_updates)
        row = QHBoxLayout()
        row.addWidget(refresh)
        row.addWidget(about)
        row.addWidget(view_log)
        row.addWidget(sample_btn)
        row.addWidget(update_btn)
        lay.addLayout(row)
        self.doctor_text = QTextEdit()
        self.doctor_text.setReadOnly(True)
        self.doctor_text.setPlaceholderText("Running the setup check…")
        lay.addWidget(self.doctor_text, 1)
        # No check here — the tab loads on first show (_tab_shown).
        return w

    def _show_about(self):
        """About dialog — version, what MIMIK is, and the attribution
        to littledivy/mimic (item 20)."""
        QMessageBox.about(
            self, "About MIMIK",
            f"MIMIK {__version__}\n\n"
            "Intercept any app, then call it from Python like a "
            "library.\n"
            "Capture your own session with mitmproxy — or import a "
            "HAR file or cURL paste — and MIMIK reuses your real "
            "auth so the server can't tell your script from the "
            "app.\n\n"
            "Linux port of littledivy/mimic (MIT).\n"
            "Own accounts only: MIMIK replays YOUR session, for "
            "YOUR apps and YOUR accounts.")

    def _show_log(self):
        """The log file's tail in a read-only dialog (item 23)."""
        dlg = QDialog(self)
        dlg.setWindowTitle("MIMIK log")
        dlg.resize(700, 450)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"Log file: {log.log_path()}"))
        text = QTextEdit()
        text.setReadOnly(True)
        tail = log.read_tail()
        text.setPlainText(
            tail or "(the log is empty — no errors recorded yet)")
        lay.addWidget(text, 1)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        lay.addWidget(close)
        self._log_dialog = dlg
        dlg.exec()

    # ---- first-run onboarding (item 28) --------------------------------------

    def _maybe_offer_sample(self):
        """First-run offer: a fresh data dir gets the sample capture.
        Called once from main() after the window is up — never from
        MainWindow itself, so tests that build a window on an empty
        dir don't get a modal dialog."""
        if not sample.is_fresh():
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Welcome to MIMIK")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "MIMIK turns an app's traffic into a Python library:\n"
            "1. Record an app  →  2. pick a host  →  "
            "3. generate a client."))
        lay.addWidget(QLabel(
            "No app to record yet? Load a sample capture of a demo "
            "game API (demo.example.com) and try the whole flow — "
            "hosts, preset, generated client — with no recording."))
        row = QHBoxLayout()
        load_btn = QPushButton("Load the sample capture")
        load_btn.clicked.connect(self._sample_offer_accept)
        row.addWidget(load_btn)
        no_btn = QPushButton("No thanks")
        no_btn.clicked.connect(self._sample_offer_decline)
        row.addWidget(no_btn)
        lay.addLayout(row)
        self._sample_dialog = dlg
        dlg.exec()

    def _sample_offer_accept(self):
        self._sample_dialog.accept()
        presets.set_sample_offered()
        result = self._load_sample()
        if not result["already_loaded"]:
            self._show_sample_guide(result)

    def _sample_offer_decline(self):
        self._sample_dialog.accept()
        presets.set_sample_offered()

    def _load_sample_clicked(self):
        """The Doctor tab's "Load sample capture" button."""
        result = self._load_sample()
        if result["already_loaded"]:
            self.doctor_text.append(
                f"The sample capture is already loaded "
                f"({result['flows']} flows for {sample.SAMPLE_HOST}).")
        else:
            self._show_sample_guide(result)

    def _load_sample(self):
        """Load the sample capture and refresh every tab that shows
        its data. Returns the load_sample result dict."""
        result = sample.load_sample()
        for key in ("hosts", "generate", "assets", "presets"):
            self._loaded.discard(key)
        self._refresh_saved_flows()
        self._update_status_bar()
        return result

    def _show_sample_guide(self, result):
        """Step 2 of the guide: the sample is loaded — offer the jump
        to the Generate tab with the sample host pre-selected, so one
        press of Generate produces the first working client."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Sample loaded")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            f"demo.example.com is ready: {result['flows']} flows, "
            f"{result['assets']} "
            f"asset{'s' if result['assets'] != 1 else ''}"
            + (", preset learned" if result["preset"] else "")
            + ".\n\nYour first client is one press away — open the "
              "Generate tab and press Generate."))
        row = QHBoxLayout()
        go_btn = QPushButton("Take me to Generate")
        go_btn.clicked.connect(self._sample_guide_go)
        row.addWidget(go_btn)
        explore_btn = QPushButton("I'll explore")
        explore_btn.clicked.connect(self._sample_guide_close)
        row.addWidget(explore_btn)
        lay.addLayout(row)
        self._sample_guide_dialog = dlg
        dlg.exec()

    def _sample_guide_go(self):
        self._sample_guide_dialog.accept()
        # The jump host rides _refresh_gen_hosts: discard "generate"
        # so switching tabs reloads it and lands on the sample host.
        self._gen_jump_host = sample.SAMPLE_HOST
        self._loaded.discard("generate")
        self.tabs.setCurrentIndex(2)  # Generate tab

    def _sample_guide_close(self):
        self._sample_guide_dialog.accept()

    def _check_for_updates(self):
        """Manual update check (item 31) — one GitHub API call, off
        the UI thread. Nothing here runs on startup or on a timer."""
        def work(worker):
            return update.check_for_update()

        def done(ok, result):
            if not ok:
                QMessageBox.warning(
                    self, "MIMIK",
                    f"could not check for updates: {result}")
                return
            if result["error"]:
                QMessageBox.warning(
                    self, "MIMIK",
                    f"could not check for updates: {result['error']}")
                return
            if not result["update_available"]:
                QMessageBox.information(
                    self, "MIMIK",
                    f"MIMIK {result['current']} is the latest version.")
                return
            answer = QMessageBox.question(
                self, "MIMIK",
                f"MIMIK {result['latest']} is available "
                f"(you have {result['current']}).\n\n"
                f"Open the releases page in your browser?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer == QMessageBox.Yes:
                QDesktopServices.openUrl(QUrl(result["url"]))

        self._run_task("checking for updates…", work, done)

    def _run_doctor(self):
        """Setup check — the network probes (mitmweb, Ollama) run off
        the UI thread, so opening the tab never freezes the window.
        Returns False if a task is already running."""
        def work(worker):
            return doctor.doctor()

        def done(ok, result):
            if ok:
                self.doctor_text.setHtml(self._render_doctor(result))

        return self._run_task("running setup check…", work, done)

    def _render_doctor(self, result):
        """The shared setup check as colored HTML — green is good, red
        has a fix (the header's promise, now true)."""
        green = "#66bb6a"
        red = "#ef5350"
        grey = "#9e9e9e"

        def line(text, color=None):
            text = html.escape(text)
            if color:
                return f'<font color="{color}">{text}</font>'
            return text

        out = []
        for c in result["required"]:
            if c["ok"]:
                out.append(line(f"✓  {c['name']}", green))
            else:
                out.append(line(f"✗  {c['name']}", red))
                if c.get("fix"):
                    out.append(line(f"      → {c['fix']}", red))
        reachable = result["mitmweb_running"]
        out.append(line(
            f"{'✓' if reachable else '–'}  mitmweb running + reachable",
            green if reachable else grey))
        if not reachable:
            out.append(line("      → press Start proxy on the Record tab",
                            grey))
        out.append("")
        out.append("optional:")
        for c in result["optional"]:
            if c["ok"]:
                out.append(line(f"✓  {c['name']}", green))
            else:
                out.append(line(f"–  {c['name']}", grey))
                if c.get("fix"):
                    out.append(line(f"      → {c['fix']}", grey))
            if c.get("detail"):
                out.append(line(f"      {c['detail']}", grey))
        out.append("")
        out.extend(line(t) for t in result["info"])
        out.append("")
        if result["ok"]:
            out.append(line("overall: READY", green))
        else:
            out.append(line("overall: SOMETHING IS MISSING", red))
        return "<br>".join(out)


def _handle_uncaught(exc_type, exc, tb):
    """sys.excepthook for the GUI (item 23): a menu-launched app has
    no stderr, so an uncaught exception was invisible. Log it and
    tell the user where the log is. The app keeps running — Qt
    survives an exception in a slot, and killing the user's session
    over one bad click would be worse than the error."""
    try:
        log.log_error("uncaught", exc)
    except Exception:
        pass
    try:
        QMessageBox.critical(
            None, "MIMIK — something went wrong",
            f"MIMIK hit an unexpected error:\n\n{exc}\n\n"
            f"The details were written to:\n{log.log_path()}\n\n"
            "MIMIK is still running — you can keep working, or "
            "restart it if something looks wrong.")
    except Exception:
        pass
    try:
        sys.__excepthook__(exc_type, exc, tb)
    except Exception:
        pass


def _install_crash_handler():
    sys.excepthook = _handle_uncaught


def main():
    _install_crash_handler()
    app = QApplication(sys.argv)
    app.setApplicationName("MIMIK")
    app.setStyleSheet(STYLESHEET)
    win = MainWindow()
    win.show()
    # After the first event-loop turn: warn if a previous capture left
    # the Wine prefix pointing at the proxy (a saved state with no
    # restore). Deferred so the window is up before any dialog appears.
    QTimer.singleShot(0, win.check_stale_wine_state)
    # Same deferral for the first-run sample offer (item 28).
    QTimer.singleShot(0, win._maybe_offer_sample)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
