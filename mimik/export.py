"""Pack/preset export-import — the backup/restore story (item 27).

Exports are zip files with a `mimik-export.json` marker naming the
kind (preset or pack), so import can validate the file is a MIMIK
export and route it to the right place. Presets export as their JSON
file; packs export as their whole directory. Import refuses to
overwrite an existing preset/pack unless overwrite=True (the GUI
asks; the CLI takes --force). Extraction is traversal-checked: no
absolute paths, no `..`, nothing lands outside its target dir.
"""
import json
import os
import shutil
import zipfile

from . import __version__, presets, reblend_bridge, store

MARKER = "mimik-export.json"
FORMAT = "mimik-export"
FORMAT_VERSION = 1


class ExportError(ValueError):
    """Not a MIMIK export, or the export is malformed."""


def _write_zip(dest, marker, files):
    """files: [(arcname, abspath)]. The marker goes in first."""
    parent = os.path.dirname(os.path.abspath(dest))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MARKER, json.dumps(marker, indent=2))
        for arcname, path in files:
            zf.write(path, arcname)
    return dest


def _preset_file(name, base=None):
    """(relpath under presets/, abspath) for an app or pattern preset
    name, or (None, None)."""
    base = base or store.data_dir()
    for scope in ("host", "group"):
        path = presets.preset_path(name, scope, base)
        if os.path.isfile(path):
            return os.path.basename(path), path
    path = presets.pattern_preset_path(name, base)
    if os.path.isfile(path):
        return os.path.join("patterns", os.path.basename(path)), path
    return None, None


def export_preset(name, dest, base=None):
    """Export one app or pattern preset to a zip. Returns dest."""
    rel, path = _preset_file(name, base)
    if rel is None:
        raise ExportError(f"no preset named {name}")
    marker = {"format": FORMAT, "version": FORMAT_VERSION,
              "kind": "preset", "name": name,
              "mimik_version": __version__}
    return _write_zip(dest, marker, [(f"preset/{rel}", path)])


def export_pack(name, dest, base=None):
    """Export one pack (by dir name, app, or host — newest when several
    match) to a zip. Returns dest."""
    found = reblend_bridge.find_packs(name, base)
    if not found:
        raise ExportError(f"no pack named {name}")
    pdir, pack = found[0]  # list_packs is newest first
    files = []
    for root, _dirs, names in os.walk(pdir):
        for n in sorted(names):
            full = os.path.join(root, n)
            files.append((os.path.join("pack",
                                       os.path.relpath(full, pdir)), full))
    marker = {"format": FORMAT, "version": FORMAT_VERSION,
              "kind": "pack", "name": os.path.basename(pdir),
              "app": pack.get("app"), "host": pack.get("host"),
              "mimik_version": __version__}
    return _write_zip(dest, marker, files)


def export_kind(path):
    """The kind ("preset" | "pack") of an export file, or None when
    the file isn't a MIMIK export."""
    try:
        with zipfile.ZipFile(path) as zf:
            marker = json.loads(zf.read(MARKER))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None
    if (isinstance(marker, dict)
            and marker.get("format") == FORMAT
            and marker.get("version") == FORMAT_VERSION
            and marker.get("kind") in ("preset", "pack")):
        return marker["kind"]
    return None


def _safe_join(root, rel):
    """root/rel, or None when rel escapes root."""
    root = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root, rel))
    if os.path.commonpath([target, root]) != root:
        return None
    return target


def import_export(path, base=None, overwrite=False):
    """Import an export zip. Returns {"kind", "name", "path"}.
    Raises ExportError for a bad file, FileExistsError when the
    target already exists and overwrite is False."""
    base = base or store.data_dir()
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise ExportError(f"not a zip file: {e}")
    with zf:
        try:
            marker = json.loads(zf.read(MARKER))
        except (KeyError, ValueError):
            raise ExportError("not a MIMIK export (no marker)")
        if (not isinstance(marker, dict)
                or marker.get("format") != FORMAT
                or marker.get("version") != FORMAT_VERSION):
            raise ExportError("not a MIMIK export (bad marker)")
        kind = marker.get("kind")
        if kind == "preset":
            return _import_preset(zf, base, overwrite)
        if kind == "pack":
            return _import_pack(zf, marker, base, overwrite)
        raise ExportError(f"unknown export kind: {kind!r}")


def _import_preset(zf, base, overwrite):
    names = [n for n in zf.namelist()
             if n.startswith("preset/") and not n.endswith("/")]
    if len(names) != 1:
        raise ExportError("preset export must hold exactly one preset")
    rel = names[0][len("preset/"):]
    target = _safe_join(presets.presets_dir(base), rel)
    if target is None:
        raise ExportError("preset path escapes the presets dir")
    try:
        data = json.loads(zf.read(names[0]))
    except ValueError:
        raise ExportError("preset file is not valid JSON")
    if not isinstance(data, dict) or "name" not in data:
        raise ExportError("preset file is malformed")
    if os.path.exists(target) and not overwrite:
        raise FileExistsError(target)
    store.ensure_private_dir(os.path.dirname(target))
    store.atomic_write(target, json.dumps(data, indent=2))
    return {"kind": "preset", "name": data["name"], "path": target}


def _import_pack(zf, marker, base, overwrite):
    pack_dir = marker.get("name")
    if not pack_dir or os.path.basename(pack_dir) != pack_dir:
        raise ExportError("pack export has a bad pack name")
    names = [n for n in zf.namelist()
             if n.startswith("pack/") and not n.endswith("/")]
    if "pack/pack.json" not in names:
        raise ExportError("pack export has no pack.json")
    try:
        pack_data = json.loads(zf.read("pack/pack.json"))
    except ValueError:
        raise ExportError("pack.json is not valid JSON")
    if not isinstance(pack_data, dict):
        raise ExportError("pack.json is malformed")
    target_dir = _safe_join(reblend_bridge.packs_dir(base), pack_dir)
    if target_dir is None:
        raise ExportError("pack name escapes the packs dir")
    if os.path.exists(target_dir):
        if not overwrite:
            raise FileExistsError(target_dir)
        # Replace wholesale — stale files from the old pack must not
        # survive a restore.
        shutil.rmtree(target_dir)
    store.ensure_private_dir(reblend_bridge.packs_dir(base))
    store.ensure_private_dir(target_dir)
    for n in names:
        rel = n[len("pack/"):]
        target = _safe_join(target_dir, rel)
        if target is None:
            raise ExportError(f"pack entry escapes the pack dir: {rel}")
        store.ensure_private_dir(os.path.dirname(target))
        store.atomic_write(target, zf.read(n), private=False)
    return {"kind": "pack", "name": pack_dir, "path": target_dir}
