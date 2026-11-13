"""MIMIK — intercept any app, then call it from Python like a library.

    from mimik import Session, App

Capture your own session with mitmproxy (or import a HAR / cURL paste), and
MIMIK reuses your real auth so the server can't tell your script from the app.
Point it at a host and generate a client:  `mimik gen <host>`.

Linux port of littledivy/mimic (MIT) — see LICENSE-NOTICE. Own accounts only:
MIMIK replays YOUR session, for YOUR apps and YOUR accounts.
"""
from .session import App, Session

__all__ = ["Session", "App"]
__version__ = "0.3.0"
