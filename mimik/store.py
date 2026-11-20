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
import tempfile
import time
from datetime import datetime

from . import crypto

DEFAULT_DATA_DIR = os.path.join(
    os.path.expanduser("~"), ".local", "share", "mimik"
)

MAX_FLOWS_PER_HOST = 5000

DEFAULT_KEEP_SAVES = 20


def effective_flows_cap():
    """The per-host capture cap for this machine (v0.5 item 17/U35):
    the machine module sizes it from measured RAM (5000 on 4-16 GB,
    2500 below 4 GB, 10000 above 16 GB). Falls back to
    MAX_FLOWS_PER_HOST if the measurement fails."""
    try:
        from . import machine

        return machine.flows_cap()
    except Exception:
        return MAX_FLOWS_PER_HOST


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
            # WebSocket messages (v0.4 item 13): mitmweb's /flows JSON
            # carries only message meta — the messages come from the
            # content-view endpoint. A failed fetch never breaks the
            # save, same as bodies.
            if "websocket" in f:
                try:
                    messages = mitm.ws_messages(f["id"])
                except Exception:
                    messages = None
                if messages:
                    g["_saved_ws_messages"] = messages
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


# v0.8 B7: capture files that exist but cannot be parsed. Recorded
# once per path per process (and logged once), so a corrupt capture
# is never silently invisible: the CLI warns on stderr and the GUI
# status bar names the count. The file itself is left on disk.
_unreadable = {}


def _note_unreadable(path, exc):
    if path in _unreadable:
        return
    _unreadable[path] = str(exc)
    try:
        from . import log
        log.log_error(f"store: unreadable capture file {path}", exc)
    except Exception:
        pass  # logging must never break a read


def unreadable_captures(base=None):
    """[(filename, error)] for capture files under `base` that were
    found unreadable during this process's reads (v0.8 B7)."""
    base = base or data_dir()
    cdir = captures_dir(base)
    out = []
    for path, err in sorted(_unreadable.items()):
        if os.path.dirname(path) == cdir:
            out.append((os.path.basename(path), err))
    return out


def _read_capture(path):
    """One capture file's JSON, or None if it can't be read/parsed.

    A file that exists but does not parse is noted (v0.8 B7) — it
    used to vanish silently, indistinguishable from "never saved".
    An encrypted capture (v0.8 G1) opens only while the store is
    unlocked; locked, it reads as None and is noted as locked, NOT
    as corrupt — the file is fine, the key is just not in memory.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    try:
        obj = json.loads(text)
    except ValueError as e:
        _note_unreadable(path, e)
        return None
    if crypto.is_envelope(obj):
        key = _keys.get(_base_of_capture(path))
        if key is None:
            _locked_files.add(path)
            return None
        try:
            return crypto.decrypt_json(key, obj)
        except crypto.CryptoError as e:
            _note_unreadable(path, e)
            return None
    return obj


# ---- capture encryption (v0.8 G1) -------------------------------------
#
# Decided 2026-11-13, built in v0.8: capture files are sealed with
# AES-256-GCM under an Argon2id key derived from a passphrase. The
# key lives in _keys, in memory only, keyed by data dir. The marker
# file (encryption.json in the data dir) holds only the salt and a
# sealed verifier — never the key, never the passphrase.

class StoreLockedError(Exception):
    """Captures are encrypted and this process has not unlocked
    them. Raised on WRITE (a save must never silently land as
    plaintext or get dropped); reads of locked captures return
    empty and are reported through locked_captures()."""


_keys = {}
_locked_files = set()


def _base_of_capture(path):
    """The data dir a capture file lives under (…/captures/x.json)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(path)))


def encryption_marker_path(base=None):
    return os.path.join(base or data_dir(), "encryption.json")


def encryption_enabled(base=None):
    """True when this store has capture encryption turned on."""
    return os.path.isfile(encryption_marker_path(base))


def _load_marker(base):
    try:
        with open(encryption_marker_path(base), encoding="utf-8") as fh:
            marker = json.load(fh)
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def unlocked(base=None):
    """True when the passphrase for this store was given this run."""
    return os.path.abspath(base or data_dir()) in _keys


