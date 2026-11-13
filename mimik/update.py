"""In-app update check (item 31).

Manual only — nothing here runs on startup or on a timer. The user
asks (Help menu or Doctor tab), and one GET to GitHub's releases API
answers whether a newer MIMIK release exists. Every failure mode is
a returned error string, never a raise.
"""
import json
import urllib.error
import urllib.request

from . import __version__

REPO = "BROKE-N-BORED/MIMIK"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"


def _parse_version(text):
    """("v0.10.0" | "0.10.0") -> (0, 10, 0), or None when the tag
    isn't a dotted-number version."""
    parts = (text or "").strip().lstrip("v").split(".")
    nums = []
    for part in parts:
        if not part.isdigit():
            return None
        nums.append(int(part))
    return tuple(nums) if nums else None


def check_for_update(current=None, timeout=10):
    """Ask GitHub for the latest release. Returns a dict:
    {"current", "latest", "update_available", "url", "error"}.
    "latest"/"url" are None when the check failed; "error" is None
    on success. Never raises."""
    current = current or __version__
    current_v = _parse_version(current)
    try:
        req = urllib.request.Request(
            LATEST_URL, headers={"User-Agent": f"MIMIK/{current}",
                                 "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"current": current, "latest": None,
                "update_available": False, "url": None,
                "error": str(e)}
    latest = _parse_version(data.get("tag_name", ""))
    if latest is None:
        return {"current": current, "latest": None,
                "update_available": False, "url": None,
                "error": "the release had no version tag"}
    latest_s = ".".join(str(n) for n in latest)
    available = current_v is not None and latest > current_v
    return {"current": current, "latest": latest_s,
            "update_available": available,
            "url": data.get("html_url") or RELEASES_URL,
            "error": None}
