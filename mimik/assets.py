"""Asset capture — the look of a clone, not just its code.

When MIMIK captures an app or a game, its traffic and its program files
often carry the assets the thing is made of: images, 3D models, audio,
fonts. This module finds those bytes, saves them raw, and records what
they are — so the Reblend bridge can turn the models into a scene.

Two sources, one store:
  * traffic — asset responses in captured flows (live mitmweb, saved
    captures, or HAR imports). Raw bytes come from mitmweb when it is
    live, else from the body embedded in the saved capture / HAR entry.
  * program files — a scan of an extracted archive, a mounted disk
    image, an extracted .deb, or a folder. Copies only, never moves,
    never executes anything.

Store layout (under the data dir, MIMIK_DATA_DIR overrides):
    assets/<group>_<YYYYMMDD-HHMMSS>/assets.json   manifest per capture
    assets/<group>_<YYYYMMDD-HHMMSS>/<safe-name>   raw bytes, one per file

A group is one host (traffic) or one program (file scan). Files dedupe
by SHA-256 within a group: same bytes, one file.

Size guards, warn-never-refuse: a file over MAX_FILE_BYTES or a group
over MAX_GROUP_BYTES is skipped with a note in the manifest, never a
crash. These are machine-protection caps (a game can stream gigabytes);
the user can still convert what was saved.
"""
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from urllib.parse import urlparse

from . import extract, store

MAX_FILE_BYTES = 50 * 1024 * 1024      # 50 MB per file
MAX_GROUP_BYTES = 500 * 1024 * 1024    # 500 MB per capture group

# Scan guards (review #19): a folder scan must be bounded — the GUI lets
# the user pick any folder, including home.
MAX_SCAN_DEPTH = 12        # directory levels below the scanned root
MAX_SCAN_FILES = 50000     # files visited (assets or not)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
MODEL_EXTS = {".glb", ".gltf", ".obj", ".fbx", ".dae"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg"}
FONT_EXTS = {".woff2", ".woff", ".ttf", ".otf"}
VIDEO_EXTS = {".mp4", ".webm"}
TEXTURE_EXTS = {".dds", ".ktx2"}

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif",
               "image/svg+xml", "image/x-icon"}
MODEL_TYPES = {"model/gltf-binary", "model/gltf+json", "model/obj",
               "model/gltf", "application/octet-stream"}
AUDIO_TYPES = {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg",
               "audio/mp3"}
FONT_TYPES = {"font/woff2", "font/woff", "font/ttf", "font/otf",
              "application/font-woff", "application/font-woff2",
              "application/x-font-ttf", "application/x-font-opentype"}
VIDEO_TYPES = {"video/mp4", "video/webm"}
TEXTURE_TYPES = {"image/ktx2", "image/dds"}

_EXT_KIND = {}
for _ext in IMAGE_EXTS:
    _EXT_KIND[_ext] = "image"
for _ext in MODEL_EXTS:
    _EXT_KIND[_ext] = "model"
for _ext in AUDIO_EXTS:
    _EXT_KIND[_ext] = "audio"
for _ext in FONT_EXTS:
    _EXT_KIND[_ext] = "font"
for _ext in VIDEO_EXTS:
    _EXT_KIND[_ext] = "video"
for _ext in TEXTURE_EXTS:
    _EXT_KIND[_ext] = "texture"

_TYPE_KIND = {}
for _t in IMAGE_TYPES:
    _TYPE_KIND[_t] = "image"
for _t in MODEL_TYPES:
    _TYPE_KIND[_t] = "model"
for _t in AUDIO_TYPES:
    _TYPE_KIND[_t] = "audio"
for _t in FONT_TYPES:
    _TYPE_KIND[_t] = "font"
for _t in VIDEO_TYPES:
    _TYPE_KIND[_t] = "video"
for _t in TEXTURE_TYPES:
    _TYPE_KIND[_t] = "texture"

# application/octet-stream matches anything binary, so it only counts
# when the URL extension also says model — see detect_kind.
_GENERIC_TYPES = {"application/octet-stream"}

KIND_LABELS = {"image": "Image", "model": "3D model",
               "audio": "Audio", "font": "Font",
               "video": "Video", "texture": "Texture"}


def assets_dir(base=None):
    return os.path.join(base or store.data_dir(), "assets")


def _safe_name(name):
    """A filename-safe stem: letters, digits, dot, dash, underscore."""
    out = "".join(ch if (ch.isalnum() or ch in "._-") else "-"
                  for ch in (name or "asset"))
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip(".-") or "asset"


