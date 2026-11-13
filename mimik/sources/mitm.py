"""Read captured traffic from a running mitmweb instance.

mitmweb exposes a JSON API on http://127.0.0.1:8081. Auth is a token passed
once as a query param, which sets a session cookie; subsequent requests reuse
the cookie. This module pulls the raw flows and normalizes them into a shape
that both the runtime Session and the codegen step can consume.

Ported from littledivy/mimic (MIT) — see LICENSE-NOTICE.
"""
import json
import os

import requests

from .. import websocket as ws_mod

DEFAULT_URL = os.environ.get("MITM_URL", "http://127.0.0.1:8081")
DEFAULT_TOKEN = os.environ.get("MITM_TOKEN", "test")


# Localhost calls must never go through a proxy (item 32g): every
# request passes explicit None proxies instead of relying on NO_PROXY
# being set in the environment. Per-request, not session-level —
# requests strips None values when merging session proxies, so the
# session attribute would silently do nothing (probed on 2.31.0).
# "all" is in there too: requests' select_proxy falls back to the
# "all" key, so an ALL_PROXY env var would otherwise still route
# localhost through the proxy.
_NO_PROXY = {"http": None, "https": None, "all": None}


class MitmError(RuntimeError):
    pass


class Mitm:
    """A thin client over a running mitmweb's flow API."""

    def __init__(self, url=DEFAULT_URL, token=DEFAULT_TOKEN):
        self.url = url.rstrip("/")
        self.token = token
        self._http = requests.Session()

    def _auth(self):
        try:
            self._http.get(f"{self.url}/", params={"token": self.token},
                           proxies=_NO_PROXY, timeout=5)
        except requests.RequestException as e:
            raise MitmError(
                f"can't reach mitmweb at {self.url}, is it running? "
                f"start it with `mimik record` (original error: {e})"
            )

    def flows(self):
        """All captured flows as a list of dicts (mitmweb's own schema)."""
        self._auth()
        r = self._http.get(f"{self.url}/flows", proxies=_NO_PROXY,
                           timeout=15)
        if r.status_code != 200:
            raise MitmError(f"mitmweb /flows returned {r.status_code}")
        return r.json()

    def body(self, flow_id, side):
        """Raw request/response body bytes for a flow. side is 'request' or 'response'."""
        r = self._http.get(
            f"{self.url}/flows/{flow_id}/{side}/content.data",
            proxies=_NO_PROXY, timeout=15
        )
        return r.content

    def ws_messages(self, flow_id):
        """WebSocket messages for a flow (v0.4 item 13).

        Returns [{"from_client", "timestamp", "text"} |
        {"from_client", "timestamp", "b64"}] — text for text messages,
        base64 for binary ones. Fetches the auto + raw content views:
        text comes from "raw" (exact), binary from "auto"'s hex dump
        (parsed back to bytes). A hex dump that won't parse is kept as
        text — visible in the transcript, replayed as text.
        Raises MitmError when mitmweb doesn't answer.
        """
        self._auth()
        views = {}
        for view in ("auto", "raw"):
            try:
                r = self._http.get(
                    f"{self.url}/flows/{flow_id}/messages/content/"
                    f"{view}.json",
                    proxies=_NO_PROXY, timeout=15)
            except requests.RequestException as e:
                raise MitmError(f"can't fetch WS messages: {e}")
            if r.status_code != 200:
                raise MitmError(
                    f"mitmweb WS messages returned {r.status_code}")
            views[view] = r.json()
        auto = views["auto"]
        raw = views["raw"]
        out = []
        for i, message in enumerate(auto):
            entry = {"from_client": message.get("from_client", True),
                     "timestamp": message.get("timestamp", 0)}
            if message.get("view_name") == "Hex Dump":
                parsed = ws_mod.parse_hex_dump(message.get("text", ""))
                if parsed is not None:
                    import base64

                    entry["b64"] = base64.b64encode(parsed).decode("ascii")
                else:
                    entry["text"] = message.get("text", "")
            elif i < len(raw):
                entry["text"] = raw[i].get("text", "")
            else:
                entry["text"] = message.get("text", "")
            out.append(entry)
        return out


