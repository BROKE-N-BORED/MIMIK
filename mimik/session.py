"""The runtime every generated client is built on.

A Session holds your captured auth and makes calls that look like they came
from the real app. Generated clients subclass App and call self.get/.post.

    from hinge_client import Hinge
    acc = Hinge()          # live mitmweb first, saved capture if proxy is off
    acc.get_recs()

Ported from littledivy/mimic (MIT) — see LICENSE-NOTICE — with MIMIK's
persistent-capture additions: Session.from_saved() and App's live-then-saved
fallback, so clients work after the proxy stops.
"""
import requests

from . import extract, store
from .sources.mitm import Mitm, MitmError


_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# Every request times out after 30 s unless the caller says otherwise —
# a hung server must never hang the user's script forever. Override per
# session (Session(..., timeout=60) / App(timeout=60)) or per call
# (session.get("/x", timeout=120)); timeout=None disables it.
DEFAULT_TIMEOUT = 30


class Session:
    """Base URL + reusable headers, with helpers that return parsed JSON.

    Construct one of five ways:
        Session.from_mitm("prod-api.hingeaws.net")   # pull from mitmweb
        Session.from_saved("prod-api.hingeaws.net")  # saved capture, no proxy
        Session(base_url=..., headers={...})         # explicit
        Session.from_curl(text)                      # paste a copied cURL
        Session.from_har("session.har")              # load a browser HAR export
    """

    def __init__(self, base_url, headers=None, host=None, mitm=None,
                 timeout=DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.host = host
        self._mitm = mitm  # kept for token refresh
        self.timeout = timeout
        self._http = requests.Session()

    # ---- constructors -------------------------------------------------------
    @classmethod
    def from_mitm(cls, host, mitm=None):
        # No saving here: the recorder already saves every flow it
        # pulls, so a client that re-saved on construction would
        # rewrite captures on every Hinge() (review #23). Construction
        # is cheap and side-effect-free.
        mitm = mitm or Mitm()
        flows = mitm.flows()
        headers = extract.session_headers(flows, host)
        if not headers:
            raise RuntimeError(
                f"no authenticated request to {host} captured yet — "
                f"open the app once so mitmweb sees a real call, then retry"
            )
        return cls(extract.base_url(flows, host), headers, host=host, mitm=mitm)

    @classmethod
    def from_saved(cls, host):
        """Build a session from a saved capture — no proxy needed."""
        flows = store.load_host(host)
        if not flows:
            raise RuntimeError(
                f"no saved capture for {host} — run `mimik record` and use "
                f"the app once, or import a HAR with `mimik gen --har`"
            )
        headers = extract.session_headers(flows, host)
        if not headers:
            raise RuntimeError(
                f"saved capture for {host} has no authenticated request — "
                f"open the app once while recording, then retry"
            )
        return cls(extract.base_url(flows, host), headers, host=host)

    @classmethod
    def from_curl(cls, text):
        from .sources import curl

        base_url, headers = curl.parse_base_headers(text)
        return cls(base_url, headers)

    @classmethod
    def from_har(cls, path, host=None):
        """Build a session from a HAR file exported from browser devtools.

        With no host, picks the most-requested host in the file.
        """
        from .sources import har

        data = har.parse(path)  # parse once, share across load + hosts
        flows = har.load(data)
        if not host:
            ranked = har.hosts(data)
            if not ranked:
                raise RuntimeError("HAR file has no entries")
            host = ranked[0][0]
        headers = extract.session_headers(flows, host)
        if not headers:
            raise RuntimeError(f"no authenticated request to {host} in the HAR file")
        return cls(extract.base_url(flows, host), headers, host=host)

    # ---- calls --------------------------------------------------------------
    def request(self, method, path, json=None, params=None, refresh=None, **kw):
        """Make a request and return its parsed body.

        Failed responses raise ``requests.HTTPError``. A 401 refreshes captured
        credentials and retries idempotent methods once. Pass ``refresh=True``
        to explicitly allow one retry for a non-idempotent method such as POST.
        Every call times out after ``self.timeout`` (30 s by default) unless
        ``timeout=...`` is passed for that call.
        """
        method = method.upper()
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kw.setdefault("timeout", self.timeout)
        r = self._http.request(
            method, url, headers=self.headers, json=json, params=params, **kw
        )
        should_refresh = refresh is True or (
            refresh is None and method in _IDEMPOTENT_METHODS
        )
        if r.status_code == 401 and should_refresh and self.host and self._mitm:
            # Retry only when mitmweb has a non-empty, changed credential set.
            # Keeping the old headers avoids turning a refresh miss into an
            # unauthenticated retry.
            new_headers = extract.session_headers(self._mitm.flows(), self.host)
            if new_headers and new_headers != self.headers:
                self.headers = new_headers
                return self.request(
                    method, path, json=json, params=params, refresh=False, **kw
                )
        r.raise_for_status()
        return _parse(r)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def put(self, path, json=None, **kw):
        return self.request("PUT", path, json=json, **kw)

    def patch(self, path, json=None, **kw):
        return self.request("PATCH", path, json=json, **kw)

    def delete(self, path, json=None, **kw):
        return self.request("DELETE", path, json=json, **kw)

    def head(self, path, **kw):
        return self.request("HEAD", path, **kw)

    def options(self, path, **kw):
        return self.request("OPTIONS", path, **kw)


class App(Session):
    """Subclass this in a generated client and add named methods.

    class Hinge(App):
        HOST = "prod-api.hingeaws.net"
        def get_recs(self):
            return self.post("/rec/v2", {"playerId": self.player_id})

    With no arguments and a HOST set, App pulls auth from live mitmweb when
    it's running, and falls back to your saved capture when it's not.
    """

    HOST = None

    def __init__(self, **kw):
        timeout = kw.pop("timeout", DEFAULT_TIMEOUT)
        if not kw and self.HOST:
            try:
                got = Session.from_mitm(self.HOST)
            except (MitmError, RuntimeError, requests.RequestException):
                got = Session.from_saved(self.HOST)
            super().__init__(got.base_url, got.headers, host=got.host,
                             mitm=got._mitm, timeout=timeout)
        else:
            super().__init__(timeout=timeout, **kw)


def _parse(r):
    try:
        return r.json()
    except ValueError:
        return r.text
