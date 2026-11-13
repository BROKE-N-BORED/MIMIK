"""Screenshot the desktop while recording (v0.4 item 15).

One screenshot per recording session, taken when recording stops
(while the target app is still up, before anything is torn down),
saved with the capture for reference:

    <data>/captures/screenshots/screenshot-<timestamp>.png

Two ways to grab it, in order:

  - Qt (the GUI path): QScreen.grabWindow(0) — the whole desktop.
    Only on the GUI thread with a running QApplication; Qt GUI calls
    off the main thread can crash, so anything else skips Qt.
  - External tools (the CLI path, no Qt): gnome-screenshot, scrot,
    ImageMagick's import, whichever is installed.

Everything here never raises: a failed screenshot is a lost
reference, not a lost capture. The caller reports the outcome.
"""
import os
import shutil
import subprocess
import time

from . import store

_TOOLS = [
    ("gnome-screenshot", ["gnome-screenshot", "-f", "{path}"]),
    ("scrot", ["scrot", "{path}"]),
    ("import", ["import", "-window", "root", "{path}"]),
]


def screenshots_dir(base=None):
    """captures/screenshots/, created 0700 like the rest of the store."""
    d = os.path.join(store.captures_dir(base), "screenshots")
    store.ensure_private_dir(d)
    return d


def _qt_shot(path):
    """Grab the desktop with Qt. False unless a QApplication is running
    on this thread and the grab produces a real pixmap."""
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return False
        if QThread.currentThread() != app.thread():
            return False
        screen = app.primaryScreen()
        if screen is None:
            return False
        pixmap = screen.grabWindow(0)
        if pixmap.isNull():
            return False
        return bool(pixmap.save(path, "PNG"))
    except Exception:
        return False


def _tool_shot(path):
    """Grab the desktop with the first installed external tool."""
    for name, argv in _TOOLS:
        if shutil.which(name) is None:
            continue
        cmd = [a.format(path=path) for a in argv]
        try:
            r = subprocess.run(cmd, check=False, capture_output=True,
                               timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if (r.returncode == 0 and os.path.isfile(path)
                and os.path.getsize(path) > 0):
            return True
    return False


def take_screenshot(path):
    """Screenshot the desktop to `path`. Qt first, then external tools.
    Returns True on success. Never raises."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return False
    if _qt_shot(path):
        return True
    return _tool_shot(path)


def capture_for_session(base=None):
    """Take the session screenshot. Returns the path, or None when no
    method worked."""
    name = time.strftime("screenshot-%Y%m%d-%H%M%S.png",
                         time.localtime())
    path = os.path.join(screenshots_dir(base), name)
    if take_screenshot(path):
        return path
    return None