def hosts(flows):
    """Count requests per host, most frequent first."""
    counts = {}
    for f in flows:
        req = f.get("request")
        if req:
            counts[req["host"]] = counts.get(req["host"], 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def endpoints(mitm, flows, host):
    """Distinct (method, path) endpoints for a host, with a sample req/resp body.

    Returns a list of dicts ready to feed to codegen. Latest capture of each
    endpoint wins, so bodies reflect the most recent real call.
    """
    by_key = {}
    counts = {}
    for f in flows:
        req = f.get("request")
        if not req or req["host"] != host:
            continue
        key = (req["method"], req["path"].split("?")[0])
        by_key[key] = f
        counts[key] = counts.get(key, 0) + 1

    out = []
    for (method, path), f in by_key.items():
        req = f.get("request") or {}
        resp = f.get("response") or {}
        out.append(
            {
                "method": method,
                "path": path,
                "status": resp.get("status_code"),
                "query": req["path"].split("?", 1)[1] if "?" in req["path"] else "",
                "request_body": _decode(mitm.body(f["id"], "request")),
                "response_body": _decode(mitm.body(f["id"], "response")),
                # The sample flow itself (v0.4 item 10) — the GUI's
                # Replay button re-sends it.
                "flow": f,
                # How many captured calls fed this endpoint (v0.4
                # item 11) — codegen flags single-sample methods.
                "samples": counts[(method, path)],
                # WebSocket (v0.4 item 13): the sample flow's messages,
                # fetched live — codegen turns them into a ws_replay
                # method. A failed fetch means an empty session, never
                # a failed endpoint list.
                "websocket": ws_mod.is_websocket_flow(f),
                "ws_messages": _live_ws_messages(mitm, f),
            }
        )
    return out


def _live_ws_messages(mitm, flow):
    """A live flow's WS messages, or [] (not a WS flow, or the fetch
    failed — the endpoint list must never break on a WS fetch)."""
    if not ws_mod.is_websocket_flow(flow):
        return []
    try:
        return mitm.ws_messages(flow["id"])
    except Exception:
        return []


def endpoints_from_saved(flows, host):
    """Like endpoints(), but reads bodies embedded in saved captures.

    Saved flows carry ``_saved_request_body`` / ``_saved_response_body``
    (plain text when the body decoded as UTF-8, else base64 with a
    ``_saved_*_body_b64`` flag) — see mimik.store. No mitmweb needed.
    """
    import base64

    by_key = {}
    counts = {}
    for f in flows:
        req = f.get("request")
        if not req or req["host"] != host:
            continue
        key = (req["method"], req["path"].split("?")[0])
        by_key[key] = f
        counts[key] = counts.get(key, 0) + 1

    out = []
    for (method, path), f in by_key.items():
        req = f.get("request") or {}
        resp = f.get("response") or {}
        out.append(
            {
                "method": method,
                "path": path,
                "status": resp.get("status_code"),
                "query": req["path"].split("?", 1)[1] if "?" in req["path"] else "",
                "request_body": _saved_body(f, "request", base64),
                "response_body": _saved_body(f, "response", base64),
                # The sample flow itself (v0.4 item 10) — the GUI's
                # Replay button re-sends it.
                "flow": f,
                # How many captured calls fed this endpoint (v0.4
                # item 11) — codegen flags single-sample methods.
                "samples": counts[(method, path)],
                # WebSocket (v0.4 item 13): the saved messages ride
                # with the flow (see store._embed_bodies).
                "websocket": ws_mod.is_websocket_flow(f),
                "ws_messages": ws_mod.ws_messages(f),
            }
        )
    return out


def _saved_body(flow, side, base64):
    key = f"_saved_{side}_body"
    if flow.get(key + "_b64"):
        try:
            raw = base64.b64decode(flow[key])
        except (ValueError, TypeError):
            return ""
        return _decode(raw)
    return _decode((flow.get(key) or "").encode("utf-8"))


def _decode(raw, limit=4000):
    """Best-effort decode of a body to a truncated JSON/text string."""
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<{len(raw)} bytes binary>"
    try:  # pretty-print + truncate JSON so codegen sees structure, not noise
        text = json.dumps(json.loads(text), indent=2)
    except ValueError:
        pass
    return text[:limit] + ("\n…(truncated)" if len(text) > limit else "")
