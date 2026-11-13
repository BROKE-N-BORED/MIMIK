"""Compare two captures (v0.4 item 12).

New/removed endpoints, changed responses. A "capture" here is a flow
list — a host's saved flows, a capture save file, or a HAR file (see
load_source). Endpoints are identified the way codegen identifies
them: method + generalized path, so /users/123 and /users/456 are the
same endpoint and a new id doesn't look like a new endpoint.

A response counts as changed when its status differs, or when its
JSON body's top-level keys differ (fields added/removed). Bodies are
compared by shape, not text — a changed timestamp or id inside the
same shape is not a change. Flows without a saved response body
compare by status only.
"""

import base64
import json
import os

from . import codegen, store
from .sources import har


class DiffError(Exception):
    """A diff source couldn't be loaded (unknown host, bad file)."""


def _response_body(flow):
    """The saved response body as text ("" when there is none)."""
    raw = flow.get("_saved_response_body")
    if raw is None:
        return ""
    if flow.get("_saved_response_body_b64"):
        try:
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return ""
    return raw if isinstance(raw, str) else ""


def _response_shape(flow):
    """(status, top-level JSON keys or None) for one flow's response."""
    resp = flow.get("response") or {}
    status = resp.get("status_code")
    body = _response_body(flow)
    if not body:
        return status, None
    try:
        parsed = json.loads(body)
    except ValueError:
        return status, None
    if isinstance(parsed, dict):
        return status, frozenset(parsed.keys())
    if isinstance(parsed, list):
        return status, frozenset({"<list>"})
    return status, None


def _endpoint_map(flows):
    """{(method, generalized path): (latest flow, sample count)}."""
    by_key = {}
    counts = {}
    for f in flows:
        req = f.get("request") or {}
        path = (req.get("path") or "/").split("?")[0]
        key = ((req.get("method") or "GET").upper(),
               codegen.generalize_path(path))
        by_key[key] = f  # latest capture of the endpoint wins
        counts[key] = counts.get(key, 0) + 1
    return {key: (flow, counts[key]) for key, flow in by_key.items()}


def _entry(key, flow, samples):
    resp = flow.get("response") or {}
    return {"method": key[0], "path": key[1],
            "status": resp.get("status_code"), "samples": samples}


def diff_captures(flows_a, flows_b):
    """Compare two flow lists. Returns a result dict.

    "new": endpoints only in B; "removed": only in A; "changed":
    in both, with a different status or response shape; "unchanged":
    count of endpoints in both that didn't change.
    """
    map_a = _endpoint_map(flows_a)
    map_b = _endpoint_map(flows_b)
    keys_a, keys_b = set(map_a), set(map_b)

    new = [_entry(k, *map_b[k]) for k in sorted(keys_b - keys_a)]
    removed = [_entry(k, *map_a[k]) for k in sorted(keys_a - keys_b)]
    changed = []
    for key in sorted(keys_a & keys_b):
        flow_a, _n = map_a[key]
        flow_b, samples_b = map_b[key]
        status_a, keys_a_shape = _response_shape(flow_a)
        status_b, keys_b_shape = _response_shape(flow_b)
        entry = None
        if status_a != status_b:
            entry = _entry(key, flow_b, samples_b)
            entry["old_status"] = status_a
            entry["new_status"] = status_b
        if (keys_a_shape is not None and keys_b_shape is not None
                and keys_a_shape != keys_b_shape):
            if entry is None:
                entry = _entry(key, flow_b, samples_b)
            entry["added_keys"] = sorted(keys_b_shape - keys_a_shape)
            entry["removed_keys"] = sorted(keys_a_shape - keys_b_shape)
        if entry is not None:
            changed.append(entry)
    return {"new": new, "removed": removed, "changed": changed,
            "unchanged": len(keys_a & keys_b) - len(changed),
            "total_a": len(map_a), "total_b": len(map_b)}


def format_diff(result, name_a, name_b):
    """The plain-text report — the CLI prints it, the GUI shows it."""
    lines = [f"Capture diff: {name_a} -> {name_b}", "",
             f"{result['total_a']} endpoints -> {result['total_b']} endpoints",
             f"{len(result['new'])} new, {len(result['removed'])} removed, "
             f"{len(result['changed'])} changed, "
             f"{result['unchanged']} unchanged"]
    if result["new"]:
        lines += ["", "--- New ---"]
        for e in result["new"]:
            lines.append(f"  {e['method']} {e['path']} "
                         f"({e['status']}, {e['samples']} sample"
                         f"{'s' if e['samples'] != 1 else ''})")
    if result["removed"]:
        lines += ["", "--- Removed ---"]
        for e in result["removed"]:
            lines.append(f"  {e['method']} {e['path']} "
                         f"({e['status']}, {e['samples']} sample"
                         f"{'s' if e['samples'] != 1 else ''})")
    if result["changed"]:
        lines += ["", "--- Changed ---"]
        for e in result["changed"]:
            bits = []
            if "old_status" in e:
                bits.append(f"status {e['old_status']} -> "
                            f"{e['new_status']}")
            if e.get("added_keys") or e.get("removed_keys"):
                keys = " ".join(f"+{k}" for k in e.get("added_keys", []))
                keys += "".join(f" -{k}" for k in e.get("removed_keys", []))
                bits.append(f"response keys {keys.strip()}")
            lines.append(f"  {e['method']} {e['path']}: "
                         + "; ".join(bits))
    if not result["new"] and not result["removed"] \
            and not result["changed"]:
        lines += ["", "No differences."]
    return "\n".join(lines) + "\n"


def load_source(arg):
    """(flows, label) — arg is a host name, a .json capture save file,
    or a .har file. Raises DiffError when nothing loads."""
    if os.path.isfile(arg):
        if arg.lower().endswith(".har"):
            return har.load(arg), arg
        flows = store.load_capture_file(arg)
        if flows is None:
            raise DiffError(f"can't read capture file: {arg}")
        return flows, arg
    flows = store.load_host(arg)
    if not flows:
        raise DiffError(f"no captures for host: {arg}")
    return flows, arg
