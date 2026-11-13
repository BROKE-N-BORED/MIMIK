"""Traffic fingerprints — recognizing the same process, algorithm, or code
across different apps.

Joshua's direction (2026-11-12): presets must match NOT JUST the same app,
but the same process, algorithm, or code when MIMIK sees it elsewhere. A
fingerprint is a small set of traffic-derived signals with a stable id and
a plain-language name. When `mimik presets learn` finds a fingerprint in
an app's captures, it saves a PATTERN preset for it (see mimik.presets);
when another app shows the same fingerprint, that pattern preset applies
there too — the rules travel with the shape, not the app name.

Matching is CONSERVATIVE on purpose: a fingerprint needs at least TWO
independent signals, or one unambiguous signal. One stray header never
claims a pattern — false positives are the failure mode, and the tests
pin the negative cases (similar-but-different traffic must NOT match).

Every fingerprint below documents exactly what signals trigger it.
Nothing here stores a secret value: fingerprints record names, shapes,
and counts only. A JWT is recognized by its 3-segment base64url SHAPE —
the token itself is never kept. Grant types are recorded only when they
are known protocol constants (authorization_code, client_credentials,
refresh_token, password, device_code); anything else is ignored.
"""
import base64
import binascii
import json
import os
import re
from urllib.parse import parse_qsl, urlparse

from . import assets, codegen, extract, launch, log, paths, store

# ---- the catalog ---------------------------------------------------------------
# id -> {name, kind, about}. "kind" groups fingerprints for display:
# auth (how the app signs in), request (how it shapes its calls),
# framework (what the server says it runs), assets (where its files live).

FINGERPRINTS = {
    "oauth2-code": {
        "name": "OAuth2 sign-in (authorization code)",
        "kind": "auth",
        "about": "grant_type=authorization_code in a token request "
                 "(unambiguous), or an authorize endpoint plus a token "
                 "endpoint both seen",
    },
    "oauth2-client-credentials": {
        "name": "OAuth2 machine sign-in (client credentials)",
        "kind": "auth",
        "about": "grant_type=client_credentials in a token request "
                 "(unambiguous)",
    },
    "jwt-bearer": {
        "name": "JWT bearer tokens",
        "kind": "auth",
        "about": "Bearer token with the 3-segment JWT shape on at least "
                 "2 endpoints, or on 1 endpoint plus a token endpoint "
                 "that returned a JWT access_token",
    },
    "basic-auth": {
        "name": "HTTP Basic sign-in",
        "kind": "auth",
        "about": "Authorization: Basic on at least 2 endpoints, or on "
                 "1 endpoint plus a 401 that asks for Basic",
    },
    "api-key-header": {
        "name": "API key header",
        "kind": "auth",
        "about": "an X-Api-Key-style header on at least 2 endpoints — "
                 "one lone key header never claims this alone",
    },
    "session-cookie": {
        "name": "Session cookie sign-in",
        "kind": "auth",
        "about": "the server set a cookie with HttpOnly/Secure/SameSite "
                 "and a later request sent the same cookie back",
    },
    "json-rpc": {
        "name": "JSON-RPC calls",
        "kind": "request",
        "about": 'a request body with "jsonrpc" and "method" keys '
                 "(unambiguous)",
    },
    "graphql": {
        "name": "GraphQL",
        "kind": "request",
        "about": 'a /graphql path, or a request body with a "query" key '
                 "holding GraphQL syntax (unambiguous)",
    },
    "cursor-pagination": {
        "name": "Cursor pagination",
        "kind": "request",
        "about": "the same cursor parameter (cursor/after/page_token/"
                 "end_cursor/next_cursor/starting_after) on the same "
                 "endpoint with at least 2 different values",
    },
    "offset-pagination": {
        "name": "Offset pagination",
        "kind": "request",
        "about": "an offset+limit or page+per_page (or page_size/limit) "
                 "pair on the same endpoint across at least 2 requests "
                 "with a changing value",
    },
    "express": {
        "name": "Express web framework",
        "kind": "framework",
        "about": 'X-Powered-By response header naming "Express" '
                 "(unambiguous — the server declares it)",
    },
    "aspnet": {
        "name": "ASP.NET web framework",
        "kind": "framework",
        "about": 'X-Powered-By response header naming "ASP.NET" '
                 "(unambiguous — the server declares it)",
    },
    "php": {
        "name": "PHP backend",
        "kind": "framework",
        "about": 'X-Powered-By response header naming "PHP" '
                 "(unambiguous — the server declares it)",
    },
    "stripe-api": {
        "name": "Stripe API",
        "kind": "framework",
        "about": "requests to api.stripe.com, or a Stripe-Version "
                 "response header (unambiguous)",
    },
    "cdn-assets": {
        "name": "CDN-hosted assets",
        "kind": "assets",
        "about": "at least 2 captured assets served from CDN-shaped "
                 "URLs (a /cdn/, /assets/, /static/ or /media/ path, "
                 "or a cdn/static/assets host)",
    },
}

