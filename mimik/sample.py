"""The first-run sample capture (item 28).

A tiny fictional game API — demo.example.com — so a new user can try
the whole flow (hosts -> preset -> generated client -> asset pack)
without recording anything first. Every value is plainly fake: the
host is a reserved example.com name that can never be a real app,
and the token/password carry "fake" in the value itself.
"""
import json

from . import assets, presets, store

SAMPLE_HOST = "demo.example.com"

# A 1x1 transparent PNG, base64 — the sample's one asset.
_LOGO_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
             "Xw8AAwMCAO+ip1sAAAAASUVORK5CYII=")

_FAKE_TOKEN = "fake-demo-token-sample-only"
_FAKE_PASSWORD = "fake-demo-password-sample-only"


def _flow(fid, method, path, status=200, req_headers=None,
          resp_headers=None, req_body=None, resp_body=None,
          resp_b64=False):
    """One flow dict in the store's shape, bodies already embedded
    (save_flows passes embedded bodies through when mitm is None)."""
    flow = {
        "id": fid,
        "request": {"host": SAMPLE_HOST, "method": method, "path": path,
                    "scheme": "https", "port": 443,
                    "headers": req_headers or []},
        "response": {"status_code": status,
                     "headers": resp_headers or []},
    }
    if req_body is not None:
        flow["_saved_request_body"] = req_body
    if resp_body is not None:
        flow["_saved_response_body"] = resp_body
        if resp_b64:
            flow["_saved_response_body_b64"] = True
    return flow


def sample_flows():
    """The sample capture: 6 flows for a fictional game API. Fresh
    dicts on every call, so callers can mutate them freely."""
    auth = [["Authorization", f"Bearer {_FAKE_TOKEN}"],
            ["User-Agent", "DemoGame/1.0"]]
    json_hdr = [["Content-Type", "application/json"]]
    return [
        _flow("sample-login", "POST", "/api/auth/login",
              req_headers=[["Content-Type", "application/json"]],
              resp_headers=json_hdr,
              req_body=json.dumps({"username": "demo-player",
                                   "password": _FAKE_PASSWORD}),
              resp_body=json.dumps({"access_token": _FAKE_TOKEN,
                                    "player_id": "player-1"})),
        _flow("sample-user-1", "GET", "/api/users/123",
              req_headers=auth, resp_headers=json_hdr,
              resp_body=json.dumps({"id": 123, "name": "Demo Player",
                                    "level": 7})),
        _flow("sample-user-2", "GET", "/api/users/456",
              req_headers=auth, resp_headers=json_hdr,
              resp_body=json.dumps({"id": 456, "name": "Second Player",
                                    "level": 3})),
        _flow("sample-items", "GET", "/api/items",
              req_headers=auth, resp_headers=json_hdr,
              resp_body=json.dumps(
                  {"items": [{"id": 1, "name": "Sword"},
                             {"id": 2, "name": "Shield"}]})),
        _flow("sample-item-1", "GET", "/api/items/7",
              req_headers=auth, resp_headers=json_hdr,
              resp_body=json.dumps({"id": 7, "name": "Potion",
                                    "price": 50})),
        _flow("sample-logo", "GET", "/assets/logo.png",
              req_headers=auth,
              resp_headers=[["Content-Type", "image/png"]],
              resp_body=_LOGO_B64, resp_b64=True),
    ]


def load_sample(base=None):
    """Save the sample capture + its asset + its preset. Returns
    {"flows", "assets", "preset", "already_loaded"}. Idempotent —
    a second load finds the flows already saved and only makes sure
    the preset exists (no duplicate asset group)."""
    base = base or store.data_dir()
    existing = store.load_host(SAMPLE_HOST, base)
    if existing:
        preset = presets.load_preset(SAMPLE_HOST, base=base)
        if preset is None:
            result = presets.learn(SAMPLE_HOST, base=base)
            preset = result[0] if result else None
        return {"flows": len(existing), "assets": 0,
                "preset": preset["name"] if preset else None,
                "already_loaded": True}
    flows = sample_flows()
    store.save_flows(flows, base=base)
    _gdir, manifest = assets.extract_from_flows(
        flows, SAMPLE_HOST, host=SAMPLE_HOST, base=base)
    n_assets = len(manifest["assets"]) if manifest else 0
    result = presets.learn(SAMPLE_HOST, base=base)
    return {"flows": len(flows), "assets": n_assets,
            "preset": result[0]["name"] if result else None,
            "already_loaded": False}


def is_fresh(base=None):
    """True on a fresh data dir that hasn't been offered the sample:
    no captures at all, and the offer not yet shown."""
    if store.list_captures(base):
        return False
    return not presets.sample_offered(base)
