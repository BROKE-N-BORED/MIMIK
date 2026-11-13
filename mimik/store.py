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

DEFAULT_KEEP_SAVES = 20


def format_size(size):
    """Human-readable byte count ("12 B", "3.4 KB", "2.0 MB", "2.0 GB")."""
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024 * 1024):.1f} GB"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def dir_size(path):
    """Total bytes under a directory (0 if it doesn't exist)."""
    total = 0
    if not os.path.isdir(path):
        return 0
    for _root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(_root, name))
            except OSError:
                continue
    return total


def disk_usage(base=None):
    """{"captures", "clients", "assets", "packs", "presets", "other",
    "total"} — bytes per area of the data dir. "other" is everything
    else (settings files, Reblend path, future areas)."""
    base = base or data_dir()
    usage = {}
    for area in ("captures", "clients", "assets", "packs", "presets"):
        usage[area] = dir_size(os.path.join(base, area))
    known = set(usage)
    other = 0
    if os.path.isdir(base):
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if name in known:
                continue
            if os.path.isfile(path):
                try:
                    other += os.path.getsize(path)
                except OSError:
                    continue
            elif os.path.isdir(path):
                other += dir_size(path)
    usage["other"] = other
    usage["total"] = sum(usage.values())
    return usage


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


def _host_files(host, base):
    """Sorted capture filenames for one host, oldest first."""
    cdir = captures_dir(base)
    if not os.path.isdir(cdir):
        return []
    safe = _safe_host(host)
    return sorted(n for n in os.listdir(cdir)
                  if n.startswith(safe + "_") and n.endswith(".json"))


