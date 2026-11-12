"""Turn captured endpoints into an ergonomic Python client.

MIMIK's key change from upstream: generation is DETERMINISTIC and $0 — no LLM,
no network, no API key. Method names come from method+path, parameters from
the values in sample bodies, constants stay as defaults. The output always
compiles and never contains tokens or secrets.

Optional polish, in order: (a) Ollama at localhost:11434 if reachable (uses a
small local model if one is already there — never downloads anything);
(b) the `claude` or `opencode` CLI if installed; (c) --prompt-only prints the
prompt. Any polish failure silently falls back to the deterministic output.
"""
import json
import re
import subprocess
import sys
from urllib.parse import parse_qsl

# Words in a hostname label that carry no identity (api.example.com -> Example).
_GENERIC_LABEL_WORDS = {
    "api", "www", "app", "prod", "production", "mobile", "web", "gateway",
    "edge", "cdn", "static", "live", "v1", "v2", "v3", "internal", "public",
}

# POST to a bare verb endpoint keeps the verb: POST /like -> like().
_POST_VERBS = {
    "like", "unlike", "login", "logout", "log-in", "log-out", "signin",
    "signout", "signup", "register", "search", "send", "verify", "refresh",
    "reset", "subscribe", "unsubscribe", "follow", "unfollow", "block",
    "unblock", "report", "share", "upload", "download", "sync", "check",
    "confirm", "cancel", "accept", "decline", "approve", "reject", "join",
    "leave", "save", "publish", "unpublish", "archive", "restore", "pin",
    "unpin", "mute", "unmute", "flag", "vote", "rate", "comment", "react",
}

# Keys whose sample values must NEVER appear in generated code.
_SECRET_HINTS = ("token", "password", "secret", "apikey", "api_key", "auth",
                 "session", "cookie", "credential", "passwd", "private")


def _is_secret_key(key):
    k = key.lower()
    return any(h in k for h in _SECRET_HINTS)


def _snake(name):
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    if name and name[0].isdigit():
        name = "n_" + name
    return name or "value"


def _camel(name):
    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", name) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts)


def _singular(word):
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("ses") and len(word) > 3:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def _plural(word):
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def class_name_for_host(host):
    """ExampleClient for api.example.com; HingeawsClient for prod-api.hingeaws.net."""
    for label in host.split("."):
        words = [w for w in re.split(r"[^a-zA-Z0-9]+", label.lower()) if w]
        kept = [w for w in words if w not in _GENERIC_LABEL_WORDS]
        if kept:
            return _camel("_".join(kept)) + "Client"
    return "ApiClient"


_ID_PATTERNS = (
    # UUID
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I),
    # long hex blob
    re.compile(r"^[0-9a-f]{16,}$", re.I),
    # prefixed id: user-9f2c4e77a1b8, msg-001234
    re.compile(r"^[a-z]+-[0-9a-f]{8,}$", re.I),
    re.compile(r"^[a-z]+-\d{4,}$", re.I),
)


def _looks_like_id(seg):
    """True when a path segment is a concrete id, not a resource name."""
    if seg.isdigit():
        return True
    return any(p.match(seg) for p in _ID_PATTERNS)


def _path_parts(path):
    """(name parts, path params) for a path, dropping versions and /api/.

    Id segments (numeric, UUID, long hex, prefixed ids) become by_<name>
    parts with a matching parameter.
    """
    parts, params = [], []
    prev_name = None
    for seg in [s for s in path.split("/") if s]:
        m = re.fullmatch(r"\{(\w+)\}", seg)
        if m:
            name = _snake(m.group(1))
            params.append(name)
            parts.append("by_" + name)
        elif _looks_like_id(seg):
            name = (_singular(prev_name) + "_id") if prev_name else "id"
            name = _snake(name)
            if name not in params:
                params.append(name)
            parts.append("by_" + name)
        elif re.fullmatch(r"v\d+", seg, re.I) or seg.lower() == "api":
            continue
        else:
            parts.append(_snake(seg))
            prev_name = _snake(seg)
    return parts or ["index"], params