def unlock(passphrase, base=None):
    """Try a passphrase against the store's marker. True (and the
    key is kept in memory) when it opens the verifier; False when
    it does not. Raises crypto.CryptoError when the crypto packages
    are missing — a plain-words refusal, not a silent no."""
    base = base or data_dir()
    marker = _load_marker(base)
    if marker is None:
        return False
    key = crypto.check_marker(marker, passphrase)
    if key is None:
        return False
    _keys[os.path.abspath(base)] = key
    return True


def lock(base=None):
    """Forget the key for this store (captures stay encrypted)."""
    _keys.pop(os.path.abspath(base or data_dir()), None)


def locked_captures(base=None):
    """Basenames of encrypted capture files this process tried to
    read while locked (v0.8 G1) — so callers can say "locked"
    instead of pretending the captures do not exist."""
    base = base or data_dir()
    cdir = captures_dir(base)
    return sorted(os.path.basename(p) for p in _locked_files
                  if os.path.dirname(p) == cdir)


def _seal_payload(base, payload):
    """The text to write for a capture payload: plain JSON, or a
    sealed envelope while encryption is on. Writing while locked
    is a hard error (StoreLockedError), never a plaintext save."""
    if encryption_enabled(base):
        key = _keys.get(os.path.abspath(base))
        if key is None:
            raise StoreLockedError(
                "captures are encrypted and locked: unlock before "
                "saving (the passphrase is asked at startup)")
        return json.dumps(crypto.encrypt_json(key, payload))
    return json.dumps(payload)


def _read_capture_plain(path):
    """Parse a capture file's raw JSON with NO decryption (the
    migration path in enable_encryption reads files it is about to
    seal)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def enable_encryption(passphrase, base=None):
    """Turn capture encryption ON for this store and seal every
    existing plaintext capture in place (same filenames). Returns
    how many capture files were sealed. The marker is written
    first, so an interrupted migration still leaves a store that
    unlocks and reads its (mixed) files correctly."""
    base = base or data_dir()
    if encryption_enabled(base):
        raise crypto.CryptoError(
            "capture encryption is already on for this store")
    marker, key = crypto.make_marker(passphrase)
    ensure_private_dir(base)
    ensure_private_dir(captures_dir(base))
    atomic_write(encryption_marker_path(base),
                 json.dumps(marker, indent=2))
    _keys[os.path.abspath(base)] = key
    sealed = 0
    cdir = captures_dir(base)
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cdir, name)
        data = _read_capture_plain(path)
        if data is None or crypto.is_envelope(data):
            continue
        atomic_write(path, json.dumps(crypto.encrypt_json(key, data)))
        sealed += 1
    return sealed


def disable_encryption(base=None):
    """Turn capture encryption OFF: unseal every capture back to
    plain JSON and remove the marker. Needs the store unlocked.
    Returns how many capture files were unsealed."""
    base = base or data_dir()
    if not encryption_enabled(base):
        return 0
    if not unlocked(base):
        raise StoreLockedError(
            "unlock the captures before turning encryption off")
    unsealed = 0
    cdir = captures_dir(base)
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cdir, name)
        raw = _read_capture_plain(path)
        if raw is None or not crypto.is_envelope(raw):
            continue
        data = _read_capture(path)
        if data is None:
            continue
        atomic_write(path, json.dumps(data))
        unsealed += 1
    try:
        os.remove(encryption_marker_path(base))
    except OSError:
        pass
    lock(base)
    return unsealed


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

    This is for live handles (the recorder log, written incrementally).
    One-shot saves should use atomic_write() instead — same 0600, plus
    the write is all-or-nothing.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(fd, mode)


def ensure_private_dir(path):
    """Create a directory 0700 (owner only), tightening an existing one.

    The data dir and its subdirs hold live session tokens — they must
    not be group/other-readable. os.makedirs' mode is masked by umask
    and only applies at creation, so chmod afterwards too: a dir left
    0755 by an older version tightens on next use. A failed chmod never
    breaks the caller (best effort on odd filesystems). Returns path.
    """
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def atomic_write(path, data, private=True):
    """Write bytes/str to path atomically: temp file in the same
    directory, fsync, os.replace. An interrupted save leaves the
    previous good file intact (or no file, if there was none) — never
    a partial file. private=True (default) keeps the file 0600;
    private=False writes 0644. Returns the path.

    A crashed save can leave a stray `.mimik-tmp-*` file in the
    directory; the store only reads *.json, so strays are invisible
    to it (and tiny — one per crash at most).
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".mimik-tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data if isinstance(data, bytes)
                     else data.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        if private:
            os.chmod(tmp, 0o600)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        try:  # fsync the directory so the rename itself is durable
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def _stamp_flow(flow, now):
    """(v0.6 T14) Stamp a flow with its capture time. Live mitmweb
    flows already carry request.timestamp_start (verified live
    2026-11-16); anything else (sample, HAR, cURL) gets the save
    time, so every saved flow has a time to scrub by."""
    req = flow.get("request")
    if isinstance(req, dict) and req.get("timestamp_start") is None:
        req["timestamp_start"] = now


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
    ensure_private_dir(base)
    ensure_private_dir(captures_dir(base))
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
        # v0.6 T14: stamp each new flow at save time (live flows
        # already carry their own timestamp; the stamp fills the
        # gap for sample / HAR / cURL flows).
        now = time.time()
        for f in new:
            _stamp_flow(f, now)
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
        atomic_write(path, _seal_payload(
            base, {"host": host, "saved_at": stamp, "flows": embedded,
                   "meta": meta or {}}))
        paths.append(path)
    return paths