def _safe_group(group):
    return re.sub(r"[^a-zA-Z0-9.-]+", "_", group or "unknown"
                  ).strip("._") or "unknown"


def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def detect_kind(url, content_type=None):
    """'image' | 'model' | 'audio' | 'font' | 'video' | 'texture' | None
    for a URL + type.

    The URL extension decides first (a .glb served as octet-stream is
    still a model); the Content-Type is the fallback for extensionless
    URLs. A generic octet-stream with no asset extension is not an
    asset — it could be anything.
    """
    path = urlparse(url or "").path or ""
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXT_KIND:
        return _EXT_KIND[ext]
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in _GENERIC_TYPES:
        return None
    return _TYPE_KIND.get(ctype)


def _flow_url(flow):
    req = flow.get("request") or {}
    scheme = req.get("scheme", "https")
    host = req.get("host", "")
    port = req.get("port")
    netloc = host
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    return f"{scheme}://{netloc}{req.get('path', '')}"


def _saved_response_bytes(flow):
    """Raw response bytes embedded in a saved flow, or b'' if none."""
    key = "_saved_response_body"
    if key not in flow:
        return b""
    if flow.get(key + "_b64"):
        try:
            return base64.b64decode(flow[key])
        except (binascii.Error, ValueError, TypeError):
            return b""
    return (flow.get(key) or "").encode("utf-8")


# ---- the asset store ---------------------------------------------------------

class AssetGroup:
    """One capture group's folder + manifest, built up as assets land."""

    def __init__(self, group, source, base=None, host=None, program=None):
        self.group = group
        self.source = source  # "traffic" | "har" | "program-files"
        self.base = base or store.data_dir()
        # One dir per extraction event. Two extractions in the same
        # second must NOT share a dir — the later manifest would
        # overwrite the earlier one — so a taken name gets -2, -3, ...
        wanted = os.path.join(assets_dir(self.base),
                              f"{_safe_group(group)}_{_stamp()}")
        self.dir = wanted
        n = 2
        while os.path.exists(self.dir):
            self.dir = f"{wanted}-{n}"
            n += 1
        os.makedirs(self.dir, exist_ok=True)
        self.manifest = {
            "group": group,
            "host": host,
            "program": program,
            "source": source,
            "captured_at": _now_iso(),
            "assets": [],
            "skipped": [],
            "total_bytes": 0,
        }
        self._by_hash = {}   # sha256 -> saved filename
        self._names = set()  # filenames already used in this group

    def _unique_filename(self, name):
        stem, ext = os.path.splitext(_safe_name(name))
        candidate = f"{stem}{ext}"
        n = 2
        while candidate in self._names:
            candidate = f"{stem}-{n}{ext}"
            n += 1
        self._names.add(candidate)
        return candidate

    def _skip_for_group_cap(self, name, kind, source_path, size,
                            max_group_bytes, url=None):
        """Record a skip: the group would pass its byte cap (one source
        of truth for the reason — used by add_bytes and by the scan's
        pre-read check, so both report the same words)."""
        self.manifest["skipped"].append({
            "name": name, "url": url, "source_path": source_path,
            "size": size, "kind": kind,
            "reason": f"group would pass the "
                      f"{max_group_bytes // (1024 * 1024)} MB cap — "
                      "skipped, nothing saved",
        })

    def add_bytes(self, data, name, kind, url=None, source_path=None,
                  content_type=None, max_file_bytes=MAX_FILE_BYTES,
                  max_group_bytes=MAX_GROUP_BYTES):
        """Save raw bytes as an asset. Returns the manifest entry, or
        None when the file was skipped (recorded in manifest['skipped']).

        Same bytes twice in one group = one file; the second entry
        points at the first file and is flagged as a duplicate.
        """
        size = len(data)
        if size == 0:
            return None
        if size > max_file_bytes:
            self.manifest["skipped"].append({
                "name": name, "url": url, "source_path": source_path,
                "size": size, "kind": kind,
                "reason": f"over the {max_file_bytes // (1024 * 1024)} MB "
                          "per-file cap — skipped, nothing saved",
            })
            return None
        if self.manifest["total_bytes"] + size > max_group_bytes:
            self._skip_for_group_cap(name, kind, source_path, size,
                                     max_group_bytes, url=url)
            return None
        digest = hashlib.sha256(data).hexdigest()
        duplicate = digest in self._by_hash
        if duplicate:
            filename = self._by_hash[digest]
        else:
            filename = self._unique_filename(name)
            with open(os.path.join(self.dir, filename), "wb") as fh:
                fh.write(data)
            self._by_hash[digest] = filename
            self.manifest["total_bytes"] += size
        entry = {
            "name": os.path.basename(name) or filename,
            "kind": kind,
            "file": filename,
            "url": url,
            "source_path": source_path,
            "content_type": content_type,
            "size": size,
            "sha256": digest,
            "duplicate": duplicate,
            "captured_at": _now_iso(),
        }
        self.manifest["assets"].append(entry)
        return entry

    def finish(self):
        """Write the manifest. Returns (group_dir, manifest)."""
        with open(os.path.join(self.dir, "assets.json"), "w") as fh:
            json.dump(self.manifest, fh, indent=2)
        return self.dir, self.manifest


