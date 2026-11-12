"""MIMIK–Reblend bridge: a clone gets its look, not just its code.

Takes the assets MIMIK captured (see mimik.assets) and turns them
into a Reblend asset pack: converted meshes, a scene file, and a
manifest — so a cloned game or program opens in Reblend looking like
itself.

Rules of the bridge:
  * MIMIK never vendors or copies Reblend code, and never modifies
    Reblend's tree. It loads Reblend's own light_engine package by
    path and calls its own import_mesh / save_mesh / scene functions
    — the same code Reblend itself runs.
  * Reblend materials carry PROCEDURAL textures only ("none",
    "wood_grain", ...). There is no image-file texture support in
    scene v2, so images become color-only materials (base color from
    the image's average color) plus the original file kept in the
    pack. The pack manifest says this plainly; nothing pretends
    otherwise.
  * Reblend has no audio support: audio and fonts are kept as
    originals for the clone, not wired into a scene. The manifest
    and the GUI say so plainly.
  * .gltf (JSON + separate .bin) is not supported by Reblend's
    importer — the original is kept with a note to convert it to
    .glb in Blender. No glTF parser is written here.
  * No Reblend install, no conversion — but capture and listing
    still work, and the pack still assembles with originals. The
    user is told plainly to point MIMIK at their Reblend folder.

Pack layout (under the data dir, MIMIK_DATA_DIR overrides):
    packs/<app>_<YYYYMMDD-HHMMSS>/
        <app>.json            Reblend scene (v2), when Reblend was found
        assets/               converted .mesh.json files
        textures/             image originals
        originals/            model / audio / font originals
        pack.json             manifest: sources, conversions, warnings
"""
import glob
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

from . import assets as assets_mod
from . import store

DEFAULT_SCALE = 100.0
NEUTRAL_GRAY = "#808080"

REBLEND_NOT_FOUND = (
    "Reblend not found — point MIMIK at your Reblend folder "
    "(the folder with light_engine/ in it)"
)

AUDIO_NOTE = ("kept for the clone, not wired into a Reblend scene yet "
              "— Reblend has no audio support")
FONT_NOTE = ("kept for the clone, not wired into a Reblend scene yet "
             "— Reblend has no font support")
GLTF_NOTE = "needs .glb — convert in Blender (File, Export, glTF 2.0)"
IMAGE_MATERIAL_NOTE = ("color only — Reblend materials use procedural "
                       "textures, not image files")

# Where a Reblend install might live on Joshua's Mint PC. The GUI
# picker and --reblend always win over auto-detect.
_AUTO_GLOBS = [
    os.path.join(os.path.expanduser("~"), ".local", "share", "reblend*"),
    os.path.join(os.path.expanduser("~"), "Desktop", "reblend*"),
    os.path.join(os.path.expanduser("~"), "reblend*"),
]

_loaded = {}  # abspath -> loaded light_engine namespace


# ---- finding Reblend ---------------------------------------------------------

def _is_reblend_dir(path):
    """A folder counts as Reblend when it holds light_engine/ with the
    importer and scene modules. main.py is only needed to launch it."""
    if not path or not os.path.isdir(path):
        return False
    le = os.path.join(path, "light_engine")
    for name in ("__init__.py", "meshio.py", "meshfile.py", "scene.py"):
        if not os.path.isfile(os.path.join(le, name)):
            return False
    return True


def _settings_path(base=None):
    return os.path.join(base or store.data_dir(), "reblend.json")


