"""Parse a copied cURL command into a MIMIK flow (or a Session's base+headers).

Browsers and apps let you "Copy as cURL" — a single real request, pasted as
text. That one request is enough to seed a session and to generate a client
with one endpoint; combine with HAR or a live capture for the full API.
"""
import shlex
from urllib.parse import urlsplit


def parse(text):
    """Parse `curl 'URL' -X POST -H 'k: v' --data '...'` into its parts.

    Returns dict with keys: url, method, headers, data (raw body or "").
    Raises ValueError when no URL is found.
    """
    tokens = shlex.split(text.replace("\\\n", " "))
    url, method, headers, data = None, "GET", {}, ""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header"):
            i += 1
            if i < len(tokens):
                k, _, v = tokens[i].partition(":")
                headers[k.strip()] = v.strip()
        elif t in ("-X", "--request"):
            i += 1
            if i < len(tokens):
                method = tokens[i].upper()
        elif t in ("-d", "--data", "--data-raw", "--data-binary", "--json"):
            i += 1
            if i < len(tokens):
                data = tokens[i]
                if method == "GET":
                    method = "POST"
        elif t.startswith("http"):
            url = t
        i += 1
    if not url:
        raise ValueError("no URL found in cURL text")
    return {"url": url, "method": method, "headers": headers, "data": data}


def parse_base_headers(text):
    """(base_url, headers) from a cURL paste — the Session.from_curl shape."""
    p = parse(text)
    parts = urlsplit(p["url"])
    return f"{parts.scheme}://{parts.netloc}", p["headers"]


def load(text_or_path):
    """One cURL paste (text, or a path to a file holding it) as flow dicts."""
    import os

    text = text_or_path
    if os.path.exists(text_or_path) and os.path.isfile(text_or_path):
        with open(text_or_path) as f:
            text = f.read()
    p = parse(text)
    parts = urlsplit(p["url"])
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    flow = {
        "id": "curl-0",
        "request": {
            "host": parts.hostname,
            "method": p["method"],
            "path": path,
            "scheme": parts.scheme,
            "port": port,
            "headers": [[k, v] for k, v in p["headers"].items()],
        },
        "response": {"status_code": 0},
    }
    if p["data"]:
        flow["_saved_request_body"] = p["data"]
    return [flow]