# ---- traffic -----------------------------------------------------------------

def extract_from_flows(flows, group, mitm=None, base=None, host=None,
                       source="traffic", max_file_bytes=MAX_FILE_BYTES,
                       max_group_bytes=MAX_GROUP_BYTES):
    """Detect asset responses in flow dicts and save them.

    With a live mitm client, raw bytes come from mitmweb (preferred);
    otherwise from the body embedded in the saved capture. Returns
    (group_dir, manifest) or (None, None) when nothing was an asset.
    """
    agroup = AssetGroup(group, source, base=base, host=host or group)
    for flow in flows:
        req = flow.get("request") or {}
        if host and req.get("host") != host:
            continue
        resp = flow.get("response") or {}
        ctype = extract.header_dict(resp.get("headers")).get("content-type")
        url = _flow_url(flow)
        kind = detect_kind(url, ctype)
        if kind is None:
            continue
        data = b""
        if mitm is not None:
            try:
                data = mitm.body(flow["id"], "response") or b""
            except Exception:
                data = b""
        if not data:
            data = _saved_response_bytes(flow)
        if not data:
            continue
        name = os.path.basename(urlparse(url).path) or f"asset.{kind}"
        agroup.add_bytes(data, name, kind, url=url, content_type=ctype,
                         max_file_bytes=max_file_bytes,
                         max_group_bytes=max_group_bytes)
    if not agroup.manifest["assets"] and not agroup.manifest["skipped"]:
        try:
            os.rmdir(agroup.dir)
        except OSError:
            pass
        return None, None
    return agroup.finish()


def extract_from_har(har_path, base=None, max_file_bytes=MAX_FILE_BYTES,
                     max_group_bytes=MAX_GROUP_BYTES):
    """Detect assets in a HAR file, grouped per host.

    Returns [(group_dir, manifest), ...] — one group per host that had
    assets. HAR entries embed their bodies, so no proxy is needed.
    """
    with open(har_path) as fh:
        har_doc = json.load(fh)
    entries = har_doc.get("log", {}).get("entries", [])
    groups = {}
    for entry in entries:
        req = entry.get("request", {})
        resp = entry.get("response", {})
        url = req.get("url", "")
        host = urlparse(url).hostname or "unknown"
        content = resp.get("content", {})
        ctype = content.get("mimeType", "")
        if not ctype:
            ctype = extract.header_dict(
                [(h.get("name"), h.get("value")) for h in
                 resp.get("headers", [])]).get("content-type")
        kind = detect_kind(url, ctype)
        if kind is None:
            continue
        text = content.get("text", "")
        if not text:
            continue
        if content.get("encoding") == "base64":
            try:
                data = base64.b64decode(text)
            except (binascii.Error, ValueError):
                continue
        else:
            data = text.encode("utf-8")
        agroup = groups.get(host)
        if agroup is None:
            agroup = AssetGroup(host, "har", base=base, host=host)
            groups[host] = agroup
        name = os.path.basename(urlparse(url).path) or f"asset.{kind}"
        agroup.add_bytes(data, name, kind, url=url, content_type=ctype,
                         max_file_bytes=max_file_bytes,
                         max_group_bytes=max_group_bytes)
    out = []
    for agroup in groups.values():
        if agroup.manifest["assets"] or agroup.manifest["skipped"]:
            out.append(agroup.finish())
        else:
            try:
                os.rmdir(agroup.dir)
            except OSError:
                pass
    return out


# ---- program files -----------------------------------------------------------

