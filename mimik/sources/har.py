"""Read captured traffic from a HAR (HTTP Archive) file.

HAR is the standard export from browser devtools (Chrome, Firefox, Safari) and
desktop proxies (Charles, Proxyman). This module turns HAR entries into MIMIK's
flow dicts so the rest of the pipeline (extract, hosts, endpoints, codegen)
works unchanged, with no mitmproxy or phone setup at all.

Ported from littledivy/mimic (MIT) — see LICENSE-NOTICE.
"""
import base64
import binascii
import hashlib
import json
from urllib.parse import urlencode, urlparse

from . import mitm


class HarError(Exception):
    """A HAR file that can't be read or isn't a HAR file. The message is
    plain language, safe to show to the user as-is."""


def parse(path):
    """Parse a HAR file once; returns the parsed dict.

    Raises HarError with a plain message for a missing file, bad JSON,
    or a JSON doc that isn't a HAR file. Callers that need flows AND
    hosts AND endpoints from one file should parse once and pass the
    dict to load()/hosts()/endpoints() instead of re-reading the file.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise HarError(f"no such file: {path}")
    except json.JSONDecodeError as e:
        raise HarError(
            f"{path} is not valid JSON ({e.msg} at line {e.lineno})")
    except OSError as e:
        raise HarError(f"can't read {path}: {e.strerror or e}")
    if not isinstance(data, dict) or not isinstance(data.get("log"), dict):
        raise HarError(f"{path} doesn't look like a HAR file "
                       f"(no 'log' object)")
    return data


def _entries(path_or_data):
    """The entries list from a path or an already-parsed HAR dict."""
    if isinstance(path_or_data, dict):
        return path_or_data.get("log", {}).get("entries", [])
    return _entries(parse(path_or_data))


def _flow_id_prefix(entries):
    """Deterministic id prefix from the entries' content: two different
    HAR files get different flow ids (so importing both keeps both),
    and re-importing the same file reuses the ids (so the store
    dedupes instead of duplicating)."""
    blob = json.dumps(entries, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def _embed(flow, side, raw):
    """Embed raw body bytes in a flow the way store._embed_bodies does:
    UTF-8 text when it decodes, else base64 with a _b64 flag."""
    if not raw:
        return
    try:
        flow[f"_saved_{side}_body"] = raw.decode("utf-8")
    except UnicodeDecodeError:
        flow[f"_saved_{side}_body"] = base64.b64encode(raw).decode("ascii")
        flow[f"_saved_{side}_body_b64"] = True


def load(path_or_data, bodies=True):
    """A HAR file (path or parsed dict) as MIMIK flow dicts.

    Bodies are embedded as _saved_*_body (text, or base64 with the
    _b64 flag) so the flows can go straight into the store and
    endpoints_from_saved() reads them — no second parse needed.
    Pass bodies=False when only hosts/counts are wanted (skips the
    body decoding entirely).
    """
    entries = _entries(path_or_data)
    prefix = _flow_id_prefix(entries)
    return [_entry_to_flow(prefix, i, e, bodies)
            for i, e in enumerate(entries)]


def _entry_to_flow(prefix, index, entry, bodies=True):
    """Convert one HAR entry into a MIMIK flow dict."""
    req = entry.get("request", {})
    resp = entry.get("response", {})
    parsed = urlparse(req.get("url", ""))
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        # A malformed port ("http://host:abc/") — default, don't crash.
        port = 443 if parsed.scheme == "https" else 80
    query = parsed.query
    flow = {
        "id": f"har-{prefix}-{index}",
        "request": {
            "host": parsed.hostname,
            "method": req.get("method"),
            "path": parsed.path + (f"?{query}" if query else ""),
            "scheme": parsed.scheme,
            "port": port,
            "headers": [[h["name"], h["value"]] for h in req.get("headers", [])],
        },
        "response": {
            "status_code": resp.get("status", 0),
        },
    }
    if bodies:
        _embed(flow, "request", _body_bytes(req.get("postData")))
        _embed(flow, "response", _content_bytes(resp.get("content")))
    return flow


def hosts(path_or_data):
    """Count requests per host in a HAR file, most frequent first."""
    return mitm.hosts(load(path_or_data, bodies=False))


def endpoints(path_or_data, host):
    """Distinct (method, path) endpoints for a host, with inline bodies.

    Mirrors mitm.endpoints(), except HAR embeds the request/response bodies in
    each entry, so there's no separate body fetch. Latest capture of each
    endpoint wins, and bodies are decoded/truncated the same way as the mitm
    backend so codegen sees consistent input. Pass the dict from parse()
    to avoid re-reading the file.
    """
    by_key = {}
    for entry in _entries(path_or_data):
        req = entry.get("request", {})
        parsed = urlparse(req.get("url", ""))
        if parsed.hostname != host:
            continue
        by_key[(req.get("method"), parsed.path)] = entry

    out = []
    for (method, path_only), entry in by_key.items():
        req = entry.get("request", {})
        resp = entry.get("response", {})
        out.append(
            {
                "method": method,
                "path": path_only,
                "status": resp.get("status", 0),
                "query": urlparse(req.get("url", "")).query,
                "request_body": mitm._decode(_body_bytes(req.get("postData"))),
                "response_body": mitm._decode(_content_bytes(resp.get("content"))),
            }
        )
    return out


def _body_bytes(post_data):
    """Bytes of a HAR request postData block (text, or url-encoded params)."""
    if not post_data:
        return b""
    text = post_data.get("text")
    if text:
        return text.encode("utf-8")
    params = post_data.get("params")
    if params:
        return urlencode(
            [(p.get("name", ""), p.get("value", "")) for p in params]
        ).encode("utf-8")
    return b""


def _content_bytes(content):
    """Bytes of a HAR response content block, decoding base64 when flagged."""
    if not content:
        return b""
    text = content.get("text", "")
    if not text:
        return b""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text)
        except (binascii.Error, ValueError):
            return text.encode("utf-8")
    return text.encode("utf-8")