# Only these grant_type values are ever recorded — they are protocol
# constants, not secrets. Anything else in a grant_type field is ignored.
_KNOWN_GRANT_TYPES = {
    "authorization_code", "client_credentials", "refresh_token",
    "password", "urn:ietf:params:oauth:grant-type:device_code",
}

# Header names that carry an API key (lowercase match).
_KEY_HEADER_NAMES = {"x-api-key", "apikey", "api-key", "api_key"}

# Query parameter names that carry a pagination cursor.
_CURSOR_PARAMS = {"cursor", "after", "page_token", "end_cursor",
                  "next_cursor", "starting_after"}

# (first, second) query parameter pairs for offset pagination.
_OFFSET_PAIRS = (("offset", "limit"), ("page", "per_page"),
                 ("page", "page_size"), ("page", "limit"))

# X-Powered-By markers (lowercase substring) -> fingerprint id.
_POWERED_BY = {"express": "express", "asp.net": "aspnet", "php": "php"}

# URL shapes that mark an asset as CDN-hosted.
_CDN_PATH_PREFIXES = ("cdn", "assets", "static", "media")
_CDN_HOST_LABELS = {"cdn", "static", "assets", "media"}

# Well-known library/engine files, by NAME only. These are weak signals:
# they can corroborate a traffic fingerprint, but they NEVER create one
# alone — a scanned program tree with no traffic matches nothing.
CODE_SIGNALS = {
    "unity-engine": {
        "name": "Unity engine",
        "filenames": {"unityplayer.dll", "unityplayer.so", "libunity.so",
                      "unityengine.dll"},
    },
    "electron-app": {
        "name": "Electron app",
        "filenames": {"app.asar", "electron", "electron.exe"},
    },
    "godot-engine": {
        "name": "Godot engine",
        "filenames": {"libgodot.so", "godot", "godot.exe"},
    },
    "cef-app": {
        "name": "Chromium Embedded Framework",
        "filenames": {"libcef.so", "libcef.dll", "cef.pak"},
    },
}

_FILENAME_TO_SIGNAL = {
    fn: sid
    for sid, spec in CODE_SIGNALS.items()
    for fn in spec["filenames"]
}

# Bodies bigger than this are never parsed as JSON (asset bodies are
# base64 and huge; a JSON-RPC/GraphQL body is small).
_MAX_BODY_PARSE = 1024 * 1024

_B64URL_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_B64_SHAPE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_MAX_ENDPOINTS_PER_FINGERPRINT = 20


# ---- small helpers ---------------------------------------------------------------

def _is_jwt_shape(value):
    """True when a token has the 3-segment JWT shape (shape only — the
    token itself is never decoded or kept)."""
    parts = (value or "").split(".")
    if len(parts) != 3:
        return False
    if not all(_B64URL_SEGMENT.match(p) for p in parts):
        return False
    return len(parts[0]) >= 10 and len(parts[2]) >= 10 and len(value) >= 30


def _is_base64_shape(value):
    """True when a value looks like base64 (Basic auth credentials)."""
    value = value or ""
    return (len(value) >= 8 and len(value) % 4 == 0
            and bool(_B64_SHAPE.match(value)))


