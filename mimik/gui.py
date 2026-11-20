"""MIMIK desktop GUI — a native Qt window, never a web page.

One window, eight tabs: Record | Hosts and Learn | Generate | Assets |
Clients | Binary | Doctor | Presets. Dark, plain, big readable text.
Accent violet #b48fdd only where needed.
"""
import base64
import html
import os
import subprocess
import sys
import threading
import time

from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QProcess, QPropertyAnimation, QRect,
    QSettings, QSize, Qt, QThread, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QBrush, QColor, QDesktopServices, QIcon, QKeySequence,
    QPainter, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QProgressDialog, QPushButton, QScrollArea,
    QSlider, QSpinBox, QSystemTrayIcon, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from . import assets, binary, codegen, crypto, diff, doctor, export, filters, ghidra, howto, launch, log, presets, reblend_bridge, replay, sample, screenshot, store, unpin, update, websocket, wine
from . import __version__
from .flows import fetch_flows
from .recorder import ProxySession
from .sources import har
from .sources import mitm as mitm_mod

ACCENT = "#b48fdd"

# QSettings identity — explicit args, not the application name, so the
# settings location is the same whether the window came from main() or
# was constructed directly (tests, embeds).
SETTINGS_ORG = "BrokeNBored"
SETTINGS_APP = "MIMIK"


def _settings():
    """The app's QSettings (window geometry, theme — item 8, item 9)."""
    return QSettings(SETTINGS_ORG, SETTINGS_APP)

STYLESHEET = """
QMainWindow, QWidget { background: #17171c; color: #e8e8ea; font-size: 14px; }
QTabWidget::pane { border: 1px solid #33333a; }
QTabBar::tab { background: #222228; padding: 10px 18px; margin-right: 2px; }
QTabBar::tab:selected { background: #2c2c33; color: #b48fdd; }
QPushButton { background: #2c2c33; border: 1px solid #44444c; border-bottom: 2px solid #55555e; border-radius: 6px; padding: 10px 16px; }
QPushButton:hover { border-color: #b48fdd; }
QPushButton:pressed { background: #232329; border-bottom: 1px solid #44444c; }
QPushButton:disabled { color: #66666e; }
QPushButton#big { font-size: 16px; padding: 14px; background: #3a2f4a; }
QPushButton#big:pressed { background: #2e2438; }
/* v0.6 T11: visible focus ring (entry 001): violet border, quiet. */
QPushButton:focus, QToolButton:focus, QComboBox:focus, QListWidget:focus, QTableWidget:focus, QTextEdit:focus, QLineEdit:focus, QCheckBox:focus { border: 2px solid #b48fdd; }
QListWidget, QTableWidget, QTextEdit, QComboBox {
    background: #1e1e24; border: 1px solid #33333a; }
QComboBox { padding: 6px; }
QGroupBox { border: 1px solid #33333a; border-radius: 8px; margin-top: 12px; padding-top: 8px; }
QGroupBox::title { color: #b48fdd; }
QLabel#banner { background: #3a2f4a; padding: 8px 12px; font-weight: bold; }
QLabel#explain { color: #9a9aa2; font-size: 12px; }
QLabel#section { font-size: 15px; font-weight: bold; }
QLabel#wordmark { font-size: 20px; font-weight: bold; color: #b48fdd; }
QDialog { background: #232329; border: 1px solid #4a4a52; }
QDialog QLabel { background: transparent; }
QWidget#livestrip { background: #222228; }
QLabel#livedot { color: #e06c6c; font-size: 16px; }
QToolButton { background: #2c2c33; border: 1px solid #44444c; border-bottom: 2px solid #55555e; border-radius: 6px; padding: 8px 12px; text-align: left; }
QToolButton:pressed { background: #232329; border-bottom: 1px solid #44444c; }
QFrame#pad { background: #2c2c33; border: 1px solid #44444c; border-bottom: 2px solid #55555e; border-radius: 8px; }
QFrame#tile { background: #26262c; border: 1px solid #3d3d46; border-radius: 16px; }
QFrame#tile QLabel { background: transparent; }
QFrame#pad[padState="selected"] { border: 2px solid #b48fdd; background: #3a2f4a; }
QFrame#pad[padState="recording"] { border: 2px solid #e06c6c; background: #3a2f2f; }
QFrame#pad[padPressed="true"] { background: #232329; }
/* v0.6 T11: a focused pad rings near-white, so focus never reads
   as selection (violet) or recording (red). */
QFrame#pad:focus { border: 2px solid #e8e8ea; }
QLabel#padtitle { font-size: 15px; font-weight: bold; background: transparent; }
QLabel#padtitle:disabled { color: #66666e; }
QLabel#padsub { font-size: 12px; color: #9a9aa2; background: transparent; }
QLabel#padsub:disabled { color: #66666e; }
"""

LIGHT_STYLESHEET = """
QMainWindow, QWidget { background: #f2f2f4; color: #1c1c1e; font-size: 14px; }
QTabWidget::pane { border: 1px solid #c9c9cf; }
QTabBar::tab { background: #e2e2e8; padding: 10px 18px; margin-right: 2px; }
QTabBar::tab:selected { background: #ffffff; color: #6b3fa0; }
QPushButton { background: #ffffff; border: 1px solid #b9b9c0; border-bottom: 2px solid #a9a9b0; border-radius: 6px; padding: 10px 16px; }
QPushButton:hover { border-color: #b48fdd; }
QPushButton:pressed { background: #e8e8ec; border-bottom: 1px solid #b9b9c0; }
QPushButton:disabled { color: #9a9aa2; }
QPushButton#big { font-size: 16px; padding: 14px; background: #e8ddf2; }
QPushButton#big:pressed { background: #d9c8ea; }
/* v0.6 T11: visible focus ring (entry 001): violet border, quiet. */
QPushButton:focus, QToolButton:focus, QComboBox:focus, QListWidget:focus, QTableWidget:focus, QTextEdit:focus, QLineEdit:focus, QCheckBox:focus { border: 2px solid #6b3fa0; }
QListWidget, QTableWidget, QTextEdit, QComboBox {
    background: #ffffff; border: 1px solid #c9c9cf; }
QComboBox { padding: 6px; }
QGroupBox { border: 1px solid #c9c9cf; border-radius: 8px; margin-top: 12px; padding-top: 8px; }
QGroupBox::title { color: #6b3fa0; }
QLabel#banner { background: #e8ddf2; padding: 8px 12px; font-weight: bold; }
QLabel#explain { color: #66666e; font-size: 12px; }
QLabel#section { font-size: 15px; font-weight: bold; }
QLabel#wordmark { font-size: 20px; font-weight: bold; color: #6b3fa0; }
QDialog { background: #ffffff; border: 1px solid #b9b9c0; }
QDialog QLabel { background: transparent; }
QWidget#livestrip { background: #e2e2e8; }
QLabel#livedot { color: #c0392b; font-size: 16px; }
QToolButton { background: #ffffff; border: 1px solid #b9b9c0; border-bottom: 2px solid #a9a9b0; border-radius: 6px; padding: 8px 12px; text-align: left; }
QToolButton:pressed { background: #e8e8ec; border-bottom: 1px solid #b9b9c0; }
QFrame#pad { background: #ffffff; border: 1px solid #b9b9c0; border-bottom: 2px solid #a9a9b0; border-radius: 8px; }
QFrame#tile { background: #f4f4f6; border: 1px solid #d0d0d6; border-radius: 16px; }
QFrame#tile QLabel { background: transparent; }
QFrame#pad[padState="selected"] { border: 2px solid #6b3fa0; background: #e8ddf2; }
QFrame#pad[padState="recording"] { border: 2px solid #c0392b; background: #f2dddd; }
QFrame#pad[padPressed="true"] { background: #e8e8ec; }
/* v0.6 T11: a focused pad rings near-black, so focus never reads
   as selection (violet) or recording (red). */
QFrame#pad:focus { border: 2px solid #1c1c1e; }
QLabel#padtitle { font-size: 15px; font-weight: bold; background: transparent; }
QLabel#padtitle:disabled { color: #9a9aa2; }
QLabel#padsub { font-size: 12px; color: #66666e; background: transparent; }
QLabel#padsub:disabled { color: #9a9aa2; }
"""

HIGH_CONTRAST_STYLESHEET = """
QMainWindow, QWidget { background: #000000; color: #ffffff; font-size: 14px; }
QTabWidget::pane { border: 2px solid #ffffff; }
QTabBar::tab { background: #000000; color: #ffffff; padding: 10px 18px; margin-right: 2px; }
QTabBar::tab:selected { background: #ffffff; color: #000000; }
QPushButton { background: #000000; border: 2px solid #ffffff; border-radius: 6px; padding: 10px 16px; }
QPushButton:hover { border-color: #ffff00; }
QPushButton:pressed { background: #333333; }
QPushButton:disabled { color: #808080; }
QPushButton#big { font-size: 16px; padding: 14px; background: #000000; }
QPushButton#big:pressed { background: #333333; }
/* v0.6 T13: high-contrast focus ring: yellow, 2 px. */
QPushButton:focus, QToolButton:focus, QComboBox:focus, QListWidget:focus, QTableWidget:focus, QTextEdit:focus, QLineEdit:focus, QCheckBox:focus { border: 2px solid #ffff00; }
QListWidget, QTableWidget, QTextEdit, QComboBox {
    background: #000000; border: 2px solid #ffffff; }
QComboBox { padding: 6px; }
QGroupBox { border: 2px solid #ffffff; border-radius: 8px; margin-top: 12px; padding-top: 8px; }
QGroupBox::title { color: #ffff00; }
QLabel#banner { background: #ffffff; color: #000000; padding: 8px 12px; font-weight: bold; }
QLabel#explain { color: #e0e0e0; font-size: 12px; }
QLabel#section { font-size: 15px; font-weight: bold; }
QLabel#wordmark { font-size: 20px; font-weight: bold; color: #ffff00; }
QDialog { background: #000000; border: 2px solid #ffffff; }
QDialog QLabel { background: transparent; }
QWidget#livestrip { background: #000000; }
QLabel#livedot { color: #ff6b6b; font-size: 16px; }
QToolButton { background: #000000; border: 2px solid #ffffff; border-radius: 6px; padding: 8px 12px; text-align: left; }
QToolButton:pressed { background: #333333; }
QFrame#pad { background: #000000; border: 2px solid #ffffff; border-radius: 8px; }
QFrame#tile { background: #000000; border: 2px solid #ffffff; border-radius: 16px; }
QFrame#tile QLabel { background: transparent; }
QFrame#pad[padState="selected"] { border: 2px solid #ffff00; background: #1a1a1a; }
QFrame#pad[padState="recording"] { border: 2px solid #ff6b6b; background: #1a0000; }
QFrame#pad[padPressed="true"] { background: #333333; }
/* v0.6 T13: a focused pad rings white, so focus never reads as
   selection (yellow) or recording (red). */
QFrame#pad:focus { border: 2px solid #ffffff; }
QLabel#padtitle { font-size: 15px; font-weight: bold; background: transparent; }
QLabel#padtitle:disabled { color: #808080; }
QLabel#padsub { font-size: 12px; color: #e0e0e0; background: transparent; }
QLabel#padsub:disabled { color: #808080; }
"""

THEMES = ("dark", "light")


def current_theme():
    """The saved theme name ("dark" default — the theme before v0.4)."""
    theme = _settings().value("theme", "dark")
    return theme if theme in THEMES else "dark"


def theme_stylesheet(name):
    """The stylesheet for a theme name (unknown names get dark)."""
    return LIGHT_STYLESHEET if name == "light" else STYLESHEET


# Accessibility presets (v0.6 T13, entry 015): one tap for a whole
# comfort setup. Each is its own setting — a preset is a starting
# point, not a wall, so every one stays adjustable on its own.
ACCESS_PRESETS = (
    ("big_text", "Big text"),
    ("high_contrast", "High contrast"),
    ("reduced_motion", "Reduced motion"),
    ("one_touch", "One-touch capture"),
)


def access_setting(key):
    """An accessibility setting (v0.6 T13): all off by default."""
    return _settings().value(f"access/{key}", False, type=bool)


def set_access_setting(key, value):
    """Persist one accessibility setting."""
    _settings().setValue(f"access/{key}", bool(value))


def effective_stylesheet():
    """The stylesheet to apply: the high-contrast sheet when that
    preset is on, else the current theme — with big text applied
    on top when that preset is on (v0.6 T13)."""
    if access_setting("high_contrast"):
        sheet = HIGH_CONTRAST_STYLESHEET
    else:
        sheet = theme_stylesheet(current_theme())
    if access_setting("big_text"):
        sheet = sheet.replace("font-size: 14px", "font-size: 18px")
        sheet = sheet.replace("font-size: 12px", "font-size: 15px")
    return sheet

# Record-tab instructions, worded for the GUI (item 21): they point at
# tabs and buttons, never at terminal commands. The CLI keeps its own
# wording in cli.py (ANDROID_STEPS etc.) — a terminal user IS in a
# terminal, so `mimik hosts` is right there and wrong here.
GUI_ANDROID_STEPS = """
Android phone capture: do this once, then just reopen the app to add traffic:

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

  Android 7+ apps only trust user-installed CAs if the app opts in, so some
  apps show no traffic because of that, and pinned apps (banks, Instagram)
  show none at all. If the Hosts and Learn tab shows the app's API host,
  you're good.
"""

GUI_LINUX_APP_STEPS = """
Linux app capture: run the app through the proxy:

  Launch it yourself with the proxy env vars, for example:

      http_proxy=http://127.0.0.1:8080 https_proxy=http://127.0.0.1:8080 <app>

  Or let MIMIK launch it: type the app's command in the
  "Linux app to launch" box above, then press Start proxy.

  The app's own CA trust matters: apps using the system store need the
  mitmproxy CA installed (see README); apps with their own store may refuse.
  If the Hosts and Learn tab shows the app's API host, you're good.
"""

GUI_WEB_STEPS = """
Web app capture: no proxy needed:

  1. Browser → DevTools (F12) → Network → use the web app normally
  2. Right-click the network log → Save all as HAR → save session.har
  3. On the Generate tab, press Import HAR… and pick session.har,
     then pick the host and press Generate client.

  Or copy one request as cURL (right-click → Copy → Copy as cURL)
  and press Import cURL… on the Generate tab.
"""

GUI_WINDOWS_STEPS = """
Windows program capture (via WinLinx): MIMIK captures, WinLinx runs:

  1. Launch the Windows program in WinLinx (it owns running Windows programs)
  2. MIMIK points its Wine prefix at this proxy and installs the mitmproxy
     CA into the prefix, so the program's HTTPS traffic can be captured
  3. Use the program normally: its API traffic lands in MIMIK
  4. Stop here restores the prefix's original proxy settings

  Some Windows programs ignore the system proxy or pin their certificates,
  those show no traffic, same as pinned phone apps.
"""

# Capture pads (v0.6 T16, entry 024): one pad per target — tap it
# and it starts. The subtitle is the plain-words line (U20) for
# that pad. They replace the v0.5 target combo (Joshua's call,
# 2026-11-13).
PAD_TARGETS = [
    # v0.8 B5: every subtitle names what a tap DOES — the pads read
    # as selectors, and nothing else on the tab said tapping starts
    # a recording (the Web pad is the odd one out: it only shows the
    # HAR/cURL steps, no proxy starts, and now it says so).
    ("android", "Android phone", "Your phone, over Wi-Fi. Tap to start"),
    ("linux-app", "Linux app", "An app installed on this PC. Tap to start"),
    ("file", "Program from file", "AppImage, archive, ISO, or .deb. Tap to choose"),
    ("web", "Web app", "Via HAR or cURL, no proxy. Tap to see how"),
    ("windows", "Windows program", "Already running in WinLinx. Tap to start"),
]

# First-run checklist (v0.6 T3, entry 020): four steps to the first
# client. "picked" starts ticked — installing and opening the app
# were real work (entry 020: the first step is already ticked,
# honestly). The rest tick only when they really happen.
_CHECKLIST_STEPS = [
    ("picked", "Pick a source"),
    ("started", "Start the proxy"),
    ("host_seen", "First host seen"),
    ("generated", "Generate your first client"),
]


def _fmt_elapsed(seconds):
    """03:24 for under an hour, 1:03:24 beyond it."""
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _fmt_bytes(nbytes):
    """512 B, 1.5 KB, 3.4 MB — one decimal, no trailing .0."""
    nbytes = max(0, int(nbytes))
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        value = nbytes / 1024
        unit = "KB"
    else:
        value = nbytes / (1024 * 1024)
        unit = "MB"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text} {unit}"


def _dir_size(path):
    """Total bytes under a directory. Never raises (0 on error) —
    the live strip must not die on a missing or unreadable dir."""
    total = 0
    try:
        for _root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(
                        os.path.join(_root, name))
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def _empty_item(text):
    """A non-selectable placeholder row for an empty QListWidget —
    guidance instead of a blank black box. NoItemFlags means it can
    never become current, so selection handlers never see it."""
    item = QListWidgetItem(text)
    item.setFlags(Qt.NoItemFlags)
    return item


def _snapshot_list(widget):
    """(v0.6 T5) A QListWidget's place before a refill: the selected
    item's UserRole data + the scrollbar value."""
    item = widget.currentItem()
    data = item.data(Qt.UserRole) if item is not None else None
    return (data, widget.verticalScrollBar().value())


def _restore_list(widget, snap):
    """Restore a _snapshot_list snapshot after a refill. A selection
    whose item is gone stays gone (no guessing); the scrollbar
    clamps to the new content."""
    data, scroll = snap
    if data is not None:
        for i in range(widget.count()):
            if widget.item(i).data(Qt.UserRole) == data:
                widget.setCurrentRow(i)
                break
    widget.verticalScrollBar().setValue(scroll)


def _snapshot_table(widget):
    """(v0.6 T5) A QTableWidget's place before a refill: the selected
    row's column-0 UserRole data + the scrollbar value."""
    row = widget.currentRow()
    item = widget.item(row, 0) if row >= 0 else None
    data = item.data(Qt.UserRole) if item is not None else None
    return (data, widget.verticalScrollBar().value())


def _restore_table(widget, snap, key=None):
    """Restore a _snapshot_table snapshot after a refill. `key`
    picks the identity out of the column-0 UserRole data (a dict
    key); without it the whole data value is compared."""
    data, scroll = snap
    if data is not None:
        want = data.get(key) if key else data
        for i in range(widget.rowCount()):
            item = widget.item(i, 0)
            if item is None:
                continue
            got = item.data(Qt.UserRole)
            if key and isinstance(got, dict):
                got = got.get(key)
            if got == want:
                widget.setCurrentCell(i, 0)
                break
    widget.verticalScrollBar().setValue(scroll)


class _SeenItem(QTableWidgetItem):
    """A Seen-column cell (v0.6 T6): sorts by the count, not the
    text, so 10 sorts after 2."""

    def __lt__(self, other):
        try:
            return int(self.text()) < int(other.text())
        except ValueError:
            return super().__lt__(other)


def _preview_auth(headers):
    """(v0.6 T7) The auth shape for the Generate preview: scheme
    words and header NAMES only, never values."""
    scheme = None
    cookie = False
    api_key = False
    for pair in headers:
        name, value = pair[0], pair[1] if len(pair) > 1 else ""
        low = name.lower()
        if low == "authorization" and scheme is None:
            words = (value or "").split(None, 1)
            scheme = words[0].lower() if words else "authorization"
        elif low == "cookie":
            cookie = True
        elif "api" in low and "key" in low:
            api_key = True
    if scheme:
        return scheme
    if cookie:
        return "cookie"
    if api_key:
        return "api key"
    return None


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