def _read_capture(path):
    """One capture file's JSON, or None if it can't be read/parsed."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _saved_ids(host, base):
    """Flow ids already saved for a host.

    Includes None once if any saved flow lacks an id (mirrors load_host's
    dedupe, where the first id-less flow is kept and the rest dropped).
    """
    ids = set()
    cdir = captures_dir(base)
    for name in _host_files(host, base):
        data = _read_capture(os.path.join(cdir, name))
        if data is None or data.get("host") != host:
            continue
        for f in data.get("flows", []):
            ids.add(f.get("id"))
    return ids


def open_private(path, mode="w"):
    """Open a file for writing with 0600 permissions (owner only).

    Captures hold live session tokens, preset files hold auth shapes,
    and the mitmweb log holds the web-UI token — none of them may be
    world-readable (review #13). os.open's mode only applies when the
    file is created, so chmod afterwards too: a file left 0644 by an
    older version tightens on the next write to the same path.
    Returns a file object, like open().
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(fd, mode)


def save_flows(flows, mitm=None, base=None, meta=None):
    """Save a pulled flow set, grouped per host. Returns list of file paths.

    Incremental: only flows whose id is not already saved for that host
    are written — and only their bodies are fetched from mitmweb. A host
    with no new flows gets no new file, so a poll that re-pulls the same
    flows costs a local id scan instead of a full re-fetch + rewrite.
    Flows without an id can't be deduped and are always saved (mitmweb
    always assigns ids, so this is a malformed-input corner case).

    One file per host per save event: captures/<host>_<timestamp>.json.
    Two saves in the same second get _2, _3 suffixes instead of the
    later one overwriting the earlier one. (Underscore, not the asset
    store's -2: load_host relies on filenames sorting chronologically,
    and "-" sorts before "." so a -2 file would read as OLDER than the
    base file; "_" sorts after ".", so _2 reads as newer, as it is.)
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
        known = _saved_ids(host, base)
        new, seen_here = [], set()
        for f in host_flows:
            fid = f.get("id")
            if fid is not None and (fid in known or fid in seen_here):
                continue
            seen_here.add(fid)
            new.append(f)
        if not new:
            continue
        embedded = _embed_bodies(mitm, new)
        wanted = os.path.join(
            captures_dir(base), f"{_safe_host(host)}_{stamp}.json"
        )
        path = wanted
        n = 2
        while os.path.exists(path):
            path = os.path.join(
                captures_dir(base),
                f"{_safe_host(host)}_{stamp}_{n}.json",
            )
            n += 1
        with open_private(path, "w") as fh:
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
    seen, flows = set(), []
    for name in _host_files(host, base):
        data = _read_capture(os.path.join(cdir, name))
        if data is None or data.get("host") != host:
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
    newest = None
    for name in _host_files(host, base):
        data = _read_capture(os.path.join(cdir, name))
        if data is None or data.get("host") != host:
            continue
        newest = data  # filenames sort chronologically
    meta = (newest or {}).get("meta")
    return meta if isinstance(meta, dict) else {}


def list_captures(base=None):
    """[(host, flow_count, latest_saved_at)] for every captured host.

    flow_count is the number of distinct flows load_host() would return
    for the host (deduped by id, capped at MAX_FLOWS_PER_HOST) — the two
    agree by construction, so a host saved across many polls counts its
    flows once, not once per save.
    """
    base = base or data_dir()
    cdir = captures_dir(base)
    if not os.path.isdir(cdir):
        return []
    info = {}
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".json"):
            continue
        data = _read_capture(os.path.join(cdir, name))
        if data is None:
            continue
        host = data.get("host")
        if not host:
            continue
        stamp = data.get("saved_at", "")
        entry = info.setdefault(host, {"ids": set(), "latest": ""})
        for f in data.get("flows", []):
            entry["ids"].add(f.get("id"))
        entry["latest"] = max(entry["latest"], stamp)
    return sorted(
        [(h, min(len(v["ids"]), MAX_FLOWS_PER_HOST), v["latest"])
         for h, v in info.items()],
        key=lambda kv: -kv[1],
    )


def capture_stats(host, base=None):
    """{"flows", "kept", "truncated", "files", "latest", "bytes"} for
    one host.

    flows: distinct flows saved for the host; kept: what load_host()
    returns (capped at MAX_FLOWS_PER_HOST); truncated: True when flows
    exceed the cap — the oldest flows exist on disk but load_host()
    only sees the newest MAX_FLOWS_PER_HOST of them. bytes: the host's
    capture files on disk.
    """
    base = base or data_dir()
    ids = _saved_ids(host, base)
    files = _host_files(host, base)
    latest = ""
    cdir = captures_dir(base)
    total_bytes = 0
    for name in files:
        path = os.path.join(cdir, name)
        try:
            total_bytes += os.path.getsize(path)
        except OSError:
            pass
        data = _read_capture(path)
        if data is not None and data.get("host") == host:
            latest = max(latest, data.get("saved_at", ""))
    total = len(ids)
    return {
        "flows": total,
        "kept": min(total, MAX_FLOWS_PER_HOST),
        "truncated": total > MAX_FLOWS_PER_HOST,
        "files": len(files),
        "latest": latest,
        "bytes": total_bytes,
    }


def prune(base=None, keep=DEFAULT_KEEP_SAVES, dry_run=False):
    """Delete old capture saves, keeping the `keep` most recent save files
    per host. Returns {"deleted": [paths], "flows_lost": n, "kept": n}.

    Deleting a save file loses the flows only it holds — this is an
    explicit user action (the `mimik prune` command), never automatic.
    With dry_run=True nothing is deleted; the report is what WOULD go.
    """
    base = base or data_dir()
    cdir = captures_dir(base)
    keep = max(0, keep)
    deleted, flows_lost, kept = [], 0, 0
    if not os.path.isdir(cdir):
        return {"deleted": deleted, "flows_lost": 0, "kept": 0}
    by_host = {}
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".json"):
            continue
        data = _read_capture(os.path.join(cdir, name))
        if data is None or not data.get("host"):
            continue
        by_host.setdefault(data["host"], []).append(
            (name, len(data.get("flows", []))))
    for _host, files in by_host.items():
        # filenames sort chronologically; newest last
        drop = files[:-keep] if keep > 0 else files
        kept += len(files) - len(drop)
        for name, n in drop:
            path = os.path.join(cdir, name)
            if not dry_run:
                try:
                    os.remove(path)
                except OSError:
                    continue
            deleted.append(path)
            flows_lost += n
    return {"deleted": deleted, "flows_lost": flows_lost, "kept": kept}


def delete_host(host, base=None):
    """Delete ALL saved captures for one host. Returns {"deleted":
    [paths], "flows_lost": n, "bytes_freed": n}. A host with no saves
    deletes nothing (empty report, not an error). Callers invalidate
    their flow caches after this — the flows are gone."""
    base = base or data_dir()
    cdir = captures_dir(base)
    files = _host_files(host, base)
    flows_lost = len(_saved_ids(host, base))
    deleted, bytes_freed = [], 0
    for name in files:
        path = os.path.join(cdir, name)
        try:
            bytes_freed += os.path.getsize(path)
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError:
            continue
        deleted.append(path)
    return {"deleted": deleted, "flows_lost": flows_lost,
            "bytes_freed": bytes_freed}


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


def delete_client(name, base=None):
    """Delete one generated client by filename. Returns {"deleted":
    path or None, "bytes_freed": n}. The name must be a bare filename
    ending _client.py — separators and ".." are refused, and the
    resolved path must stay inside clients/."""
    base = base or data_dir()
    cdir = clients_dir(base)
    if (not name or os.path.basename(name) != name
            or not name.endswith("_client.py")):
        return {"deleted": None, "bytes_freed": 0}
    path = os.path.join(cdir, name)
    if os.path.commonpath([os.path.abspath(path),
                           os.path.abspath(cdir)]) != os.path.abspath(cdir):
        return {"deleted": None, "bytes_freed": 0}
    if not os.path.isfile(path):
        return {"deleted": None, "bytes_freed": 0}
    try:
        freed = os.path.getsize(path)
    except OSError:
        freed = 0
    try:
        os.remove(path)
    except OSError:
        return {"deleted": None, "bytes_freed": 0}
    return {"deleted": path, "bytes_freed": freed}
