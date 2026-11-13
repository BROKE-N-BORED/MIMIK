"""Learned presets — rules MIMIK teaches itself from your captures.

Joshua's direction: MIMIK should LEARN PRESETS for everything, so its
behavior gets slowly PROGRAMMED (deterministic rules) instead of using
AI for absolutely everything. A preset is a small JSON file holding
rules extracted from saved captures by pure rule extraction — no LLM,
no network, no new dependency.

Two layers:

* APP presets — one file per app, host, or program. The rules that
  app taught MIMIK (auth, endpoint shapes, asset patterns, pack
  scale). Applied when the SAME app is seen again.
* PATTERN presets — one file per traffic fingerprint (see
  mimik.fingerprints): the same process, algorithm, or code, seen
  anywhere. A pattern preset learned from one OAuth2 app applies to
  the next OAuth2 app MIMIK meets. App presets win per-section;
  pattern presets fill the sections the app preset lacks.

A preset holds:
  * auth — header/cookie NAMES that carry tokens (never values), the
    auth scheme word (Bearer/Basic/...) when the header says one, and
    sign-in endpoint shapes when one is visible. A pattern preset
    holds only the auth its fingerprint evidences.
  * endpoints — generalized endpoint shapes (ids become {id}) with a
    seen count and first/last seen dates. A pattern preset holds
    only the shapes that evidenced its fingerprint.
  * asset_mapping — which URL/path patterns carry which asset kinds.
  * pack — the model scale from that app's last built pack.

Hard rule: a preset NEVER contains a secret value. Only names and
shapes are learned; token and cookie VALUES are dropped at extraction
time. tests/test_presets.py and tests/test_pattern_presets.py pin
this with a byte search.

Layout (under the data dir, MIMIK_DATA_DIR overrides):
    presets/<scope>_<safe-name>.json   one file per host or program
    presets/patterns/<fingerprint>.json  one file per fingerprint
    settings.json                      {"auto_learn_presets": false}

Learning is explicit by default (`mimik presets learn <name>` or the
GUI Presets tab) — it learns the app preset AND the pattern presets
the captures exhibit. The "Learn presets from captures" setting
(default OFF) also refreshes presets when a record run stops.
"""
import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse

from . import assets, codegen, extract, fingerprints, paths, store

MAX_ENDPOINTS = 200
MAX_ASSET_PATTERNS = 50
MAX_TOKEN_ENDPOINTS = 10

# Only these scheme words are ever recorded from an Authorization
# header — a bare token has no scheme, and its first word must never
# be mistaken for one.
_KNOWN_SCHEMES = {"bearer": "Bearer", "basic": "Basic",
                  "digest": "Digest", "token": "Token",
                  "dpop": "DPoP", "hawk": "Hawk", "oauth": "OAuth"}

# Path words that mark an endpoint as a sign-in / token endpoint.
_AUTH_PATH_WORDS = ("login", "signin", "sign-in", "sign_in", "auth",
                    "token", "refresh", "oauth", "session", "sso")

# Header-name hints (lowercase substrings) for token-carrying headers.
_AUTH_HEADER_HINTS = ("token", "apikey", "api-key", "api_key", "auth")

_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def presets_dir(base=None):
    return os.path.join(base or store.data_dir(), "presets")


def _safe_name(name):
    out = "".join(ch if (ch.isalnum() or ch in "._-") else "-"
                  for ch in (name or "app"))
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip(".-") or "app"


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def preset_path(name, scope, base=None):
    return os.path.join(presets_dir(base), f"{scope}_{_safe_name(name)}.json")


# The preset file format version (item 32f). Every preset saved by
# this build stamps it; loads validate it (see _read_preset_file).
PRESET_VERSION = 1