def is_key_header(name):
    """True when a header name is an API-key header (X-Api-Key style)."""
    n = (name or "").lower()
    return (n in _KEY_HEADER_NAMES or "api-key" in n or "apikey" in n
            or "api_key" in n or n.endswith("-key") or n.endswith("_key"))


def _body_text(flow, side):
    """A saved flow's request/response body as text, or "".

    Bodies are stored as UTF-8 text when they decode, else base64
    with a _saved_<side>_body_b64 flag (see mimik.store). Anything
    undecodable or missing is "" — fingerprinting never raises.
    """
    raw = flow.get(f"_saved_{side}_body") or ""
    if not raw:
        return ""
    if flow.get(f"_saved_{side}_body_b64"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return ""
    return raw if isinstance(raw, str) else ""


def _json_from_text(text):
    """A body text parsed as a JSON dict, or None."""
    if len(text) > _MAX_BODY_PARSE:
        return None
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _json_body(flow, side):
    """A saved body parsed as a JSON dict, or None."""
    return _json_from_text(_body_text(flow, side))


_UNSET = object()


def _grant_types(flow, req_text=_UNSET, req_body=_UNSET):
    """Known grant_type values in a request body (form or JSON).

    req_text / req_body let the caller share work it already did —
    observe() extracts each body once and passes the results in, so a
    flow's JSON is parsed once per side, not 3–4 times (review #21).
    """
    text = _body_text(flow, "request") if req_text is _UNSET else req_text
    found = set()
    if "grant_type" in text:
        if len(text) <= _MAX_BODY_PARSE:
            for key, value in parse_qsl(text, keep_blank_values=True):
                if key == "grant_type" and value in _KNOWN_GRANT_TYPES:
                    found.add(value)
        body = (_json_body(flow, "request") if req_body is _UNSET
                else req_body)
        if body is not None:
            value = body.get("grant_type")
            if value in _KNOWN_GRANT_TYPES:
                found.add(value)
    return found


def _generalized_endpoint(flow):
    req = flow.get("request") or {}
    return ((req.get("method") or "").upper(),
            paths.generalize_path(req.get("path") or ""))


def _query_params(path):
    """{param: [values]} for a request path's query string."""
    out = {}
    for key, value in parse_qsl(urlparse(path or "").query,
                                keep_blank_values=True):
        out.setdefault(key, []).append(value)
    return out


def _cookie_names(value):
    names = []
    for part in (value or "").split(";"):
        name = part.split("=", 1)[0].strip()
        if _COOKIE_NAME_RE.match(name):
            names.append(name)
    return names


def _set_cookie_parts(value):
    """(name, has_auth_attr) for a Set-Cookie header value."""
    parts = (value or "").split(";")
    name = parts[0].split("=", 1)[0].strip() if parts else ""
    if not _COOKIE_NAME_RE.match(name):
        return None, False
    attrs = {p.strip().split("=", 1)[0].lower() for p in parts[1:]}
    has_attr = bool(attrs & {"httponly", "secure", "samesite"})
    return name, has_attr


def _is_token_path(path):
    """True when a path looks like an OAuth/token endpoint."""
    segs = [s.lower() for s in (path or "").split("?")[0].split("/")
            if s]
    if not segs:
        return False
    if segs[-1] == "token":
        return True
    return "oauth" in segs and "token" in segs


def _asset_pattern(url):
    """A URL generalized to a pattern: /cdn/m/crate.glb -> /cdn/m/*.glb."""
    parsed = urlparse(url or "")
    path = parsed.path or ""
    ext = os.path.splitext(path)[1].lower()
    if not ext:
        return None
    return f"{os.path.dirname(path)}/*{ext}"


def _is_cdn_url(url):
    parsed = urlparse(url or "")
    labels = (parsed.hostname or "").lower().split(".")
    if any(label in _CDN_HOST_LABELS or "cdn" in label
           for label in labels):
        return True
    first = (parsed.path or "").strip("/").split("/", 1)[0].lower()
    return first in _CDN_PATH_PREFIXES


def is_cdn_pattern(pattern):
    """True when an asset pattern (e.g. /cdn/m/*.glb) is CDN-shaped."""
    first = (pattern or "").strip("/").split("/", 1)[0].lower()
    return first in _CDN_PATH_PREFIXES or "cdn" in (pattern or "").lower()


# ---- code signals (program files, by name only) ----------------------------------

def code_signal_for_name(name):
    """The code signal a filename evidences, or None."""
    return _FILENAME_TO_SIGNAL.get((name or "").lower())


def code_signals_for_names(names):
    """{signal_id: [filenames seen]} for a list of basenames."""
    out = {}
    for name in names:
        sid = code_signal_for_name(name)
        if sid:
            out.setdefault(sid, [])
            if name not in out[sid]:
                out[sid].append(name)
    return out


# ---- flow fingerprinting ---------------------------------------------------------

class _Evidence:
    """One pass over a flow list, collecting what each fingerprint needs."""

    def __init__(self):
        self.jwt_endpoints = set()
        self.jwt_access_token = False
        self.basic_endpoints = set()
        self.basic_401 = False
        self.key_headers = {}  # lower name -> (orig name, endpoints)
        self.set_cookies = []  # (index, name, has_auth_attr)
        self.sent_cookies = []  # (index, name, endpoint)
        self.grant_types = set()
        self.authorize_seen = False
        self.token_endpoints = set()
        self.auth_seen = False
        self.bearer_seen = False
        self.jsonrpc = False
        self.jsonrpc_endpoints = set()
        self.graphql = False
        self.graphql_endpoints = set()
        self.cursor_values = {}  # (endpoint, param) -> set(values)
        self.offset_values = {}  # endpoint -> {param: set(values)}
        self.powered_by = set()
        self.stripe = False

    def observe(self, index, flow):
        req = flow.get("request") or {}
        resp = flow.get("response") or {}
        endpoint = _generalized_endpoint(flow)
        path = req.get("path") or ""
        path_only = path.split("?", 1)[0]
        host = (req.get("host") or "").lower()

        if host == "api.stripe.com":
            self.stripe = True

        for name, value in extract.flow_header_pairs(flow, "request"):
            lname = name.lower()
            if lname == "authorization":
                self.auth_seen = True
                parts = str(value).split(None, 1)
                scheme = parts[0].lower() if parts else ""
                token = parts[1] if len(parts) == 2 else ""
                if scheme == "bearer":
                    self.bearer_seen = True
                    if _is_jwt_shape(token):
                        self.jwt_endpoints.add(endpoint)
                elif scheme == "basic" and _is_base64_shape(token):
                    self.basic_endpoints.add(endpoint)
            elif lname == "cookie":
                for cname in _cookie_names(str(value)):
                    self.sent_cookies.append((index, cname, endpoint))
            elif is_key_header(name):
                _orig, eps = self.key_headers.setdefault(
                    lname, (name, set()))
                eps.add(endpoint)
        for name, value in extract.flow_header_pairs(flow, "response"):
            lname = name.lower()
            if lname == "set-cookie":
                cname, has_attr = _set_cookie_parts(str(value))
                if cname:
                    self.set_cookies.append((index, cname, has_attr))
            elif lname == "x-powered-by":
                marker = str(value).lower()
                for key, fid in _POWERED_BY.items():
                    if key in marker:
                        self.powered_by.add(fid)
            elif lname == "stripe-version":
                self.stripe = True
            elif lname == "www-authenticate":
                if (resp.get("status_code") == 401
                        and str(value).strip().lower().startswith("basic")):
                    self.basic_401 = True

        # Each body is extracted + parsed once per flow and shared
        # between the grant-type, JSON-RPC, and GraphQL checks
        # (review #21 — this used to re-parse 3–4 times).
        req_text = _body_text(flow, "request")
        req_body = _json_from_text(req_text)
        self.grant_types |= _grant_types(flow, req_text, req_body)
        params = _query_params(path)
        if ("authorize" in path_only.lower()
                and params.get("response_type") == ["code"]):
            self.authorize_seen = True
        if _is_token_path(path_only):
            self.token_endpoints.add(endpoint)

        if req_body is not None:
            if (str(req_body.get("jsonrpc", "")).startswith("2.0")
                    and isinstance(req_body.get("method"), str)):
                self.jsonrpc = True
                self.jsonrpc_endpoints.add(endpoint)
            query = req_body.get("query")
            if isinstance(query, str):
                stripped = query.strip().lower()
                if stripped.startswith(
                        ("query", "mutation", "subscription",
                         "fragment", "{")):
                    self.graphql = True
                    self.graphql_endpoints.add(endpoint)
        if path_only.rstrip("/").lower().endswith("/graphql"):
            self.graphql = True
            self.graphql_endpoints.add(endpoint)
        resp_body = _json_body(flow, "response")
        if resp_body is not None:
            token = resp_body.get("access_token")
            if isinstance(token, str) and _is_jwt_shape(token):
                self.jwt_access_token = True

        for param in _CURSOR_PARAMS:
            if param in params:
                key = (endpoint, param)
                self.cursor_values.setdefault(key, set()).update(
                    params[param])
        present = {p: set(v) for p, v in params.items()}
        if present:
            bucket = self.offset_values.setdefault(endpoint, {})
            for param, values in present.items():
                bucket.setdefault(param, set()).update(values)

    # -- decision rules (each documents its own threshold) --

    def matches(self):
        out = []

        def add(fid, signals, auth=None, endpoints=()):
            spec = FINGERPRINTS[fid]
            out.append({
                "id": fid,
                "name": spec["name"],
                "kind": spec["kind"],
                "signals": signals,
                "auth": auth or {"headers": [], "schemes": [],
                                 "cookies": [], "token_endpoints": []},
                "endpoints": [{"method": m, "path": p}
                              for m, p in sorted(endpoints)
                              ][:_MAX_ENDPOINTS_PER_FINGERPRINT],
            })

        token_shapes = sorted(
            f"{m} {p}" for m, p in self.token_endpoints)
        bearer_auth = {
            "headers": ["Authorization"] if self.auth_seen else [],
            "schemes": ["Bearer"] if self.bearer_seen else [],
            "cookies": [],
            "token_endpoints": token_shapes,
        }
        # OAuth2, authorization code: the grant_type is unambiguous;
        # otherwise an authorize endpoint AND a token endpoint.
        if "authorization_code" in self.grant_types:
            add("oauth2-code",
                ["sign-in used grant_type=authorization_code"],
                auth=bearer_auth, endpoints=self.token_endpoints)
        elif self.authorize_seen and self.token_endpoints:
            add("oauth2-code",
                ["authorize endpoint plus token endpoint seen"],
                auth=bearer_auth, endpoints=self.token_endpoints)
        # OAuth2, client credentials: the grant_type is unambiguous.
        if "client_credentials" in self.grant_types:
            add("oauth2-client-credentials",
                ["sign-in used grant_type=client_credentials"],
                auth=bearer_auth, endpoints=self.token_endpoints)
        # JWT bearer: JWT shape on 2+ endpoints, or 1 endpoint plus a
        # token endpoint that returned a JWT access_token.
        if len(self.jwt_endpoints) >= 2:
            add("jwt-bearer",
                [f"Bearer token with JWT shape on "
                 f"{len(self.jwt_endpoints)} endpoints"],
                auth=bearer_auth, endpoints=self.jwt_endpoints)
        elif self.jwt_endpoints and self.jwt_access_token:
            add("jwt-bearer",
                ["Bearer token with JWT shape, and the token endpoint "
                 "returned a JWT access_token"],
                auth=bearer_auth, endpoints=self.jwt_endpoints)
        # Basic: 2+ endpoints, or 1 endpoint plus a 401 asking for it.
        if len(self.basic_endpoints) >= 2:
            add("basic-auth",
                [f"Authorization: Basic on "
                 f"{len(self.basic_endpoints)} endpoints"],
                auth={"headers": ["Authorization"], "schemes": ["Basic"],
                      "cookies": [], "token_endpoints": []},
                endpoints=self.basic_endpoints)
        elif self.basic_endpoints and self.basic_401:
            add("basic-auth",
                ["Authorization: Basic, and the server asked for Basic "
                 "with a 401"],
                auth={"headers": ["Authorization"], "schemes": ["Basic"],
                      "cookies": [], "token_endpoints": []},
                endpoints=self.basic_endpoints)
        # API key header: the same key header on 2+ endpoints. One
        # lone key header never claims this alone.
        for _lname, (orig, eps) in sorted(self.key_headers.items()):
            if len(eps) >= 2:
                add("api-key-header",
                    [f"{orig} header on {len(eps)} endpoints"],
                    auth={"headers": [orig], "schemes": [],
                          "cookies": [], "token_endpoints": []},
                    endpoints=eps)
                break
        # Session cookie: the server set a cookie (with an auth-ish
        # attribute) and a LATER request sent the same cookie back.
        session_names = set()
        session_endpoints = set()
        for set_idx, name, has_attr in self.set_cookies:
            if not has_attr:
                continue
            for send_idx, sent_name, endpoint in self.sent_cookies:
                if sent_name == name and send_idx > set_idx:
                    session_names.add(name)
                    session_endpoints.add(endpoint)
        if session_names:
            add("session-cookie",
                [f"server set cookie {name} and later requests sent it"
                 for name in sorted(session_names)],
                auth={"headers": [], "schemes": [],
                      "cookies": sorted(session_names),
                      "token_endpoints": []},
                endpoints=session_endpoints)
        # JSON-RPC: one body with jsonrpc + method is unambiguous.
        if self.jsonrpc:
            add("json-rpc",
                ['request bodies with "jsonrpc" and "method" keys'],
                endpoints=self.jsonrpc_endpoints)
        # GraphQL: a /graphql path, or a body with a GraphQL query.
        if self.graphql:
            add("graphql",
                ["GraphQL path or query bodies seen"],
                endpoints=self.graphql_endpoints)
        # Cursor pagination: one cursor param, one endpoint, 2+ values.
        for (endpoint, param), values in sorted(self.cursor_values.items()):
            if len(values) >= 2:
                add("cursor-pagination",
                    [f"{param} cursor on {endpoint[0]} {endpoint[1]} "
                     f"with {len(values)} different values"],
                    endpoints={endpoint})
                break
        # Offset pagination: a pair on one endpoint, values changing.
        for endpoint, params in sorted(self.offset_values.items()):
            matched_pair = None
            for first, second in _OFFSET_PAIRS:
                if first in params and second in params:
                    if (len(params[first]) >= 2
                            or len(params[second]) >= 2):
                        matched_pair = (first, second)
                        break
            if matched_pair:
                add("offset-pagination",
                    [f"{matched_pair[0]}+{matched_pair[1]} on "
                     f"{endpoint[0]} {endpoint[1]} with changing values"],
                    endpoints={endpoint})
                break
        # Frameworks: the server declares itself in X-Powered-By.
        for fid in sorted(self.powered_by):
            add(fid, [f"X-Powered-By names {FINGERPRINTS[fid]['name']}"])
        # Stripe: its host, or its version header.
        if self.stripe:
            add("stripe-api", ["talking to the Stripe API"])
        return out


def fingerprint_flows(flows):
    """[{id, name, kind, signals, auth, endpoints}] for the fingerprints
    these flows exhibit, in catalog order. Pure function — no disk.

    "auth" is the fingerprint-relevant auth only (names and shapes):
    e.g. the jwt-bearer match carries Authorization + Bearer, never
    the other headers the app happened to use. "endpoints" are the
    generalized shapes that evidenced the fingerprint.
    """
    ev = _Evidence()
    for i, flow in enumerate(flows or []):
        try:
            ev.observe(i, flow)
        except Exception as e:
            # One malformed flow never breaks fingerprinting — but the
            # error is logged, not dropped (item 23).
            log.log_error("fingerprints observe", e)
            continue
    matches = ev.matches()
    order = list(FINGERPRINTS)
    matches.sort(key=lambda m: order.index(m["id"]))
    return matches


def fingerprint_entries(entries):
    """The cdn-assets fingerprint for captured asset entries, or [].

    Needs at least 2 assets served from CDN-shaped URLs — one lone
    asset under /cdn/ is not a pattern.
    """
    patterns = {}
    for e in entries or []:
        url = e.get("url")
        if not url or not _is_cdn_url(url):
            continue
        pattern = _asset_pattern(url)
        if pattern is None:
            continue
        patterns[pattern] = patterns.get(pattern, 0) + 1
    total = sum(patterns.values())
    if total < 2:
        return []
    example = sorted(patterns.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    spec = FINGERPRINTS["cdn-assets"]
    return [{
        "id": "cdn-assets",
        "name": spec["name"],
        "kind": spec["kind"],
        "signals": [f"{total} assets served from CDN-shaped paths "
                    f"(e.g. {example})"],
        "auth": {"headers": [], "schemes": [], "cookies": [],
                 "token_endpoints": []},
        "endpoints": [],
    }]


# ---- scoping (a host or program group, from the store) ---------------------------

def _program_for_host(host, base):
    """The program a host's traffic came from, or None.

    A program run from a file stores its path in the capture meta
    ({"launched_file": ...}); the program name is derived exactly
    the way the record command derives it, so a host's traffic links
    back to its program scan — and its code signals.
    """
    meta = store.capture_meta(host, base)
    launched = meta.get("launched_file")
    if not launched:
        return None
    return os.path.basename(launch.run_dir_for(launched))


def _code_signals_for_group(group_name, base):
    """Merged code signals from a program group's scan manifests."""
    out = {}
    for _gdir, manifest in assets.list_asset_groups(base):
        if (manifest.get("group") != group_name
                and manifest.get("program") != group_name):
            continue
        for sid, files in (manifest.get("code_signals") or {}).items():
            merged = out.setdefault(sid, [])
            for name in files:
                if name not in merged:
                    merged.append(name)
    return out


def _combine(flows, entries, code_signals):
    """{"fingerprints": [...], "code_signals": {...}} for loaded data —
    one fingerprint pass, in catalog order."""
    found = fingerprint_flows(flows) + fingerprint_entries(entries)
    order = list(FINGERPRINTS)
    found.sort(key=lambda m: order.index(m["id"]))
    return {"fingerprints": found, "code_signals": code_signals}


def fingerprint_loaded(scope_type, scope_name, flows, entries, base=None):
    """{"fingerprints": [...], "code_signals": {...}} for scope data the
    caller already resolved and loaded — the same result as
    fingerprint_scope without re-resolving the scope or re-loading
    the flows (review #21: learn + match share one pass)."""
    base = base or store.data_dir()
    code_signals = {}
    if scope_type == "host":
        program = _program_for_host(scope_name, base)
        if program:
            code_signals = _code_signals_for_group(program, base)
    else:
        code_signals = _code_signals_for_group(scope_name, base)
    return _combine(flows, entries, code_signals)


def fingerprint_scope(name, base=None):
    """{"fingerprints": [...], "code_signals": {...}} for a host or
    program group name — the flows and assets the store holds for it,
    plus the code signals from its program scan (if any).

    Code signals never create a fingerprint on their own; they ride
    along so a match can say what code it was seen with.
    """
    base = base or store.data_dir()
    scope = assets.resolve_scope(name, base)
    flows, entries, code_signals = [], [], {}
    if "host" in scope:
        flows = store.load_host(name, base)
        entries = assets.list_assets(base, host=name)
        program = _program_for_host(name, base)
        if program:
            code_signals = _code_signals_for_group(program, base)
    elif "group_name" in scope:
        entries = assets.list_assets(base, group_name=name)
        code_signals = _code_signals_for_group(name, base)
    elif "group" in scope:
        entries = assets.list_assets(base, group=name)
        for gdir, manifest in assets.list_asset_groups(base):
            if os.path.basename(gdir) == name:
                gname = manifest.get("group") or manifest.get("program")
                if gname:
                    code_signals = _code_signals_for_group(gname, base)
                break
    return _combine(flows, entries, code_signals)