def get_saved_path(base=None):
    """The Reblend folder the user picked before, or None."""
    try:
        with open(_settings_path(base)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    path = data.get("path") if isinstance(data, dict) else None
    return path if _is_reblend_dir(path) else None


def set_reblend_path(path, base=None):
    """Remember a Reblend folder. Returns (ok, message)."""
    if not _is_reblend_dir(path):
        return False, (
            f"{path} does not look like a Reblend folder — it needs "
            "light_engine/ with meshio.py, meshfile.py and scene.py in it")
    base = base or store.data_dir()
    os.makedirs(base, exist_ok=True)
    with open(_settings_path(base), "w") as fh:
        json.dump({"path": os.path.abspath(path)}, fh)
    return True, f"Reblend folder set to {os.path.abspath(path)}"


def find_reblend(explicit=None, base=None):
    """A Reblend install folder, or None.

    Order: --reblend / picker argument, REBLEND_PATH env, the saved
    setting, then auto-detect globs. Never raises for a missing
    install — that is a normal state, reported plainly by callers.
    """
    for cand in (explicit, os.environ.get("REBLEND_PATH")):
        if cand and _is_reblend_dir(cand):
            return os.path.abspath(cand)
    saved = get_saved_path(base)
    if saved:
        return os.path.abspath(saved)
    for pattern in _AUTO_GLOBS:
        for cand in sorted(glob.glob(pattern)):
            if _is_reblend_dir(cand):
                return os.path.abspath(cand)
    return None


def load_light_engine(reblend_path):
    """Load Reblend's own light_engine package from its folder.

    Returns a namespace with meshio, meshfile, scene, version. The
    package is loaded by path (never copied, never modified); a
    conflicting light_engine already imported from elsewhere is a
    plain error, not something to guess around.
    """
    path = os.path.abspath(reblend_path)
    if path in _loaded:
        return _loaded[path]
    le_dir = os.path.join(path, "light_engine")
    existing = sys.modules.get("light_engine")
    if existing is not None:
        existing_file = getattr(existing, "__file__", "") or ""
        if not existing_file.startswith(le_dir):
            raise RuntimeError(
                "a different light_engine package is already loaded "
                f"({existing_file}) — restart MIMIK to switch Reblend "
                "folders")
        le_pkg = existing
    else:
        spec = importlib.util.spec_from_file_location(
            "light_engine", os.path.join(le_dir, "__init__.py"),
            submodule_search_locations=[le_dir])
        le_pkg = importlib.util.module_from_spec(spec)
        sys.modules["light_engine"] = le_pkg
        spec.loader.exec_module(le_pkg)
    import types

    ns = types.SimpleNamespace(
        package=le_pkg,
        meshio=importlib.import_module("light_engine.meshio"),
        meshfile=importlib.import_module("light_engine.meshfile"),
        scene=importlib.import_module("light_engine.scene"),
        version=getattr(le_pkg, "__version__", "unknown"),
        path=path,
    )
    _loaded[path] = ns
    return ns


# ---- image color ---------------------------------------------------------------

def average_color(image_path):
    """(hex_color, note) — the average color of an image.

    Uses QImage when PySide6 is available (it is a MIMIK dependency);
    without it — or for SVG, which QImage cannot rasterize — the
    neutral gray fallback is returned with a plain note.
    """
    ext = os.path.splitext(image_path)[1].lower()
    if ext == ".svg":
        return NEUTRAL_GRAY, "SVG — average color not computed"
    try:
        from PySide6.QtGui import QImage
    except ImportError:
        return NEUTRAL_GRAY, "PySide6 not available — color not computed"
    img = QImage(image_path)
    if img.isNull():
        return NEUTRAL_GRAY, "image could not be read — color not computed"
    if max(img.width(), img.height()) > 32:
        img = img.scaled(32, 32)
    r = g = b = n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            px = img.pixel(x, y)
            r += (px >> 16) & 0xFF
            g += (px >> 8) & 0xFF
            b += px & 0xFF
            n += 1
    if not n:
        return NEUTRAL_GRAY, "empty image — color not computed"
    return "#%02x%02x%02x" % (r // n, g // n, b // n), None


# ---- conversion ------------------------------------------------------------------

def convert_model(asset_path, assets_dir, scale, le, used):
    """One model file -> .mesh.json files via Reblend's own importer.

    Mirrors Reblend's own import flow (window.import_mesh_objects):
    import_mesh, unique_asset_name, save_mesh. Returns a result dict;
    a failed import is a recorded failure, never an exception.
    """
    stem = os.path.splitext(os.path.basename(asset_path))[0]
    result = {"source": os.path.basename(asset_path), "ok": False,
              "objects": [], "triangles": 0, "warnings": [], "error": None}
    try:
        objects, report = le.meshio.import_mesh(asset_path, scale=scale)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    result["triangles"] = report.get("triangles", 0)
    result["warnings"].extend(report.get("warnings", []))
    if le.meshio.is_heavy(result["triangles"]):
        result["warnings"].append(
            f"{result['triangles']:,} triangles — heavy on this machine, "
            "imported anyway")
    for item in objects:
        filename = le.meshio.unique_asset_name(
            stem, item["name"], assets_dir, used)
        path = os.path.join(assets_dir, filename)
        try:
            le.meshfile.save_mesh(
                path, item["mesh"], source=os.path.basename(asset_path),
                imported=datetime.now().isoformat(timespec="seconds"))
        except OSError as exc:
            result["warnings"].append(f"could not write {filename}: {exc}")
            continue
        result["objects"].append({
            "name": item["name"],
            "mesh_file": f"assets/{filename}",
            "material": item.get("material"),
            "position": item.get("position"),
            "rotation": item.get("rotation"),
            "scale": item.get("scale"),
            "triangles": len(item["mesh"]["indices"]) // 3,
        })
    result["ok"] = bool(result["objects"])
    if not result["ok"] and not result["error"]:
        result["error"] = "no objects came out of the import"
    return result


# ---- packs -----------------------------------------------------------------------

def packs_dir(base=None):
    return os.path.join(base or store.data_dir(), "packs")


def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_app(name):
    out = "".join(ch if (ch.isalnum() or ch in "._-") else "-"
                  for ch in (name or "app"))
    return out.strip(".-") or "app"


def build_pack(host=None, group=None, group_name=None, reblend=None,
               out=None, scale=None, base=None):
    """Assemble a Reblend asset pack from captured assets.

    host picks every group captured from that host; group picks one
    group dir name; group_name picks every group whose manifest group
    name matches (program scans). scale=None means "the saved
    preset's scale for this app, else the first matching pattern
    preset's scale, else the default 100" — an explicit scale always
    wins, and the scale used is recorded on the preset (when one
    exists) and on the pattern presets that applied, for the next
    build. Returns a result dict (pack_dir, scene_path or None,
    counts, warnings, notes, reblend_path or None, scale,
    preset_applied, pattern_presets_applied). Never raises for a
    missing Reblend install or a failed conversion — those land in
    the result and in pack.json.
    """
    base = base or store.data_dir()
    entries = assets_mod.list_assets(base, host=host, group=group,
                                     group_name=group_name)
    # The preset for this app (if one was learned): its scale is the
    # default when no explicit scale was given; a matching pattern
    # preset's scale is next, then the default 100.
    from . import presets as presets_mod

    preset = presets_mod.find_preset_for(host=host, group=group,
                                         group_name=group_name, base=base)
    preset_applied = False
    pattern_presets_applied = []
    if scale is None:
        scale, preset_applied, pattern_presets_applied = (
            presets_mod.resolve_pack_scale(
                host=host, group=group, group_name=group_name, base=base))
        if scale is None:
            scale = DEFAULT_SCALE
    app = host or group_name or group or "app"
    resolved_group_name = group_name
    if resolved_group_name is None and group is not None:
        for gdir, manifest in assets_mod.list_asset_groups(base):
            if os.path.basename(gdir) == group:
                resolved_group_name = manifest.get("group")
                break
    stamp = _stamp()
    pack_dir = out or os.path.join(packs_dir(base),
                                   f"{_safe_app(app)}_{stamp}")
    assets_out = os.path.join(pack_dir, "assets")
    textures_out = os.path.join(pack_dir, "textures")
    originals_out = os.path.join(pack_dir, "originals")
    for d in (assets_out, textures_out, originals_out):
        os.makedirs(d, exist_ok=True)

    reblend_path = find_reblend(reblend, base=base)
    le = None
    notes = []
    warnings = []
    if reblend_path is None:
        notes.append(REBLEND_NOT_FOUND + " — the pack still holds the "
                     "captured originals; conversions are pending")
    else:
        try:
            le = load_light_engine(reblend_path)
        except (RuntimeError, ImportError, OSError) as exc:
            notes.append(f"Reblend found at {reblend_path} but its "
                         f"light_engine could not be loaded ({exc}) — "
                         "the pack still holds the captured originals; "
                         "conversions are pending")
            reblend_path = None

    scene_objects = []
    materials = []
    converted = []
    seen_hashes = set()
    used_names = set()  # .mesh.json names claimed in this pack, so two
    # models can never land on the same asset file
    counts = {"image": 0, "model": 0, "audio": 0, "font": 0, "other": 0}
    for entry in entries:
        digest = entry.get("sha256")
        if digest in seen_hashes:
            continue  # same bytes captured twice — convert once
        seen_hashes.add(digest)
        kind = entry.get("kind", "other")
        counts[kind] = counts.get(kind, 0) + 1
        src = assets_mod.asset_file_path(entry, base)
        if not src or not os.path.isfile(src):
            warnings.append(f"{entry.get('name')}: saved bytes are gone — "
                            "skipped")
            continue
        name = entry.get("name") or os.path.basename(src)
        ext = os.path.splitext(name)[1].lower()
        if kind == "image":
            dest = os.path.join(textures_out, _unique(textures_out, name))
            shutil.copyfile(src, dest)
            color, note = average_color(src)
            mat = {"name": os.path.splitext(name)[0],
                   "base_color": color,
                   "sha256": digest,
                   "source": f"textures/{os.path.basename(dest)}",
                   "note": IMAGE_MATERIAL_NOTE}
            if note:
                mat["note"] = f"{IMAGE_MATERIAL_NOTE} ({note})"
            materials.append(mat)
        elif kind == "model" and ext == ".gltf":
            dest = os.path.join(originals_out, _unique(originals_out, name))
            shutil.copyfile(src, dest)
            converted.append({"name": name, "kind": kind, "ok": False,
                              "sha256": digest,
                              "note": GLTF_NOTE,
                              "original": f"originals/{os.path.basename(dest)}"})
        elif kind == "model":
            dest = os.path.join(originals_out, _unique(originals_out, name))
            shutil.copyfile(src, dest)
            record = {"name": name, "kind": kind, "sha256": digest,
                      "original": f"originals/{os.path.basename(dest)}"}
            if le is None:
                record["ok"] = False
                record["note"] = "Reblend not found — conversion pending"
            else:
                conv = convert_model(src, assets_out, scale, le, used_names)
                record.update({"ok": conv["ok"],
                               "objects": conv["objects"],
                               "triangles": conv["triangles"],
                               "warnings": conv["warnings"],
                               "error": conv["error"]})
                warnings.extend(conv["warnings"])
                for obj in conv["objects"]:
                    scene_objects.append(obj)
            converted.append(record)
        elif kind == "audio":
            dest = os.path.join(originals_out, _unique(originals_out, name))
            shutil.copyfile(src, dest)
            converted.append({"name": name, "kind": kind, "ok": False,
                              "sha256": digest,
                              "note": AUDIO_NOTE,
                              "original": f"originals/{os.path.basename(dest)}"})
        elif kind == "font":
            dest = os.path.join(originals_out, _unique(originals_out, name))
            shutil.copyfile(src, dest)
            converted.append({"name": name, "kind": kind, "ok": False,
                              "sha256": digest,
                              "note": FONT_NOTE,
                              "original": f"originals/{os.path.basename(dest)}"})
        else:
            dest = os.path.join(originals_out, _unique(originals_out, name))
            shutil.copyfile(src, dest)
            converted.append({"name": name, "kind": kind, "ok": False,
                              "sha256": digest,
                              "note": "kept as an original",
                              "original": f"originals/{os.path.basename(dest)}"})

    scene_path = None
    scene_name = f"{_safe_app(app)}.json"
    if le is not None and scene_objects:
        scene = le.scene.new_scene(f"{app} — MIMIK asset pack")
        for obj in scene_objects:
            sobj = le.scene.new_object("mesh", name=obj["name"],
                                       material=obj.get("material"))
            sobj["mesh_file"] = obj["mesh_file"]
            for key in ("position", "rotation", "scale"):
                if obj.get(key) is not None:
                    sobj[key] = [float(v) for v in obj[key]]
            scene["objects"].append(sobj)
        scene_path = os.path.join(pack_dir, scene_name)
        le.scene.save_scene(scene, scene_path)
    elif le is not None:
        notes.append("no 3D models converted — no scene file was written "
                     "(images, audio and fonts travel as originals)")

    pack = {
        "app": app,
        "host": host,
        "group": group,
        "group_name": resolved_group_name,
        "preset": preset["name"] if preset else None,
        "preset_applied": preset_applied,
        "pattern_presets_applied": pattern_presets_applied,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "reblend_path": reblend_path,
        "reblend_version": le.version if le is not None else None,
        "scale": scale,
        "scene": scene_name if scene_path else None,
        "counts": counts,
        "materials": materials,
        "assets": converted,
        "warnings": warnings,
        "notes": notes,
    }
    with open(os.path.join(pack_dir, "pack.json"), "w") as fh:
        json.dump(pack, fh, indent=2)

    # Remember the scale used, on the preset (when one exists) and on
    # the pattern presets that applied — the next build reuses it.
    # Never breaks a built pack.
    try:
        presets_mod.record_pack(host=host, group=group,
                                group_name=group_name, scale=scale,
                                base=base)
        presets_mod.record_pattern_pack(pattern_presets_applied,
                                        scale=scale, base=base)
    except Exception:
        pass

    return {
        "pack_dir": pack_dir,
        "scene_path": scene_path,
        "scene_name": scene_name if scene_path else None,
        "reblend_path": reblend_path,
        "reblend_version": le.version if le is not None else None,
        "scale": scale,
        "preset": preset["name"] if preset else None,
        "preset_applied": preset_applied,
        "pattern_presets_applied": pattern_presets_applied,
        "counts": counts,
        "models_converted": sum(1 for c in converted
                                if c.get("kind") == "model" and c.get("ok")),
        "materials": materials,
        "warnings": warnings,
        "notes": notes,
        "pack": pack,
    }


def _unique(directory, name):
    """A collision-safe filename inside directory."""
    stem, ext = os.path.splitext(name)
    candidate = name
    n = 2
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{stem}-{n}{ext}"
        n += 1
    return candidate


def list_packs(base=None):
    """[(pack_dir, pack.json dict)] for every built pack, newest first."""
    base = base or store.data_dir()
    pdir = packs_dir(base)
    if not os.path.isdir(pdir):
        return []
    out = []
    for name in sorted(os.listdir(pdir)):
        jpath = os.path.join(pdir, name, "pack.json")
        if not os.path.isfile(jpath):
            continue
        try:
            with open(jpath) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append((os.path.join(pdir, name), data))
    out.sort(key=lambda pd: pd[1].get("built_at", ""), reverse=True)
    return out


# ---- opening a pack in Reblend ---------------------------------------------------

def reblend_launch_args(reblend_path, scene_path):
    """argv to open a scene in Reblend, or None when it cannot launch.

    Reblend's main.py takes --scene <path>, so the pack opens
    directly. Without main.py there is no launch — the caller opens
    the pack folder instead and says so.
    """
    if not reblend_path:
        return None
    main_py = os.path.join(reblend_path, "main.py")
    if not os.path.isfile(main_py):
        return None
    return [sys.executable, main_py, "--scene", scene_path]


def open_in_reblend(reblend_path, scene_path):
    """Launch Reblend on a pack scene. Returns (ok, message)."""
    args = reblend_launch_args(reblend_path, scene_path)
    if args is None:
        return False, (
            "Reblend cannot be launched from here — open the pack folder "
            "and open the scene from Reblend's File menu instead")
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return False, f"could not launch Reblend: {exc}"
    return True, "opening the pack in Reblend"