def _explain_label(text):
    """A small plain-words explanation under a field (U20)."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("explain")
    return label


def _addr_int(text):
    """A Ghidra address string ("0x00401000", leading zeros and all)
    as an int, or None when it does not parse. Ghidra pads addresses;
    the stdlib side does not — comparing ints is the only honest
    match."""
    try:
        return int(text, 16)
    except (TypeError, ValueError):
        return None


def _string_vaddr(offset, sections):
    """A stdlib string's file offset as a virtual address, via the
    section containing it. None when no section covers the offset."""
    for s in sections:
        if s["offset"] <= offset < s["offset"] + s["size"]:
            return s["address"] + (offset - s["offset"])
    return None


def _ws_message_line(message):
    """One WS message as a transcript line (item 13): the text, or a
    byte count for binary."""
    if message.get("b64") is not None:
        try:
            n = len(base64.b64decode(message["b64"]))
        except (ValueError, TypeError):
            return "<binary, undecodable>"
        return f"<{n} bytes binary>"
    return message.get("text", "")


class RecordWorker(QThread):
    """Runs the proxy (+ launched program / Wine proxy) off the UI thread."""

    hosts_updated = Signal(list)
    status = Signal(str)
    finished = Signal()
    # v0.6 T9: the capture filter's dropped hosts, when the set
    # changes (a sorted list; empty when nothing is dropped).
    dropped_updated = Signal(list)

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
                                "file(s) in the program, saved for the "
                                "asset pack"
                                + (f" ({note})" if note else ""))
                    except Exception as e:
                        self.status.emit(f"asset scan skipped: {e}")

            session = ProxySession(
                meta=meta,
                on_update=self._hosts_updated,
                on_dropped=self.dropped_updated.emit,
                # v0.6 T8: the record honors this target's own
                # filter choice, not just the global value.
                target=self.target,
            )
            self._session = session
            try:
                session.start()
            except mitm_mod.MitmError as e:
                self.status.emit(str(e))
                return
            self.status.emit("proxy up, capturing")

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


def _icon_files():
    """Candidate icon files, best first: installed hicolor PNGs, the
    installed SVG, then the source tree's PNGs + SVG (dev runs from
    the tree, where the installed icon doesn't exist yet)."""
    home = os.path.expanduser("~")
    hicolor = os.path.join(home, ".local", "share", "icons", "hicolor")
    tree = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "icons")
    files = []
    for size in (256, 128, 48):
        files.append((os.path.join(hicolor, f"{size}x{size}", "apps",
                                   "mimik.png"), size))
    files.append((os.path.join(hicolor, "scalable", "apps", "mimik.svg"),
                  None))
    for size in (256, 128, 48):
        files.append((os.path.join(tree, f"mimik-{size}.png"), size))
    files.append((os.path.join(tree, "mimik.svg"), None))
    return [(p, s) for p, s in files if os.path.exists(p)]


def window_icon():
    """The MIMIK window icon — every available size, so the window
    decoration, taskbar and Alt-Tab all get a sharp icon. PNG when
    present, SVG fallback (QIcon renders SVGs via QtSvg). Empty when
    no icon file exists anywhere."""
    icon = QIcon()
    for path, size in _icon_files():
        if size:
            icon.addFile(path, QSize(size, size))
        else:
            icon.addFile(path)
    return icon


_TRAY_COLORS = {"idle": "#9a9aa2", "recording": "#e06c6c"}


def _tray_pixmap(state):
    """The tray icon (v0.6 T2, entry 021): one colored dot, the
    recording state at a glance. Grey = idle, red = recording. Drawn
    in code, not a new artwork file — the numbers live in the
    tooltip, the dot is the glance."""
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QBrush(QColor(_TRAY_COLORS.get(state, "#9a9aa2"))))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(4, 4, 24, 24)
    painter.end()
    return pix


