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

T21 alignment (v0.5 item 15, design rule U12): the BNB S61 standard
+ T21 module ask for rotating timestamped logs, a last-200 ring
buffer, traceback capture, and a scrubbed report. MIMIK already met
two of the four before v0.5 and now meets all four:
  - rotating timestamped logs: EQUIVALENT — size-based rotation
    (1 MB cap, oldest half dropped) instead of date-based files;
    every entry is timestamped. Same guarantee, fewer files.
  - last-200 ring buffer: ADOPTED — _RING holds the newest 200
    entries in memory (ring_lines()), so the report works even if
    the log file was just rotated away.
  - traceback capture: EQUIVALENT — log_error formats the full
    traceback (unchanged since item 23).
  - scrubbed report: ADOPTED — scrub() redacts tokens and auth
    headers; debug_report() bundles version, platform, measured
    specs, and the scrubbed ring for pasting into a bug report.
"""
import os
import re
import sys
import traceback
from collections import deque
from datetime import datetime

from . import machine, store

MAX_BYTES = 1024 * 1024
KEEP_BYTES = 512 * 1024
_RING_MAX = 200
_RING = deque(maxlen=_RING_MAX)

_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+")
_AUTH_RE = re.compile(r"(Authorization:\s*)(?:Bearer\s+)?\S+",
                        re.IGNORECASE)
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+")
_APIKEY_RE = re.compile(r"(api[_-]?key[\"'\s:=]+)[^\"'\s,}]+",
                        re.IGNORECASE)


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


def scrub(text):
    """Redact secrets from log text before it leaves the machine:
    token= query values, Authorization headers, Bearer tokens, and
    api-key values. Plain text passes through unchanged."""
    text = _TOKEN_RE.sub(r"\1<redacted>", text)
    text = _AUTH_RE.sub(r"\1<redacted>", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    text = _APIKEY_RE.sub(r"\1<redacted>", text)
    return text


def ring_lines():
    """The in-memory ring: the newest 200 log entries, oldest first.
    Empty in a fresh process (nothing logged yet)."""
    return list(_RING)


def debug_report():
    """A scrubbed report for a bug report: version, platform,
    measured specs, and the recent log. Never raises; never
    includes a raw token."""
    try:
        from . import __version__

        lines = [f"MIMIK {__version__}",
                 f"Python {sys.version.split()[0]} on {sys.platform}"]
        try:
            lines.extend(machine.describe())
        except Exception:
            pass
        lines.append("")
        lines.append("recent log:")
        ring = ring_lines()
        if ring:
            lines.extend(line.rstrip("\n") for line in ring)
        else:
            tail = read_tail()
            lines.append(tail.rstrip("\n") if tail
                         else "(no log entries yet)")
        return scrub("\n".join(lines) + "\n")
    except Exception:
        return "(could not build a debug report)\n"


def _append(text):
    """Append one entry; never raises — logging must never break the
    app it's reporting on."""
    _RING.append(text)
    try:
        path = log_path()
        store.ensure_private_dir(os.path.dirname(path))
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