def method_name_for(method, path):
    """Deterministic method name from method + path.

    GET /rec/v2 -> get_recs, POST /like -> like, GET /users/{id} -> get_users_by_id.
    """
    parts, _params = _path_parts(path)
    joined = "_".join(parts)
    method = method.upper()
    if method == "GET":
        if (len(parts) == 1 and not parts[0].startswith("by_")
                and not parts[0].endswith("s")):
            return "get_" + _plural(parts[0])
        return "get_" + joined
    if method == "POST":
        if len(parts) == 1:
            if parts[0] in _POST_VERBS:
                return parts[0]
            return "create_" + _singular(parts[0])
        return "post_" + joined
    if method in ("PUT", "PATCH"):
        parts = [p if p.startswith("by_") else _singular(p) for p in parts]
        return "update_" + "_".join(parts)
    if method == "DELETE":
        parts = [p if p.startswith("by_") else _singular(p) for p in parts]
        return "delete_" + "_".join(parts)
    return _snake(method.lower()) + "_" + joined


def _looks_variable(key, value):
    """True when a sample body value varies per call (id, text, number)."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (dict, list)):
        return True  # nested structure: param, default None
    if isinstance(value, int):
        return abs(value) > 1000  # big ints are ids; small ones are constants
    if isinstance(value, float):
        return True
    if isinstance(value, str):
        if not value:
            return False
        if len(value) > 8:
            return True
        if any(c.isdigit() for c in value):
            return True
        if any(c in value for c in " @/"):
            return True
        return False
    return True


def _params_for_endpoint(ep):
    """(params, literals, query_params) for one endpoint.

    params: [(py_name, json_key, default_repr_or_None, required)] — values
    that vary per call become method parameters. literals: {json_key: value}
    constants baked into the body template. query_params: [(py_name, key)].
    Secret-keyed values NEVER appear as literals.
    """
    params, literals = [], {}
    body = {}
    if ep.get("request_body"):
        try:
            parsed = json.loads(ep["request_body"])
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            pass
    for key, value in body.items():
        py = _snake(key)
        if _is_secret_key(key):
            params.append((py, key, None, False))
        elif _looks_variable(key, value):
            if isinstance(value, (dict, list)):
                params.append((py, key, None, False))
            elif isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and abs(value) <= 1000:
                params.append((py, key, repr(value), False))
            else:
                params.append((py, key, None, False))
        else:
            literals[key] = value
    query_params = []
    for key, _val in parse_qsl(ep.get("query") or ""):
        py = _snake(key)
        if py not in [p[0] for p in params]:
            query_params.append((py, key))
    return params, literals, query_params


def _render_method(ep, used_names):
    """Render one endpoint as a method source block. Returns (name, source)."""
    method, path = ep["method"].upper(), ep["path"]
    base = method_name_for(method, path)
    name = base
    n = 2
    while name in used_names:
        name = f"{base}_{n}"
        n += 1
    used_names.add(name)

    _parts, path_params = _path_parts(path)
    params, literals, query_params = _params_for_endpoint(ep)

    sig_parts = list(path_params)
    for py, _key, default, _req in params:
        if py in sig_parts:
            continue
        # Body params are always optional: default None, or a literal default.
        sig_parts.append(f"{py}={default}" if default is not None
                         else f"{py}=None")
    for py, _key in query_params:
        if py not in [s.split("=")[0] for s in sig_parts]:
            sig_parts.append(f"{py}=None")
    sig = ", ".join(["self"] + sig_parts)

    lines = [f"    def {name}({sig}):"]
    # Path with {param} placeholders for f-string substitution.
    url = _url_with_params(path)
    lines.append(f'        """{method} {url} -> {ep.get("status")}."""')

    use_json = method in ("POST", "PUT", "PATCH")
    body_lines = []
    if literals or params:
        if use_json:
            body_lines.append(f"        body = {literals!r}")
            for py, key, _d, _r in params:
                body_lines.append(f"        if {py} is not None:")
                body_lines.append(f"            body[{key!r}] = {py}")
        else:
            body_lines.append(f"        params = {literals!r}")
            for py, key, _d, _r in params:
                body_lines.append(f"        if {py} is not None:")
                body_lines.append(f"            params[{key!r}] = {py}")
    if query_params:
        if not any(l.startswith("        params = ") for l in body_lines):
            body_lines.append("        params = {}")
        for py, key in query_params:
            body_lines.append(f"        if {py} is not None:")
            body_lines.append(f"            params[{key!r}] = {py}")

    call_kw = []
    if use_json and (literals or params):
        call_kw.append("json=body")
    if not use_json and (literals or params or query_params):
        call_kw.append("params=params")
    if use_json and query_params:
        call_kw.append("params=params")

    url_expr = f'"{url}"'
    if path_params:
        url_expr = f'f"{url}"'
    call = f"self.{method.lower()}({url_expr}"
    if call_kw:
        call += ", " + ", ".join(call_kw)
    call += ")"
    lines.extend(body_lines)
    lines.append(f"        return {call}")
    return name, "\n".join(lines)


def _url_with_params(path):
    """Rewrite a captured path so ids become {param} slots.

    /users/{user_id} stays as-is (snake-cased); /users/123 becomes
    /users/{user_id}; plain paths are unchanged.
    """
    segs = [s for s in path.split("/") if s]
    out = []
    prev_name = None
    for seg in segs:
        m = re.fullmatch(r"\{(\w+)\}", seg)
        if m:
            out.append("{" + _snake(m.group(1)) + "}")
        elif _looks_like_id(seg):
            name = (_singular(prev_name) + "_id") if prev_name else "id"
            out.append("{" + _snake(name) + "}")
        else:
            out.append(seg)
            if not (re.fullmatch(r"v\d+", seg, re.I) or seg.lower() == "api"):
                prev_name = _snake(seg)
    return "/" + "/".join(out)


def generate_deterministic(host, endpoints):
    """The $0 generator: pure rules, no LLM, no network. Returns source."""
    cls = class_name_for_host(host)
    used = set()
    blocks = []
    for ep in endpoints:
        _name, src = _render_method(ep, used)
        blocks.append(src)
    methods = "\n\n".join(blocks) if blocks else "    pass"
    return f'''"""{cls} — generated by MIMIK from your own captured traffic.

