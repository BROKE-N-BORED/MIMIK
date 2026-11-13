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


def _version():
    """The package version — pyproject.toml is the one source.

    Read the adjacent pyproject.toml first (source tree); fall back to
    the installed metadata (pip install, where no pyproject.toml sits
    next to the package). Never raises: "0.0.0+unknown" if neither
    works. Regex parse, not tomllib — requires-python is >=3.10 and
    tomllib is 3.11+.
    """
    import re
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text:
        in_project = False
        for line in text.splitlines():
            if line.startswith("["):
                in_project = line.strip() == "[project]"
                continue
            if in_project:
                match = re.match(r'\s*version\s*=\s*"([^"]+)"', line)
                if match:
                    return match.group(1)
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("mimik")
    except Exception:
        return "0.0.0+unknown"


__version__ = _version()