def scan_program_tree(root, program, base=None,
                      max_file_bytes=MAX_FILE_BYTES,
                      max_group_bytes=MAX_GROUP_BYTES,
                      max_depth=MAX_SCAN_DEPTH,
                      max_files=MAX_SCAN_FILES,
                      cancelled=None):
    """Copy asset files out of a program tree (extraction / mount / folder).

    Copies only, never moves, never executes anything. Returns
    (group_dir, manifest) or (None, None) when no assets were found.
    For ISOs, call this BEFORE unmounting.

    The manifest also records "code_signals" when the tree holds
    well-known library/engine files by name (see
    mimik.fingerprints.CODE_SIGNALS) — a weak signal that can
    corroborate a traffic fingerprint, never one on its own.

    Bounded (review #19): the walk stops descending past `max_depth`
    levels below the root and stops entirely after `max_files` files
    visited; either limit lands a "scan_note" in the manifest so the
    partial result says so. `cancelled` is an optional callable the
    walk checks per file — when it returns True the scan stops early
    and what was found so far is saved, with a "scan cancelled" note.
    The group cap is checked BEFORE a file is read, so a file the
    group has no room for is never loaded into memory.
    """
    # Lazy import: fingerprints imports this module for scoping.
    from . import fingerprints as fp_mod

    agroup = AssetGroup(program, "program-files", base=base, program=program)
    code_signals = {}
    visited = 0
    stopped = None      # hard stop: file cap or cancel (ends the walk)
    depth_limited = False
    for dirpath, dirnames, filenames in os.walk(root):
        if stopped is not None:
            break
        if cancelled is not None and cancelled():
            stopped = "scan cancelled — partial results"
            break
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(dirpath, d))]
        if depth >= max_depth and dirnames:
            # Don't descend deeper — but still scan this level's files.
            depth_limited = True
            dirnames[:] = []
        for name in sorted(filenames):
            if stopped is not None:
                break
            if cancelled is not None and cancelled():
                stopped = "scan cancelled — partial results"
                break
            visited += 1
            if visited > max_files:
                stopped = (f"scan stopped at the {max_files}-file cap "
                           "— some files were not scanned")
                break
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            signal = fp_mod.code_signal_for_name(name)
            if signal:
                seen = code_signals.setdefault(signal, [])
                if name not in seen:
                    seen.append(name)
            ext = os.path.splitext(name)[1].lower()
            kind = _EXT_KIND.get(ext)
            if kind is None:
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size == 0:
                continue
            if size > max_file_bytes:
                agroup.manifest["skipped"].append({
                    "name": name, "source_path": os.path.relpath(full, root),
                    "size": size, "kind": kind,
                    "reason": f"over the "
                              f"{max_file_bytes // (1024 * 1024)} MB "
                              "per-file cap — skipped, nothing saved",
                })
                continue
            # Group cap BEFORE reading: a file the group has no room
            # for is skipped without ever loading it into memory
            # (a duplicate would cost 0 bytes, but its hash can't be
            # known without reading — failing closed is the safe side).
            if (agroup.manifest["total_bytes"] + size
                    > max_group_bytes):
                agroup._skip_for_group_cap(
                    name, kind, os.path.relpath(full, root), size,
                    max_group_bytes)
                continue
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            agroup.add_bytes(data, name, kind,
                             source_path=os.path.relpath(full, root),
                             max_file_bytes=max_file_bytes,
                             max_group_bytes=max_group_bytes)
    if stopped is not None:
        agroup.manifest["scan_note"] = stopped
    elif depth_limited:
        agroup.manifest["scan_note"] = (
            f"scan did not descend past {max_depth} levels — "
            "deeper files were not scanned")
    if not agroup.manifest["assets"] and not agroup.manifest["skipped"]:
        try:
            os.rmdir(agroup.dir)
        except OSError:
            pass
        return None, None
    if code_signals:
        agroup.manifest["code_signals"] = code_signals
    return agroup.finish()


# ---- listing -----------------------------------------------------------------