def _read_preset_file(path):
    """A preset file's dict, version-validated — or None.

    Missing "version" migrates to 1 in memory (v0.1/v0.2 files
    predate the field; their shape is v1). A version other than 1
    reads as None — a newer MIMIK wrote it, and this one must not
    misread it. Unreadable, unparsable, or non-dict files are None
    too. The migration is in-memory only: a read never rewrites a
    user's file — the next learn stamps the version on save."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if version is None:
        data["version"] = PRESET_VERSION
        return data
    if version != PRESET_VERSION:
        return None
    return data


# ---- store / load ------------------------------------------------------------

def load_preset(name, scope=None, base=None):
    """The preset for a host or program name, or None.

    With no scope, a host preset wins over a group preset of the same
    name (a name is almost always one or the other).
    """
    scopes = (scope,) if scope else ("host", "group")
    for sc in scopes:
        path = preset_path(name, sc, base)
        if not os.path.isfile(path):
            continue
        data = _read_preset_file(path)
        if data is not None and data.get("name") == name:
            return data
    return None


def _save_preset(preset, base=None):
    base = base or store.data_dir()
    os.makedirs(presets_dir(base), exist_ok=True)
    path = preset_path(preset["name"], preset["scope"], base)
    with store.open_private(path, "w") as fh:
        json.dump(preset, fh, indent=2)
    return path


def list_presets(base=None):
    """All saved presets, sorted by name."""
    base = base or store.data_dir()
    pdir = presets_dir(base)
    if not os.path.isdir(pdir):
        return []
    out = []
    for name in sorted(os.listdir(pdir)):
        if not name.endswith(".json"):
            continue
        data = _read_preset_file(os.path.join(pdir, name))
        if data is not None and data.get("name") and data.get("scope"):
            out.append(data)
    out.sort(key=lambda p: (p["name"].lower(), p["scope"]))
    return out


def delete_preset(name, scope=None, base=None):
    """Delete a preset file. Returns True when one was removed."""
    scopes = (scope,) if scope else ("host", "group")
    removed = False
    for sc in scopes:
        path = preset_path(name, sc, base)
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed = True
            except OSError:
                pass
    return removed


def find_preset_for(host=None, group=None, group_name=None, base=None):
    """The preset for a build-pack scope (host / group dir / group name)."""
    if host:
        return load_preset(host, scope="host", base=base)
    if group_name:
        return load_preset(group_name, scope="group", base=base)
    if group:
        for gdir, manifest in assets.list_asset_groups(base):
            if os.path.basename(gdir) == group:
                gname = manifest.get("group")
                if gname:
                    return load_preset(gname, scope="group", base=base)
                break
    return None


# ---- pattern presets (the fingerprint layer) -----------------------------------

def patterns_dir(base=None):
    return os.path.join(presets_dir(base), "patterns")


def pattern_preset_path(fid, base=None):
    return os.path.join(patterns_dir(base), f"{_safe_name(fid)}.json")


def load_pattern_preset(fid, base=None):
    """The saved pattern preset for a fingerprint id, or None."""
    path = pattern_preset_path(fid, base)
    if not os.path.isfile(path):
        return None
    data = _read_preset_file(path)
    if data is not None and data.get("scope") == "pattern":
        return data
    return None


def _save_pattern_preset(preset, base=None):
    base = base or store.data_dir()
    os.makedirs(patterns_dir(base), exist_ok=True)
    path = pattern_preset_path(preset["name"], base)
    with store.open_private(path, "w") as fh:
        json.dump(preset, fh, indent=2)
    return path


def list_pattern_presets(base=None):
    """All saved pattern presets, sorted by fingerprint id."""
    base = base or store.data_dir()
    pdir = patterns_dir(base)
    if not os.path.isdir(pdir):
        return []
    out = []
    for name in sorted(os.listdir(pdir)):
        if not name.endswith(".json"):
            continue
        data = _read_preset_file(os.path.join(pdir, name))
        if data is not None and data.get("scope") == "pattern":
            out.append(data)
    out.sort(key=lambda p: p["name"])
    return out


def delete_pattern_preset(fid, base=None):
    """Delete a pattern preset file. Returns True when one was removed."""
    path = pattern_preset_path(fid, base)
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# ---- extraction (names and shapes only — never values) -----------------------

def _is_auth_header(name):
    n = name.lower()
    if n == "authorization":
        return True
    return any(h in n for h in _AUTH_HEADER_HINTS)


def _scheme_of(value):
    """The auth scheme word of a header value, or None."""
    parts = value.split(None, 1)
    if len(parts) != 2:
        return None
    return _KNOWN_SCHEMES.get(parts[0].lower())


def _cookie_names(value):
    """Cookie names from a Cookie header value (every ;-part is one)."""
    names = []
    for part in value.split(";"):
        name = part.split("=", 1)[0].strip()
        if _COOKIE_NAME_RE.match(name):
            names.append(name)
    return names


def _set_cookie_name(value):
    """The cookie name from a Set-Cookie header (only the first ;-part)."""
    first = value.split(";", 1)[0]
    name = first.split("=", 1)[0].strip()
    return [name] if _COOKIE_NAME_RE.match(name) else []


def _auth_from_flows(flows):
    """Auth rules from request/response headers — NAMES ONLY."""
    headers = {}  # lower name -> original-case name
    schemes, cookies = set(), set()
    token_eps = {}
    for f in flows:
        req = f.get("request") or {}
        for name, value in extract.flow_header_pairs(f, "request"):
            lname = name.lower()
            if lname == "cookie":
                cookies.update(_cookie_names(str(value)))
            elif _is_auth_header(name):
                headers.setdefault(lname, name)
                if lname == "authorization":
                    scheme = _scheme_of(str(value))
                    if scheme:
                        schemes.add(scheme)
        for name, value in extract.flow_header_pairs(f, "response"):
            if name.lower() == "set-cookie":
                cookies.update(_set_cookie_name(str(value)))
        method = (req.get("method") or "").upper()
        path = paths.generalize_path(req.get("path") or "")
        if any(w in path.lower() for w in _AUTH_PATH_WORDS):
            key = f"{method} {path}"
            token_eps[key] = token_eps.get(key, 0) + 1
    token_list = [k for k, _n in sorted(
        token_eps.items(), key=lambda kv: (-kv[1], kv[0])
    )][:MAX_TOKEN_ENDPOINTS]
    return {
        "headers": sorted(headers.values()),
        "schemes": sorted(schemes),
        "cookies": sorted(cookies),
        "token_endpoints": token_list,
    }


def _endpoints_from_flows(flows, now):
    """Generalized endpoint shapes with counts."""
    counts = {}
    for f in flows:
        req = f.get("request") or {}
        method = (req.get("method") or "").upper()
        if not method:
            continue
        path = paths.generalize_path(req.get("path") or "")
        key = (method, path)
        counts[key] = counts.get(key, 0) + 1
    out = [{"method": m, "path": p, "seen": n,
            "first_seen": now, "last_seen": now}
           for (m, p), n in counts.items()]
    out.sort(key=lambda e: (-e["seen"], e["method"], e["path"]))
    return out[:MAX_ENDPOINTS]


def _asset_mapping_from_entries(entries):
    """URL/path patterns -> asset kinds, with counts.

    /m/crate.glb becomes /m/*.glb; a program file assets/tex/wall.png
    becomes assets/tex/*.png. Patterns are shapes, not file lists.
    """
    counts = {}
    for e in entries:
        kind = e.get("kind") or "other"
        pattern = None
        url = e.get("url")
        if url:
            p = urlparse(url).path or ""
            ext = os.path.splitext(p)[1].lower()
            if ext:
                pattern = f"{os.path.dirname(p)}/*{ext}"
        elif e.get("source_path"):
            sp = e["source_path"]
            d = os.path.dirname(sp)
            ext = os.path.splitext(sp)[1].lower()
            if ext:
                pattern = f"{d}/*{ext}" if d else f"*{ext}"
        if pattern is None:
            ext = os.path.splitext(e.get("name") or "")[1].lower()
            pattern = f"*{ext}" if ext else "*"
        key = (pattern, kind)
        counts[key] = counts.get(key, 0) + 1
    out = [{"pattern": p, "kind": k, "seen": n}
           for (p, k), n in counts.items()]
    out.sort(key=lambda m: (-m["seen"], m["pattern"], m["kind"]))
    return out[:MAX_ASSET_PATTERNS]


def _pack_block_for(name, scope_type, packs):
    """The newest pack's scale for this preset's scope, or {}.

    Packs are newest-first. A host preset matches pack["host"]; a
    group preset matches the pack's recorded group name, else its app
    name (CLI group builds name the pack after the group).
    """
    for _pdir, pack in packs:
        if scope_type == "host":
            match = pack.get("host") == name
        else:
            match = (pack.get("group_name") == name
                     or pack.get("app") == name)
        if match and pack.get("scale") is not None:
            return {"scale": pack["scale"],
                    "last_pack": pack.get("built_at", "")}
    return {}


# ---- merging -----------------------------------------------------------------

def _merge_auth(old, new):
    return {
        "headers": sorted(set(old.get("headers", []))
                          | set(new.get("headers", []))),
        "schemes": sorted(set(old.get("schemes", []))
                          | set(new.get("schemes", []))),
        "cookies": sorted(set(old.get("cookies", []))
                          | set(new.get("cookies", []))),
        "token_endpoints": sorted(
            set(old.get("token_endpoints", []))
            | set(new.get("token_endpoints", [])))[:MAX_TOKEN_ENDPOINTS],
    }


def _merge_endpoints(old, new):
    by_key = {}
    for e in old:
        by_key[(e["method"], e["path"])] = dict(e)
    for e in new:
        key = (e["method"], e["path"])
        if key in by_key:
            cur = by_key[key]
            cur["seen"] = cur.get("seen", 0) + e.get("seen", 0)
            cur["first_seen"] = min(cur.get("first_seen", e["first_seen"]),
                                    e["first_seen"])
            cur["last_seen"] = max(cur.get("last_seen", e["last_seen"]),
                                   e["last_seen"])
        else:
            by_key[key] = dict(e)
    out = list(by_key.values())
    out.sort(key=lambda e: (-e.get("seen", 0), e["method"], e["path"]))
    return out[:MAX_ENDPOINTS]


def _merge_mapping(old, new):
    by_key = {}
    for m in old:
        by_key[(m["pattern"], m["kind"])] = dict(m)
    for m in new:
        key = (m["pattern"], m["kind"])
        if key in by_key:
            by_key[key]["seen"] = by_key[key].get("seen", 0) + m.get("seen", 0)
        else:
            by_key[key] = dict(m)
    out = list(by_key.values())
    out.sort(key=lambda m: (-m.get("seen", 0), m["pattern"], m["kind"]))
    return out[:MAX_ASSET_PATTERNS]


def _merge_code_signals(old, new):
    """{signal_id: [filenames]} merged, filenames deduped and sorted."""
    out = {}
    for source in (old or {}, new or {}):
        for sid, files in source.items():
            merged = out.setdefault(sid, [])
            for name in files or []:
                if name not in merged:
                    merged.append(name)
    for sid in out:
        out[sid] = sorted(out[sid])
    return out


def _auth_is_empty(auth):
    auth = auth or {}
    return not (auth.get("headers") or auth.get("cookies")
                or auth.get("token_endpoints"))


# ---- learning ------------------------------------------------------------------

def _learn_from(scope_type, scope_name, flows, entries, packs, base):
    """The body of learn() for already-resolved scope data."""
    pack_block = _pack_block_for(scope_name, scope_type, packs)
    if not flows and not entries and not pack_block:
        return None
    now = _now_iso()
    new = {
        "version": PRESET_VERSION,
        "scope": scope_type,
        "name": scope_name,
        "learned_at": now,
        "auth": _auth_from_flows(flows),
        "endpoints": _endpoints_from_flows(flows, now),
        "asset_mapping": _asset_mapping_from_entries(entries),
        "pack": pack_block or {},
    }
    existing = load_preset(scope_name, scope=scope_type, base=base)
    if existing is None:
        path = _save_preset(new, base)
        return new, path, True
    merged = {
        "version": PRESET_VERSION,
        "scope": scope_type,
        "name": scope_name,
        "learned_at": now,
        "auth": _merge_auth(existing.get("auth") or {}, new["auth"]),
        "endpoints": _merge_endpoints(existing.get("endpoints") or [],
                                      new["endpoints"]),
        "asset_mapping": _merge_mapping(existing.get("asset_mapping") or [],
                                        new["asset_mapping"]),
        "pack": new["pack"] or existing.get("pack") or {},
    }
    path = _save_preset(merged, base)
    return merged, path, False


def learn(name, base=None, packs=None):
    """Learn (or refresh) the preset for a host or program group.

    Reads saved flows + captured asset groups + the newest built pack,
    merges the new rules into the existing preset if there is one, and
    saves it. Returns (preset, path, created) or None when there is
    nothing to learn from (no flows, no assets, no pack).
    """
    base = base or store.data_dir()
    scope_type, scope_name, flows, entries = _scope_inputs(name, base)
    if packs is None:
        packs = _packs_for(base)
    return _learn_from(scope_type, scope_name, flows, entries, packs, base)


def _scope_inputs(name, base):
    """(scope_type, scope_name, flows, entries) for a host/group name.

    The same resolution learn() uses, shared with learn_patterns so
    both layers learn from exactly the same captures.
    """
    scope = assets.resolve_scope(name, base)
    if "group_name" in scope:
        return "group", name, [], assets.list_assets(base, group_name=name)
    if "group" in scope:
        scope_name = name
        for gdir, manifest in assets.list_asset_groups(base):
            if os.path.basename(gdir) == name:
                scope_name = manifest.get("group") or name
                break
        return "group", scope_name, [], assets.list_assets(base, group=name)
    return "host", name, store.load_host(name, base), \
        assets.list_assets(base, host=name)


def _packs_for(base):
    try:
        from . import reblend_bridge

        return reblend_bridge.list_packs(base)
    except Exception:
        return []


def _scope_fingerprint(name, base):
    """(scope_type, scope_name, flows, entries, found) for a host/group
    name — one scope resolution, one flow load, one fingerprint pass,
    shared by learn, learn_patterns, and match (review #21)."""
    scope_type, scope_name, flows, entries = _scope_inputs(name, base)
    found = fingerprints.fingerprint_loaded(
        scope_type, scope_name, flows, entries, base)
    return scope_type, scope_name, flows, entries, found


def _learn_patterns_from(scope_type, scope_name, flows, entries, found,
                         packs, base):
    """The body of learn_patterns() for already-fingerprinted data."""
    matches = found["fingerprints"]
    if not matches:
        return []
    pack_block = _pack_block_for(scope_name, scope_type, packs)
    now = _now_iso()
    all_endpoints = {(e["method"], e["path"]): e
                     for e in _endpoints_from_flows(flows, now)}
    all_mapping = _asset_mapping_from_entries(entries)
    code_signals = found["code_signals"]
    out = []
    for m in matches:
        fid = m["id"]
        # Endpoints: only the shapes that evidenced this fingerprint,
        # with their seen counts from the full extraction.
        endpoints = []
        for shape in m.get("endpoints") or []:
            entry = all_endpoints.get((shape["method"], shape["path"]))
            if entry is not None:
                endpoints.append(dict(entry))
        # Asset mapping: only the CDN patterns, and only for the
        # fingerprint that evidences them.
        mapping = ([dict(x) for x in all_mapping
                    if fingerprints.is_cdn_pattern(x["pattern"])]
                   if fid == "cdn-assets" else [])
        new = {
            "version": PRESET_VERSION,
            "scope": "pattern",
            "name": fid,
            "display_name": m["name"],
            "fingerprint": {"id": fid, "name": m["name"],
                            "signals": sorted(m.get("signals") or [])},
            "learned_from": [scope_name],
            "learned_at": now,
            "auth": m.get("auth") or {"headers": [], "schemes": [],
                                      "cookies": [], "token_endpoints": []},
            "endpoints": endpoints,
            "asset_mapping": mapping,
            "pack": pack_block or {},
            "code_signals": code_signals,
        }
        existing = load_pattern_preset(fid, base=base)
        if existing is None:
            _save_pattern_preset(new, base)
            out.append((new, True))
            continue
        old_signals = ((existing.get("fingerprint") or {})
                       .get("signals") or [])
        merged = {
            "version": PRESET_VERSION,
            "scope": "pattern",
            "name": fid,
            "display_name": m["name"],
            "fingerprint": {
                "id": fid, "name": m["name"],
                "signals": sorted(set(old_signals)
                                  | set(m.get("signals") or [])),
            },
            "learned_from": sorted(
                set(existing.get("learned_from") or []) | {scope_name}),
            "learned_at": now,
            "auth": _merge_auth(existing.get("auth") or {},
                                new["auth"]),
            "endpoints": _merge_endpoints(existing.get("endpoints") or [],
                                          new["endpoints"]),
            "asset_mapping": _merge_mapping(
                existing.get("asset_mapping") or [], new["asset_mapping"]),
            "pack": new["pack"] or existing.get("pack") or {},
            "code_signals": _merge_code_signals(
                existing.get("code_signals") or {}, code_signals),
        }
        _save_pattern_preset(merged, base)
        out.append((merged, False))
    return out


def learn_patterns(name, base=None, packs=None):
    """Learn (or refresh) the pattern presets an app's captures exhibit.

    Fingerprints the scope's traffic + assets; for each matched
    fingerprint, builds or merges a pattern preset from the same
    captures the app preset learns from — but only the rules the
    fingerprint evidences: the fingerprint-relevant auth, the
    endpoint shapes that evidenced it, the CDN asset patterns (for
    cdn-assets), and the pack scale. Plus provenance: which apps it
    was learned from, which signals define it, and which code signals
    were seen with it. Returns [(preset, created)] in fingerprint
    order, or [] when nothing matched.
    """
    base = base or store.data_dir()
    (scope_type, scope_name, flows, entries,
     found) = _scope_fingerprint(name, base)
    if packs is None:
        packs = _packs_for(base)
    return _learn_patterns_from(scope_type, scope_name, flows, entries,
                                found, packs, base)


def learn_all(name, base=None):
    """Learn the app preset AND the pattern presets for a host/group,
    and say which pattern presets match it — one scope resolution,
    one flow load, one fingerprint pass (review #21). Returns
    (app_result, pattern_results, matched_ids) where app_result is
    learn()'s return and pattern_results is learn_patterns()'s.
    matched_ids are the fingerprint ids with a saved pattern preset
    after the learn (so newly learned patterns count)."""
    base = base or store.data_dir()
    (scope_type, scope_name, flows, entries,
     found) = _scope_fingerprint(name, base)
    packs = _packs_for(base)
    app = _learn_from(scope_type, scope_name, flows, entries, packs, base)
    patterns = _learn_patterns_from(scope_type, scope_name, flows, entries,
                                    found, packs, base)
    matched = [m["id"] for m in found["fingerprints"]
               if load_pattern_preset(m["id"], base=base) is not None]
    return app, patterns, matched


def match(name, base=None):
    """Dry-run fingerprinting for `presets match`.

    Returns ([(fingerprint, pattern_preset_or_None, corroborated)],
    code_signals) — which fingerprints the app's captures exhibit,
    which of them have a saved pattern preset (those are the ones
    that would apply), and which code signals the saved preset
    shares with this app. Changes nothing on disk.
    """
    base = base or store.data_dir()
    found = fingerprints.fingerprint_scope(name, base)
    code_signals = found["code_signals"]
    out = []
    for m in found["fingerprints"]:
        preset = load_pattern_preset(m["id"], base=base)
        corroborated = []
        if preset is not None:
            shared = (set((preset.get("code_signals") or {}).keys())
                      & set(code_signals.keys()))
            corroborated = sorted(
                fingerprints.CODE_SIGNALS.get(sid, {}).get("name", sid)
                for sid in shared)
        out.append((m, preset, corroborated))
    return out, code_signals


def matched_pattern_ids(name, base=None):
    """Fingerprint ids with a saved pattern preset for this app."""
    matches, _code = match(name, base)
    return [m["id"] for m, preset, _c in matches if preset is not None]


def resolve_for_gen(host, flows=None, base=None):
    """(effective_preset, applied, pattern_ids, auth_label) for gen.

    App preset wins per-section; matching pattern presets fill the
    sections the app preset lacks. "applied" names what was used
    ("app preset for <host>", "pattern <id>"); "auth_label" is the
    wording for the generated client's auth comment when the auth
    came from a pattern. Returns (None, [], [], None) when neither
    an app preset nor a matching pattern preset exists.
    """
    base = base or store.data_dir()
    app = load_preset(host, scope="host", base=base)
    if flows is None:
        flows = store.load_host(host, base)
    else:
        flows = [f for f in flows
                 if (f.get("request") or {}).get("host") == host]
    matched = []
    if flows:
        found = fingerprints.fingerprint_flows(flows)
        entries = assets.list_assets(base, host=host)
        found = found + fingerprints.fingerprint_entries(entries)
        for m in found:
            preset = load_pattern_preset(m["id"], base=base)
            if preset is not None:
                matched.append((m, preset))
    if app is None and not matched:
        return None, [], [], None

    def pattern_section(key):
        """The merged section from matched patterns, or None."""
        values = [p.get(key) for _m, p in matched if p.get(key)]
        if not values:
            return None
        if key == "auth":
            merged = {"headers": [], "schemes": [], "cookies": [],
                      "token_endpoints": []}
            for value in values:
                merged = _merge_auth(merged, value)
            return merged
        if key == "endpoints":
            merged = []
            for value in values:
                merged = _merge_endpoints(merged, value)
            return merged
        if key == "asset_mapping":
            merged = []
            for value in values:
                merged = _merge_mapping(merged, value)
            return merged
        return values[0]

    applied = []
    pattern_ids = []
    if app is not None:
        applied.append(f"app preset for {host}")
    auth = (app or {}).get("auth") or {}
    auth_label = None
    if _auth_is_empty(auth):
        merged_auth = pattern_section("auth")
        if merged_auth is not None and not _auth_is_empty(merged_auth):
            auth = merged_auth
            suppliers = [p["name"] for _m, p in matched
                         if not _auth_is_empty(p.get("auth"))]
            auth_label = "pattern " + ", ".join(suppliers)
    endpoints = (app or {}).get("endpoints") or []
    if not endpoints:
        endpoints = pattern_section("endpoints") or []
    mapping = (app or {}).get("asset_mapping") or []
    if not mapping:
        mapping = pattern_section("asset_mapping") or []
    pack = (app or {}).get("pack") or {}
    if pack.get("scale") is None:
        for _m, p in matched:
            if (p.get("pack") or {}).get("scale") is not None:
                pack = p["pack"]
                break
    # A pattern counts as applied when it supplied a section the app
    # preset lacked.
    for _m, p in matched:
        supplied = (
            (not _auth_is_empty(p.get("auth"))
             and _auth_is_empty((app or {}).get("auth")))
            or (p.get("endpoints")
                and not (app or {}).get("endpoints"))
            or (p.get("asset_mapping")
                and not (app or {}).get("asset_mapping"))
            or ((p.get("pack") or {}).get("scale") is not None
                and ((app or {}).get("pack") or {}).get("scale") is None))
        if supplied:
            applied.append(f"pattern {p['name']}")
            pattern_ids.append(p["name"])
    effective = {
        "version": PRESET_VERSION,
        "scope": "host" if app is not None else "pattern",
        "name": host,
        "learned_at": (app or {}).get("learned_at", ""),
        "auth": auth,
        "endpoints": endpoints,
        "asset_mapping": mapping,
        "pack": pack,
    }
    return effective, applied, pattern_ids, auth_label


def resolve_pack_scale(host=None, group=None, group_name=None, base=None):
    """(scale_or_None, app_applied, pattern_ids) for a pack build.

    App preset scale wins; else the first matching pattern preset's
    scale (fingerprints in catalog order); else None (the caller
    uses its default). Pattern ids are the ones that supplied the
    scale — at most one, since the first match wins.
    """
    base = base or store.data_dir()
    preset = find_preset_for(host=host, group=group,
                             group_name=group_name, base=base)
    pscale = (preset or {}).get("pack", {}).get("scale")
    if pscale is not None:
        return float(pscale), True, []
    name = host or group_name or group
    if not name:
        return None, False, []
    try:
        found = fingerprints.fingerprint_scope(name, base)
    except Exception:
        return None, False, []
    for m in found["fingerprints"]:
        pattern = load_pattern_preset(m["id"], base=base)
        if pattern is None:
            continue
        scale = (pattern.get("pack") or {}).get("scale")
        if scale is not None:
            return float(scale), False, [m["id"]]
    return None, False, []


def record_pattern_pack(pattern_ids, scale, base=None):
    """Remember the scale a pack was built with, on the pattern
    presets that applied. Returns how many were updated."""
    if scale is None:
        return 0
    updated = 0
    for fid in pattern_ids or []:
        preset = load_pattern_preset(fid, base=base)
        if preset is None:
            continue
        preset["pack"] = {"scale": float(scale), "last_pack": _now_iso()}
        _save_pattern_preset(preset, base)
        updated += 1
    return updated


def record_pack(host=None, group=None, group_name=None, scale=None,
                base=None):
    """Remember the scale a pack was built with, on its preset.

    Only updates an EXISTING preset — building a pack never creates
    one. Returns True when a preset was updated.
    """
    if scale is None:
        return False
    preset = find_preset_for(host=host, group=group, group_name=group_name,
                             base=base)
    if preset is None:
        return False
    preset["pack"] = {"scale": float(scale), "last_pack": _now_iso()}
    _save_preset(preset, base)
    return True


# ---- the auto-learn setting ----------------------------------------------------

def _settings_path(base=None):
    return os.path.join(base or store.data_dir(), "settings.json")


def _read_settings(base=None):
    try:
        with open(_settings_path(base)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def auto_learn_enabled(base=None):
    """The "Learn presets from captures" setting. Default OFF."""
    return bool(_read_settings(base).get("auto_learn_presets", False))


def set_auto_learn(enabled, base=None):
    """Turn the "Learn presets from captures" setting on or off."""
    base = base or store.data_dir()
    settings = _read_settings(base)
    settings["auto_learn_presets"] = bool(enabled)
    os.makedirs(base, exist_ok=True)
    with open(_settings_path(base), "w") as fh:
        json.dump(settings, fh, indent=2)
    return bool(enabled)


def sample_offered(base=None):
    """Whether the first-run sample offer has been shown (accepted or
    declined). Default False — a fresh data dir hasn't seen it."""
    return bool(_read_settings(base).get("sample_offered", False))


def set_sample_offered(base=None):
    """Mark the first-run sample offer as shown, so it never pops
    again."""
    base = base or store.data_dir()
    settings = _read_settings(base)
    settings["sample_offered"] = True
    os.makedirs(base, exist_ok=True)
    with open(_settings_path(base), "w") as fh:
        json.dump(settings, fh, indent=2)
    return True


# ---- the capture-exclusion settings (item 26) -------------------------------

def excluded_hosts(base=None):
    """The capture-exclusion list. Defaults to
    filters.DEFAULT_EXCLUDED until the user edits it (an explicitly
    emptied list stays empty — only a missing key means defaults)."""
    from . import filters

    stored = _read_settings(base).get("excluded_hosts")
    if stored is None:
        return list(filters.DEFAULT_EXCLUDED)
    return [str(h).strip().lower() for h in stored if str(h).strip()]


def set_excluded_hosts(hosts, base=None):
    """Replace the capture-exclusion list (normalized, deduped)."""
    base = base or store.data_dir()
    settings = _read_settings(base)
    seen = []
    for h in hosts:
        h = str(h).strip().lower()
        if h and h not in seen:
            seen.append(h)
    settings["excluded_hosts"] = seen
    os.makedirs(base, exist_ok=True)
    with open(_settings_path(base), "w") as fh:
        json.dump(settings, fh, indent=2)
    return seen


def filtering_enabled(base=None):
    """The "exclude telemetry + analytics hosts" toggle. Default ON."""
    return bool(_read_settings(base).get("filtering_enabled", True))


def set_filtering_enabled(enabled, base=None):
    """Turn capture filtering on or off."""
    base = base or store.data_dir()
    settings = _read_settings(base)
    settings["filtering_enabled"] = bool(enabled)
    os.makedirs(base, exist_ok=True)
    with open(_settings_path(base), "w") as fh:
        json.dump(settings, fh, indent=2)
    return bool(enabled)


def after_record(hosts, base=None):
    """Record-stop preset handling for the hosts just captured.

    When auto-learn is on, refreshes each host's preset (app AND
    pattern presets) from the saved captures — one fingerprint pass
    per host, shared by the learn and the match report (review #21).
    When auto-learn is off, no learning happens, but the match report
    still fingerprints once per host. Returns
    [(host, refreshed, has_preset, matched_ids)]. Never raises —
    preset learning must never break a capture.
    """
    out = []
    auto = auto_learn_enabled(base)
    for host in hosts:
        refreshed = False
        matched = []
        if auto:
            try:
                app, patterns, matched = learn_all(host, base=base)
                refreshed = app is not None or bool(patterns)
            except Exception:
                refreshed = False
                matched = []
        else:
            try:
                _t, _n, _f, _e, found = _scope_fingerprint(host, base)
                matched = [m["id"] for m in found["fingerprints"]
                           if load_pattern_preset(m["id"], base=base)
                           is not None]
            except Exception:
                matched = []
        try:
            has = load_preset(host, scope="host", base=base) is not None
        except Exception:
            has = False
        out.append((host, refreshed, has, matched))
    return out


# ---- plain-language views ------------------------------------------------------

def summary(preset):
    """One-line summary for lists: what this preset holds."""
    parts = []
    eps = preset.get("endpoints") or []
    if eps:
        parts.append(f"{len(eps)} endpoint{'s' if len(eps) != 1 else ''}")
    auth = preset.get("auth") or {}
    headers = auth.get("headers") or []
    if headers:
        parts.append("auth: " + ", ".join(headers))
    mapping = preset.get("asset_mapping") or []
    if mapping:
        parts.append(
            f"{len(mapping)} asset pattern{'s' if len(mapping) != 1 else ''}")
    pack = preset.get("pack") or {}
    if pack.get("scale") is not None:
        parts.append(f"pack scale {pack['scale']}")
    return ", ".join(parts) if parts else "nothing learned yet"


def describe(preset):
    """Plain-language lines describing a preset (for `presets show`).

    Pattern presets get their provenance up top: which apps they
    were learned from, which signals define them, and which code
    they were seen with. The rule sections are the same for both.
    """
    if preset.get("scope") == "pattern":
        lines = [f"Pattern preset: {preset.get('display_name') or preset.get('name')}"
                 f" ({preset.get('name')})"]
    else:
        lines = [f"Preset for {preset.get('name')} ({preset.get('scope')})"]
    learned = preset.get("learned_at", "")
    if learned:
        lines.append(f"Last learned {learned.replace('T', ' ')}")
    if preset.get("scope") == "pattern":
        learned_from = preset.get("learned_from") or []
        if learned_from:
            lines.append(f"Learned from: {', '.join(learned_from)}")
        signals = ((preset.get("fingerprint") or {}).get("signals") or [])
        if signals:
            lines.append("What defines this pattern:")
            for s in signals:
                lines.append(f"  {s}")
        code = preset.get("code_signals") or {}
        if code:
            names = [fingerprints.CODE_SIGNALS.get(sid, {}).get("name", sid)
                     for sid in sorted(code)]
            lines.append(f"Also seen in code: {', '.join(names)}")
    lines.append("")
    auth = preset.get("auth") or {}
    headers = auth.get("headers") or []
    schemes = auth.get("schemes") or []
    cookies = auth.get("cookies") or []
    token_eps = auth.get("token_endpoints") or []
    lines.append("How this app signs in:")
    if not (headers or cookies or token_eps):
        lines.append("  nothing learned yet — no signed-in requests seen")
    for h in headers:
        extra = ""
        if h.lower() == "authorization" and schemes:
            extra = f" ({', '.join(schemes)})"
        lines.append(f"  header: {h}{extra}")
    for c in cookies:
        lines.append(f"  cookie: {c}")
    for t in token_eps:
        lines.append(f"  sign-in endpoint: {t}")
    lines.append("")
    eps = preset.get("endpoints") or []
    lines.append(f"Endpoints ({len(eps)}):")
    if not eps:
        lines.append("  none learned yet")
    for e in eps:
        seen = e.get("seen", 1)
        lines.append(f"  {e['method']} {e['path']} — seen {seen} "
                     f"time{'s' if seen != 1 else ''}")
    lines.append("")
    mapping = preset.get("asset_mapping") or []
    lines.append(f"Asset patterns ({len(mapping)}):")
    if not mapping:
        lines.append("  none learned yet")
    for m in mapping:
        kind = assets.KIND_LABELS.get(m["kind"], m["kind"])
        seen = m.get("seen", 1)
        lines.append(f"  {m['pattern']} — {kind.lower()} "
                     f"(seen {seen} time{'s' if seen != 1 else ''})")
    lines.append("")
    pack = preset.get("pack") or {}
    lines.append("Pack settings:")
    if pack.get("scale") is not None:
        last = pack.get("last_pack", "").replace("T", " ")
        lines.append(f"  model scale: {pack['scale']} cm per unit"
                     + (f" (last pack {last})" if last else ""))
    else:
        lines.append("  no pack built yet — the default scale (100) applies")
    return lines