class _PadButton(QFrame):
    """One capture pad (v0.6 T16, entry 024): a big tappable tile
    with a title + plain-words subtitle. A real button's semantics
    (click, Space/Enter, focus) on a tile's layout — QPushButton
    can't render two text sizes, so the pad is a frame with two
    labels. States (entry 024: every pad needs a clear state):
    idle, selected, recording — via the padState property, which
    the stylesheet reads. Pressed feedback is a static style
    change, not an animation (no motion cost, nothing for reduced
    motion to switch off)."""

    clicked = Signal()

    def __init__(self, key, title, subtitle, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("pad")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setProperty("padState", "idle")
        self.setProperty("padPressed", False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("padtitle")
        self.title_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("padsub")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(self.subtitle_label)

    def set_state(self, state):
        """idle | selected | recording — re-polish so the
        stylesheet's [padState="..."] rule re-applies."""
        self.setProperty("padState", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _set_pressed(self, pressed):
        self.setProperty("padPressed", pressed)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._set_pressed(True)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._set_pressed(False)
        if (event.button() == Qt.LeftButton and self.isEnabled()
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"MIMIK {__version__}: any app, as a Python library")
        self.setWindowIcon(window_icon())
        self.setStyleSheet(effective_stylesheet())
        self.resize(900, 650)  # 620 + room for the status bar (item 7)
        self._restore_geometry()  # reopens where it was left (item 8)
        self.worker = None
        self.plan = None
        self.file_path = None
        # v0.6 T9: hosts the capture filter dropped in this session.
        self._dropped_hosts = []
        self.har_path = None
        self.curl_path = None
        self.gen_out = None
        self.pack_dir = None
        self.pack_scene = None
        self.pack_reblend = None
        # Background-task state (item 4): one task at a time, cached
        # flows, tabs that load their data on first show.
        self._flows_cache = None
        # v0.6 T14: the timeline's range for the selected host
        # (None until a host with timed endpoints loads).
        self._timeline_min = None
        self._timeline_max = None
        self._task = None
        self._task_on_done = None
        # v0.6 T10: the polish-availability probe thread (its own
        # thread, not the single _run_task slot).
        self._polish_task = None
        # v0.7: the update check/download thread (its own thread, not
        # the single _run_task slot, like the polish probe).
        self._update_task = None
        self._update_phase = None  # "check" or "download"
        self._update_silent = False  # the launch check says nothing
        self._update_result = None  # the last check's result, while
        # its dialog or download is up
        self._update_progress = None  # the download progress dialog
        self._update_check_ran = False  # the launch check runs once
        self._restart_script = None  # set when the user says yes to
        # restarting for an update; launched from closeEvent once the
        # close is definitely going ahead
        # v0.7: a note main() leaves for the window (a staged update
        # that did not verify, or a failed install) — shown once,
        # after the window is up.
        self._startup_update_note = None
        self._startup_update_failed = False
        self._task_disable = []
        self._pending_host = None
        self._loaded = set()
        self._close_pending = False
        # Hosts → Generate jump (item 10): the host to pick once the
        # Generate tab's host combo has loaded.
        self._gen_jump_host = None
        # v0.6 T7: the Generate host combo starts unselected on its
        # first load only — refreshes keep the picked host.
        self._gen_hosts_loaded_once = False
        # The latest asset preview dialog (item 10).
        self._preview_dialog = None
        # Live strip (item 19): built by _record_tab below; None
        # until then, so _update_status_bar can run before the tab
        # exists. MUST init here, not in the status-bar block —
        # that runs after the tabs and would overwrite the strip.
        self.live_strip = None
        self._record_bytes_start = 0
        # Micro-interactions (item 20): the settled completion text
        # the status bar shows after a record/generate finishes, and
        # one press-dip animation per button, reused across presses.
        self._done_text = None
        self._dip_anims = {}
        # Tray icon / status chip (v0.6 T2): the out-of-window
        # recording surface. Set up at the end of __init__ (after
        # the status bar exists); None until then, so
        # _update_status_bar can call _update_tray safely.
        self.tray_icon = None
        self._tray_state = None
        self.status_chip = None
        self._chip_label = None

        tabs = QTabWidget()
        tabs.addTab(self._record_tab(), "Record")
        tabs.addTab(self._hosts_tab(), "Hosts and Learn")
        tabs.addTab(self._generate_tab(), "Generate")
        tabs.addTab(self._assets_tab(), "Assets")
        tabs.addTab(self._clients_tab(), "Clients")
        tabs.addTab(self._binary_tab(), "Binary")
        tabs.addTab(self._doctor_tab(), "Doctor")
        tabs.addTab(self._presets_tab(), "Presets")
        self.tabs = tabs
        tabs.currentChanged.connect(self._tab_shown)
        self._build_menus()
        # v0.6 T11: F9 starts or stops recording from the keyboard.
        # No conflict: the menus use Ctrl+R / Ctrl+Q / Ctrl+1..8 /
        # F5, and nothing else claims F9.
        self._record_toggle_shortcut = QShortcut(
            QKeySequence("F9"), self)
        self._record_toggle_shortcut.activated.connect(
            self._toggle_recording)

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
        # Tray icon / status chip (v0.6 T2, entry 021): the recording
        # state visible from another app. Tray when the desktop has
        # one; a tiny always-on-top chip when it doesn't.
        self._setup_tray()
        self._update_tray()
        self._apply_accessible_names()

    def _apply_accessible_names(self):
        """v0.8 G8: screen readers had almost nothing to work with
        (zero accessible names in the whole window). Give every
        button and checkbox its visible label, every line edit its
        placeholder, and every tab page its tab title, wherever a
        name was not set by hand."""
        for btn in self.findChildren(QPushButton):
            if not btn.accessibleName() and btn.text():
                btn.setAccessibleName(btn.text().replace("&", ""))
        for box in self.findChildren(QCheckBox):
            if not box.accessibleName() and box.text():
                box.setAccessibleName(box.text().replace("&", ""))
        for edit in self.findChildren(QLineEdit):
            if not edit.accessibleName() and edit.placeholderText():
                edit.setAccessibleName(edit.placeholderText())
        self.tabs.setAccessibleName("Main sections")
        for i in range(self.tabs.count()):
            page = self.tabs.widget(i)
            if page is not None and not page.accessibleName():
                page.setAccessibleName(
                    self.tabs.tabText(i).replace("&", ""))

    def _setup_tray(self):
        """The out-of-window recording surface (v0.6 T2). One fact,
        one action — not a mini-app: the tray icon shows the state,
        a click stops a recording (or shows the window when idle).
        No context menu on purpose (entry 021: the moment a
        glanceable surface grows a menu it has become a mini-app)."""
        if QSystemTrayIcon.isSystemTrayAvailable():
            tray = QSystemTrayIcon(self)
            tray.setIcon(QIcon(_tray_pixmap("idle")))
            tray.setToolTip("MIMIK: Proxy stopped")
            tray.activated.connect(self._tray_activated)
            tray.show()
            self.tray_icon = tray
            return
        # No tray on this desktop: a tiny always-on-top chip instead,
        # shown only while recording. A top-level window does not
        # inherit the window's stylesheet, so the chip carries its
        # own (kept in step by set_theme).
        chip = QWidget(self, Qt.Tool | Qt.WindowStaysOnTopHint
                       | Qt.FramelessWindowHint)
        chip.setObjectName("livestrip")
        chip.setStyleSheet(effective_stylesheet())
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(10, 6, 10, 6)
        dot = QLabel("●")
        dot.setObjectName("livedot")
        lay.addWidget(dot)
        self._chip_label = QLabel("")
        lay.addWidget(self._chip_label, 1)
        stop = QPushButton("Stop")
        stop.clicked.connect(self._stop_recording)
        lay.addWidget(stop)
        chip.setVisible(False)
        self.status_chip = chip

    def _update_tray(self):
        """Sync the tray icon / chip with the recording state. The
        icon is rebuilt only when the state changes; the tooltip (or
        chip text) updates every tick with live numbers."""
        recording = (self.worker is not None
                     and self._record_started is not None)
        state = "recording" if recording else "idle"
        if recording:
            elapsed = time.monotonic() - self._record_started
            flows = "flow" if self._flow_count == 1 else "flows"
            hosts = "host" if self._host_count == 1 else "hosts"
            text = (f"MIMIK: Recording {_fmt_elapsed(elapsed)}, "
                    f"{self._flow_count} {flows} from "
                    f"{self._host_count} {hosts}. Click to stop.")
            chip_text = (f"Recording {_fmt_elapsed(elapsed)}  ·  "
                         f"{self._flow_count} {flows}")
        else:
            text = "MIMIK: Proxy stopped. Click to show MIMIK."
            chip_text = ""
        if self.tray_icon is not None:
            if state != self._tray_state:
                self.tray_icon.setIcon(QIcon(_tray_pixmap(state)))
                self._tray_state = state
            self.tray_icon.setToolTip(text)
        if self.status_chip is not None:
            if recording:
                self._chip_label.setText(chip_text)
                if not self.status_chip.isVisible():
                    self._place_chip()
                    self.status_chip.setVisible(True)
            else:
                self.status_chip.setVisible(False)

    def _place_chip(self):
        """Bottom-right of the primary screen, 12 px off the edges."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        self.status_chip.adjustSize()
        area = screen.availableGeometry()
        size = self.status_chip.size()
        self.status_chip.move(area.right() - size.width() - 12,
                              area.bottom() - size.height() - 12)

    def _tray_activated(self, reason):
        """Tray click (v0.6 T2): one action — Stop while recording,
        show the window when idle. Both Trigger and DoubleClick:
        some desktops send one, some the other."""
        if reason not in (QSystemTrayIcon.Trigger,
                          QSystemTrayIcon.DoubleClick):
            return
        if self.worker is not None:
            self._stop_recording()
        else:
            self.bring_to_front()

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
                f"● Recording: {_fmt_elapsed(elapsed)}")
            flows = "flow" if self._flow_count == 1 else "flows"
            hosts = "host" if self._host_count == 1 else "hosts"
            self.status_flows.setText(
                f"{self._flow_count} {flows} · {self._host_count} {hosts}")
            if self.live_strip is not None:
                # Bytes are the captures-dir size delta since record
                # start — real numbers from the store, never invented.
                captured = max(
                    0, _dir_size(store.captures_dir())
                    - self._record_bytes_start)
                self.live_text.setText(
                    f"{self._flow_count} {flows} · "
                    f"{self._host_count} {hosts} · "
                    f"{_fmt_bytes(captured)} · "
                    f"{_fmt_elapsed(elapsed)}")
                self.live_strip.setVisible(True)
        else:
            # Quiet completion state (item 20): after a record or a
            # generate finishes, the status bar settles on what was
            # done until the next recording starts. No modal, no
            # popup — just the line.
            self.status_proxy.setText(self._done_text or "Proxy stopped")
            flows = "flow" if self._saved_flows == 1 else "flows"
            text = f"{self._saved_flows} saved {flows}"
            # v0.8 G1: while locked, captures read as zero — say
            # "locked", never a fake empty store.
            if store.encryption_enabled() and not store.unlocked():
                text = "Captures locked: File › Unlock captures…"
            # v0.8 B7: unreadable capture files are named in the
            # status bar instead of vanishing silently.
            bad = store.unreadable_captures()
            if bad:
                text += (f" · {len(bad)} unreadable capture "
                         f"file{'s' if len(bad) != 1 else ''} skipped")
            self.status_flows.setText(text)
            if self.live_strip is not None:
                self.live_strip.setVisible(False)
        self._update_tray()

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
        if self._polish_task is not None:
            # v0.6 T10: the probe is bounded (a 3 s Ollama timeout
            # at most) — wait it out so the thread never outlives
            # the window.
            self._polish_task.wait(5000)
            self._polish_task = None
        if self._update_task is not None:
            # v0.7: an update check or download is running — cancel it
            # and wait briefly; a blocking download defers the close
            # until it ends (_update_task_finished closes then). The
            # cancel flag also suppresses the update dialog, so a
            # check that finished mid-close never pops up over a
            # window the user is closing.
            self._update_task.request_cancel()
            task = self._update_task
            if task is not None and not task.wait(2000):
                self._close_pending = True
                event.ignore()
                return
            QApplication.instance().processEvents()
            if self._update_task is not None:
                self._close_pending = True
                event.ignore()
                return
        worker = self.worker
        if worker is not None and worker.isRunning():
            if not self._confirm_stop_recording():
                event.ignore()
                return
            self._take_stop_screenshot()
            worker.force_stop()
        # The close is going ahead — remember where the window was.
        _settings().setValue("geometry", self.saveGeometry())
        if self.tray_icon is not None:
            self.tray_icon.hide()
        if self._restart_script is not None:
            # v0.7: the user said yes to restarting for an update, and
            # the close is going ahead — launch the updater now. It
            # waits for this process to exit, installs the staged zip,
            # and relaunches MIMIK.
            script = self._restart_script
            self._restart_script = None
            QProcess.startDetached(script)
        event.accept()

    def _restore_geometry(self):
        """Reopen where the window was left (size + position). A saved
        geometry entirely off the current screen (monitor changed)
        falls back to the default size at a sane position — a window
        restored off-screen is a lost window."""
        geometry = _settings().value("geometry")
        if geometry is None:
            return
        try:
            restored = self.restoreGeometry(geometry)
        except (TypeError, ValueError):
            return
        if not restored:
            return
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            if not available.intersects(self.frameGeometry()):
                self.resize(900, 650)
                self.move(100, 100)

    def check_stale_wine_state(self):
        """A saved Wine proxy state means a capture ended without
        restoring the prefix — warn and offer to restore it now."""
        if not wine.has_saved_state():
            return
        answer = QMessageBox.question(
            self, "MIMIK: Wine proxy settings",
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
                 "binary", "doctor", "presets")

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
                self.progress_label.text() + ", cancelling…")

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
        """File / Record / Generate / View / Help menus + shortcuts
        (item 21, U38). All actions connect to bound methods — no
        lambdas on signals (heap rule). Every app-level action is
        reachable from a menu; per-selection contextual actions
        (delete a host, replay a flow, link a binary, preview an
        asset) stay on their tabs — they need a selection to act
        on, so a menu item for them could do nothing useful."""
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        self._file_menu = file_menu
        import_har = QAction("Import HAR…", self)
        import_har.triggered.connect(self._import_har)
        file_menu.addAction(import_har)
        import_curl = QAction("Import cURL…", self)
        import_curl.triggered.connect(self._import_curl)
        file_menu.addAction(import_curl)
        scan_act = QAction("Scan a program folder…", self)
        scan_act.triggered.connect(self._menu_scan_folder)
        file_menu.addAction(scan_act)
        compare_act = QAction("Compare captures…", self)
        compare_act.triggered.connect(self._compare_captures)
        file_menu.addAction(compare_act)
        self._compare_action = compare_act
        unpin_act = QAction("Unpin a pinned app…", self)
        unpin_act.triggered.connect(self._unpin_dialog)
        file_menu.addAction(unpin_act)
        self._unpin_action = unpin_act
        file_menu.addSeparator()
        analyze_act = QAction("Analyze a binary…", self)
        analyze_act.triggered.connect(self._menu_analyze_binary)
        file_menu.addAction(analyze_act)
        ghidra_act = QAction("Ghidra setup steps…", self)
        ghidra_act.triggered.connect(self._show_ghidra_setup)
        file_menu.addAction(ghidra_act)
        sample_act = QAction("Load sample capture", self)
        sample_act.triggered.connect(self._menu_load_sample)
        file_menu.addAction(sample_act)
        file_menu.addSeparator()
        # Capture encryption (v0.8 G1): unlock for this session, or
        # turn encryption on/off. Their labels/states follow the
        # store (see _refresh_encryption_actions).
        unlock_act = QAction("Unlock captures…", self)
        unlock_act.triggered.connect(self._unlock_captures)
        file_menu.addAction(unlock_act)
        self._unlock_action = unlock_act
        encrypt_act = QAction("Encrypt captures…", self)
        encrypt_act.triggered.connect(self._encrypt_captures_toggle)
        file_menu.addAction(encrypt_act)
        self._encrypt_action = encrypt_act
        self._refresh_encryption_actions()
        file_menu.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)
        self._quit_action = quit_act

        record_menu = menubar.addMenu("Record")
        self._record_menu = record_menu
        start_act = QAction("Start recording", self)
        start_act.setShortcut("Ctrl+R")
        start_act.triggered.connect(self._menu_start_recording)
        record_menu.addAction(start_act)
        self._menu_start_act = start_act
        stop_act = QAction("Stop recording", self)
        stop_act.setEnabled(False)
        stop_act.triggered.connect(self._menu_stop_recording)
        record_menu.addAction(stop_act)
        self._menu_stop_act = stop_act
        record_menu.addSeparator()
        filter_act = QAction("Exclude telemetry + analytics hosts", self)
        filter_act.setCheckable(True)
        filter_act.setChecked(self.filter_check.isChecked())
        filter_act.triggered.connect(self._menu_toggle_filter)
        record_menu.addAction(filter_act)
        self._menu_filter_act = filter_act
        shot_act = QAction(
            "Screenshot the desktop when recording stops", self)
        shot_act.setCheckable(True)
        shot_act.setChecked(self.screenshot_check.isChecked())
        shot_act.triggered.connect(self._menu_toggle_screenshot)
        record_menu.addAction(shot_act)
        self._menu_shot_act = shot_act

        gen_menu = menubar.addMenu("Generate")
        self._generate_menu = gen_menu
        gen_act = QAction("Generate client", self)
        gen_act.triggered.connect(self._menu_generate_client)
        gen_menu.addAction(gen_act)
        pack_act = QAction("Build asset pack", self)
        pack_act.triggered.connect(self._menu_build_pack)
        gen_menu.addAction(pack_act)
        learn_act = QAction("Learn preset", self)
        learn_act.triggered.connect(self._menu_learn_preset)
        gen_menu.addAction(learn_act)

        view_menu = menubar.addMenu("View")
        self._view_menu = view_menu
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
        view_menu.addSeparator()
        dark_act = QAction("Dark theme", self)
        dark_act.setCheckable(True)
        dark_act.setChecked(current_theme() == "dark")
        dark_act.triggered.connect(self._menu_toggle_theme)
        view_menu.addAction(dark_act)
        self._dark_theme_action = dark_act
        view_menu.addSeparator()
        # v0.6 T13: accessibility presets, one tap each (entry 015).
        access_menu = view_menu.addMenu("Accessibility")
        self._access_actions = {}
        for key, label in ACCESS_PRESETS:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(access_setting(key))
            act.setData(key)
            act.triggered.connect(self._access_action_triggered)
            access_menu.addAction(act)
            self._access_actions[key] = act

        help_menu = menubar.addMenu("Help")
        self._help_menu = help_menu
        howto_act = QAction("How to Use…", self)
        howto_act.triggered.connect(self._show_howto)
        help_menu.addAction(howto_act)
        self._howto_action = howto_act
        checklist_act = QAction("Show the first-run checklist", self)
        checklist_act.triggered.connect(self._checklist_show)
        help_menu.addAction(checklist_act)
        self._checklist_action = checklist_act
        about_act = QAction("About MIMIK", self)
        about_act.triggered.connect(self._show_about)
        help_menu.addAction(about_act)
        self._about_action = about_act
        log_act = QAction("View log", self)
        log_act.triggered.connect(self._show_log)
        help_menu.addAction(log_act)
        debug_act = QAction("Copy debug report", self)
        debug_act.triggered.connect(self._copy_debug_report)
        help_menu.addAction(debug_act)
        self._debug_action = debug_act
        update_act = QAction("Check for updates…", self)
        update_act.triggered.connect(self._check_for_updates)
        help_menu.addAction(update_act)
        self._update_action = update_act
        # v0.7: the silent launch check, on by default. Untick to stop
        # MIMIK checking on startup; the manual check still works.
        startup_act = QAction("Check for updates on startup", self)
        startup_act.setCheckable(True)
        startup_act.setChecked(
            _settings().value("update/check_on_startup", True, type=bool))
        startup_act.triggered.connect(self._toggle_startup_update_check)
        help_menu.addAction(startup_act)
        self._startup_update_action = startup_act

    def _menu_toggle_theme(self):
        """View menu theme toggle — the action's checked state is the theme."""
        action = self.sender()
        checked = action.isChecked() if action is not None else True
        self.set_theme("dark" if checked else "light")

    # ---- menu actions (item 21) -------------------------------------------
    # Each action goes to the tab that owns it, then runs it when its
    # inputs are already there; otherwise it lands on the tab, whose
    # guidance says what to pick. No action warns about a missing
    # input the tab is still loading.

    def _menu_start_recording(self):
        """Record menu: go to the Record tab and start."""
        self.tabs.setCurrentIndex(self._TAB_KEYS.index("record"))
        self._start_recording()

    def _menu_stop_recording(self):
        """Record menu: go to the Record tab and stop."""
        self.tabs.setCurrentIndex(self._TAB_KEYS.index("record"))
        self._stop_recording()

    def _menu_toggle_filter(self, checked=False):
        """Record menu: the telemetry toggle mirrors the checkbox."""
        self.filter_check.setChecked(checked)

    def _menu_toggle_screenshot(self, checked=False):
        """Record menu: the screenshot toggle mirrors the checkbox."""
        self.screenshot_check.setChecked(checked)

    def _menu_generate_client(self):
        """Generate menu: go to the Generate tab and generate when a
        host is already picked."""
        self.tabs.setCurrentIndex(self._TAB_KEYS.index("generate"))
        if self.gen_host.currentText().strip():
            self._generate()

    def _menu_build_pack(self):
        """Generate menu: go to the Assets tab and build when a
        group is picked."""
        self.tabs.setCurrentIndex(self._TAB_KEYS.index("assets"))
        if self.asset_group.currentData():
            self._build_pack()

    def _menu_learn_preset(self):
        """Generate menu: go to the Presets tab and learn when a
        source is picked."""
        self.tabs.setCurrentIndex(self._TAB_KEYS.index("presets"))
        if self.preset_source.currentData():
            self._learn_preset()

    def _menu_analyze_binary(self):
        """File menu: go to the Binary tab and pick a file."""
        self.tabs.setCurrentIndex(self._TAB_KEYS.index("binary"))
        self._analyze_file_clicked()

    def _menu_scan_folder(self):
        """File menu: go to the Assets tab and pick a folder."""
        self.tabs.setCurrentIndex(self._TAB_KEYS.index("assets"))
        self._scan_assets_folder()

    def _menu_load_sample(self):
        """File menu: go to the Doctor tab and load the sample — the
        handler reports into the Doctor text, so the tab must show."""
        self.tabs.setCurrentIndex(self._TAB_KEYS.index("doctor"))
        self._load_sample_clicked()

    # ---- capture encryption (v0.8 G1) --------------------------------

    def _refresh_encryption_actions(self):
        """File-menu encryption actions follow the store: Unlock
        only while locked, and the toggle names what it will do."""
        enabled = store.encryption_enabled()
        unlocked = enabled and store.unlocked()
        self._unlock_action.setVisible(enabled and not unlocked)
        self._encrypt_action.setText(
            "Turn off capture encryption…" if enabled
            else "Encrypt captures…")

    def _ask_passphrase(self, title, label):
        """A password-echo dialog. Returns the text, or None when
        cancelled. Never logged, never stored."""
        text, ok = QInputDialog.getText(
            self, title, label, QLineEdit.Password)
        return text if ok else None

    def _unlock_captures(self):
        """File › Unlock captures…: ask once, and on success reload
        everything that read the store while it was locked."""
        pw = self._ask_passphrase(
            "Unlock captures",
            "Your captures are encrypted. Type the passphrase:")
        if pw is None:
            return
        try:
            ok = store.unlock(pw)
        except crypto.CryptoError as e:
            QMessageBox.warning(self, "MIMIK", str(e))
            return
        if not ok:
            QMessageBox.warning(
                self, "MIMIK",
                "That passphrase did not unlock the captures.")
            return
        self._refresh_encryption_actions()
        self._invalidate_flows()
        self._loaded.discard("hosts")
        self._loaded.discard("generate")
        self._refresh_saved_flows()
        self._update_status_bar()
        self.statusBar().showMessage(
            "Captures unlocked for this session.", 4000)

    def _encrypt_captures_toggle(self):
        """File › Encrypt captures… / Turn off capture encryption…"""
        if not store.encryption_enabled():
            pw1 = self._ask_passphrase(
                "Encrypt captures",
                "Choose a passphrase. MIMIK will ask for it every "
                "time it starts, and it is never stored:")
            if pw1 is None:
                return
            if not pw1:
                QMessageBox.warning(
                    self, "MIMIK",
                    "An empty passphrase would not protect anything: "
                    "encryption stayed off.")
                return
            pw2 = self._ask_passphrase(
                "Encrypt captures", "Type the passphrase again:")
            if pw2 is None:
                return
            if pw1 != pw2:
                QMessageBox.warning(
                    self, "MIMIK",
                    "The two passphrases did not match: encryption "
                    "stayed off.")
                return
            try:
                n = store.enable_encryption(pw1)
            except crypto.CryptoError as e:
                QMessageBox.warning(self, "MIMIK", str(e))
                return
            self._refresh_encryption_actions()
            self._update_status_bar()
            QMessageBox.information(
                self, "MIMIK",
                f"Encryption is on: {n} capture file(s) sealed.\n\n"
                "If you forget the passphrase, the encrypted "
                "captures cannot be recovered, by design.")
            return
        # Turning off: needs the store unlocked first.
        if not store.unlocked():
            self._unlock_captures()
            if not store.unlocked():
                return
        answer = QMessageBox.question(
            self, "MIMIK",
            "Turn capture encryption off?\n\n"
            "Every capture goes back to plain JSON on disk (still "
            "mode 0600, in a 0700 folder).",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            n = store.disable_encryption()
        except (crypto.CryptoError, store.StoreLockedError) as e:
            QMessageBox.warning(self, "MIMIK", str(e))
            return
        self._refresh_encryption_actions()
        self._update_status_bar()
        self.statusBar().showMessage(
            f"Capture encryption off ({n} file(s) unsealed).", 4000)

    def set_theme(self, name):
        """Switch the theme now + persist it (item 9). The window's own
        stylesheet wins over the app-level one, so both are set."""
        name = name if name in THEMES else "dark"
        _settings().setValue("theme", name)
        self._apply_stylesheet()
        if hasattr(self, "_dark_theme_action"):
            self._dark_theme_action.setChecked(name == "dark")

    def _apply_stylesheet(self):
        """(v0.6 T13) Apply the effective stylesheet — theme, plus
        the big-text / high-contrast presets — to the app, this
        window, and the status chip."""
        sheet = effective_stylesheet()
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(sheet)
        self.setStyleSheet(sheet)
        if self.status_chip is not None:
            self.status_chip.setStyleSheet(sheet)

    def _set_access(self, key, value):
        """(v0.6 T13) One accessibility preset changed — persist it,
        apply it, and sync the other control that shows it (the View
        menu action or the checklist checkbox)."""
        set_access_setting(key, bool(value))
        if key in ("big_text", "high_contrast"):
            self._apply_stylesheet()
        elif key == "one_touch":
            if value:
                # One-touch starts with defaults: filter on,
                # screenshot off, for the current target. The
                # checkboxes' own handlers write the presets and
                # sync the Record menu.
                self.filter_check.setChecked(True)
                self.screenshot_check.setChecked(False)
            self._apply_one_touch()
        # Reduced motion needs no apply step: _dip_button reads the
        # setting on every press.
        for control in (getattr(self, "_access_actions", {}).get(key),
                        getattr(self, "_access_checks", {}).get(key)):
            if control is not None:
                control.blockSignals(True)
                control.setChecked(bool(value))
                control.blockSignals(False)

    def _access_action_triggered(self, checked):
        """(v0.6 T13) A View-menu Accessibility action toggled. A
        bound method, not a lambda: lambdas on signals corrupt the
        heap at teardown (see the guard test). The action carries
        its preset key."""
        action = self.sender()
        if action is not None:
            self._set_access(action.data(), checked)

    def _access_check_toggled(self, checked):
        """(v0.6 T13) A checklist comfort checkbox toggled. A bound
        method, not a lambda (heap rule, as above). The checkbox
        carries its preset key as a property."""
        check = self.sender()
        if check is not None:
            self._set_access(check.property("access_key"), checked)

    def bring_to_front(self):
        """Raise this window (used by the single-instance guard,
        v0.5 item 14, U27)."""
        self.show()
        self.raise_()
        self.activateWindow()

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
        elif key == "binary":
            self._refresh_binaries()
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
        if key == "generate":
            # v0.6 T10: engines may have started (or stopped) since
            # the last visit — re-check on every show.
            self._refresh_polish()
        # v0.6 T11: focus lands on the tab's primary action, on
        # every show — the tab is usable without the mouse.
        self._focus_tab_primary(key)
        if key in self._loaded or key == "record":
            return
        if self._load_tab(key):
            self._loaded.add(key)

    def _focus_tab_primary(self, key):
        """(v0.6 T11) Put keyboard focus on the tab's primary action:
        the selected pad (or Stop while recording) on Record, the
        list or first field everywhere else."""
        target = None
        if key == "record":
            if self.worker is not None:
                target = self.stop_btn
            else:
                target = self.pads.get(self._current_target)
        elif key == "hosts":
            target = self.hosts_list
        elif key == "generate":
            target = self.gen_host
        elif key == "assets":
            target = self.asset_group
        elif key == "clients":
            target = self.clients_list
        elif key == "binary":
            target = self.binary_analyze_btn
        elif key == "doctor":
            target = self.doctor_recheck_btn
        elif key == "presets":
            target = self.presets_list
        if target is not None and target.isEnabled():
            target.setFocus()

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
        if key == "binary":
            self._refresh_binaries()
            return True
        if key == "doctor":
            return self._run_doctor()
        if key == "presets":
            self._refresh_presets()
            return True
        return True

    # ---- Record tab ----------------------------------------------------------

    def _record_tab(self):
        # Toy surface (v0.5 item 16, U34; pads v0.6 T16): tap a pad
        # to start, Stop to end — nothing else visible by default.
        # Everything else lives behind one collapsible "More options"
        # section; NOTHING is removed (U1: reorganize only). Every
        # remaining field explains itself in plain words directly
        # underneath it (U20), and every pad carries its own line.
        #
        # The content sits in a scroll area: with the file target +
        # More options expanded the tab is taller than the window,
        # and Qt squeezes a shown window's children instead of
        # growing it (seen as the file box crushed to strips).
        # Start/Stop stay OUTSIDE the scroll area: the primary
        # action is always visible, never below the fold.
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        w = QWidget()
        scroll.setWidget(w)
        outer.addWidget(scroll, 1)
        lay = QVBoxLayout(w)

        # Identity header (v0.6 T1, U15): the MIMIK mark + wordmark
        # inside the window, static — the icon used to appear only in
        # the title bar. No new artwork: the same icon files as the
        # window icon. An empty pixmap (no icon files anywhere) leaves
        # a blank label, never a crash.
        head = QHBoxLayout()
        self.identity_icon = QLabel()
        pix = window_icon().pixmap(32, 32)
        if not pix.isNull():
            self.identity_icon.setPixmap(pix)
        head.addWidget(self.identity_icon)
        self.identity_wordmark = QLabel("MIMIK")
        self.identity_wordmark.setObjectName("wordmark")
        head.addWidget(self.identity_wordmark)
        head.addStretch(1)
        lay.addLayout(head)

        lay.addWidget(QLabel(
            "Capture your own app's traffic, then turn it into a Python "
            "library. Own accounts only."))
        self.start_here = QLabel(
            "Start here: 1 Record an app → 2 pick a host on Hosts and "
            "Learn → 3 Generate a client.")
        self.start_here.setWordWrap(True)
        self.start_here.setObjectName("banner")
        lay.addWidget(self.start_here)

        # First-run checklist (v0.6 T3, entries 014 + 020): four
        # steps to the first client, tracked live. It replaces the
        # old welcome modal + sample-guide modal chain (U18): the
        # sample is pre-loaded, the steps tick as they really
        # happen, and the box goes away when done or dismissed —
        # never reappearing on its own. Hidden until _maybe_first_run
        # (main-only) decides to show it.
        self.checklist_box = QGroupBox(
            "Your first client, in four steps")
        clay = QVBoxLayout(self.checklist_box)
        self._checklist_labels = {}
        for key, text in _CHECKLIST_STEPS:
            lbl = QLabel()
            self._checklist_labels[key] = lbl
            clay.addWidget(lbl)
        # v0.6 T13: the comfort presets at first boot (entry 015) —
        # one tap each, and every one stays adjustable on its own
        # from the View menu afterwards. Signals blocked while the
        # saved state loads: the window is still being built.
        clay.addWidget(QLabel("Comfort, one tap each:"))
        self._access_checks = {}
        for key, label in ACCESS_PRESETS:
            check = QCheckBox(label)
            check.blockSignals(True)
            check.setChecked(access_setting(key))
            check.blockSignals(False)
            check.setProperty("access_key", key)
            check.toggled.connect(self._access_check_toggled)
            self._access_checks[key] = check
            clay.addWidget(check)
        dismiss = QPushButton("Done with this")
        dismiss.clicked.connect(self._checklist_dismiss)
        clay.addWidget(dismiss)
        self.checklist_box.setVisible(False)
        lay.addWidget(self.checklist_box)

        # Capture pads (v0.6 T16, entry 024): one pad per target —
        # tap it and it starts. They replace the v0.5 target combo
        # (Joshua's call, 2026-11-13). Each pad explains itself in
        # plain words (U20); the steps below still change with the
        # target. 3 + 2 grid: big enough to tap, small enough to fit.
        pads_label = QLabel("What do you want to capture?")
        pads_label.setObjectName("section")
        lay.addWidget(pads_label)
        # v0.8 B5: the one line the tab was missing — the pads ARE
        # the Start button (T16), so say it where the user looks.
        self.pads_hint = _explain_label(
            "Tap a card to start recording. Tap the recording card "
            "again (or Stop below) to end it.")
        lay.addWidget(self.pads_hint)
        grid = QGridLayout()
        self.pads = {}
        for i, (key, title, subtitle) in enumerate(PAD_TARGETS):
            pad = _PadButton(key, title, subtitle)
            pad.clicked.connect(self._pad_clicked)
            self.pads[key] = pad
            grid.addWidget(pad, i // 3, i % 3)
        lay.addLayout(grid)
        self._current_target = "android"

        # "More options": one collapsible section holding everything
        # the toy surface does not need. It auto-expands when the
        # target needs it (file / linux-app), so those flows never
        # hit a dead end behind a collapsed section.
        self.more_btn = QToolButton()
        self.more_btn.setText("More options")
        self.more_btn.setCheckable(True)
        self.more_btn.setChecked(False)
        self.more_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.more_btn.setArrowType(Qt.RightArrow)
        self.more_btn.toggled.connect(self._more_options_toggled)
        lay.addWidget(self.more_btn)
        self.more_box = QWidget()
        more_lay = QVBoxLayout(self.more_box)
        more_lay.setContentsMargins(0, 0, 0, 0)
        self.more_box.setVisible(False)
        lay.addWidget(self.more_box)

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
        more_lay.addLayout(frow)
        more_lay.addWidget(_explain_label(
            "Telemetry and analytics hosts are skipped, so the host "
            "list stays about the app."))

        # Stop-time screenshot (v0.4 item 15): one desktop screenshot
        # when recording stops, saved with the capture.
        self.screenshot_check = QCheckBox(
            "Screenshot the desktop when recording stops")
        self.screenshot_check.setChecked(presets.screenshot_enabled())
        self.screenshot_check.stateChanged.connect(
            self._screenshot_toggled)
        more_lay.addWidget(self.screenshot_check)
        more_lay.addWidget(_explain_label(
            "A picture of the desktop, saved with the capture, so "
            "you can see what was open."))

        self.lan_label = QLabel(f"LAN IP for the phone proxy: {doctor.lan_ip()}:8080")
        more_lay.addWidget(self.lan_label)

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
        more_lay.addWidget(self.file_box)

        # Linux-app launch command — a one-line shell command, so a
        # QLineEdit (item 32d), not the QTextEdit it used to be.
        self.app_box = QGroupBox("Linux app to launch (optional)")
        alay = QVBoxLayout(self.app_box)
        self.launch_edit = QLineEdit()
        self.launch_edit.setPlaceholderText("e.g. firefox   (leave blank to "
                                            "launch it yourself)")
        # v0.6 T8: remember the last launch command. Restored
        # before the signal connects, so startup doesn't re-save.
        saved_cmd = _settings().value("record/last_launch_cmd", "") or ""
        if saved_cmd:
            self.launch_edit.setText(saved_cmd)
        self.launch_edit.textChanged.connect(self._launch_cmd_changed)
        alay.addWidget(self.launch_edit)
        alay.addWidget(_explain_label(
            "A command MIMIK runs for you. Leave it blank and launch "
            "the app yourself."))
        more_lay.addWidget(self.app_box)

        self.steps = QTextEdit()
        self.steps.setReadOnly(True)
        # v0.8 B5: at the default 900x650 the steps box was squeezed
        # until the instructions cut mid-step; a floor keeps the
        # first steps readable (the tab scrolls for the rest).
        self.steps.setMinimumHeight(140)
        lay.addWidget(self.steps, 1)

        self.host_list = QListWidget()
        self.host_list.setMaximumHeight(110)
        self.host_list.addItem(_empty_item(
            "No live hosts yet. Start the proxy and use the app."))
        # v0.6 T13: stored, so one-touch capture can hide it with
        # the list.
        self.live_hosts_label = QLabel("Live hosts while recording:")
        self.live_hosts_label.setObjectName("section")
        lay.addWidget(self.live_hosts_label)
        lay.addWidget(self.host_list)

        # Stop stays pinned at the bottom, full width: the pads start
        # a capture (tap to start), Stop ends it. The old Start proxy
        # button is gone with the combo (v0.6 T16).
        brow = QHBoxLayout()
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("big")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_recording)
        self.stop_btn.pressed.connect(self._dip_stop)
        # Live measurements strip (item 19, U37): display only,
        # shown while recording, hidden when idle. It sits outside
        # the scroll area with the buttons, so it is always visible
        # while a recording runs. Calm by design: one small line, a
        # red dot, no popups for routine state.
        self.live_strip = QWidget()
        self.live_strip.setObjectName("livestrip")
        slay = QHBoxLayout(self.live_strip)
        slay.setContentsMargins(12, 6, 12, 6)
        self.live_dot = QLabel("●")
        self.live_dot.setObjectName("livedot")
        slay.addWidget(self.live_dot)
        self.live_text = QLabel("")
        slay.addWidget(self.live_text, 1)
        # v0.6 T9: what the capture filter dropped, quietly — one
        # tap opens the Excluded hosts dialog with the names.
        self.live_dropped = QPushButton("")
        self.live_dropped.setFlat(True)
        self.live_dropped.setVisible(False)
        self.live_dropped.clicked.connect(self._show_excluded_hosts)
        slay.addWidget(self.live_dropped)
        self.live_strip.setVisible(False)
        outer.addWidget(self.live_strip)

        brow.addWidget(self.stop_btn, 1)
        outer.addLayout(brow)

        # v0.6 T8: restore the last capture setup. The file restores
        # before the target is selected, so a restored file target
        # builds its plan on select.
        saved_file = _settings().value("record/last_file", "") or ""
        if saved_file and os.path.exists(saved_file):
            self.file_path = saved_file
            self.file_label.setText(saved_file)
        saved_target = _settings().value("record/last_target", "") or ""
        if saved_target not in self.pads:
            saved_target = "android"
        self._select_target(saved_target)
        return tab

    def _dip_button(self, button):
        """Press dip (item 20): the button shrinks to 97% and settles
        back over 150 ms. Geometry only — no layout thrash, no
        shadow or blur work, cheap on a 2-core PC. One animation per
        button, reused across presses (a re-press restarts it)."""
        if access_setting("reduced_motion"):
            return  # v0.6 T13: the off-switch for this animation.
        anim = self._dip_anims.get(button)
        if anim is None:
            anim = QPropertyAnimation(button, b"geometry", self)
            anim.setDuration(150)
            anim.setEasingCurve(QEasingCurve.OutBack)
            self._dip_anims[button] = anim
        anim.stop()
        start = button.geometry()
        center = start.center()
        width = max(1, int(start.width() * 0.97))
        height = max(1, int(start.height() * 0.97))
        small = QRect(center.x() - width // 2, center.y() - height // 2,
                      width, height)
        anim.setStartValue(start)
        anim.setKeyValueAt(0.5, small)
        anim.setEndValue(start)
        anim.start()

    def _dip_stop(self):
        self._dip_button(self.stop_btn)

    def _more_options_toggled(self, checked):
        """The Record tab's "More options" section (item 16)."""
        self.more_box.setVisible(checked)
        self.more_btn.setArrowType(
            Qt.DownArrow if checked else Qt.RightArrow)

    def _apply_one_touch(self):
        """(v0.6 T13) One-touch capture: the Record tab shows only
        the pads and Stop. The options box (filter, screenshot,
        file/app pickers), the steps text, and the live-hosts list
        hide — their settings stay as they are, adjustable from
        the Record menu while hidden. With the preset off, each
        control goes back to its natural state."""
        simple = access_setting("one_touch")
        self.more_btn.setVisible(not simple)
        self.more_box.setVisible(
            not simple and self.more_btn.isChecked())
        self.steps.setVisible(not simple)
        self.live_hosts_label.setVisible(not simple)
        self.host_list.setVisible(not simple)

    def _select_target(self, key):
        """Select a capture target (v0.6 T16): the steps text, the
        file/app boxes, and the pad states follow the selection.
        Selection only — starting is _tap_pad's job."""
        self._current_target = key
        # v0.6 T8: remember the last target, and show this target's
        # filter + screenshot choices. Signals blocked: this is a
        # restore, not a user toggle.
        _settings().setValue("record/last_target", key)
        self.filter_check.blockSignals(True)
        self.filter_check.setChecked(
            presets.filtering_enabled(target=key))
        self.filter_check.blockSignals(False)
        self.screenshot_check.blockSignals(True)
        self.screenshot_check.setChecked(
            presets.screenshot_enabled(target=key))
        self.screenshot_check.blockSignals(False)
        for k, pad in self.pads.items():
            if self.worker is not None and k == key:
                pad.set_state("recording")
            elif k == key:
                pad.set_state("selected")
            else:
                pad.set_state("idle")
        self.file_box.setVisible(key == "file")
        self.app_box.setVisible(key == "linux-app")
        # The file picker and the launch box live inside "More
        # options": auto-expand it when the target needs one, so
        # those flows never hit a dead end behind a collapsed
        # section. Never auto-collapses: the user's state is theirs.
        if key in ("file", "linux-app") \
                and not self.more_btn.isChecked():
            self.more_btn.setChecked(True)

        if key == "android":
            self.steps.setText(GUI_ANDROID_STEPS.format(ip=doctor.lan_ip()))
        elif key == "linux-app":
            self.steps.setText(GUI_LINUX_APP_STEPS)
        elif key == "file":
            self.steps.setText(
                "Pick a program file: AppImage, executable, script, archive\n"
                "(.zip / .tar.gz), disk image (.iso / .img), or .deb.\n\n"
                "Archives are extracted to ~/.cache/mimik/runs/ and the main\n"
                "program is auto-picked (change it in the list). Disk images\n"
                "are mounted READ-ONLY and you pick the program inside.\n\n"
                "Windows .exe/.msi are WinLinx's job: MIMIK never runs them.\n"
                "To capture one, run it in WinLinx and use the Windows target.")
            # v0.6 T8: a restored file's plan builds lazily on
            # first select (not at startup — archives extract).
            if self.file_path and self.plan is None:
                self._prepare_file()
            else:
                self._refresh_file_pad()
        elif key == "web":
            self.steps.setText(GUI_WEB_STEPS)
        elif key == "windows":
            self.steps.setText(GUI_WINDOWS_STEPS + "\nCommands MIMIK will run:\n"
                "  wine regedit /s <proxy.reg>\n"
                "  wine certutil -addstore -f -user Root "
                "~/.mitmproxy/mitmproxy-ca-cert.pem\n"
                "Stop restores the prefix's original proxy settings.")
        # v0.6 T13: one-touch re-hides what the auto-expand above
        # just showed, so a target switch never undoes the preset.
        self._apply_one_touch()

    def _tap_pad(self, key):
        """A pad was tapped (v0.6 T16, entry 024): tap it and it
        starts. Tapping the recording pad stops it (like a playing
        clip); tapping another pad mid-recording does nothing — one
        recording at a time. The file pad with no file opens the
        picker; the web pad only selects (no proxy to start)."""
        if self.worker is not None:
            if key == self._current_target:
                self._stop_recording()
            return
        self._select_target(key)
        if key == "file" and (self.plan is None
                              or not self.plan.command):
            self._choose_file()
            return
        if key == "web":
            return
        self._start_recording()

    def _pad_clicked(self):
        """A pad's clicked signal — the pad carries its key."""
        pad = self.sender()
        if pad is not None:
            self._tap_pad(pad.key)

    def _set_pads_enabled(self, enabled):
        """Grey the pads while a recording runs (the recording pad
        stays enabled so tapping it stops)."""
        for key, pad in self.pads.items():
            pad.setEnabled(enabled or key == self._current_target)

    def _refresh_file_pad(self):
        """The file pad's subtitle names the chosen file, or says
        to tap to choose one."""
        pad = self.pads.get("file")
        if pad is None:
            return
        if self.plan is not None and self.plan.command:
            name = os.path.basename(self.file_path or "")
            pad.subtitle_label.setText(f"Ready: {name}, tap to record")
        else:
            pad.subtitle_label.setText("Tap to choose a file")

    def _launch_cmd_changed(self, text):
        """(v0.6 T8) Remember the last launch command."""
        _settings().setValue("record/last_launch_cmd", text)

    def _filter_toggled(self, _state=None):
        """The Record tab's filter checkbox — a persistent setting,
        like auto-learn. Per target (v0.6 T8): the choice follows
        the job. Guarded: this fires during tab construction,
        before any target is selected."""
        presets.set_filtering_enabled(
            self.filter_check.isChecked(),
            target=getattr(self, "_current_target", None))
        # Keep the Record menu toggle in sync. Guarded: this fires
        # during tab construction, before the menus exist.
        action = getattr(self, "_menu_filter_act", None)
        if action is not None:
            action.setChecked(self.filter_check.isChecked())

    def _show_excluded_hosts(self):
        """The exclusion-list editor: the effective list, add/remove,
        restore defaults (item 26)."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Excluded hosts")
        dlg.resize(500, 400)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "Hosts never captured: their traffic is dropped before\n"
            "it reaches the store or the host list. Subdomains of a\n"
            "listed host are excluded too."))
        # v0.6 T9: the hosts actually dropped in this session.
        if self._dropped_hosts:
            dropped = QLabel("Dropped while recording: "
                             + ", ".join(self._dropped_hosts))
            dropped.setWordWrap(True)
            lay.addWidget(dropped)
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
        # v0.6 T8: remember the last chosen file.
        _settings().setValue("record/last_file", path)
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
            self._refresh_file_pad()
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
        else:
            self.cmd_label.setText("pick the program to run from the list")
        self._refresh_file_pad()

    def _candidate_changed(self, row):
        if self.plan is None or row < 0:
            return
        exe = self.plan.candidates[row]
        self.plan.command = launch.build_launch_command(exe)
        self.cmd_label.setText(f"will run:  {self.plan.command_display}")
        self._refresh_file_pad()

    def _clear_runs(self):
        n = launch.clear_runs()
        QMessageBox.information(self, "MIMIK", f"cleared {n} temp extraction(s)")

    def _toggle_recording(self):
        """F9 (v0.6 T11): start the proxy, or stop it if it is
        running. The pads and the Record menu do the same job with
        the mouse; this is the keyboard path."""
        if self.worker is not None:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        key = self._current_target
        if key == "web":
            # No proxy to start: the steps already say to import a
            # HAR. (The pads never call this for web; the Record
            # menu and F9 can.)
            self.steps.append(
                "Web apps need no proxy: import a HAR on the "
                "Generate tab.")
            return
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
        self.worker.dropped_updated.connect(self._dropped_updated)
        # v0.6 T9: a fresh recording starts with no dropped hosts.
        self._dropped_hosts = []
        self.live_dropped.setVisible(False)
        self.worker.start()
        self.stop_btn.setEnabled(True)
        self._menu_start_act.setEnabled(False)
        self._menu_stop_act.setEnabled(True)
        self._set_pads_enabled(False)
        self._select_target(key)
        self.host_list.clear()
        self._record_started = time.monotonic()
        self._flow_count = 0
        self._host_count = 0
        self._done_text = None
        self._record_bytes_start = _dir_size(store.captures_dir())
        self._status_timer.start()
        self._update_status_bar()
        self._checklist_tick("started")

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
                "Listening. Use the app and its hosts appear here."))
            return
        self._checklist_tick("host_seen")
        for host, n in rows:
            self.host_list.addItem(f"{n:>6}  {host}")

    def _dropped_updated(self, hosts):
        """(v0.6 T9) The capture filter dropped hosts — say so
        quietly in the live strip; the names are one tap away in
        the Excluded hosts dialog."""
        self._dropped_hosts = list(hosts)
        n = len(hosts)
        self.live_dropped.setVisible(n > 0)
        if n:
            self.live_dropped.setText(
                f"telemetry dropped: {n} host{'s' if n != 1 else ''}")

    def _stop_recording(self):
        if self.worker is not None:
            self._take_stop_screenshot()
            self.worker.request_stop()
            self.stop_btn.setEnabled(False)
            self._menu_stop_act.setEnabled(False)
            self.steps.append("stopping…")

    def _screenshot_toggled(self):
        """Per target (v0.6 T8), like the filter toggle. Guarded:
        this fires during tab construction, before any target is
        selected."""
        presets.set_screenshot_enabled(
            self.screenshot_check.isChecked(),
            target=getattr(self, "_current_target", None))
        # Keep the Record menu toggle in sync. Guarded: this fires
        # during tab construction, before the menus exist.
        action = getattr(self, "_menu_shot_act", None)
        if action is not None:
            action.setChecked(self.screenshot_check.isChecked())

    def _take_stop_screenshot(self):
        """The stop-time screenshot (v0.4 item 15), on the GUI thread
        while the target app is still up. Called from Stop and from
        window-close; never raises."""
        if not self.screenshot_check.isChecked():
            return
        try:
            path = screenshot.capture_for_session()
        except Exception as e:
            log.log_error("stop screenshot", e)
            path = None
        if path:
            self.steps.append(f"screenshot saved: {path}")
        else:
            self.steps.append(
                "screenshot failed (no screenshot tool found)")

    def _recording_finished(self):
        self.worker = None
        self._record_started = None
        self._status_timer.stop()
        self._refresh_saved_flows()
        # Quiet completion state (item 20): the settled status names
        # what the recording caught. The counts are the session's
        # (reset only when the next recording starts).
        flows = "flow" if self._flow_count == 1 else "flows"
        hosts = "host" if self._host_count == 1 else "hosts"
        self._done_text = (f"✓ Recorded {self._flow_count} {flows} "
                           f"from {self._host_count} {hosts}")
        self._update_status_bar()
        self.stop_btn.setEnabled(False)
        self._menu_start_act.setEnabled(True)
        self._menu_stop_act.setEnabled(False)
        self._set_pads_enabled(True)
        self._select_target(self._current_target)
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
        link_btn = QPushButton("Link binary…")
        link_btn.clicked.connect(self._link_binary_clicked)
        row.addWidget(link_btn)
        # v0.8 B3: these three act on the selected host — disabled
        # until a real host row is current (the Binary tab's pattern).
        self.host_delete_btn = delete_btn
        self.host_gen_btn = gen_btn
        self.host_link_btn = link_btn
        for b in (delete_btn, gen_btn, link_btn):
            b.setEnabled(False)
        row.addStretch(1)
        lay.addLayout(row)
        self.hosts_list = QListWidget()
        self.hosts_list.currentItemChanged.connect(self._host_selected)
        # v0.6 T4: pending until the first load answers — never a
        # false empty. The honest empty state arrives in
        # _refresh_hosts' done.
        self.hosts_list.addItem(_empty_item("Loading captures…"))
        lay.addWidget(self.hosts_list, 1)
        brow = QHBoxLayout()
        self.host_binary_label = QLabel("Binary: none linked yet.")
        brow.addWidget(self.host_binary_label, 1)
        self.host_binary_jump = QPushButton("View in Binary tab")
        self.host_binary_jump.clicked.connect(self._view_linked_binary)
        self.host_binary_jump.setEnabled(False)
        brow.addWidget(self.host_binary_jump)
        lay.addLayout(brow)
        ep_label = QLabel("Endpoints for the selected host:")
        ep_label.setObjectName("section")
        lay.addWidget(ep_label)
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Filter:"))
        self.ep_filter = QLineEdit()
        self.ep_filter.setPlaceholderText("filter endpoints…")
        self.ep_filter.textChanged.connect(self._filter_ep_table)
        frow.addWidget(self.ep_filter, 1)
        self.ep_filter_info = QLabel("")
        frow.addWidget(self.ep_filter_info)
        lay.addLayout(frow)
        self.ep_table = QTableWidget(0, 4)
        self.ep_table.setHorizontalHeaderLabels(
            ["Method", "Path", "Status", "Seen"])
        self.ep_table.horizontalHeader().setStretchLastSection(True)
        # v0.6 T6: sortable — the Seen column sorts numerically
        # (see _SeenItem); the other columns sort as text. The
        # header's indicator defaults to column 0 descending in
        # this Qt build, which would reorder the table on load —
        # clear it so insertion order stands until the user clicks.
        self.ep_table.setSortingEnabled(True)
        self.ep_table.horizontalHeader().setSortIndicator(
            -1, Qt.AscendingOrder)
        _empty_table_row(self.ep_table,
                         "Select a host to see its endpoints.")
        lay.addWidget(self.ep_table, 2)
        # v0.6 T14: capture timeline — drag to filter the endpoints
        # to the calls made up to that moment. The data (first_seen
        # per endpoint) is computed when the host loads, before any
        # drag (entry 018).
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Timeline:"))
        self.timeline_slider = QSlider(Qt.Horizontal)
        self.timeline_slider.setRange(0, 1000)
        self.timeline_slider.setValue(1000)
        self.timeline_slider.setEnabled(False)
        self.timeline_slider.valueChanged.connect(self._timeline_moved)
        trow.addWidget(self.timeline_slider, 1)
        self.timeline_label = QLabel("no times recorded yet")
        trow.addWidget(self.timeline_label)
        lay.addLayout(trow)
        rrow = QHBoxLayout()
        self.replay_btn = QPushButton("Replay selected…")
        self.replay_btn.clicked.connect(self._replay_selected)
        self.replay_btn.setEnabled(False)
        rrow.addWidget(self.replay_btn)
        rrow.addWidget(QLabel("Re-sends the selected request, shows the "
                              "response."))
        rrow.addStretch(1)
        lay.addLayout(rrow)
        self.ep_table.itemSelectionChanged.connect(
            self._update_replay_button)
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
            worker.progress.emit(f"read {len(flows)} flows…")
            rows = []
            for host, n in mitm_mod.hosts(flows):
                stats = store.capture_stats(host)
                rows.append((host, n,
                             src == "saved" and stats["truncated"],
                             stats["bytes"]))
            worker.progress.emit(f"{len(rows)} hosts found")
            return rows

        def done(ok, rows):
            if not ok:
                # First load never answered: say so honestly
                # instead of leaving the pending row up forever. A
                # refresh failure keeps the last good content.
                if (self.hosts_list.count() == 1
                        and self.hosts_list.item(0).text().startswith(
                            "Loading")):
                    self.hosts_list.clear()
                    self.hosts_list.addItem(_empty_item(
                        "Couldn't load captures. Press Refresh to "
                        "try again."))
                return
            # v0.6 T5: refresh keeps your place — the snapshot is
            # taken here, at refill time, so a host picked while the
            # load ran is the one restored. Restoring the selection
            # re-fires _host_selected, so the endpoints view comes
            # back with it instead of being wiped.
            snap = _snapshot_list(self.hosts_list)
            self.hosts_list.clear()
            if not rows:
                self.hosts_list.addItem(_empty_item(
                    "No captures yet. Record an app, or import a HAR "
                    "on the Generate tab."))
                self._update_host_action_buttons()
                return
            for host, n, truncated, size in rows:
                text = f"{n:>6}  {host}"
                if size:
                    text += f"  ({store.format_size(size)})"
                if truncated:
                    text += ("  (truncated, showing newest %d)"
                             % store.effective_flows_cap())
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, host)
                self.hosts_list.addItem(item)
            _restore_list(self.hosts_list, snap)
            # v0.8 G9: a lone host selects itself — otherwise the tab
            # sits on "Select a host…" with exactly one thing to pick.
            if len(rows) == 1 and self.hosts_list.currentItem() is None:
                self.hosts_list.setCurrentRow(0)
            self._update_host_action_buttons()

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

    def _update_host_action_buttons(self):
        """(v0.8 B3) Delete / Generate / Link act on the selected
        host: enabled only while a real host row is current (the
        empty-state placeholder carries no host data)."""
        item = self.hosts_list.currentItem()
        has = item is not None and bool(item.data(Qt.UserRole))
        for b in (self.host_delete_btn, self.host_gen_btn,
                  self.host_link_btn):
            b.setEnabled(has)

    def _host_selected(self, current, _prev):
        self._update_host_action_buttons()
        self.ep_table.setRowCount(0)
        if current is None:
            self._update_host_binary_link(None)
            self._setup_timeline([])
            return
        host = current.data(Qt.UserRole)
        self._update_host_binary_link(host)
        self._pending_host = host
        if self._task is None:
            host = self._pending_host
            self._pending_host = None
            self._start_host_task(host)

    def _update_host_binary_link(self, host):
        """The linked-binary label under the host list (v0.5 item 10):
        the same app, two views (traffic + code)."""
        sha = binary.binary_for_host(host) if host else None
        if sha is None:
            self.host_binary_label.setText("Binary: none linked yet.")
            self.host_binary_jump.setEnabled(False)
            return
        meta = binary.load_meta(sha)
        name = meta["source_name"] if meta else sha[:12]
        self.host_binary_label.setText(
            f"Binary: {name} (same app, traffic and code)")
        self.host_binary_jump.setEnabled(True)

    def _link_binary_clicked(self):
        """Manual 'Link binary…' for a host (v0.5 item 10). No
        binaries yet is not a dead end — the dialog offers the
        Binary tab."""
        item = self.hosts_list.currentItem()
        if item is None or not item.data(Qt.UserRole):
            QMessageBox.warning(self, "MIMIK", "select a host first")
            return
        host = item.data(Qt.UserRole)
        rows = binary.list_binaries()
        if not rows:
            answer = QMessageBox.question(
                self, "MIMIK",
                "No binaries analyzed yet. Open the Binary tab to "
                "analyze one?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer == QMessageBox.Yes:
                self.tabs.setCurrentIndex(5)  # Binary tab
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Link a binary to {host}")
        dlg.resize(500, 300)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "Pick the program file this host belongs to:"))
        lst = QListWidget()
        for row in rows:
            entry = QListWidgetItem(
                f"{row['source_name']}  ({row['format'].upper()})")
            entry.setData(Qt.UserRole, row["sha256"])
            lst.addItem(entry)
        lay.addWidget(lst, 1)
        brow = QHBoxLayout()
        link = QPushButton("Link")
        link.clicked.connect(self._link_dialog_accept)
        brow.addWidget(link)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(dlg.reject)
        brow.addWidget(cancel)
        lay.addLayout(brow)
        self._link_dialog = dlg
        self._link_list = lst
        self._link_host = host
        dlg.exec()

    def _link_dialog_accept(self):
        """The Link button in the link dialog."""
        item = self._link_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "MIMIK", "select a binary first")
            return
        binary.link_binary(self._link_host,
                           item.data(Qt.UserRole))
        self._link_dialog.accept()
        self._update_host_binary_link(self._link_host)

    def _view_linked_binary(self):
        """Jump to the Binary tab with the linked binary selected."""
        item = self.hosts_list.currentItem()
        if item is None or not item.data(Qt.UserRole):
            return
        sha = binary.binary_for_host(item.data(Qt.UserRole))
        if sha is None:
            return
        self.tabs.setCurrentIndex(5)  # Binary tab
        if "binary" not in self._loaded:
            self._refresh_binaries()
            self._loaded.add("binary")
        self._select_binary(sha)

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
                self._setup_timeline([])
                return
            # Sorting stays off during the fill: every setItem
            # would re-sort, and the UserRole below would land on
            # the wrong row after a move (v0.6 T6). The user's sort
            # (if any) is re-applied after; otherwise insertion
            # order stands.
            header = self.ep_table.horizontalHeader()
            section = header.sortIndicatorSection()
            order = header.sortIndicatorOrder()
            self.ep_table.setSortingEnabled(False)
            self.ep_table.setRowCount(len(eps))
            for i, e in enumerate(eps):
                self.ep_table.setItem(i, 0, QTableWidgetItem(e["method"]))
                self.ep_table.setItem(i, 1, QTableWidgetItem(e["path"]))
                status = str(e["status"])
                if e.get("websocket"):
                    status += " (WS)"
                self.ep_table.setItem(i, 2, QTableWidgetItem(status))
                # v0.6 T6: how many captured calls fed this endpoint.
                self.ep_table.setItem(
                    i, 3, _SeenItem(str(e.get("samples", 1))))
                # The endpoint carries its sample flow (item 10) — the
                # Replay button re-sends it.
                self.ep_table.item(i, 0).setData(Qt.UserRole, e)
            header.setSortIndicator(-1, Qt.AscendingOrder)
            self.ep_table.setSortingEnabled(True)
            if section >= 0:
                self.ep_table.sortByColumn(section, order)
            self._update_replay_button()
            # v0.6 T14: the timeline's range is computed here, once,
            # from the loaded endpoints — the drag only reads it.
            self._setup_timeline(eps)
            self._filter_ep_table()

        return self._run_task(f"loading endpoints for {host}…", work, done)

    def _setup_timeline(self, eps):
        """(v0.6 T14) The timeline's range, computed once when the
        host loads — the drag only reads it (entry 018)."""
        times = [e["first_seen"] for e in eps
                 if e.get("first_seen") is not None]
        if not times or min(times) == max(times):
            self._timeline_min = None
            self._timeline_max = None
            self.timeline_slider.setEnabled(False)
            self.timeline_slider.setValue(1000)
            self.timeline_label.setText(
                "no times recorded yet" if not times
                else "all calls at one moment")
            return
        self._timeline_min = min(times)
        self._timeline_max = max(times)
        self.timeline_slider.setEnabled(True)
        self.timeline_slider.blockSignals(True)
        self.timeline_slider.setValue(1000)
        self.timeline_slider.blockSignals(False)
        self._update_timeline_label()

    def _timeline_cutoff(self):
        """The timeline's cutoff epoch, or None when the timeline
        shows everything (slider at max, or disabled)."""
        if (self._timeline_min is None or self._timeline_max is None
                or not self.timeline_slider.isEnabled()
                or self.timeline_slider.value() >= 1000):
            return None
        frac = self.timeline_slider.value() / 1000
        return (self._timeline_min
                + (self._timeline_max - self._timeline_min) * frac)

    def _update_timeline_label(self):
        """The timeline's readout: the cutoff time + how many
        endpoints it shows. Counts the timeline filter only — the
        search box has its own count."""
        if self._timeline_min is None:
            return  # _setup_timeline already wrote the label
        cutoff = self._timeline_cutoff()
        shown = total = 0
        for r in range(self.ep_table.rowCount()):
            if self.ep_table.columnSpan(r, 0) > 1:
                continue
            total += 1
            item = self.ep_table.item(r, 0)
            e = item.data(Qt.UserRole) if item is not None else None
            first = e.get("first_seen") if isinstance(e, dict) else None
            if first is None or cutoff is None or first <= cutoff:
                shown += 1
        if cutoff is None:
            self.timeline_label.setText(f"all {total} endpoints")
        else:
            when = time.strftime("%H:%M:%S", time.localtime(cutoff))
            self.timeline_label.setText(
                f"up to {when} ({shown} of {total})")

    def _timeline_moved(self, _value=None):
        """(v0.6 T14) The scrubber moved: re-filter + update the
        readout. The filter data was computed at host load."""
        self._update_timeline_label()
        self._filter_ep_table()

    def _update_replay_button(self):
        """The Replay button is live only when a row with a flow is
        selected (empty-state rows carry no endpoint)."""
        row = self.ep_table.currentRow()
        enabled = False
        if row >= 0:
            item = self.ep_table.item(row, 0)
            enabled = bool(item is not None
                           and item.data(Qt.UserRole))
        self.replay_btn.setEnabled(enabled)

    def _replay_selected(self):
        """Replay the selected endpoint's captured request (item 10):
        re-send it on a worker thread, show the response in a dialog.
        State-changing methods ask first — replaying a POST can repeat
        the action it did."""
        row = self.ep_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "MIMIK", "select an endpoint first")
            return
        item = self.ep_table.item(row, 0)
        endpoint = item.data(Qt.UserRole) if item is not None else None
        if not endpoint or not endpoint.get("flow"):
            QMessageBox.warning(self, "MIMIK", "select an endpoint first")
            return
        method = (endpoint.get("method") or "GET").upper()
        if method not in ("GET", "HEAD"):
            answer = QMessageBox.question(
                self, "MIMIK",
                f"Replay this {method} request?\n\n"
                "It may repeat the action (send again, create a "
                "duplicate).",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        flow = endpoint["flow"]
        if endpoint.get("websocket"):
            # WebSocket replay (item 13): re-send the captured client
            # messages, show the transcript. The endpoint's messages
            # cover both paths — live flows don't carry theirs in the
            # flow dict.
            ws_messages = [m for m in endpoint.get("ws_messages") or []
                           if m.get("from_client")]

            def ws_work(worker):
                return websocket.replay_ws(flow, messages=ws_messages)

            def ws_done(ok, result):
                if not ok:
                    return
                self._show_ws_replay_result(flow, ws_messages, result)

            self._run_task(f"replaying WS {flow['request']['path']}…",
                           ws_work, ws_done)
            return
        # Empty means "use the captured body" — live flows don't carry
        # theirs in the flow dict, saved ones do (exact bytes).
        body = endpoint.get("request_body") or None
        original_status = endpoint.get("status")

        def work(worker):
            return replay.replay_flow(flow, body=body)

        def done(ok, result):
            if not ok:
                return
            self._show_replay_result(flow, original_status, result)

        self._run_task(f"replaying {method} "
                       f"{flow['request']['path']}…", work, done)

    def _show_replay_result(self, flow, original_status, result):
        """The replay result, in a non-modal dialog (item 10)."""
        req = flow.get("request") or {}
        url = replay.flow_url(flow)
        lines = [f"{req.get('method', 'GET')} {url}", ""]
        if result["ok"]:
            lines.append(
                f"Replay: {result['status']} {result['reason']} "
                f"({result['elapsed']:.2f} s)")
            if original_status is not None:
                lines.append(f"Original: {original_status}")
            lines.append("")
            lines.append("--- response headers ---")
            for name, value in result["headers"].items():
                lines.append(f"{name}: {value}")
            lines.append("")
            lines.append("--- body ---")
            lines.append(result["body"] or "(empty)")
            if result["truncated"]:
                lines.append(
                    f"... (truncated to {replay.BODY_PREVIEW_CHARS} chars)")
        else:
            lines.append(f"Replay failed: {result['error']}")
        self._show_result_dialog("Replay result", "\n".join(lines))

    def _show_ws_replay_result(self, flow, sent_messages, result):
        """The WS replay transcript, in a non-modal dialog (item 13)."""
        lines = [f"{websocket.ws_url(flow)} (WebSocket replay)", ""]
        if not result["ok"]:
            lines.append(f"Replay failed: {result['error']}")
            self._show_result_dialog("Replay result", "\n".join(lines))
            return
        lines.append(f"Sent {result['sent']} message(s), received "
                     f"{len(result['received'])}.")
        lines.append("")
        lines.append("--- sent ---")
        for message in sent_messages:
            lines.append("→ " + _ws_message_line(message))
        lines.append("")
        lines.append("--- received ---")
        for item in result["received"]:
            lines.append("← " + _ws_message_line(item))
        if not result["received"]:
            lines.append("(nothing came back)")
        self._show_result_dialog("Replay result", "\n".join(lines))

    def _show_result_dialog(self, title, text):
        """A non-modal text dialog (replay results, HTTP + WS)."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(700, 450)
        lay = QVBoxLayout(dlg)
        text_widget = QTextEdit()
        text_widget.setReadOnly(True)
        text_widget.setPlainText(text)
        lay.addWidget(text_widget)
        self._replay_text = text_widget
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        lay.addWidget(close_btn)
        self._replay_dialog = dlg
        dlg.show()

    # ---- capture diff dialog (item 12) ----------------------------------------

    def _compare_captures(self):
        """Compare two captures — hosts or files — in a dialog."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Compare captures")
        dlg.resize(700, 450)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "Pick two captures: a saved host, or a capture/HAR file. "
            "New/removed endpoints and changed responses are listed."))
        self._diff_combo_a = self._diff_picker(lay, "From:")
        self._diff_combo_b = self._diff_picker(lay, "To:")
        compare_btn = QPushButton("Compare")
        compare_btn.clicked.connect(self._run_diff_dialog)
        lay.addWidget(compare_btn)
        self._diff_text = QTextEdit()
        self._diff_text.setReadOnly(True)
        lay.addWidget(self._diff_text)
        self._diff_dialog = dlg
        dlg.show()

    def _diff_picker(self, lay, label_text):
        """One side of the diff dialog: hosts + a file-browse entry."""
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        combo = QComboBox()
        for host, _n, _latest in store.list_captures():
            combo.addItem(f"{host}", ("host", host))
        combo.addItem("Browse for a file…", ("browse", None))
        # activated (not currentIndexChanged) fires even when the
        # browse entry is already current — the no-hosts case.
        combo.activated.connect(self._diff_browse)
        row.addWidget(combo, 1)
        lay.addLayout(row)
        return combo

    def _diff_browse(self):
        """A diff picker chose "Browse for a file…" — swap in the file."""
        combo = self.sender()
        if combo is None:
            return
        data = combo.currentData()
        if not data or data[0] != "browse":
            return
        path, _selected = QFileDialog.getOpenFileName(
            self, "Pick a capture", "",
            "Captures (*.json *.har);;All files (*)")
        if not path:
            combo.setCurrentIndex(0)
            return
        idx = combo.findData(("browse", None))
        combo.insertItem(idx, f"File: {os.path.basename(path)}",
                         ("file", path))
        combo.setCurrentIndex(idx)

    def _run_diff_dialog(self):
        """Run the dialog's comparison on a worker thread."""
        def _source(combo):
            data = combo.currentData()
            if not data or data[0] not in ("host", "file"):
                return None
            return data[1]

        a = _source(self._diff_combo_a)
        b = _source(self._diff_combo_b)
        if a is None or b is None:
            QMessageBox.warning(self, "MIMIK", "pick two captures first")
            return

        def work(worker):
            flows_a, label_a = diff.load_source(a)
            flows_b, label_b = diff.load_source(b)
            return diff.format_diff(diff.diff_captures(flows_a, flows_b),
                                    label_a, label_b)

        def done(ok, value):
            if not ok:
                # The failure (unknown host, unreadable file) goes in
                # the results area, not a modal — the dialog stays up.
                self._diff_text.setPlainText(
                    f"Could not compare:\n{value}")
                return
            self._diff_text.setPlainText(value)

        self._run_task("comparing captures…", work, done)

    def _unpin_dialog(self):
        """Prepare a Frida unpinning run (v0.4 item 14)."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Unpin a pinned app")
        dlg.resize(700, 450)
        lay = QVBoxLayout(dlg)
        intro = QLabel(
            "A pinned app rejects the proxy's certificate, so it shows "
            "no traffic. Frida hooks the app's TLS check at runtime. "
            "MIMIK fetches the maintained unpinning scripts, bakes in "
            "your mitmproxy CA + proxy address, and gives you the exact "
            "command to run.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("Target:"))
        self._unpin_target = QLineEdit()
        self._unpin_target.setPlaceholderText(
            "com.example.app, an .apk file, or a Linux program")
        row.addWidget(self._unpin_target, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._unpin_browse)
        row.addWidget(browse_btn)
        lay.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Kind:"))
        self._unpin_kind = QComboBox()
        self._unpin_kind.addItem("Auto detect", None)
        self._unpin_kind.addItem("Android app", "android")
        self._unpin_kind.addItem("Linux program", "linux")
        row.addWidget(self._unpin_kind)
        row.addStretch(1)
        lay.addLayout(row)
        prepare_btn = QPushButton("Prepare")
        prepare_btn.clicked.connect(self._run_unpin_dialog)
        lay.addWidget(prepare_btn)
        self._unpin_text = QTextEdit()
        self._unpin_text.setReadOnly(True)
        lay.addWidget(self._unpin_text)
        self._unpin_dialog_ref = dlg
        dlg.show()

    def _unpin_browse(self):
        """Pick an APK file as the unpin target."""
        path, _selected = QFileDialog.getOpenFileName(
            self, "Pick an APK", "",
            "Android apps (*.apk);;All files (*)")
        if path:
            self._unpin_target.setText(path)

    def _run_unpin_dialog(self):
        """Run the dialog's preparation on a worker thread."""
        target = self._unpin_target.text().strip()
        if not target:
            QMessageBox.warning(self, "MIMIK", "enter a target first")
            return
        platform = self._unpin_kind.currentData()

        def work(worker):
            plan = unpin.prepare(target, platform=platform)
            return unpin.format_plan(plan, target)

        def done(ok, value):
            if not ok:
                # The failure (no CA, no git, no objection) goes in
                # the results area, not a modal — the dialog stays up.
                self._unpin_text.setPlainText(
                    f"Could not prepare:\n{value}")
                return
            self._unpin_text.setPlainText(value)

        self._run_task("preparing unpin…", work, done)

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
        # v0.6 T14: search and timeline compose — a row shows only
        # when it passes both. The shared helper does the search
        # pass; the timeline pass hides more rows, never unhides.
        visible, total = self._apply_table_filter(
            self.ep_table, self.ep_filter.text())
        cutoff = self._timeline_cutoff()
        if cutoff is not None:
            for r in range(self.ep_table.rowCount()):
                if self.ep_table.isRowHidden(r):
                    continue  # search already hid it
                if self.ep_table.columnSpan(r, 0) > 1:
                    continue  # empty-state row stays visible
                item = self.ep_table.item(r, 0)
                e = item.data(Qt.UserRole) if item is not None else None
                first = e.get("first_seen") if isinstance(e, dict) else None
                if first is not None and first > cutoff:
                    self.ep_table.setRowHidden(r, True)
                    visible -= 1
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
            "Generate a Python client from captured traffic: "
            "deterministic and free, no LLM."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Host:"))
        self.gen_host = QComboBox()
        self.gen_host.setEditable(True)
        # Set it on the line edit, not the combo: QComboBox's own
        # placeholderText stores the value but never forwards it to
        # the line edit in this Qt build, so nothing renders.
        # v0.6 T4: pending until the first load answers.
        self.gen_host.lineEdit().setPlaceholderText("Loading hosts…")
        row.addWidget(self.gen_host, 1)
        refresh = QPushButton("Refresh hosts")
        refresh.clicked.connect(self._refresh_gen_hosts_clicked)
        row.addWidget(refresh)
        lay.addLayout(row)
        lay.addWidget(_explain_label(
            "The server the app talks to. Usually the one with JSON, "
            "not media or CDN."))
        # v0.6 T7: what Generate will make for the picked host,
        # before the button is pressed. Empty until a host with
        # flows is picked.
        self.gen_preview = QLabel("")
        self.gen_preview.setWordWrap(True)
        lay.addWidget(self.gen_preview)
        self.gen_host.currentTextChanged.connect(self._update_gen_preview)
        self.gen_host.currentTextChanged.connect(
            self._update_gen_button)

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
        # v0.6 T10: real availability lands from the background probe
        # when the tab is shown; until then, say so honestly.
        self.polish_combo.addItem("none (deterministic, free)", "none")
        self.polish_combo.addItem("ollama (checking…)", "ollama")
        self.polish_combo.addItem("claude (checking…)", "claude")
        self.polish_combo.addItem("opencode (checking…)", "opencode")
        row.addWidget(self.polish_combo, 1)
        lay.addLayout(row)
        self.polish_explain = _explain_label(
            "Polish rewrites the generated client so it reads better. "
            "None is free and always works.")
        lay.addWidget(self.polish_explain)

        gen_btn = QPushButton("Generate client")
        gen_btn.setObjectName("big")
        gen_btn.clicked.connect(self._generate)
        self.gen_btn = gen_btn
        # v0.8 B3: nothing to generate until a host is picked/typed.
        gen_btn.setEnabled(False)
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
            worker.progress.emit(f"read {len(flows)} flows…")
            hosts = [h for h, _n in mitm_mod.hosts(flows)]
            worker.progress.emit(f"{len(hosts)} hosts found")
            return hosts

        def done(ok, hosts):
            if not ok:
                # First load never answered: say so honestly
                # instead of leaving the pending text up forever.
                if self.gen_host.lineEdit().placeholderText(
                        ).startswith("Loading"):
                    self.gen_host.lineEdit().setPlaceholderText(
                        "Couldn't load hosts. Press Refresh hosts "
                        "to try again.")
                return
            current = self.gen_host.currentText()
            self.gen_host.clear()
            self.gen_host.addItems(hosts)
            # The honest empty text: it only renders when the
            # combo has no items (v0.6 T4).
            self.gen_host.lineEdit().setPlaceholderText(
                "No hosts yet. Record an app or import a HAR")
            jumped = False
            if current:
                self.gen_host.setCurrentText(current)
            if self._gen_jump_host:
                # A Hosts-tab "Generate client…" jump is waiting — the
                # jump host wins over the restored selection.
                idx = self.gen_host.findText(self._gen_jump_host)
                if idx >= 0:
                    self.gen_host.setCurrentIndex(idx)
                    jumped = True
                self._gen_jump_host = None
            if not self._gen_hosts_loaded_once:
                self._gen_hosts_loaded_once = True
                if not current and not jumped and hosts:
                    # First load: start unselected (v0.6 T7) — the
                    # user picks a host, and the preview says what
                    # Generate will make for it.
                    self.gen_host.setCurrentIndex(-1)
                    self.gen_host.lineEdit().setPlaceholderText(
                        "pick a host")

        return self._run_task("loading hosts…", work, done)

    def _refresh_polish(self):
        """Re-probe polish-engine availability off the UI thread (v0.6
        T10). Runs on its own thread, not the single _run_task slot,
        so it never blocks a hosts load or flashes the progress bar.
        One probe at a time."""
        if self._polish_task is not None:
            return

        def work(_worker):
            return doctor.polish_engines()

        worker = TaskWorker(work)
        self._polish_task = worker
        worker.result.connect(self._polish_finished)
        worker.start()

    def _polish_finished(self, ok, engines):
        """The availability probe answered — rebuild the polish combo
        with real labels, keeping the user's pick."""
        worker = self._polish_task
        self._polish_task = None
        if worker is not None:
            # Join before dropping the reference (see _task_finished):
            # the result signal fires at the end of run(), while the
            # thread is still winding down.
            worker.wait()
        if not ok:
            # The probe itself failed — say nothing is ready rather
            # than leaving "checking…" up forever.
            engines = [
                {"key": "ollama", "ok": False,
                 "label": "ollama (not running)"},
                {"key": "claude", "ok": False,
                 "label": "claude (not installed)"},
                {"key": "opencode", "ok": False,
                 "label": "opencode (not installed)"},
            ]
        current = self.polish_combo.currentData()
        self.polish_combo.clear()
        self.polish_combo.addItem("none (deterministic, free)", "none")
        for engine in engines:
            self.polish_combo.addItem(engine["label"], engine["key"])
        idx = self.polish_combo.findData(current)
        self.polish_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _update_gen_button(self, _text=None):
        """(v0.8 B3) Generate needs a host: enabled only while the
        host box names one (picked from the list or typed). The
        "No hosts yet" state is a placeholder with an empty combo,
        so the button stays disabled there."""
        host = self.gen_host.currentText().strip()
        has_hosts = self.gen_host.count() > 0
        self.gen_btn.setEnabled(bool(host) and has_hosts)

    def _update_gen_preview(self, _text=None):
        """(v0.6 T7) What Generate will make for the picked host,
        from the already-cached flows — distinct endpoints counted
        WITHOUT fetching bodies. Empty until a host with flows is
        picked. Reads the cache only: never triggers a fetch from
        the UI thread."""
        host = self.gen_host.currentText().strip()
        self.gen_preview.setText("")
        if not host or self._flows_cache is None:
            return
        _m, flows, _src = self._flows_cache
        endpoints = set()
        auth = None
        for f in flows:
            req = f.get("request") or {}
            if req.get("host") != host:
                continue
            endpoints.add((req.get("method", ""),
                           (req.get("path") or "").split("?")[0]))
            if auth is None:
                auth = _preview_auth(req.get("headers") or [])
        if not endpoints:
            return
        n = len(endpoints)
        self.gen_preview.setText(
            f"{n} endpoint{'s' if n != 1 else ''} → {n} method"
            f"{'s' if n != 1 else ''}, auth: {auth or 'none seen'}")

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
                        f"not saved: {os.path.basename(out)} already "
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
            self._checklist_tick("generated")
            # Quiet completion state (item 20): the settled status
            # names the client that was written.
            self._done_text = (f"✓ Client written: "
                               f"{os.path.basename(out)}")
            self._update_status_bar()

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
            "Assets captured from your apps: images, models, audio, "
            "fonts. Build them into a Reblend pack."))

        # v0.6 T15: bento — hero asset table on the left, Reblend +
        # pack as side tiles stacked on the right (entry 005: 20 px
        # gaps, 16 px corners, size is the hierarchy).
        brow = QHBoxLayout()
        brow.setSpacing(20)

        # Hero tile: the assets themselves — the main point, the
        # biggest tile.
        hero = QFrame()
        hero.setObjectName("tile")
        hlay = QVBoxLayout(hero)
        hlay.setContentsMargins(12, 12, 12, 12)
        hlay.setSpacing(8)

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
        # v0.8 B3: stored + state-managed by _update_asset_buttons.
        self.asset_delete_group_btn = delete_group_btn
        delete_group_btn.setEnabled(False)
        hlay.addLayout(row)

        self.asset_table = QTableWidget(0, 5)
        self.asset_table.setHorizontalHeaderLabels(
            ["Name", "Kind", "Size", "Source", "Status"])
        # v0.8 B4: Source is the wide column (URLs/paths) — it takes
        # the stretch, not Status; every cell gets a tooltip with its
        # full text, so an elided cell is still readable.
        self.asset_table.horizontalHeader().setStretchLastSection(False)
        self.asset_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.Stretch)
        self.asset_table.itemSelectionChanged.connect(
            self._update_asset_buttons)
        self.asset_table.itemDoubleClicked.connect(self._preview_asset_item)
        _empty_table_row(self.asset_table,
                         "No assets yet. Capture an app or import a HAR.")
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Filter:"))
        self.asset_filter = QLineEdit()
        self.asset_filter.setPlaceholderText("filter assets…")
        self.asset_filter.textChanged.connect(self._filter_asset_table)
        frow.addWidget(self.asset_filter, 1)
        self.asset_filter_info = QLabel("")
        frow.addWidget(self.asset_filter_info)
        hlay.addLayout(frow)
        hlay.addWidget(self.asset_table, 2)

        grid = QGridLayout()
        grid.setSpacing(8)
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
        # v0.8 B3: both act on the selected asset row.
        self.asset_preview_btn = preview_btn
        self.asset_reveal_btn = reveal_btn
        preview_btn.setEnabled(False)
        reveal_btn.setEnabled(False)
        grid.addWidget(har_btn, 0, 0)
        grid.addWidget(scan_btn, 0, 1)
        grid.addWidget(preview_btn, 1, 0)
        grid.addWidget(reveal_btn, 1, 1)
        hlay.addLayout(grid)
        brow.addWidget(hero, 3)

        # Side tiles, stacked: Reblend status, then pack actions.
        side = QVBoxLayout()
        side.setSpacing(20)

        reblend_tile = QFrame()
        reblend_tile.setObjectName("tile")
        rlay = QVBoxLayout(reblend_tile)
        rlay.setContentsMargins(12, 12, 12, 12)
        rlay.setSpacing(8)
        reblend_head = QLabel(
            "Reblend (for converting models into a scene)")
        reblend_head.setObjectName("section")
        rlay.addWidget(reblend_head)
        self.reblend_label = QLabel("")
        self.reblend_label.setWordWrap(True)
        rlay.addWidget(self.reblend_label)
        pick_btn = QPushButton("Choose Reblend folder…")
        pick_btn.clicked.connect(self._choose_reblend)
        rlay.addWidget(pick_btn)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Model scale (cm per unit):"))
        self.scale_spin = QSpinBox()
        self.scale_spin.setRange(1, 10000)
        self.scale_spin.setValue(100)
        srow.addWidget(self.scale_spin)
        srow.addStretch(1)
        rlay.addLayout(srow)
        side.addWidget(reblend_tile)

        pack_tile = QFrame()
        pack_tile.setObjectName("tile")
        play = QVBoxLayout(pack_tile)
        play.setContentsMargins(12, 12, 12, 12)
        play.setSpacing(8)
        pack_head = QLabel("Asset pack")
        pack_head.setObjectName("section")
        play.addWidget(pack_head)
        build_btn = QPushButton("Build asset pack")
        build_btn.setObjectName("big")
        build_btn.clicked.connect(self._build_pack)
        self.build_pack_btn = build_btn
        # v0.8 B3: no capture group picked, nothing to build from.
        build_btn.setEnabled(False)
        play.addWidget(build_btn)
        grid = QGridLayout()
        grid.setSpacing(8)
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
        # v0.8 B3: both act on the current group's built pack.
        self.asset_delete_pack_btn = delete_pack_btn
        self.asset_export_pack_btn = export_pack_btn
        delete_pack_btn.setEnabled(False)
        export_pack_btn.setEnabled(False)
        import_pack_btn = QPushButton("Import pack…")
        import_pack_btn.clicked.connect(self._import_pack)
        grid.addWidget(self.open_pack_btn, 0, 0)
        grid.addWidget(self.open_reblend_btn, 0, 1)
        grid.addWidget(delete_pack_btn, 1, 0)
        grid.addWidget(export_pack_btn, 1, 1)
        grid.addWidget(import_pack_btn, 2, 0)
        play.addLayout(grid)
        self.assets_log = QTextEdit()
        self.assets_log.setReadOnly(True)
        self.assets_log.setMaximumHeight(80)
        self.assets_log.setPlaceholderText(
            "Build a pack to see the result here.")
        play.addWidget(self.assets_log)
        side.addWidget(pack_tile)
        side.addStretch(1)
        brow.addLayout(side, 2)
        lay.addLayout(brow, 1)

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
            # v0.8 B4: short label — the full timestamp made every
            # real entry overflow the combo ("demo.example.com:
            # 2026-11-19T11:…"). The date rides in the tooltip.
            label = (f"{manifest.get('group')} "
                     f"({n} asset{'s' if n != 1 else ''})")
            self.asset_group.addItem(label, gdir)
            if manifest.get("captured_at"):
                self.asset_group.setItemData(
                    self.asset_group.count() - 1,
                    f"captured {manifest['captured_at']}",
                    Qt.ToolTipRole)
        if not groups:
            # No data — a placeholder item (no UserRole data, so
            # _asset_group_selected treats it as "nothing selected").
            self.asset_group.addItem("No captures yet")
        self.asset_group.blockSignals(False)
        if current:
            idx = self.asset_group.findData(current)
            if idx >= 0:
                self.asset_group.setCurrentIndex(idx)
        self._asset_group_selected()

    def _update_asset_buttons(self):
        """(v0.8 B3) Assets action states: Delete group / Build pack
        need a capture group; Preview / Reveal need a selected asset
        row; Delete / Export pack need a built pack for the group
        (pack_dir, wired by _wire_pack_buttons)."""
        has_group = bool(self.asset_group.currentData())
        self.asset_delete_group_btn.setEnabled(has_group)
        self.build_pack_btn.setEnabled(has_group)
        has_asset = self._selected_asset() is not None
        self.asset_preview_btn.setEnabled(has_asset)
        self.asset_reveal_btn.setEnabled(has_asset)
        has_pack = getattr(self, "pack_dir", None) is not None
        if hasattr(self, "asset_delete_pack_btn"):
            self.asset_delete_pack_btn.setEnabled(has_pack)
            self.asset_export_pack_btn.setEnabled(has_pack)

    def _asset_group_selected(self, _idx=None):
        # v0.6 T5: refresh keeps your place — the asset's path is
        # the identity, so a group switch (where the path is gone)
        # clears the selection instead of keeping a wrong row.
        snap = _snapshot_table(self.asset_table)
        self.asset_table.setRowCount(0)
        gdir = self.asset_group.currentData()
        tip = self.asset_group.currentData(Qt.ToolTipRole)
        self.asset_group.setToolTip(
            tip or self.asset_group.currentText())
        if not gdir:
            _empty_table_row(self.asset_table,
                             "No assets yet. Capture an app or import "
                             "a HAR.")
            self.pack_dir = None
            self._update_asset_buttons()
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
            self._update_asset_buttons()
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
                # v0.8 B4: the full URL — the column stretches and
                # the cell tooltip carries it whole; no more
                # code-side cut at 60 chars.
                source = f"traffic: {e['url']}"
            elif e.get("source_path"):
                source = f"program file: {e['source_path']}"
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
            self.asset_table.item(i, 3).setToolTip(source)
            self.asset_table.setItem(
                i, 4, QTableWidgetItem(
                    status_map.get(e.get("sha256"), "saved")))
        _restore_table(self.asset_table, snap, key="path")
        self._filter_asset_table()
        self._update_asset_buttons()

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
            f"{pix.width()} × {pix.height()} px, "
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
                "Reblend not found. Pick your Reblend folder to convert "
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
            QMessageBox.warning(self, "MIMIK", "no captured assets yet. "
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
            "The source assets stay. Only the built pack goes. "
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
            "Presets are rules MIMIK learns from your captures: sign-in, "
            "endpoints, assets. They never hold passwords or tokens."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Learn from:"))
        self.preset_source = QComboBox()
        row.addWidget(self.preset_source, 1)
        learn_btn = QPushButton("Learn preset")
        learn_btn.clicked.connect(self._learn_preset)
        self.learn_btn = learn_btn
        # v0.8 B3: nothing to learn from until a capture exists.
        learn_btn.setEnabled(False)
        self.preset_source.currentIndexChanged.connect(
            self._update_preset_buttons)
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
        # v0.8 B3: both act on the selected preset.
        self.preset_delete_btn = delete_btn
        self.preset_export_btn = export_btn
        delete_btn.setEnabled(False)
        export_btn.setEnabled(False)
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
        # v0.6 T5: refresh keeps your place in both lists.
        snap = _snapshot_list(self.presets_list)
        snap_patterns = _snapshot_list(self.pattern_presets_list)
        self.presets_list.clear()
        saved = presets.list_presets()
        if not saved:
            self.presets_list.addItem(_empty_item(
                "No presets yet. Learn one from a capture above."))
        for p in saved:
            item = QListWidgetItem(
                f"{p['name']} ({p['scope']}): {presets.summary(p)}")
            item.setData(Qt.UserRole, p["name"])
            self.presets_list.addItem(item)
        self.pattern_presets_list.clear()
        patterns = presets.list_pattern_presets()
        if not patterns:
            self.pattern_presets_list.addItem(_empty_item(
                "No pattern presets yet. Learn one from a capture "
                "above."))
        for p in patterns:
            learned_from = p.get("learned_from") or []
            item = QListWidgetItem(
                f"{p.get('display_name') or p['name']}, learned from "
                f"{len(learned_from)} "
                f"app{'s' if len(learned_from) != 1 else ''}")
            item.setData(Qt.UserRole, p["name"])
            self.pattern_presets_list.addItem(item)
        _restore_list(self.presets_list, snap)
        _restore_list(self.pattern_presets_list, snap_patterns)
        # v0.8 G9: a lone preset (either kind) selects itself, so
        # its details and actions are right there.
        if self.presets_list.currentItem() is None \
                and self.pattern_presets_list.currentItem() is None:
            real = []
            for lst in (self.presets_list, self.pattern_presets_list):
                for r in range(lst.count()):
                    if lst.item(r).data(Qt.UserRole):
                        real.append((lst, r))
            if len(real) == 1:
                lst, r = real[0]
                lst.setCurrentRow(r)
        self._preset_selected()

    def _update_preset_buttons(self):
        """(v0.8 B3) Learn needs a capture in the source combo;
        Delete / Export need a selected preset in either list."""
        if hasattr(self, "learn_btn"):
            self.learn_btn.setEnabled(
                bool(self.preset_source.currentData()))
        if hasattr(self, "preset_delete_btn"):
            _is_pat, name = self._selected_preset()
            self.preset_delete_btn.setEnabled(bool(name))
            self.preset_export_btn.setEnabled(bool(name))

    def _preset_selected(self, _current=None, _prev=None):
        self._update_preset_buttons()
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
        self._update_preset_buttons()
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
                    f"nothing to learn from for {name} yet. "
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
        self.clients_list.currentItemChanged.connect(self._client_selected)
        lay.addWidget(self.clients_list, 1)
        # v0.6 T12: preview pane — host, methods, date for the
        # selected client, parsed with ast (never executed).
        self.client_preview = QTextEdit()
        self.client_preview.setReadOnly(True)
        self.client_preview.setMaximumHeight(120)
        self.client_preview.setPlaceholderText(
            "Select a client to see its host, methods, and date.")
        lay.addWidget(self.client_preview)
        smoke = QPushButton("Smoke-test selected client")
        smoke.clicked.connect(self._smoke_test)
        self.smoke_btn = smoke
        delete_btn = QPushButton("Delete selected client…")
        delete_btn.clicked.connect(self._delete_client)
        open_btn = QPushButton("Open selected…")
        open_btn.clicked.connect(self._open_client)
        reveal_btn = QPushButton("Reveal in folder")
        reveal_btn.clicked.connect(self._reveal_client)
        # v0.8 B3: all four act on the selected client.
        self.client_delete_btn = delete_btn
        self.client_open_btn = open_btn
        self.client_reveal_btn = reveal_btn
        for b in (smoke, delete_btn, open_btn, reveal_btn):
            b.setEnabled(False)
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

    def _update_client_buttons(self):
        """(v0.8 B3) The four client actions need a selected client
        (a row with a file path; the placeholder has none)."""
        item = self.clients_list.currentItem()
        has = item is not None and bool(item.data(Qt.UserRole))
        for b in (self.smoke_btn, self.client_delete_btn,
                  self.client_open_btn, self.client_reveal_btn):
            b.setEnabled(has)

    def _refresh_clients(self):
        # v0.6 T5: refresh keeps your place.
        snap = _snapshot_list(self.clients_list)
        self.clients_list.clear()
        clients = store.list_clients()
        if not clients:
            self.clients_list.addItem(_empty_item(
                "No clients yet. Generate one on the Generate tab."))
            self._update_client_buttons()
            return
        for name, path, size in clients:
            item = QListWidgetItem(f"{name}  ({size} bytes)")
            item.setData(Qt.UserRole, path)
            self.clients_list.addItem(item)
        _restore_list(self.clients_list, snap)
        self._filter_clients_list()
        self._update_client_buttons()

    def _client_selected(self, current, _prev):
        """(v0.6 T12) Fill the preview pane for the selected client,
        or clear it when nothing is selected."""
        self._update_client_buttons()
        if current is None or not current.data(Qt.UserRole):
            self.client_preview.clear()
            return
        info = codegen.preview_client(current.data(Qt.UserRole))
        if info is None:
            self.client_preview.setPlainText("Could not read this file.")
            return
        host = info["host"] or "unknown (not a MIMIK client)"
        methods = info["methods"]
        if methods:
            method_line = (f"Methods ({len(methods)}): "
                           + ", ".join(methods))
        else:
            method_line = "Methods: none found"
        generated = info["date"] or "unknown"
        self.client_preview.setPlainText(
            f"Host: {host}\n{method_line}\nGenerated: {generated}")

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
            "This deletes the kept copy: the client file in the "
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
                f"MIMIK.\n\nThe smoke test runs the file's code, "
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

    # ---- Binary tab (v0.5 item 8) --------------------------------------------

    def _binary_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Analyze a program file: what it is, what it calls, what "
            "words it contains. MIMIK reads the file, it never runs it."))
        row = QHBoxLayout()
        self.binary_analyze_btn = QPushButton("Analyze a file…")
        self.binary_analyze_btn.clicked.connect(self._analyze_file_clicked)
        row.addWidget(self.binary_analyze_btn)
        self.binary_ghidra_btn = QPushButton("Run Ghidra pass")
        self.binary_ghidra_btn.clicked.connect(self._run_ghidra_clicked)
        self.binary_ghidra_btn.setEnabled(False)
        row.addWidget(self.binary_ghidra_btn)
        self.binary_delete_btn = QPushButton("Delete analysis…")
        self.binary_delete_btn.clicked.connect(self._delete_binary)
        self.binary_delete_btn.setEnabled(False)
        row.addWidget(self.binary_delete_btn)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_binaries_clicked)
        row.addWidget(refresh)
        row.addStretch(1)
        lay.addLayout(row)
        split = QHBoxLayout()
        self.binaries_list = QListWidget()
        # v0.8 B4: the list is a third of the tab — long item text
        # (and the empty-state sentence) must wrap, not clip
        # mid-word behind a horizontal scrollbar.
        self.binaries_list.setWordWrap(True)
        self.binaries_list.currentItemChanged.connect(
            self._binary_selected)
        self.binaries_list.addItem(_empty_item(
            "No binaries analyzed yet. Analyze a file to see it here."))
        split.addWidget(self.binaries_list, 1)
        self.binary_views = QTabWidget()
        self.binary_summary = QTextEdit()
        self.binary_summary.setReadOnly(True)
        self.binary_summary.setPlaceholderText(
            "Select a binary to see its summary.")
        self.binary_views.addTab(self.binary_summary, "Summary")
        func_w = QWidget()
        func_lay = QVBoxLayout(func_w)
        self.binary_func_filter = QLineEdit()
        self.binary_func_filter.setPlaceholderText("filter functions…")
        self.binary_func_filter.textChanged.connect(
            self._filter_binary_functions)
        func_lay.addWidget(self.binary_func_filter)
        self.binary_func_list = QListWidget()
        self.binary_func_list.currentItemChanged.connect(
            self._binary_function_selected)
        func_lay.addWidget(self.binary_func_list, 1)
        self.binary_views.addTab(func_w, "Functions")
        self.binary_decomp_text = QTextEdit()
        self.binary_decomp_text.setReadOnly(True)
        (decomp_w, self.binary_decomp_state,
         self.binary_decomp_btn) = self._ghidra_state_view(
            self.binary_decomp_text)
        self.binary_views.addTab(decomp_w, "Decompiled")
        self.binary_asm_text = QTextEdit()
        self.binary_asm_text.setReadOnly(True)
        (asm_w, self.binary_asm_state,
         self.binary_asm_btn) = self._ghidra_state_view(
            self.binary_asm_text)
        self.binary_views.addTab(asm_w, "Disassembly")
        str_w = QWidget()
        str_lay = QVBoxLayout(str_w)
        self.binary_str_info = QLabel("")
        self.binary_str_info.setWordWrap(True)
        str_lay.addWidget(self.binary_str_info)
        self.binary_str_filter = QLineEdit()
        self.binary_str_filter.setPlaceholderText("filter strings…")
        self.binary_str_filter.textChanged.connect(
            self._filter_binary_strings)
        str_lay.addWidget(self.binary_str_filter)
        self.binary_str_table = QTableWidget(0, 3)
        self.binary_str_table.setHorizontalHeaderLabels(
            ["Offset", "Text", "Referenced by"])
        self.binary_str_table.horizontalHeader().setStretchLastSection(
            True)
        str_lay.addWidget(self.binary_str_table, 1)
        self.binary_views.addTab(str_w, "Strings")
        self.binary_call_tree = QTreeWidget()
        self.binary_call_tree.setHeaderLabel("Call graph")
        (cg_w, self.binary_cg_state,
         self.binary_cg_btn) = self._ghidra_state_view(
            self.binary_call_tree)
        self.binary_views.addTab(cg_w, "Call graph")
        split.addWidget(self.binary_views, 2)
        lay.addLayout(split, 1)
        self._binary_sha = None
        self._binary_analysis = None
        self._binary_func = None
        self._binary_functions = []
        self._binary_strings = []
        # No load here — the tab loads on first show (_tab_shown).
        return w

    def _ghidra_state_view(self, content):
        """A view whose content needs the Ghidra pass: a state label +
        setup button on top, the content below. Returns (widget,
        state label, setup button)."""
        w = QWidget()
        lay = QVBoxLayout(w)
        state = QLabel("")
        state.setWordWrap(True)
        lay.addWidget(state)
        btn = QPushButton("Ghidra setup steps…")
        btn.clicked.connect(self._show_ghidra_setup)
        lay.addWidget(btn)
        lay.addWidget(content, 1)
        return w, state, btn

    def _set_ghidra_view(self, state, btn, content, what):
        """Show the honest state for a Ghidra-dependent view: no
        binary, no Ghidra install, pass not run yet, or content
        ready. Never a dead end — the setup button is there when
        Ghidra is the missing piece."""
        analysis = self._binary_analysis
        if analysis is None:
            state.setText("Select a binary to see its " + what + ".")
            state.setVisible(True)
            btn.setVisible(False)
            content.setVisible(False)
            return
        if (analysis.get("ghidra") or {}).get("functions") is None:
            det = ghidra.detect()
            if not det["found"] or not det["java_ok"]:
                state.setText(
                    "Ghidra is not installed, so this binary has no "
                    + what + " yet. Install Ghidra, then press "
                    "'Run Ghidra pass' above.")
                btn.setVisible(True)
            else:
                state.setText(
                    "The Ghidra pass has not run for this binary yet. "
                    "Press 'Run Ghidra pass' above.")
                btn.setVisible(False)
            state.setVisible(True)
            content.setVisible(False)
            return
        state.setVisible(False)
        btn.setVisible(False)
        content.setVisible(True)

    def _refresh_binaries_clicked(self, _checked=False):
        self._refresh_binaries()

    def _refresh_binaries(self):
        """Reload the analyzed-binaries list. Returns True (sync — no
        background task needed, the list is small)."""
        rows = binary.list_binaries()
        # v0.6 T5: refresh keeps your place — the model state
        # (_binary_sha) is the selection identity, as before, plus
        # the scrollbar value, which used to reset.
        snap = (self._binary_sha,
                self.binaries_list.verticalScrollBar().value())
        self.binaries_list.clear()
        if not rows:
            self.binaries_list.addItem(_empty_item(
                "No binaries analyzed yet. Analyze a file to see it "
                "here."))
            self._binary_sha = None
            self._binary_analysis = None
            self._binary_func = None
            self.binary_ghidra_btn.setEnabled(False)
            self.binary_delete_btn.setEnabled(False)
            self._show_binary_views()
            return True
        for row in rows:
            item = QListWidgetItem(
                f"{row['source_name']}  ({row['format'].upper()}, "
                f"ghidra: {'yes' if row['has_ghidra'] else 'no'})")
            item.setData(Qt.UserRole, row["sha256"])
            self.binaries_list.addItem(item)
        _restore_list(self.binaries_list, snap)
        return True

    def _select_binary(self, sha):
        """Select a binary in the list by sha256. Returns True when
        it was found."""
        for i in range(self.binaries_list.count()):
            item = self.binaries_list.item(i)
            if item.data(Qt.UserRole) == sha:
                self.binaries_list.setCurrentRow(i)
                return True
        return False

    def _binary_selected(self, current, _prev):
        sha = current.data(Qt.UserRole) if current is not None else None
        if not sha:
            self._binary_sha = None
            self._binary_analysis = None
            self._binary_func = None
            self.binary_ghidra_btn.setEnabled(False)
            self.binary_delete_btn.setEnabled(False)
            self._show_binary_views()
            return
        self._binary_sha = sha
        self._binary_analysis = binary.load_analysis(sha)
        self._binary_func = None
        self.binary_ghidra_btn.setEnabled(True)
        self.binary_delete_btn.setEnabled(True)
        self._show_binary_views()

    def _show_binary_views(self):
        """Fill every inner view from the selected analysis (or clear
        them when nothing is selected)."""
        analysis = self._binary_analysis
        if analysis is None:
            self.binary_summary.setPlainText("")
            self._binary_functions = []
            self._binary_strings = []
            self._fill_binary_functions()
            self._fill_binary_strings()
            self.binary_call_tree.clear()
            self._set_ghidra_view(self.binary_decomp_state,
                                  self.binary_decomp_btn,
                                  self.binary_decomp_text,
                                  "decompiled code")
            self._set_ghidra_view(self.binary_asm_state,
                                  self.binary_asm_btn,
                                  self.binary_asm_text,
                                  "disassembly listing")
            self._set_ghidra_view(self.binary_cg_state,
                                  self.binary_cg_btn,
                                  self.binary_call_tree,
                                  "call graph")
            return
        self.binary_summary.setPlainText(
            self._binary_summary_text(analysis))
        gh = analysis.get("ghidra") or {}
        self._binary_functions = gh.get("functions") or []
        self._binary_strings = analysis["strings"]
        self.binary_func_filter.setText("")
        self.binary_str_filter.setText("")
        self._fill_binary_functions()
        self._fill_binary_strings()
        self._fill_binary_callgraph()
        self._set_ghidra_view(self.binary_decomp_state,
                              self.binary_decomp_btn,
                              self.binary_decomp_text,
                              "decompiled code")
        self._set_ghidra_view(self.binary_asm_state,
                              self.binary_asm_btn,
                              self.binary_asm_text,
                              "disassembly listing")
        self._set_ghidra_view(self.binary_cg_state,
                              self.binary_cg_btn,
                              self.binary_call_tree,
                              "call graph")
        self._show_binary_function()

    def _binary_summary_text(self, analysis):
        """The Summary view: plain words, real numbers."""
        lines = [analysis["source_name"],
                 f"{analysis['format'].upper()}, "
                 f"{store.format_size(analysis['size'])}, analyzed "
                 f"{analysis['analyzed_at']}",
                 f"SHA-256: {analysis['sha256']}",
                 f"Source: {analysis['source_path']}", ""]
        if analysis["sections"]:
            lines.append(f"Sections ({len(analysis['sections'])}):")
            for s in analysis["sections"]:
                note = " (packed?)" if s["packed"] else ""
                lines.append(f"  {s['name']}: {s['size']} bytes, "
                             f"entropy {s['entropy']:.2f}{note}")
            lines.append("")
        if analysis["imports"]:
            lines.append(f"Imports ({len(analysis['imports'])}): "
                         + ", ".join(analysis["imports"]))
        if analysis["exports"]:
            lines.append(f"Exports ({len(analysis['exports'])}): "
                         + ", ".join(analysis["exports"]))
        if analysis["imports"] or analysis["exports"]:
            lines.append("")
        lines.append(f"Strings: {analysis['strings_total']}"
                     + (" (list truncated at 50,000)"
                        if analysis["strings_truncated"] else ""))
        gh = analysis.get("ghidra") or {}
        if gh.get("functions") is None:
            lines.append("Ghidra: pass not run yet")
        else:
            edges = (gh.get("callgraph") or {}).get("edges") or []
            lines.append(f"Ghidra: {len(gh['functions'])} functions, "
                         f"{len(edges)} call edges "
                         f"(Ghidra {gh.get('version') or '?'})")
        linked = [host for host, sha in binary.get_links().items()
                  if sha == analysis["sha256"]]
        if linked:
            lines.append("Linked captures: " + ", ".join(sorted(linked)))
        if analysis["parse_notes"]:
            lines.append("")
            lines.append("Notes:")
            for note in analysis["parse_notes"]:
                lines.append("  " + note)
        return "\n".join(lines)

    def _fill_binary_functions(self):
        """The Functions list, filtered by the filter box."""
        needle = self.binary_func_filter.text().strip().lower()
        self.binary_func_list.clear()
        funcs = self._binary_functions
        if needle:
            funcs = [f for f in funcs
                     if needle in f["name"].lower()
                     or needle in f["address"].lower()]
        if not self._binary_functions:
            if self._binary_analysis is None:
                self.binary_func_list.addItem(_empty_item(
                    "Select a binary to see its functions."))
            else:
                self.binary_func_list.addItem(_empty_item(
                    "No functions yet. Functions come from the "
                    "Ghidra pass (press 'Run Ghidra pass' above, "
                    "or install Ghidra first)."))
            return
        if not funcs:
            self.binary_func_list.addItem(_empty_item(
                "No functions match that filter."))
            return
        for fn in funcs:
            item = QListWidgetItem(
                f"{fn['address']}  {fn['name']}")
            item.setData(Qt.UserRole, fn["address"])
            self.binary_func_list.addItem(item)

    def _filter_binary_functions(self, _text):
        self._fill_binary_functions()

    def _binary_function_selected(self, current, _prev):
        addr = current.data(Qt.UserRole) if current is not None else None
        self._binary_func = addr
        self._show_binary_function()
        self._fill_binary_callgraph()

    def _show_binary_function(self):
        """Decompiled C + disassembly for the selected function."""
        analysis = self._binary_analysis
        if analysis is None:
            self.binary_decomp_text.setPlainText("")
            self.binary_asm_text.setPlainText("")
            return
        if (analysis.get("ghidra") or {}).get("functions") is None:
            return  # the honest state label is showing instead
        if not self._binary_func:
            self.binary_decomp_text.setPlainText(
                "Select a function (Functions tab) to see its "
                "decompiled C.")
            self.binary_asm_text.setPlainText(
                "Select a function (Functions tab) to see its "
                "disassembly.")
            return
        name = ""
        for fn in self._binary_functions:
            if fn["address"] == self._binary_func:
                name = fn["name"]
                break
        c_path, asm_path = binary.function_files(
            analysis["sha256"], self._binary_func, name)
        if c_path is not None:
            try:
                with open(c_path, "r", encoding="utf-8",
                          errors="replace") as f:
                    self.binary_decomp_text.setPlainText(f.read())
            except OSError:
                self.binary_decomp_text.setPlainText(
                    "Could not read the decompiled C file.")
        else:
            self.binary_decomp_text.setPlainText(
                "Ghidra did not produce decompiled C for this "
                "function.")
        if asm_path is not None:
            try:
                with open(asm_path, "r", encoding="utf-8",
                          errors="replace") as f:
                    self.binary_asm_text.setPlainText(f.read())
            except OSError:
                self.binary_asm_text.setPlainText(
                    "Could not read the disassembly file.")
        else:
            self.binary_asm_text.setPlainText(
                "Ghidra did not produce disassembly for this "
                "function.")

    def _fill_binary_strings(self):
        """The Strings table, filtered by the filter box. The
        'Referenced by' column is honest: without Ghidra it says
        so (item 7)."""
        needle = self.binary_str_filter.text().strip().lower()
        analysis = self._binary_analysis
        strings = self._binary_strings
        if needle:
            strings = [s for s in strings
                       if needle in s["text"].lower()]
        gh = (analysis or {}).get("ghidra") or {}
        xref_map = {}
        if gh.get("xrefs") is not None:
            names = (gh.get("callgraph") or {}).get("names") or {}
            for entry in (gh.get("xrefs") or {}).get("strings") or []:
                addr = _addr_int(entry.get("address", ""))
                if addr is None:
                    continue
                callers = [names.get(a, a)
                           for a in entry.get("referenced_by") or []]
                xref_map[addr] = ", ".join(callers) or "no references"
        total = analysis["strings_total"] if analysis else 0
        shown = min(len(strings), 1000)
        if analysis is None:
            self.binary_str_info.setText(
                "Select a binary to see the words inside it.")
        elif gh.get("xrefs") is None:
            self.binary_str_info.setText(
                f"{total} strings (showing {shown}). The "
                "'Referenced by' column needs the Ghidra pass.")
        else:
            self.binary_str_info.setText(
                f"{total} strings (showing {shown}). 'Referenced by' "
                "names the functions Ghidra found using each string.")
        table = self.binary_str_table
        table.setRowCount(0)
        table.setRowCount(shown)
        sections = analysis["sections"] if analysis else []
        for row, s in enumerate(strings[:shown]):
            table.setItem(row, 0, QTableWidgetItem(
                "0x%x" % s["offset"]))
            table.setItem(row, 1, QTableWidgetItem(s["text"]))
            if gh.get("xrefs") is None:
                ref = "needs Ghidra" if analysis else ""
            else:
                vaddr = _string_vaddr(s["offset"], sections)
                ref = xref_map.get(vaddr, "no references")
            table.setItem(row, 2, QTableWidgetItem(ref))

    def _filter_binary_strings(self, _text):
        self._fill_binary_strings()

    def _fill_binary_callgraph(self):
        """Callers/callees tree for the selected function."""
        tree = self.binary_call_tree
        tree.clear()
        analysis = self._binary_analysis
        if analysis is None:
            return
        gh = analysis.get("ghidra") or {}
        if gh.get("callgraph") is None:
            return  # the honest state label is showing instead
        names = gh["callgraph"].get("names") or {}
        edges = gh["callgraph"].get("edges") or []
        if not self._binary_func:
            tree.addTopLevelItem(QTreeWidgetItem([
                "Select a function (Functions tab) to see what it "
                "calls and what calls it."]))
            return
        addr = self._binary_func
        callees = [c for (a, c) in edges if a == addr]
        callers = [a for (a, c) in edges if c == addr]
        root = QTreeWidgetItem([names.get(addr, addr)])
        tree.addTopLevelItem(root)
        calls = QTreeWidgetItem([f"Calls ({len(callees)})"])
        root.addChild(calls)
        for callee in sorted(set(callees)):
            calls.addChild(QTreeWidgetItem(
                [names.get(callee, callee)]))
        called_by = QTreeWidgetItem(
            [f"Called by ({len(callers)})"])
        root.addChild(called_by)
        for caller in sorted(set(callers)):
            called_by.addChild(QTreeWidgetItem(
                [names.get(caller, caller)]))
        tree.expandAll()

    def _analyze_file_clicked(self):
        """Analyze a file: pick it, run the stdlib pass on a worker,
        select the new binary when it lands."""
        path, _filter = QFileDialog.getOpenFileName(
            self, "Analyze a binary", "",
            "All files (*)")
        if not path:
            return

        def work(worker):
            return binary.analyze(path)

        def done(ok, value):
            if not ok:
                QMessageBox.warning(
                    self, "MIMIK",
                    f"Could not analyze that file:\n{value}")
                return
            self._refresh_binaries()
            self._select_binary(value["sha256"])

        self._run_task(f"analyzing {os.path.basename(path)}…",
                       work, done, disable=(self.binary_analyze_btn,))

    def _run_ghidra_clicked(self):
        """Run the Ghidra pass for the selected binary (worker
        thread, progress + cancel). Ghidra missing is not a dead
        end — the setup dialog opens instead."""
        det = ghidra.detect()
        if not det["found"] or not det["java_ok"]:
            self._show_ghidra_setup()
            return
        analysis = self._binary_analysis
        if analysis is None:
            return
        sha = analysis["sha256"]
        source = analysis["source_path"]

        def work(worker):
            return ghidra.run_analysis(
                source, binary.analysis_dir(sha),
                cancelled=lambda: worker.cancelled,
                on_log=worker.progress.emit)

        def done(ok, value):
            if not ok:
                QMessageBox.warning(
                    self, "MIMIK",
                    f"The Ghidra pass failed:\n{value}")
                return
            if value.get("cancelled"):
                return
            self._binary_analysis = binary.update_ghidra(sha, value)
            self._show_binary_views()
            self._refresh_binaries()
            self._select_binary(sha)

        self._run_task("running Ghidra…", work, done,
                       disable=(self.binary_ghidra_btn,
                                self.binary_analyze_btn))

    def _delete_binary(self):
        """Delete the selected binary's analysis (never the file)."""
        analysis = self._binary_analysis
        if analysis is None:
            return
        answer = QMessageBox.question(
            self, "MIMIK",
            f"Delete the analysis for {analysis['source_name']}?\n\n"
            "The program file itself is not touched.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        binary.delete_binary(analysis["sha256"])
        self._refresh_binaries()

    # ---- Doctor tab -----------------------------------------------------------

    def _doctor_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Setup check: green is good, red has a fix."))
        # v0.6 T11: stored, so tab switches can focus it.
        self.doctor_recheck_btn = QPushButton("Re-check")
        self.doctor_recheck_btn.clicked.connect(self._run_doctor)
        about = QPushButton("About MIMIK")
        about.clicked.connect(self._show_about)
        view_log = QPushButton("View log")
        view_log.clicked.connect(self._show_log)
        sample_btn = QPushButton("Load sample capture")
        sample_btn.clicked.connect(self._load_sample_clicked)
        update_btn = QPushButton("Check for updates…")
        update_btn.clicked.connect(self._check_for_updates)
        ghidra_btn = QPushButton("Ghidra setup steps…")
        ghidra_btn.clicked.connect(self._show_ghidra_setup)
        debug_btn = QPushButton("Copy debug report")
        debug_btn.clicked.connect(self._copy_debug_report)
        row = QHBoxLayout()
        row.addWidget(self.doctor_recheck_btn)
        row.addWidget(about)
        row.addWidget(view_log)
        row.addWidget(sample_btn)
        row.addWidget(update_btn)
        row.addWidget(ghidra_btn)
        row.addWidget(debug_btn)
        lay.addLayout(row)
        self.doctor_text = QTextEdit()
        self.doctor_text.setReadOnly(True)
        self.doctor_text.setPlaceholderText("Running the setup check…")
        # v0.6 T4: the check names with a pending state, so the tab
        # is never a blank box — the real results replace them when
        # the probe finishes.
        self.doctor_text.setHtml(self._render_doctor_pending())
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
            "Capture your own session with mitmproxy, or import a "
            "HAR file or cURL paste, and MIMIK reuses your real "
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
            tail or "(the log is empty: no errors recorded yet)")
        lay.addWidget(text, 1)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        lay.addWidget(close)
        self._log_dialog = dlg
        dlg.exec()

    def _copy_debug_report(self):
        """Copy the scrubbed debug report to the clipboard (v0.5
        item 15, U12). Tokens are redacted before it leaves the
        machine."""
        QApplication.clipboard().setText(log.debug_report())
        self.doctor_text.append(
            "Debug report copied to the clipboard (tokens redacted).")

    def _show_howto(self):
        """How to Use index in its own window (v0.5 item 13, U22):
        topic list on the left, plain-language text on the right."""
        dlg = QDialog(self)
        dlg.setWindowTitle("How to Use MIMIK")
        dlg.resize(750, 500)
        lay = QVBoxLayout(dlg)
        split = QHBoxLayout()
        lst = QListWidget()
        for key, title in howto.topics():
            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, key)
            lst.addItem(item)
        split.addWidget(lst, 1)
        text = QTextEdit()
        text.setReadOnly(True)
        split.addWidget(text, 2)
        lay.addLayout(split, 1)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        lay.addWidget(close)
        self._howto_dialog = dlg
        self._howto_list = lst
        self._howto_text = text
        lst.currentItemChanged.connect(self._howto_topic_selected)
        if lst.count():
            lst.setCurrentRow(0)
        dlg.exec()

    def _howto_topic_selected(self, current, _prev):
        """Show the selected How to Use topic's text."""
        if current is None:
            return
        text = howto.body(current.data(Qt.UserRole))
        if text is not None:
            self._howto_text.setPlainText(text)

    def _show_ghidra_setup(self):
        """The Ghidra setup steps in a read-only dialog (v0.5 item 11).
        Prints steps; downloads nothing."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Ghidra setup steps")
        dlg.resize(700, 450)
        lay = QVBoxLayout(dlg)
        det = ghidra.detect()
        if det["found"] and det["java_ok"]:
            lay.addWidget(QLabel(
                f"Ghidra {det['version']} is installed and ready "
                f"(JDK {det['java_major']}, heap {det['heap']})."))
        else:
            lay.addWidget(QLabel(
                "Ghidra is not set up yet. Follow these steps "
                "(MIMIK downloads nothing on its own):"))
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n".join(ghidra.setup_steps()))
        lay.addWidget(text, 1)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        lay.addWidget(close)
        self._ghidra_setup_dialog = dlg
        dlg.exec()

    # ---- first-run checklist (v0.6 T3, entries 014 + 020) ------------------
    # The old welcome modal + sample-guide modal chain is gone (U18):
    # a fresh data dir gets the sample capture pre-loaded, silently,
    # and the Record tab shows a four-step checklist that ticks as
    # the steps really happen. No modal ever appears at startup.

    def _maybe_first_run(self):
        """First-run setup: a fresh data dir gets the sample capture
        pre-loaded (no modal) and the checklist on the Record tab. A
        returning user whose steps are incomplete sees the checklist
        too; a user who finished (or dismissed) never sees it.
        Called once from main() after the window is up — never from
        MainWindow itself, so tests that build a window on an empty
        dir don't load the sample unasked."""
        if sample.is_fresh():
            self._load_sample()
            presets.set_sample_offered()
        if _settings().value("checklist/dismissed", False, type=bool):
            return
        if not self._refresh_checklist():
            self.checklist_box.setVisible(True)

    def _checklist_done(self, key):
        """Whether a checklist step is done: the persisted flag, or
        the real store state — a returning user with captures and
        clients has done these steps already, so the checklist never
        nags someone who finished before it existed."""
        if _settings().value(f"checklist/{key}", False, type=bool):
            return True
        if key == "picked":
            return True  # opening the app was real work
        try:
            if key in ("started", "host_seen"):
                # The sample capture doesn't count: it is pre-loaded,
                # not the user's own recording. Any other host does —
                # traffic in the store means the record stage is done.
                return any(h != sample.SAMPLE_HOST
                           for h, _n, _t in store.list_captures())
            if key == "generated":
                return bool(store.list_clients())
        except Exception:
            return False
        return False

    def _refresh_checklist(self):
        """Re-render the step ticks. Returns True when every step is
        done — the box goes away and never comes back."""
        all_done = True
        for key, text in _CHECKLIST_STEPS:
            done = self._checklist_done(key)
            mark = "✓" if done else "○"
            self._checklist_labels[key].setText(f"{mark} {text}")
            all_done = all_done and done
        if all_done:
            _settings().setValue("checklist/dismissed", True)
            self.checklist_box.setVisible(False)
        return all_done

    def _checklist_tick(self, key):
        """A step really happened — persist it and re-render. A
        second tick of the same step is a no-op, so the live-hosts
        poll (every couple of seconds) costs one settings read."""
        if _settings().value(f"checklist/{key}", False, type=bool):
            return
        _settings().setValue(f"checklist/{key}", True)
        self._refresh_checklist()

    def _checklist_dismiss(self):
        """The user is done with the checklist — never reappears on
        its own (Help menu can bring it back)."""
        _settings().setValue("checklist/dismissed", True)
        self.checklist_box.setVisible(False)

    def _checklist_show(self):
        """Help menu: show the checklist again after a dismiss. A
        no-op when every step is already done — there is nothing to
        show."""
        _settings().setValue("checklist/dismissed", False)
        if not self._refresh_checklist():
            self.checklist_box.setVisible(True)

    def _load_sample_clicked(self):
        """The Doctor tab's "Load sample capture" button. Reports
        into the Doctor text — no modal (v0.6 T3)."""
        result = self._load_sample()
        if result["already_loaded"]:
            self.doctor_text.append(
                f"The sample capture is already loaded "
                f"({result['flows']} flows for {sample.SAMPLE_HOST}).")
        else:
            self.doctor_text.append(
                f"Sample capture loaded: {result['flows']} flows for "
                f"{sample.SAMPLE_HOST}. Open the Hosts and Learn tab "
                f"to see it, then Generate a client.")

    def _load_sample(self):
        """Load the sample capture and refresh every tab that shows
        its data. Returns the load_sample result dict."""
        result = sample.load_sample()
        for key in ("hosts", "generate", "assets", "presets"):
            self._loaded.discard(key)
        self._refresh_saved_flows()
        self._update_status_bar()
        return result

    # ---- updates (v0.7) -------------------------------------------------
    # The launch check is silent (current = nothing, failure = a
    # status-bar line); the manual check reports both ways. A newer
    # release is always asked about, never pulled unasked; the pull
    # is verified against the release's checksum before anything is
    # staged, and the restart that completes it is asked about too.

    def _startup_update_check_enabled(self):
        """Whether the silent launch check is on (default True)."""
        return _settings().value("update/check_on_startup", True,
                                 type=bool)

    def _toggle_startup_update_check(self):
        """Help menu toggle: the action's checked state is the
        setting."""
        action = self.sender()
        checked = action.isChecked() if action is not None else True
        _settings().setValue("update/check_on_startup", checked)

    def _maybe_startup_update_check(self):
        """The launch-time update step, deferred until the window is
        up (called from main()). A note main() left, a staged update
        that did not verify or a failed install, shows once first;
        then the silent check runs, once per launch, when enabled."""
        if self._update_check_ran:
            return
        self._update_check_ran = True
        if self._startup_update_note:
            note = self._startup_update_note
            failed = self._startup_update_failed
            self._startup_update_note = None
            self._startup_update_failed = False
            if failed:
                QMessageBox.information(self, "MIMIK", note)
            else:
                self.statusBar().showMessage(note, 10000)
        if self._startup_update_check_enabled():
            self._start_update_check(silent=True)

    def _check_for_updates(self):
        """Manual update check (Help menu, Doctor tab): one GitHub
        API call, off the UI thread, reporting both ways."""
        self._start_update_check(silent=False)

    def _start_update_check(self, silent):
        """Run one update check off the UI thread, on the update
        worker slot (not the single _run_task slot). Returns False
        when a check or download is already running: refused plainly
        for a manual check, silently for the launch check."""
        if self._update_task is not None:
            if not silent:
                QMessageBox.information(
                    self, "MIMIK", "Already checking for updates.")
            return False

        def work(_worker):
            return update.check_for_update()

        self._update_silent = silent
        self._update_phase = "check"
        worker = TaskWorker(work)
        self._update_task = worker
        worker.result.connect(self._update_task_finished)
        worker.start()
        return True

    def _update_task_finished(self, ok, value):
        """An update check or download answered. Joins the thread,
        closes the progress dialog, and, unless the close was
        deferred or the work was cancelled, hands the result to the
        phase handler."""
        worker = self._update_task
        self._update_task = None
        if worker is not None:
            worker.wait()
        if self._update_progress is not None:
            try:
                self._update_progress.close()
            except RuntimeError:
                pass  # the dialog went away with its parent
            self._update_progress = None
        cancelled = worker is not None and worker.cancelled
        if self._close_pending:
            # The close was deferred until this work ended: close now
            # instead of showing any update dialog.
            self._close_pending = False
            self.close()
            return
        if cancelled:
            return
        if self._update_phase == "download":
            self._download_finished(ok, value)
        else:
            self._check_finished(ok, value)

    def _check_finished(self, ok, result):
        """A check answered. Silent (launch) checks say nothing when
        current, and only a status-bar line when they fail; manual
        checks report both ways. A newer release is offered."""
        if not ok:
            if self._update_silent:
                self.statusBar().showMessage(
                    "could not check for updates.", 10000)
            else:
                QMessageBox.warning(
                    self, "MIMIK",
                    f"could not check for updates: {result}")
            return
        if result["error"]:
            if self._update_silent:
                self.statusBar().showMessage(
                    "could not check for updates.", 10000)
            else:
                QMessageBox.warning(
                    self, "MIMIK",
                    f"could not check for updates: {result['error']}")
            return
        if not result["update_available"]:
            if not self._update_silent:
                QMessageBox.information(
                    self, "MIMIK",
                    f"MIMIK {result['current']} is the latest version.")
            return
        self._update_result = result
        self._offer_update(result)

    def _offer_update(self, result):
        """A newer release is out: ask. Installed runs get "Update
        now"; a copy run from a source tree gets the releases page,
        as before (there is no installed copy to update)."""
        if update.is_installed():
            answer = QMessageBox.question(
                self, "MIMIK",
                f"MIMIK {result['latest']} is available "
                f"(you have {result['current']}).\n\n"
                "Download and install it now? The download is checked "
                "against the release's checksum before anything is "
                "installed.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer == QMessageBox.Yes:
                self._start_update_download(result)
            return
        answer = QMessageBox.question(
            self, "MIMIK",
            f"MIMIK {result['latest']} is available "
            f"(you have {result['current']}).\n\n"
            "Open the releases page in your browser?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl(result["url"]))

    def _start_update_download(self, result):
        """Pull the release zip on the update worker, with a progress
        dialog. The download is verified before anything is staged
        (see update.download_update)."""
        self._update_phase = "download"
        self._update_result = result
        dest = update.pending_dir()

        def work(worker):
            def progress(done, total):
                if total:
                    worker.progress.emit(str(int(done * 100 / total)))
                else:
                    worker.progress.emit("-1")
            return update.download_update(
                result["zip_url"], result["sha_url"], dest,
                progress=progress)

        dialog = QProgressDialog(
            f"Downloading MIMIK {result['latest']}…", "Cancel",
            0, 100, self)
        dialog.setCancelButton(None)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setWindowFlags(dialog.windowFlags()
                              & ~Qt.WindowCloseButtonHint)
        self._update_progress = dialog
        worker = TaskWorker(work)
        self._update_task = worker
        worker.progress.connect(self._update_download_progress)
        worker.result.connect(self._update_task_finished)
        worker.start()
        dialog.show()

    def _update_download_progress(self, text):
        """A download progress tick: a percent, or -1 when the server
        sent no length (the bar goes busy)."""
        dialog = self._update_progress
        if dialog is None:
            return
        try:
            pct = int(text)
        except ValueError:
            return
        if pct < 0:
            dialog.setRange(0, 0)
            return
        if dialog.maximum() == 0:
            dialog.setRange(0, 100)
        dialog.setValue(pct)

    def _download_finished(self, ok, result):
        """A download answered. Success stages the update and asks
        about the restart; failure says why in plain words."""
        if not ok:
            QMessageBox.warning(
                self, "MIMIK",
                f"the update download failed: {result}")
            return
        if not result["ok"]:
            QMessageBox.warning(
                self, "MIMIK",
                f"the update was not installed: {result['error']}")
            return
        info = self._update_result or {}
        latest = info.get("latest", "")
        update.write_pending(latest, result["path"], result["sha256"])
        self._update_result = None
        answer = QMessageBox.question(
            self, "MIMIK",
            f"MIMIK {latest} is downloaded and checked.\n\n"
            "Restart MIMIK now to complete the update?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            self._restart_for_update(latest)
        else:
            self.statusBar().showMessage(
                f"Update to {latest} is ready - it will install when "
                "MIMIK restarts.", 10000)

    def _restart_for_update(self, version):
        """Write the updater script and close: closeEvent launches the
        updater once the close is definitely going ahead. When the
        close is refused (a recording the user would not stop), the
        update stays staged for the next launch."""
        marker = update.read_pending()
        if marker is None:
            return
        zip_path = os.path.join(update.pending_dir(), marker["zip"])
        try:
            udir = update.updates_dir()
            os.makedirs(udir, mode=0o700, exist_ok=True)
            script = os.path.join(udir, "update.sh")
            with open(script, "w") as f:
                f.write(update.updater_script_text(
                    os.getpid(), zip_path, version))
            os.chmod(script, 0o700)
        except OSError as e:
            QMessageBox.warning(
                self, "MIMIK", f"could not start the update: {e}")
            return
        self._restart_script = script
        self.close()
        if self.isVisible() and not self._close_pending:
            # The close was refused: the update stays staged.
            self._restart_script = None

    def _render_doctor_pending(self):
        """The setup check's names with a pending marker (v0.6 T4):
        shown while the probes run, instead of a blank box."""
        grey = "#9e9e9e"

        def line(text):
            return (f'<font color="{grey}">'
                    f"{html.escape(text)}</font>")

        names = doctor.check_names()
        out = [line(f"…  {n}") for n in names["required"]]
        out.append(line("…  mitmweb running + reachable"))
        out.append("")
        out.append(line("optional:"))
        out.extend(line(f"…  {n}") for n in names["optional"])
        return "<br>".join(out)

    def _run_doctor(self):
        """Setup check — the network probes (mitmweb, Ollama) run off
        the UI thread, so opening the tab never freezes the window.
        Returns False if a task is already running."""
        def work(worker):
            return doctor.doctor()

        def done(ok, result):
            if ok:
                self.doctor_text.setHtml(self._render_doctor(result))
            else:
                self.doctor_text.setHtml(
                    "<i>The setup check failed. Press Re-check to "
                    "try again.</i>")

        started = self._run_task("running setup check…", work, done)
        if started:
            self.doctor_text.setHtml(self._render_doctor_pending())
        return started

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
            None, "MIMIK: something went wrong",
            f"MIMIK hit an unexpected error:\n\n{exc}\n\n"
            f"The details were written to:\n{log.log_path()}\n\n"
            "MIMIK is still running. You can keep working, or "
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
    # v0.7: a staged update applies before anything else opens. The
    # user already said yes to the update; only the restart was
    # deferred, so there is no second ask. A failed install is
    # reported once, after the window is up (see
    # _maybe_startup_update_check).
    failed = update.read_failed()
    outcome = None
    if failed is not None:
        update.clear_pending()
    else:
        outcome = update.apply_pending()
    if outcome == "apply":
        sys.exit(0)
    # Single instance (v0.5 item 14, U27): a second launch asks the
    # running window to come forward and exits, instead of starting
    # a second copy.
    from .single import SingleInstance

    guard = SingleInstance()
    if not guard.try_take():
        if guard.notify_running():
            sys.exit(0)
        # No server answered: the owner may have died between taking
        # the lock and starting its server. The notify retries gave
        # the lock file time to age past the stale window, so a
        # second try takes a stale lock over cleanly.
        if not guard.try_take():
            sys.exit(0)
    # Measure the PC once at startup (v0.5 item 17/U35): the values
    # land in the debug log, never in front of the user.
    try:
        from . import machine

        machine.log_measurements()
    except Exception:
        pass
    app.setApplicationName("MIMIK")
    app.setWindowIcon(window_icon())
    app.setStyleSheet(effective_stylesheet())
    win = MainWindow()
    # v0.8 G1: encrypted captures ask for the passphrase once, at
    # startup. Cancelling (or three wrong tries) opens the window
    # locked: the captures are never mistaken for an empty store,
    # and File › Unlock captures… stays available all session.
    if store.encryption_enabled() and not store.unlocked():
        for _attempt in range(3):
            pw = win._ask_passphrase(
                "Unlock captures",
                "Your captures are encrypted. Type the passphrase:")
            if pw is None:
                break
            try:
                if store.unlock(pw):
                    break
            except crypto.CryptoError:
                break
            QMessageBox.warning(
                win, "MIMIK",
                "That passphrase did not unlock the captures.")
        win._refresh_encryption_actions()
        win._refresh_saved_flows()
        win._update_status_bar()
    if failed is not None:
        win._startup_update_note = (
            f"The update did not install: {failed}\n\n"
            f"MIMIK is still on {__version__}. Checking for updates "
            "again will offer it fresh.")
        win._startup_update_failed = True
    elif outcome == "cleared":
        win._startup_update_note = (
            "The staged update did not check out, so it was thrown "
            "away. Checking for updates again will offer it fresh.")
    guard.start_server(win.bring_to_front)
    win.show()
    # After the first event-loop turn: warn if a previous capture left
    # the Wine prefix pointing at the proxy (a saved state with no
    # restore). Deferred so the window is up before any dialog appears.
    QTimer.singleShot(0, win.check_stale_wine_state)
    # Same deferral for the first-run setup (v0.6 T3): the sample
    # pre-loads silently and the checklist appears on the Record tab.
    QTimer.singleShot(0, win._maybe_first_run)
    # And for the silent launch update check (v0.7): it runs once,
    # off the UI thread, and says nothing when current.
    QTimer.singleShot(0, win._maybe_startup_update_check)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