def _read_manifest(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "assets" not in data:
        return None
    return data


def list_asset_groups(base=None):
    """[(group_dir, manifest)] for every asset group, newest first."""
    base = base or store.data_dir()
    adir = assets_dir(base)
    if not os.path.isdir(adir):
        return []
    out = []
    for name in sorted(os.listdir(adir)):
        mpath = os.path.join(adir, name, "assets.json")
        if not os.path.isfile(mpath):
            continue
        manifest = _read_manifest(mpath)
        if manifest is None:
            continue
        out.append((os.path.join(adir, name), manifest))
    out.sort(key=lambda gm: gm[1].get("captured_at", ""), reverse=True)
    return out


def find_groups(name, base=None):
    """[(group_dir, manifest)] whose dir name or manifest group name
    matches `name` — one name can match several extraction events."""
    out = []
    for gdir, manifest in list_asset_groups(base):
        if os.path.basename(gdir) == name or manifest.get("group") == name:
            out.append((gdir, manifest))
    return out


def delete_group(gdir, base=None):
    """Delete one asset group directory (files + manifest). Returns
    {"deleted": path or None, "bytes_freed": n}; None when it doesn't
    exist or isn't inside assets/. Packs built from the group are
    self-contained (they hold their own copies), so deleting a group
    never orphans a pack."""
    base = base or store.data_dir()
    adir = os.path.abspath(assets_dir(base))
    target = os.path.abspath(gdir)
    if os.path.commonpath([target, adir]) != adir:
        return {"deleted": None, "bytes_freed": 0}
    if target == adir or not os.path.isdir(target):
        return {"deleted": None, "bytes_freed": 0}
    freed = store.dir_size(target)
    try:
        shutil.rmtree(target)
    except OSError:
        return {"deleted": None, "bytes_freed": 0}
    return {"deleted": target, "bytes_freed": freed}


def list_assets(base=None, host=None, group=None, group_name=None):
    """All asset entries (each with its group dir + manifest attached).

    host filters to groups captured from that host; group filters to
    one group dir name; group_name filters to every group whose
    manifest group name matches (a program scan names its group
    after the program, and has no host). Newest groups first.
    """
    out = []
    for gdir, manifest in list_asset_groups(base):
        if host is not None and manifest.get("host") != host:
            continue
        if group is not None and os.path.basename(gdir) != group:
            continue
        if group_name is not None and manifest.get("group") != group_name:
            continue
        for entry in manifest.get("assets", []):
            item = dict(entry)
            item["group_dir"] = gdir
            item["group"] = manifest.get("group")
            item["source"] = manifest.get("source")
            out.append(item)
    return out


def resolve_scope(arg, base=None):
    """Turn a CLI host/group argument into list_assets() kwargs.

    A scanned program folder has no host — its group is named after
    the program — so `mimik assets <name>` / `mimik build-pack <name>`
    must accept a group name too. Host matches win, then group name,
    then group dir name. An unknown name stays a host, so callers can
    try the saved-capture refresh and report it plainly.
    """
    if arg is None:
        return {}
    groups = list_asset_groups(base)
    if any(m.get("host") == arg for _, m in groups):
        return {"host": arg}
    if any(m.get("group") == arg for _, m in groups):
        return {"group_name": arg}
    if any(os.path.basename(g) == arg for g, _ in groups):
        return {"group": arg}
    return {"host": arg}


def model_info(path):
    """Short human-readable info for a 3D model file, or None when the
    file doesn't exist. .obj files get vertex/face counts; .glb files
    get a mesh/primitive count from the JSON chunk; anything else gets
    the format + size. Counts are best-effort — a file that can't be
    read still gets its format + size."""
    if not os.path.isfile(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    lines = [f"format: {ext[1:].upper() or 'unknown'}",
             f"size: {store.format_size(size)}"]
    if ext == ".obj" and size <= 20 * 1024 * 1024:
        try:
            verts = faces = 0
            with open(path, "r", errors="ignore") as fh:
                for line in fh:
                    if line.startswith("v "):
                        verts += 1
                    elif line.startswith("f "):
                        faces += 1
            lines.append(f"vertices: {verts}")
            lines.append(f"faces: {faces}")
        except OSError:
            pass
    elif ext == ".glb":
        try:
            with open(path, "rb") as fh:
                header = fh.read(12)
                raw = fh.read(8)
                if (len(header) == 12 and header[:4] == b"glTF"
                        and len(raw) == 8 and raw[4:] == b"JSON"):
                    chunk_len = int.from_bytes(raw[:4], "little")
                    if chunk_len <= 1024 * 1024:
                        doc = json.loads(fh.read(chunk_len))
                        meshes = doc.get("meshes", [])
                        lines.append(f"meshes: {len(meshes)}")
                        lines.append(
                            f"primitives: {sum(len(m.get('primitives', [])) for m in meshes)}")
        except (OSError, ValueError):
            pass
    return "\n".join(lines)


def asset_file_path(entry, base=None):
    """Absolute path of a listed asset entry's saved bytes."""
    gdir = entry.get("group_dir")
    if gdir:
        return os.path.join(gdir, entry["file"])
    return None
