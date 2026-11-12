"""Persistent capture store — MIMIK's answer to upstream's live-only weakness.

Upstream mimic reads flows from a running mitmweb and nowhere else: stop the
proxy and your session is gone. MIMIK saves every pulled flow set under
``~/.local/share/mimik/captures/<host>_<timestamp>.json`` (bodies embedded),
so `hosts`, `learn`, `gen` and generated clients keep working after the proxy
stops — and across reboots.

Layout (all under the data dir, overridable with MIMIK_DATA_DIR):
    captures/<host>_<YYYYMMDD-HHMMSS>.json   one save event per file
    clients/<stem>_client.py                 copies of generated clients

Capture files are plain JSON: {"host", "saved_at", "flows": [...]}.
Flows are mitmweb's own dicts, plus optional ``_saved_request_body`` /
``_saved_response_body`` text (or base64 with a ``_saved_*_body_b64`` flag).
"""
import base64
import json
import os
import re
from datetime import datetime

DEFAULT_DATA_DIR = os.path.join(
    os.path.expanduser("~"), ".local", "share", "mimik"
)

MAX_FLOWS_PER_HOST = 5000


def data_dir():
    """MIMIK's data dir (MIMIK_DATA_DIR overrides, mainly for tests)."""
    return os.environ.get("MIMIK_DATA_DIR") or DEFAULT_DATA_DIR


def captures_dir(base=None):
    return os.path.join(base or data_dir(), "captures")


def clients_dir(base=None):
    return os.path.join(base or data_dir(), "clients")


def _safe_host(host):
    return re.sub(r"[^a-zA-Z0-9.-]+", "_", host or "unknown").strip("._") or "unknown"


def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _embed_bodies(mitm, flows):
    """Copy flows, embedding request/response bodies fetched from mitmweb.

    Bodies are stored as UTF-8 text when they decode, else base64. A failed
    body fetch never breaks the save — that flow just keeps no body.
    """
    out = []
    for f in flows:
        g = dict(f)
        if mitm is not None:
            for side in ("request", "response"):
                try:
                    raw = mitm.body(f["id"], side)
                except Exception:
                    raw = b""
                if raw:
                    try:
                        g[f"_saved_{side}_body"] = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        g[f"_saved_{side}_body"] = base64.b64encode(raw).decode("ascii")
                        g[f"_saved_{side}_body_b64"] = True
        out.append(g)
    return out


def save_flows(flows, mitm=None, base=None, meta=None):
    """Save a pulled flow set, grouped per host. Returns list of file paths.

    One file per host per save event: captures/<host>_<timestamp>.json.
    meta (e.g. {"launched_file": ..., "kind": ...}) is stored in each file.
    """
    base = base or data_dir()
    by_host = {}
    for f in flows:
        req = f.get("request")
        if req and req.get("host"):
            by_host.setdefault(req["host"], []).append(f)
    if not by_host:
        return []
    os.makedirs(captures_dir(base), exist_ok=True)
    stamp = _stamp()
    paths = []
    for host, host_flows in sorted(by_host.items()):
        embedded = _embed_bodies(mitm, host_flows)
        path = os.path.join(
            captures_dir(base), f"{_safe_host(host)}_{stamp}.json"
        )
        with open(path, "w") as fh:
            json.dump(
                {"host": host, "saved_at": stamp, "flows": embedded,
                 "meta": meta or {}}, fh
            )
        paths.append(path)
    return paths


def load_host(host, base=None):
    """All saved flows for a host, merged across save events, deduped by id.

    Oldest first, so "newest authed request" logic (which scans reversed)
    still finds the latest capture.
    """
    base = base or data_dir()
    cdir = captures_dir(base)
    if not os.path.isdir(cdir):
        return []
    safe = _safe_host(host)
    seen, flows = set(), []
    for name in sorted(os.listdir(cdir)):
        if not name.startswith(safe + "_") or not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(cdir, name)) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if data.get("host") != host:
            continue
        for f in data.get("flows", []):
            fid = f.get("id")
            if fid is not None and fid in seen:
                continue
            seen.add(fid)
            flows.append(f)
    return flows[-MAX_FLOWS_PER_HOST:]


def load_all(base=None):
    """All saved flows for every host, merged."""
    out = []
    for host, _n, _t in list_captures(base):
        out.extend(load_host(host, base))
    return out


def capture_meta(host, base=None):
    """The meta dict of the newest saved capture for a host, or {}.

    Meta is what the recorder stored with the capture (e.g.
    {"launched_file": ...} for a program run from a file) — it links
    a host's traffic back to the program it came from.
    """
    base = base or data_dir()
    cdir = captures_dir(base)
    if not os.path.isdir(cdir):
        return {}
    safe = _safe_host(host)
    newest = None
    for name in sorted(os.listdir(cdir)):
        if not name.startswith(safe + "_") or not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(cdir, name)) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if data.get("host") != host:
            continue
        newest = data  # filenames sort chronologically
    meta = (newest or {}).get("meta")
    return meta if isinstance(meta, dict) else {}


def list_captures(base=None):
    """[(host, flow_count, latest_saved_at)] for every captured host."""
    base = base or data_dir()
    cdir = captures_dir(base)
    if not os.path.isdir(cdir):
        return []
    info = {}
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(cdir, name)) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        host = data.get("host")
        if not host:
            continue
        n = len(data.get("flows", []))
        stamp = data.get("saved_at", "")
        prev = info.get(host)
        if prev is None:
            info[host] = [n, stamp]
        else:
            prev[0] += n
            prev[1] = max(prev[1], stamp)
    return sorted(
        [(h, v[0], v[1]) for h, v in info.items()], key=lambda kv: -kv[1]
    )


def save_client_copy(name, source, base=None):
    """Keep a copy of a generated client under clients/. Returns the path."""
    base = base or data_dir()
    os.makedirs(clients_dir(base), exist_ok=True)
    path = os.path.join(clients_dir(base), name)
    with open(path, "w") as fh:
        fh.write(source)
    return path


def list_clients(base=None):
    """[(name, path, size)] of generated clients kept under clients/."""
    base = base or data_dir()
    cdir = clients_dir(base)
    if not os.path.isdir(cdir):
        return []
    out = []
    for name in sorted(os.listdir(cdir)):
        if name.endswith("_client.py"):
            path = os.path.join(cdir, name)
            try:
                out.append((name, path, os.path.getsize(path)))
            except OSError:
                continue
    return out
