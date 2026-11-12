"""MIMIK desktop GUI — a native Qt window, never a web page.

One window, seven tabs: Record | Hosts and Learn | Generate | Assets |
Clients | Doctor | Presets. Dark, plain, big readable text. Accent
violet #b48fdd only where needed.
"""
import os
import subprocess
import sys

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from . import assets, codegen, launch, presets, reblend_bridge, store, wine
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
        self._last_rows = []

    def request_stop(self):
        self._stop = True

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
                # Assets inside the program's own files: copy them out
                # now, while the extraction / mount is live.
                if plan.work_dir:
                    try:
                        program = os.path.basename(
                            launch.run_dir_for(self.file_path))
                        gdir, manifest = assets.scan_program_tree(
                            plan.work_dir, program)
                        if gdir:
                            self.status.emit(
                                f"found {len(manifest['assets'])} asset "
                                "file(s) in the program — saved for the "
                                "asset pack")
                    except Exception as e:
                        self.status.emit(f"asset scan skipped: {e}")

            session = ProxySession(
                meta=meta,
                on_update=self._hosts_updated,
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
            # Presets: refresh when auto-learn is on, and say which
            # saved presets apply to the hosts just captured — app
            # presets, and pattern presets the traffic matched.
            try:
                for host, refreshed, has in presets.after_record(
                        [h for h, _n in self._last_rows]):
                    if refreshed:
                        self.status.emit(f"Preset refreshed for {host}")
                    if has:
                        self.status.emit(f"Used the saved preset for {host}")
                    matched = presets.matched_pattern_ids(host)
                    if matched:
                        self.status.emit(
                            f"Matched patterns: {', '.join(matched)}")
            except Exception:
                pass
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
        self.pack_dir = None
        self.pack_scene = None
        self.pack_reblend = None

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(self._record_tab(), "Record")
        tabs.addTab(self._hosts_tab(), "Hosts and Learn")
        tabs.addTab(self._generate_tab(), "Generate")
        tabs.addTab(self._assets_tab(), "Assets")
        tabs.addTab(self._clients_tab(), "Clients")
        tabs.addTab(self._doctor_tab(), "Doctor")
        tabs.addTab(self._presets_tab(), "Presets")
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
        preset, applied, _pids, auth_label = presets.resolve_for_gen(host)
        src = codegen.generate(host, eps, polish=polish, preset=preset,
                               preset_label=auth_label, applied=applied)
        from .cli import _default_out

        out = os.path.join(os.getcwd(), _default_out(host))
        with open(out, "w") as f:
            f.write(src)
        store.save_client_copy(os.path.basename(out), src)
        self.gen_out = out
        self.gen_log.setText(
            (f"presets applied: {'; '.join(applied)}\n\n" if applied else "")
            + f"wrote {out}\n\n{src[:3000]}"
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

    # ---- Assets tab -----------------------------------------------------------

    def _assets_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Assets captured from your apps and games — images, 3D models, "
            "audio, fonts. Build them into a Reblend pack to see the clone "
            "look like the real thing."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Capture:"))
        self.asset_group = QComboBox()
        self.asset_group.currentIndexChanged.connect(
            self._asset_group_selected)
        row.addWidget(self.asset_group, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_asset_groups)
        row.addWidget(refresh)
        lay.addLayout(row)

        self.asset_table = QTableWidget(0, 5)
        self.asset_table.setHorizontalHeaderLabels(
            ["Name", "Kind", "Size", "Source", "Status"])
        self.asset_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.asset_table, 2)

        row = QHBoxLayout()
        har_btn = QPushButton("Import HAR…")
        har_btn.clicked.connect(self._import_assets_har)
        scan_btn = QPushButton("Scan a program folder…")
        scan_btn.clicked.connect(self._scan_assets_folder)
        row.addWidget(har_btn)
        row.addWidget(scan_btn)
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
        lay.addWidget(build_btn)

        row = QHBoxLayout()
        self.open_pack_btn = QPushButton("Open pack folder")
        self.open_pack_btn.setEnabled(False)
        self.open_pack_btn.clicked.connect(self._open_pack_folder)
        self.open_reblend_btn = QPushButton("Open in Reblend")
        self.open_reblend_btn.setEnabled(False)
        self.open_reblend_btn.clicked.connect(self._open_in_reblend)
        row.addWidget(self.open_pack_btn)
        row.addWidget(self.open_reblend_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.assets_log = QTextEdit()
        self.assets_log.setReadOnly(True)
        self.assets_log.setMaximumHeight(110)
        lay.addWidget(self.assets_log, 1)

        self._refresh_reblend_status()
        self._refresh_asset_groups()
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
            self.asset_table.setItem(
                i, 1, QTableWidgetItem(
                    assets.KIND_LABELS.get(e.get("kind"), e.get("kind", ""))))
            self.asset_table.setItem(i, 2, QTableWidgetItem(size_text))
            self.asset_table.setItem(i, 3, QTableWidgetItem(source))
            self.asset_table.setItem(
                i, 4, QTableWidgetItem(
                    status_map.get(e.get("sha256"), "saved")))

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
        try:
            groups = assets.extract_from_har(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "MIMIK",
                                f"could not read that HAR file: {e}")
            return
        total = sum(len(m["assets"]) for _d, m in groups)
        self.assets_log.setText(
            f"imported {total} asset(s) from {path}" if total
            else "no assets in that HAR file")
        self._refresh_asset_groups()

    def _scan_assets_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Choose a program folder to scan",
            os.path.expanduser("~"))
        if not path:
            return
        program = os.path.basename(os.path.abspath(path)) or "folder"
        gdir, manifest = assets.scan_program_tree(path, program)
        if not gdir:
            self.assets_log.setText(f"no asset files in {path}")
        else:
            self.assets_log.setText(
                f"saved {len(manifest['assets'])} asset(s) from {path}")
        self._refresh_asset_groups()

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
        try:
            result = reblend_bridge.build_pack(
                host=host, group=None if host else group,
                scale=scale)
        except Exception as e:
            QMessageBox.warning(self, "MIMIK", f"pack build failed: {e}")
            return
        self.pack_dir = result["pack_dir"]
        self.pack_scene = result["scene_path"]
        self.pack_reblend = result["reblend_path"]
        lines = [f"pack: {result['pack_dir']}"]
        if result.get("preset_applied"):
            lines.append(f"preset: used the saved scale {result['scale']}")
        if result.get("pattern_presets_applied"):
            lines.append(
                f"patterns: used the saved scale {result['scale']} from "
                f"{', '.join(result['pattern_presets_applied'])}")
        if result["scene_path"]:
            lines.append(f"scene: {result['scene_path']}")
            lines.append(f"models converted: {result['models_converted']}")
        for note in result["notes"]:
            lines.append(f"note: {note}")
        for w in result["warnings"]:
            lines.append(f"warning: {w}")
        self.assets_log.setText("\n".join(lines))
        self.open_pack_btn.setEnabled(True)
        self.open_reblend_btn.setEnabled(result["scene_path"] is not None
                                         and result["reblend_path"] is not None)
        self._asset_group_selected()

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
            "Presets are rules MIMIK learns from your captures — how an "
            "app signs in, what its endpoints look like, where its assets "
            "live. A saved preset makes the next client and the next pack "
            "smarter, with no AI involved. Presets never hold passwords "
            "or tokens — names and shapes only. Pattern presets learn "
            "the shape itself — the same sign-in flow or API style — "
            "and apply to any app that shows it."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Learn from:"))
        self.preset_source = QComboBox()
        row.addWidget(self.preset_source, 1)
        learn_btn = QPushButton("Learn preset")
        learn_btn.clicked.connect(self._learn_preset)
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
        lay.addWidget(self.preset_detail, 2)

        row = QHBoxLayout()
        delete_btn = QPushButton("Delete preset")
        delete_btn.clicked.connect(self._delete_preset)
        row.addWidget(delete_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.presets_log = QLabel("")
        self.presets_log.setWordWrap(True)
        lay.addWidget(self.presets_log)

        self._refresh_presets()
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
        self.preset_source.addItems(sorted(sources))
        self.preset_source.blockSignals(False)
        if current:
            self.preset_source.setCurrentText(current)
        self.presets_list.clear()
        for p in presets.list_presets():
            item = QListWidgetItem(
                f"{p['name']} ({p['scope']}) — {presets.summary(p)}")
            item.setData(Qt.UserRole, p["name"])
            self.presets_list.addItem(item)
        self.pattern_presets_list.clear()
        for p in presets.list_pattern_presets():
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
        name = self.preset_source.currentText().strip()
        if not name:
            QMessageBox.warning(self, "MIMIK",
                                "pick a capture to learn from first")
            return
        result = presets.learn(name)
        patterns = presets.learn_patterns(name)
        if result is None:
            self.presets_log.setText(
                f"nothing to learn from for {name} yet — capture it first")
            return
        preset, _path, created = result
        verb = "Learned" if created else "Refreshed"
        log = f"{verb} a preset for {name}: {presets.summary(preset)}"
        if patterns:
            names = ", ".join(p["name"] for p, _c in patterns)
            log += (f" Patterns learned/updated: {names}")
        self.presets_log.setText(log)
        self._refresh_presets()

    def _delete_preset(self):
        item = self.presets_list.currentItem()
        pattern_item = self.pattern_presets_list.currentItem()
        if item is None and pattern_item is None:
            QMessageBox.warning(self, "MIMIK", "select a preset first")
            return
        if pattern_item is not None:
            fid = pattern_item.data(Qt.UserRole)
            answer = QMessageBox.question(
                self, "MIMIK", f"Delete the pattern preset {fid}?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            presets.delete_pattern_preset(fid)
            self.presets_log.setText(
                f"Deleted the pattern preset {fid}.")
            self._refresh_presets()
            return
        name = item.data(Qt.UserRole)
        answer = QMessageBox.question(
            self, "MIMIK", f"Delete the preset for {name}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        presets.delete_preset(name)
        self.presets_log.setText(f"Deleted the preset for {name}.")
        self._refresh_presets()

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
        rb = reblend_bridge.find_reblend()
        lines.append(f"{'✓' if rb else '–'}  "
                     "Reblend (asset packs)"
                     + (f"  ({rb})" if rb else
                        "  (pick the folder on the Assets tab)"))
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
