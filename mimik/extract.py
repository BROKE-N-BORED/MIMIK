"""Pull the reusable part of your captured session out of mitm flows.

The whole trick behind MIMIK: an app authenticates each request with a bundle
of headers (or cookies) — a bearer token, device ids, a session id. Those are
stable across calls. Capture them once from a real request you made, and you
can craft new calls that the server can't tell apart from your real client.

This module finds the newest authenticated request to a host and keeps only the
headers worth reusing.

Ported from littledivy/mimic (MIT) — see LICENSE-NOTICE.
"""

# Header names that carry identity/auth/device context worth replaying.
# Anything matching these prefixes or exact names is kept.
KEEP_EXACT = {
    "authorization",
    "cookie",
    "user-agent",
    "accept-language",
    "content-type",
    "accept",
}
KEEP_PREFIX = ("x-",)  # x-device-id, x-session-id, x-app-version, x-api-key, …

# Per-request noise that must NOT be replayed — the HTTP layer sets these.
DROP = {
    "content-length",
    "host",
    "connection",
    "accept-encoding",
    "content-encoding",
    "priority",
    "x-forwarded-for",
    # Request-scoped x- headers (item 32e): unique per request, so
    # replaying them would send a stale id/trace — and the server
    # may reject or misattribute the call. KEEP_PREFIX still keeps
    # the identity-ish x- headers (x-device-id, x-session-id,
    # x-api-key, x-app-version).
    "x-request-id",
    "x-correlation-id",
    "x-trace-id",
    "x-span-id",
    "x-parent-span-id",
    "x-amzn-trace-id",
    "x-b3-traceid",
    "x-b3-spanid",
    "x-b3-parentspanid",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-real-ip",
}


def header_pairs(headers):
    """A header list -> [(name, value)].

    mitmweb gives [[name, value], ...]; HAR gives [{"name": ...,
    "value": ...}]. Both normalize here — this is the one header
    normalizer (item 32c); presets, fingerprints, and assets all
    call it instead of carrying their own copy. Malformed entries
    are skipped; missing values default to "".
    """
    out = []
    for pair in headers or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            out.append((str(pair[0]), pair[1] or ""))
        elif isinstance(pair, dict) and "name" in pair:
            out.append((str(pair["name"]), pair.get("value") or ""))
    return out


def header_dict(headers, lower=True):
    """header_pairs, as a dict (later pairs win on duplicate names).
    Names are lowercased by default — header names are
    case-insensitive, and every caller looks them up lowercase."""
    if lower:
        return {name.lower(): value
                for name, value in header_pairs(headers)}
    return dict(header_pairs(headers))


def flow_header_pairs(flow, side):
    """A flow's request/response headers as [(name, value)] — the
    flow+side form presets and fingerprints need."""
    part = flow.get(side) or {}
    return header_pairs(part.get("headers"))


def _keep(name):
    n = name.lower()
    if n in DROP:
        return False
    return n in KEEP_EXACT or n.startswith(KEEP_PREFIX)


def session_headers(flows, host):
    """Reusable auth/device headers from your newest authed request to `host`.

    "Authed" = has an Authorization or Cookie header. Returns {} if none found.
    """
    for f in reversed(flows):
        req = f.get("request")
        if not req or req["host"] != host:
            continue
        pairs = header_pairs(req.get("headers"))
        names = {k.lower() for k, _v in pairs}
        if not ({"authorization", "cookie"} & names):
            continue
        return {k: v for k, v in pairs if _keep(k)}
    return {}


def base_url(flows, host):
    """Reconstruct the scheme://host base from a captured request."""
    for f in flows:
        req = f.get("request")
        if req and req["host"] == host:
            scheme = req.get("scheme", "https")
            port = req.get("port")
            netloc = host
            if port and port not in (80, 443):
                netloc = f"{host}:{port}"
            return f"{scheme}://{netloc}"
    return f"https://{host}"