def load_capture_file(path):
    """Flows from one capture save file, or None if it can't be read."""
    data = _read_capture(path)
    if data is None:
        return None
    flows = data.get("flows")
    return flows if isinstance(flows, list) else []


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
    return flows[-effective_flows_cap():]


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
    for the host (deduped by id, capped at the effective cap) — the two
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
    cap = effective_flows_cap()
    return sorted(
        [(h, min(len(v["ids"]), cap), v["latest"])
         for h, v in info.items()],
        key=lambda kv: -kv[1],
    )


def capture_stats(host, base=None):
    """{"flows", "kept", "truncated", "files", "latest", "bytes"} for
    one host.

    flows: distinct flows saved for the host; kept: what load_host()
    returns (capped at the effective cap); truncated: True when flows
    exceed the cap — the oldest flows exist on disk but load_host()
    only sees the newest ones. bytes: the host's capture files on
    disk.
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
    cap = effective_flows_cap()
    return {
        "flows": total,
        "kept": min(total, cap),
        "truncated": total > cap,
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


def _client_host_of(source):
    """The HOST a generated client was built for, or None."""
    m = re.search(r'^\s*HOST\s*=\s*"([^"]+)"', source or "", re.M)
    return m.group(1) if m else None


def save_client_copy(name, source, base=None):
    """Keep a copy of a generated client under clients/. Returns the path.

    v0.8 B2: the store copy always carries the _client.py suffix
    (list_clients / delete_client only speak that convention, so a
    copy saved under a custom --out basename like "cli-out" used to
    be invisible and undeletable). Regenerating the SAME host's
    client refreshes its copy, as before; a copy whose name is
    taken by a DIFFERENT host's client gets a _2/_3 sibling that
    still ends in _client.py.
    """
    base = base or data_dir()
    ensure_private_dir(base)
    ensure_private_dir(clients_dir(base))
    if not name.endswith("_client.py"):
        stem = name[:-3] if name.endswith(".py") else name
        name = f"{stem}_client.py"
    path = os.path.join(clients_dir(base), name)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = fh.read()
        except OSError:
            old = None
        if old is not None and old != source:
            old_host = _client_host_of(old)
            new_host = _client_host_of(source)
            if old_host and new_host and old_host != new_host:
                stem = name[: -len("_client.py")]
                n = 2
                while os.path.exists(os.path.join(
                        clients_dir(base), f"{stem}_{n}_client.py")):
                    n += 1
                path = os.path.join(
                    clients_dir(base), f"{stem}_{n}_client.py")
    atomic_write(path, source, private=False)
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