Own accounts only: this client replays YOUR session for YOUR apps.
Auth is pulled from your live mitmweb or your saved capture — no tokens
are stored in this file. Edit freely.
"""

from mimik import App


class {cls}(App):
    """Client for {host}."""

    HOST = "{host}"

{methods}


if __name__ == "__main__":
    print("methods:", ", ".join(
        m for m in dir({cls}) if not m.startswith("_")))
'''


# ---- prompt (for --prompt-only and the optional polishers) ------------------

PROMPT = """\
You are writing a Python API client. Below is real captured HTTP traffic from \
the app `{host}`, recorded by a proxy while the user exercised the app with \
their own account. Your job: turn it into a clean, ergonomic client library.

Rules:
- Output ONE Python file, nothing else. No prose, no markdown fences.
- Subclass `mimik.App`. Set `HOST = "{host}"`. Auth/device headers are pulled \
automatically by the base class — do NOT hardcode tokens or headers.
- Give methods human names for what they DO (get_posts, like, send_message), \
not the raw path. Infer intent from the path, bodies, and status codes.
- Use self.get(path)/self.post(path, json=body). Both return parsed JSON.
- If an endpoint's body reuses an id or token that another endpoint returns \
(e.g. a viewToken, a playerId, a session id), chain the calls: fetch the \
prerequisite inside the method or cache it on the instance. Read the sample \
bodies carefully to find these dependencies.
- Turn values that vary per call (ids, text, ratings) into method parameters. \
Keep values that are constant-for-this-user as defaults or instance state.
- Skip pure telemetry/analytics/config endpoints unless they're needed as a \
prerequisite for a real action.
- Add a one-line docstring per method. Keep it tight and readable.

Captured endpoints for {host}:

{digest}
"""

POLISH_PROMPT = """\
You are polishing a generated Python API client for `{host}`. It already works;
improve ONLY method names and one-line docstrings to read more naturally
(get_posts instead of get_post_list, like instead of post_like). Rules:
- Output ONE Python file, nothing else. No prose, no markdown fences.
- Keep `mimik.App`, `HOST = "{host}"`, every method's path, HTTP method,
  parameters, and body logic EXACTLY as-is. Do not add or remove methods.
- Do NOT hardcode tokens or headers. Do not change imports.

Client to polish:

{source}
"""


def build_digest(endpoints):
    """Render the endpoint list into the block the AI reads."""
    parts = []
    for e in endpoints:
        block = [f"### {e['method']} {e['path']}  -> {e['status']}"]
        if e["query"]:
            block.append(f"query: {e['query']}")
        if e["request_body"]:
            block.append(f"request body:\n{e['request_body']}")
        if e["response_body"]:
            block.append(f"response body:\n{e['response_body']}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def build_prompt(host, endpoints):
    return PROMPT.format(host=host, digest=build_digest(endpoints))


def _strip_fences(text):
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def _compiles(source):
    try:
        compile(source, "<generated>", "exec")
        return True
    except SyntaxError:
        return False


# ---- optional polishers (all best-effort; failure = deterministic output) ---

OLLAMA_URL = "http://127.0.0.1:11434"
_PREFERRED_MODELS = ("qwen3.5:2b", "qwen3:2b", "qwen3-vl:2b-instruct",
                     "gemma3:1b", "llama3.2:1b", "ministral-3:3b")


def ollama_models(timeout=3):
    """Models already present in local Ollama, or [] if unreachable."""
    try:
        import requests

        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=timeout)
        if r.status_code != 200:
            return []
        return [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        return []


def pick_ollama_model(models):
    """Prefer a small known model; else the smallest by name hint; else None."""
    for want in _PREFERRED_MODELS:
        if want in models:
            return want
    small = [m for m in models if re.search(r":(1b|2b|3b)", m)]
    if small:
        return sorted(small)[0]
    return models[0] if models else None


def polish_ollama(source, host, model=None):
    """Ask local Ollama to polish names/docstrings. Returns (source, ok)."""
    import requests

    models = ollama_models()
    model = model or pick_ollama_model(models)
    if not model:
        return source, False
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model,
                  "prompt": POLISH_PROMPT.format(host=host, source=source),
                  "stream": False},
            timeout=300,
        )
        if r.status_code != 200:
            return source, False
        polished = _strip_fences(r.json().get("response", ""))
        if polished and _compiles(polished) and f'HOST = "{host}"' in polished:
            return polished, True
    except Exception:
        pass
    return source, False


def polish_cli(source, host, generator):
    """Ask `claude` or `opencode` to polish. Returns (source, ok)."""
    prompt = POLISH_PROMPT.format(host=host, source=source)
    try:
        if generator == "opencode":
            proc = subprocess.run(["opencode", "run", prompt],
                                  capture_output=True, text=True, timeout=300)
        else:
            proc = subprocess.run(["claude", "-p"],
                                  input=prompt, capture_output=True,
                                  text=True, timeout=300)
    except FileNotFoundError:
        return source, False
    if proc.returncode != 0:
        return source, False
    polished = _strip_fences(proc.stdout)
    if polished and _compiles(polished) and f'HOST = "{host}"' in polished:
        return polished, True
    return source, False


def generate(host, endpoints, polish="none", model=None):
    """Generate a client. Deterministic by default; polish is optional."""
    source = generate_deterministic(host, endpoints)
    if polish == "ollama":
        source, ok = polish_ollama(source, host, model=model)
        if not ok:
            print("ollama polish unavailable — keeping deterministic output",
                  file=sys.stderr)
    elif polish in ("claude", "opencode"):
        source, ok = polish_cli(source, host, polish)
        if not ok:
            print(f"{polish} polish unavailable — keeping deterministic output",
                  file=sys.stderr)
    return source
