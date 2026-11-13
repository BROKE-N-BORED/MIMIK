"""Replay one captured flow (v0.4 item 10).

Re-send a request exactly as it was captured — same method, URL,
headers (minus the per-request noise extract.DROP names), and body —
and report what the server says this time. Own accounts only: this
replays YOUR session, for YOUR apps.

The GUI's Replay button uses this; the generated clients replay
through their own code (see codegen).
"""

import base64
import time

import requests

from . import extract

BODY_PREVIEW_CHARS = 4000
DEFAULT_TIMEOUT = 30

# Hop-by-hop headers on top of extract.DROP — the HTTP layer sets
# these per connection, never replay them.
DROP_EXTRA = {
    "keep-alive",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "proxy-authorization",
}


def flow_url(flow):
    """The full URL a flow was captured against."""
    req = flow.get("request") or {}
    scheme = req.get("scheme") or "https"
    host = req.get("host") or ""
    port = req.get("port")
    default = ((scheme == "https" and port in (443, None))
               or (scheme == "http" and port in (80, None)))
    port_part = "" if default else f":{port}"
    return f"{scheme}://{host}{port_part}{req.get('path') or '/'}"


def flow_request_body(flow):
    """The captured request body as bytes (b"" when there is none).

    Saved flows carry ``_saved_request_body`` — plain text, or base64
    when the body didn't decode as UTF-8 (see store).
    """
    raw = flow.get("_saved_request_body")
    if raw is None:
        return b""
    if flow.get("_saved_request_body_b64"):
        try:
            return base64.b64decode(raw)
        except (ValueError, TypeError):
            return b""
    if isinstance(raw, str):
        return raw.encode("utf-8", "replace")
    return bytes(raw)


def replay_headers(flow):
    """The captured request headers worth re-sending, as a dict."""
    drop = extract.DROP | DROP_EXTRA
    headers = {}
    for name, value in extract.header_pairs(
            (flow.get("request") or {}).get("headers") or []):
        if name.lower() in drop:
            continue
        headers[name] = value
    return headers


def replay_flow(flow, body=None, timeout=DEFAULT_TIMEOUT):
    """Re-send one captured flow. Returns a result dict, never raises.

    ``body`` overrides the captured request body (the GUI passes the
    endpoint's request_body — live flows don't carry theirs in the
    flow dict). ``None`` means the captured body.
    """
    req = flow.get("request") or {}
    method = (req.get("method") or "GET").upper()
    url = flow_url(flow)
    headers = replay_headers(flow)
    if isinstance(body, str):
        data = body.encode("utf-8", "replace")
    elif body is not None:
        data = bytes(body)
    else:
        data = flow_request_body(flow)
    started = time.monotonic()
    try:
        resp = requests.request(method, url, headers=headers,
                                data=data or None, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "status": None, "reason": "",
                "headers": {}, "body": "", "truncated": False,
                "elapsed": time.monotonic() - started,
                "error": str(e) or e.__class__.__name__}
    text = resp.text
    return {"ok": True, "status": resp.status_code, "reason": resp.reason,
            "headers": dict(resp.headers),
            "body": text[:BODY_PREVIEW_CHARS],
            "truncated": len(text) > BODY_PREVIEW_CHARS,
            "elapsed": time.monotonic() - started, "error": None}
