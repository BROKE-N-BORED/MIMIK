"""MIMIK's log file — where swallowed errors go (item 23).

Every `except Exception: pass` in the codebase used to drop its error
silently, and a menu-launched GUI has no stderr at all — a failure was
invisible. Now each swallowed error is appended here, and the GUI's
crash handler logs uncaught exceptions too. The Doctor tab's View log
button shows the tail in-app.

The log lives in the data dir (<data>/mimik.log), capped at ~1 MB
(the oldest half is dropped when it grows past the cap), mode 0600.

Rule for callers: log the exception, never flow bodies or request
data — exception messages in this codebase don't carry secrets, but
flow data does.
"""
import os
import traceback
from datetime import datetime

from . import store

MAX_BYTES = 1024 * 1024
KEEP_BYTES = 512 * 1024


def log_path():
    """The log file's path (<data>/mimik.log)."""
    return os.path.join(store.data_dir(), "mimik.log")


def _rotate(path):
    """Drop the oldest half once the log passes MAX_BYTES."""
    try:
        if os.path.getsize(path) <= MAX_BYTES:
            return
        with open(path, "rb") as f:
            f.seek(-KEEP_BYTES, os.SEEK_END)
            tail = f.read()
        # Drop the first partial line so the log starts clean.
        nl = tail.find(b"\n")
        with open(path, "wb") as f:
            f.write(tail[nl + 1:] if nl >= 0 else tail)
    except Exception:
        pass


def _append(text):
    """Append one entry; never raises — logging must never break the
    app it's reporting on."""
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        pass


def _stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_error(where, exc):
    """Record a swallowed error: where it happened, the exception,
    and its traceback."""
    tb = "".join(traceback.format_exception(
        type(exc), exc, exc.__traceback__))
    _append(f"[{_stamp()}] ERROR {where}: {exc}\n{tb}\n")


def log_info(where, message):
    """Record an informational line (startup, record start/stop)."""
    _append(f"[{_stamp()}] INFO {where}: {message}\n")


def read_tail(lines=200):
    """The last `lines` lines of the log, for the in-app view.
    Empty string when there is no log yet."""
    try:
        with open(log_path(), encoding="utf-8") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return ""
