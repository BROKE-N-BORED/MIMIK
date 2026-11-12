"""MIMIK desktop GUI — a native Qt window, never a web page.

One window, five tabs: Record | Hosts & Learn | Generate | Clients | Doctor.
Dark, plain, big readable text. Accent violet #b48fdd only where needed.
"""
import os
import subprocess
import sys

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget,
)

from . import codegen, launch, store, wine
from .cli import _lan_ip
from .recorder import ProxySession, mitmweb_cmd
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

TARGETS = [
    ("android", "Android phone (same Wi-Fi)"),
    ("linux-app", "Linux app (installed)"),
    ("file", "Program from file (AppImage / archive / ISO / .deb)"),
    ("web", "Web app (HAR / cURL — no proxy)"),
    ("windows", "Windows program (via WinLinx — capture only)"),
]


class RecordWorker(QThread):
    """Runs the proxy (+ launched program / Wine proxy) off the UI thread."""

    hosts_updated = Signal(list)
    status = Signal(str)
    finished = Signal()

    def __init__(self, target, file_path=None, pick=None, launch_cmd=None,
                 deb_install=False):
        super().__init__()
        self.target = target
        self.file_path = file_path
        self.pick = pick
        self.launch_cmd = launch_cmd
        self.deb_install = deb_install
        self._stop = False

    def request_stop(self):
        self._stop = True

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
                meta["prefix"] = prefix
            elif self.target == "file":
                try:
                    plan = launch.prepare_file(
                        self.file_path, pick=self.pick,
                        deb_install=self.deb_install)
                except launch.LaunchError as e:
                    self.status.emit(str(e))
                    return
                if plan.needs_pick:
                    self.status.emit("pick the program to run first")
                    return
                meta.update({"launched_file": self.file_path,
                             "kind": plan.kind})
                self.status.emit(f"will run: {plan.command_display}")

            session = ProxySession(
                meta=meta,
                on_update=lambda rows: self.hosts_updated.emit(rows),
            )
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
                    self.status.emit(f"launched: {plan.command_display}")
                except Exception as e:
                    self.status.emit(f"could not launch the program: {e}")
            elif self.target == "linux-app" and self.launch_cmd:
                import shlex

                env = dict(os.environ)
                env.update(launch.proxy_env())
                try:
                    app_proc = subprocess.Popen(
                        shlex.split(self.launch_cmd), env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
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
            launch.stop_process(app_proc)
            if plan is not None and plan.mount is not None:
                plan.mount.unmount()
                self.status.emit("unmounted the disk image")
            if prefix is not None:
                try:
                    wine.revert_proxy(prefix)
                    self.status.emit("prefix proxy settings restored")
                except Exception as e:
                    self.status.emit(
                        f"could not restore the prefix proxy: {e}")
            if session is not None:
                session.stop()
            self.status.emit("capture saved")
            self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MIMIK — any app, as a Python library")
        self.setStyleSheet(STYLESHEET)  # dark theme, even without main()
        self.resize(900, 620)
        self.worker = None
        self.plan = None
        self.file_path = None
        self.har_path = None
        self.curl_path = None
        self.gen_out = None

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(self._record_tab(), "Record")
        tabs.addTab(self._hosts_tab(), "Hosts & Learn")
        tabs.addTab(self._generate_tab(), "Generate")
        tabs.addTab(self._clients_tab(), "Clients")
        tabs.addTab(self._doctor_tab(), "Doctor")
        self.tabs = tabs

    # ---- Record tab ----------------------------------------------------------

    def _record_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        lay.addWidget(QLabel(
            "Capture your own app's traffic, then turn it into a Python "
            "library. Own accounts only."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Capture target:"))
        self.target_combo = QComboBox()
        for key, label in TARGETS:
            self.target_combo.addItem(label, key)
        self.target_combo.currentIndexChanged.connect(self._target_changed)
        row.addWidget(self.target_combo, 1)
        lay.addLayout(row)

        self.lan_label = QLabel(f"LAN IP for the phone proxy: {_lan_ip()}:8080")
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

        # Linux-app launch command
        self.app_box = QGroupBox("Linux app to launch (optional)")
        alay = QHBoxLayout(self.app_box)
        self.launch_edit = QTextEdit()
        self.launch_edit.setMaximumHeight(30)
        self.launch_edit.setPlaceholderText("e.g. firefox   (leave blank to "
                                            "launch it yourself)")
        alay.addWidget(self.launch_edit, 1)
        lay.addWidget(self.app_box)

        self.steps = QTextEdit()
        self.steps.setReadOnly(True)
        lay.addWidget(self.steps, 1)

        self.host_list = QListWidget()
        self.host_list.setMaximumHeight(110)
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
        from .cli import (ANDROID_STEPS, LINUX_APP_STEPS, WEB_STEPS,
                          WINDOWS_STEPS)

        if key == "android":
            self.steps.setText(ANDROID_STEPS.format(ip=_lan_ip()))
            self.start_btn.setEnabled(True)
            self.start_btn.setText("Start proxy")
        elif key == "linux-app":
            self.steps.setText(LINUX_APP_STEPS)
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
            self.steps.setText(WEB_STEPS)
            self.start_btn.setEnabled(False)
            self.start_btn.setText("No proxy needed — import a HAR instead")
        elif key == "windows":
            self.steps.setText(WINDOWS_STEPS + "\nCommands MIMIK will run:\n"
                "  wine regedit /s <proxy.reg>\n"
                "  wine certutil -addstore -f -user Root "
                "~/.mitmproxy/mitmproxy-ca-cert.pem\n"
                "Stop restores the prefix's original proxy settings.")
            self.start_btn.setEnabled(True)
            self.start_btn.setText("Start proxy (configure prefix)")

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

    def _prepare_file(self):
        self.candidate_list.clear()
        self.plan = None
        try:
            plan = launch.prepare_file(
                self.file_path,
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
        pick = None
        if key == "file" and self.plan is not None:
            row = self.candidate_list.currentRow()
            if row >= 0:
                pick = self.plan.candidates[row]
        self.worker = RecordWorker(
            key, file_path=self.file_path, pick=pick,
            launch_cmd=self.launch_edit.toPlainText().strip() or None,
            deb_install=self.deb_install_check.isChecked())
        self.worker.hosts_updated.connect(self._show_hosts)
        self.worker.status.connect(lambda s: self.steps.append(s))
        self.worker.finished.connect(self._recording_finished)
        self.worker.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.target_combo.setEnabled(False)
        self.host_list.clear()

    def _show_hosts(self, rows):
        self.host_list.clear()
        for host, n in rows:
            self.host_list.addItem(f"{n:>6}  {host}")

    def _stop_recording(self):
        if self.worker is not None:
            self.worker.request_stop()
            self.stop_btn.setEnabled(False)
            self.steps.append("stopping…")

    def _recording_finished(self):
        self.worker = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.target_combo.setEnabled(True)
        self._target_changed()
        self._refresh_hosts()

    # ---- Hosts & Learn tab ---------------------------------------------------

    def _hosts_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Captured hosts (live proxy, else saved captures)."))
        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_hosts)
        row.addWidget(refresh)
        row.addStretch(1)
        lay.addLayout(row)
        self.hosts_list = QListWidget()
        self.hosts_list.currentItemChanged.connect(self._host_selected)
        lay.addWidget(self.hosts_list, 1)
        lay.addWidget(QLabel("Endpoints for the selected host:"))
        self.ep_table = QTableWidget(0, 3)
        self.ep_table.setHorizontalHeaderLabels(["Method", "Path", "Status"])
        self.ep_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.ep_table, 2)
        self._refresh_hosts()
        return w

    def _flows(self):
        try:
            m = mitm_mod.Mitm()
            flows = m.flows()
            try:
                store.save_flows(flows, mitm=m)
            except Exception:
                pass
            return m, flows, "live"
        except mitm_mod.MitmError:
            return None, store.load_all(), "saved"

    def _refresh_hosts(self):
        _m, flows, _src = self._flows()
        self._flows_cache = flows
        self.hosts_list.clear()
        for host, n in mitm_mod.hosts(flows):
            item = QListWidgetItem(f"{n:>6}  {host}")
            item.setData(Qt.UserRole, host)
            self.hosts_list.addItem(item)

    def _host_selected(self, current, _prev):
        self.ep_table.setRowCount(0)
        if current is None:
            return
        host = current.data(Qt.UserRole)
        _m, flows, source = self._flows()
        if source == "live":
            try:
                eps = mitm_mod.endpoints(_m, flows, host)
            except Exception:
                eps = mitm_mod.endpoints_from_saved(flows, host)
        else:
            eps = mitm_mod.endpoints_from_saved(flows, host)
        self.ep_table.setRowCount(len(eps))
        for i, e in enumerate(eps):
            self.ep_table.setItem(i, 0, QTableWidgetItem(e["method"]))
            self.ep_table.setItem(i, 1, QTableWidgetItem(e["path"]))
            self.ep_table.setItem(i, 2,
                                  QTableWidgetItem(str(e["status"])))

    # ---- Generate tab ---------------------------------------------------------

    def _generate_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Generate a Python client from captured traffic. The default "
            "generator is deterministic and free — no LLM, no network."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Host:"))
        self.gen_host = QComboBox()
        self.gen_host.setEditable(True)
        row.addWidget(self.gen_host, 1)
        refresh = QPushButton("Refresh hosts")
        refresh.clicked.connect(self._refresh_gen_hosts)
        row.addWidget(refresh)
        lay.addLayout(row)

        row = QHBoxLayout()
        har_btn = QPushButton("Import HAR…")
        har_btn.clicked.connect(self._import_har)
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
        lay.addWidget(gen_btn)

        self.gen_log = QTextEdit()
        self.gen_log.setReadOnly(True)
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

        self._refresh_gen_hosts()
        return w

    def _refresh_gen_hosts(self):
        _m, flows, _src = self._flows()
        hosts = [h for h, _n in mitm_mod.hosts(flows)]
        current = self.gen_host.currentText()
        self.gen_host.clear()
        self.gen_host.addItems(hosts)
        if current:
            self.gen_host.setCurrentText(current)

    def _import_har(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a HAR file", os.path.expanduser("~"),
            "HAR files (*.har);;All files (*)")
        if path:
            self.har_path = path
            self.curl_path = None
            self.import_label.setText(f"source: HAR {path}")
            hosts = [h for h, _n in har.hosts(path)]
            self.gen_host.clear()
            self.gen_host.addItems(hosts)

    def _import_curl(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a file with a cURL command",
            os.path.expanduser("~"), "All files (*)")
        if path:
            self.curl_path = path
            self.har_path = None
            self.import_label.setText(f"source: cURL {path}")
            from .sources import curl

            flows = curl.load(path)
            hosts = [h for h, _n in mitm_mod.hosts(flows)]
            self.gen_host.clear()
            self.gen_host.addItems(hosts)

    def _generate(self):
        host = self.gen_host.currentText().strip()
        if not host:
            QMessageBox.warning(self, "MIMIK", "pick a host first")
            return
        try:
            if self.har_path:
                eps = har.endpoints(self.har_path, host)
            elif self.curl_path:
                from .sources import curl

                eps = mitm_mod.endpoints_from_saved(curl.load(self.curl_path),
                                                    host)
            else:
                m, flows, source = self._flows()
                if source == "live":
                    eps = mitm_mod.endpoints(m, flows, host)
                else:
                    eps = mitm_mod.endpoints_from_saved(flows, host)
        except Exception as e:
            QMessageBox.warning(self, "MIMIK", f"could not read endpoints: {e}")
            return
        if not eps:
            QMessageBox.warning(self, "MIMIK",
                                f"no requests to {host} found")
            return
        polish = self.polish_combo.currentData()
        src = codegen.generate(host, eps, polish=polish)
        from .cli import _default_out

        out = os.path.join(os.getcwd(), _default_out(host))
        with open(out, "w") as f:
            f.write(src)
        store.save_client_copy(os.path.basename(out), src)
        self.gen_out = out
        self.gen_log.setText(
            f"wrote {out}\n\n{src[:3000]}"
            + ("\n…(truncated in this preview)" if len(src) > 3000 else ""))
        self.open_btn.setEnabled(True)
        self.copy_btn.setEnabled(True)
        self._refresh_clients()

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
        self.clients_list = QListWidget()
        lay.addWidget(self.clients_list, 1)
        smoke = QPushButton("Smoke-test selected client")
        smoke.clicked.connect(self._smoke_test)
        lay.addWidget(smoke)
        self.smoke_log = QTextEdit()
        self.smoke_log.setReadOnly(True)
        lay.addWidget(self.smoke_log, 1)
        self._refresh_clients()
        return w

    def _refresh_clients(self):
        self.clients_list.clear()
        for name, path, size in store.list_clients():
            item = QListWidgetItem(f"{name}  ({size} bytes)")
            item.setData(Qt.UserRole, path)
            self.clients_list.addItem(item)

    def _smoke_test(self):
        item = self.clients_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "MIMIK", "select a client first")
            return
        path = item.data(Qt.UserRole)
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
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=30,
                cwd=os.path.dirname(path),
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            self.smoke_log.setText(out or "(no output)")
        except Exception as e:
            self.smoke_log.setText(f"smoke test failed: {e}")

    # ---- Doctor tab -----------------------------------------------------------

    def _doctor_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Setup check — green is good, red has a fix."))
        refresh = QPushButton("Re-check")
        refresh.clicked.connect(self._run_doctor)
        lay.addWidget(refresh)
        self.doctor_text = QTextEdit()
        self.doctor_text.setReadOnly(True)
        lay.addWidget(self.doctor_text, 1)
        self._run_doctor()
        return w

    def _run_doctor(self):
        lines = []
        ok = True

        def check(name, present, fix=""):
            nonlocal ok
            mark = "✓" if present else "✗"
            lines.append(f"{mark}  {name}")
            if not present:
                ok = False
                if fix:
                    lines.append(f"      → {fix}")

        check(f"python {sys.version_info.major}.{sys.version_info.minor} "
              f"(3.10+ needed)", sys.version_info >= (3, 10))
        try:
            import requests  # noqa: F401

            has_requests = True
        except ImportError:
            has_requests = False
        check("requests library", has_requests, "pip install requests")
        check("proxy (mitmweb or uvx)", mitmweb_cmd() is not None,
              "pip install mitmproxy — or install uv")
        check("Qt GUI (PySide6)", True)
        try:
            mitm_mod.Mitm().flows()
            reachable = True
        except mitm_mod.MitmError:
            reachable = False
        lines.append(f"{'✓' if reachable else '–'}  mitmweb running + "
                     "reachable" + ("" if reachable else
                                    "  (run `mimik record` to start it)"))
        lines.append("")
        lines.append("optional:")
        lines.append(f"{'✓' if codegen.ollama_models() else '–'}  "
                     "Ollama (optional polish)")
        ca = os.path.join(os.path.expanduser("~"), ".mitmproxy",
                          "mitmproxy-ca-cert.pem")
        lines.append(f"{'✓' if os.path.exists(ca) else '–'}  "
                     "mitmproxy CA cert (created on first record)")
        lines.append(f"{'✓' if wine.wine_available() else '–'}  "
                     "wine (Windows capture target)")
        lines.append(f"{'✓' if wine.winlinx_available() else '–'}  "
                     "WinLinx (Windows capture target)")
        caps = store.list_captures()
        lines.append("")
        lines.append(f"saved captures: {len(caps)} host(s)")
        lines.append(f"LAN IP for the phone proxy: {_lan_ip()}:8080")
        lines.append("")
        lines.append("overall: " + ("READY" if ok else "SOMETHING IS MISSING"))
        self.doctor_text.setText("\n".join(lines))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MIMIK")
    app.setStyleSheet(STYLESHEET)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
