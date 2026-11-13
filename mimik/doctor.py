"""One shared setup check — the CLI (`mimik doctor`) and the GUI
Doctor tab both render it, so they can never disagree. Returns
structured results; each front-end renders them its own way.

Result shape:
    {
      "ok": bool,  # every required check passes
      "required": [{"name", "ok", "fix"}...],
      "mitmweb_running": bool,  # live proxy reachable right now
      "optional": [{"name", "ok", "fix", "detail"?}...],
      "info": [str, ...],  # saved captures, disk usage, LAN IP
    }
A check's "fix" is the plain-language remedy shown when it fails;
"detail" (Reblend only) is an extra line shown when it passes.
"""
import os
import socket
import sys

from . import codegen, reblend_bridge, store, wine
from .recorder import mitmweb_cmd
from .sources import mitm as mitm_mod


def lan_ip():
    """This machine's LAN IP (for the phone proxy address)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "<this-machine-ip>"
    finally:
        s.close()


def doctor():
    """Run every setup check. Never raises for a failed check — a
    missing piece is a result, not an exception."""
    try:
        import requests  # noqa: F401

        has_requests = True
    except ImportError:
        has_requests = False
    try:
        import PySide6  # noqa: F401

        has_qt = True
    except ImportError:
        has_qt = False

    required = [
        {
            "name": (f"python {sys.version_info.major}."
                     f"{sys.version_info.minor} (3.10+ needed)"),
            "ok": sys.version_info >= (3, 10),
            "fix": "install Python 3.12 from your distro",
        },
        {
            "name": "requests library",
            "ok": has_requests,
            "fix": "pip install requests",
        },
        {
            "name": "proxy (mitmweb or uvx)",
            "ok": mitmweb_cmd() is not None,
            "fix": ("pip install mitmproxy  — or install uv: "
                    "https://astral.sh/uv"),
        },
        {
            "name": "Qt GUI (PySide6)",
            "ok": has_qt,
            "fix": "pip install PySide6",
        },
    ]

    try:
        mitm_mod.Mitm().flows()
        mitmweb_running = True
    except mitm_mod.MitmError:
        mitmweb_running = False

    ca = os.path.join(os.path.expanduser("~"), ".mitmproxy",
                      "mitmproxy-ca-cert.pem")
    rb = reblend_bridge.find_reblend()
    optional = [
        {
            "name": "Ollama (optional polish)",
            "ok": bool(codegen.ollama_models()),
            "fix": ("start Ollama if you want local-AI polish: "
                    "ollama serve"),
        },
        {
            "name": "mitmproxy CA cert",
            "ok": os.path.exists(ca),
            "fix": ("created automatically the first time you run "
                    "`mimik record`"),
        },
        {
            "name": "wine (Windows capture target)",
            "ok": wine.wine_available(),
            "fix": ("WinLinx brings Wine — install WinLinx to capture "
                    "Windows programs"),
        },
        {
            "name": "WinLinx (Windows capture target)",
            "ok": wine.winlinx_available(),
            "fix": "install WinLinx to run Windows programs for capture",
        },
        {
            "name": "Reblend (asset packs)",
            "ok": rb is not None,
            "fix": ("point MIMIK at your Reblend folder (Assets tab, or "
                    "`mimik build-pack --reblend PATH`)"),
            "detail": f"Reblend folder: {rb}" if rb else None,
        },
    ]

    saved = store.list_captures()
    usage = store.disk_usage()
    info = [
        f"saved captures: {len(saved)} host(s) under "
        f"{store.captures_dir()}",
        f"disk usage: {store.format_size(usage['total'])} total — "
        f"captures {store.format_size(usage['captures'])}, clients "
        f"{store.format_size(usage['clients'])}, assets "
        f"{store.format_size(usage['assets'])}, packs "
        f"{store.format_size(usage['packs'])}, presets "
        f"{store.format_size(usage['presets'])}",
        f"LAN IP for the phone proxy: {lan_ip()}:8080",
    ]

    return {
        "ok": all(c["ok"] for c in required),
        "required": required,
        "mitmweb_running": mitmweb_running,
        "optional": optional,
        "info": info,
    }
