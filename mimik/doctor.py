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
import shutil
import socket
import sys

from . import codegen, ghidra, machine, reblend_bridge, store, wine
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


def check_names():
    """The setup check's names, without running any probe — the GUI
    shows them with a pending state while the real check runs (v0.6
    T4, entry 011). Keep in sync with doctor(): test_doctor pins
    the two lists together."""
    return {
        "required": [
            f"python {sys.version_info.major}."
            f"{sys.version_info.minor} (3.10+ needed)",
            "requests library",
            "proxy (mitmweb or uvx)",
            "Qt GUI (PySide6)",
        ],
        "optional": [
            "Ollama (optional polish)",
            "mitmproxy CA cert",
            "wine (Windows capture target)",
            "WinLinx (Windows capture target)",
            "Reblend (asset packs)",
            "git (fetch unpinning scripts)",
            "frida (defeat pinning in the app)",
            "objection (patch APKs for the Frida gadget)",
            "Ghidra (binary analysis)",
        ],
    }


def polish_engines():
    """The Generate polish engines with real availability (v0.6 T10,
    U39): which engines are installed / running on this machine.
    Returns [{key, ok, label}] in picker order. "none" is not listed
    — it needs no engine and always works. Never raises for a
    missing engine — absence is a result, not an exception."""
    models = codegen.ollama_models()
    if models:
        n = len(models)
        ollama = {
            "key": "ollama",
            "ok": True,
            "label": f"ollama (running, {n} model"
                     f"{'s' if n != 1 else ''})",
        }
    else:
        ollama = {
            "key": "ollama",
            "ok": False,
            "label": "ollama (not running)",
        }
    engines = [ollama]
    for key in ("claude", "opencode"):
        found = shutil.which(key) is not None
        engines.append({
            "key": key,
            "ok": found,
            "label": (f"{key} (installed)" if found
                      else f"{key} (not installed)"),
        })
    return engines


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
            "fix": ("pip install mitmproxy, or install uv: "
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
    gd = ghidra.detect()
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
            "fix": ("WinLinx brings Wine, install WinLinx to capture "
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
        {
            "name": "git (fetch unpinning scripts)",
            "ok": shutil.which("git") is not None,
            "fix": "install git to use `mimik unpin` (pinned apps)",
        },
        {
            "name": "frida (defeat pinning in the app)",
            "ok": shutil.which("frida") is not None,
            "fix": "pipx install frida-tools to use `mimik unpin`",
        },
        {
            "name": "objection (patch APKs for the Frida gadget)",
            "ok": shutil.which("objection") is not None,
            "fix": ("pipx install objection to unpin a stock Android "
                    "phone (APK path only)"),
        },
        {
            "name": "Ghidra (binary analysis)",
            "ok": gd["found"] and gd["java_ok"],
            "fix": gd["fix"] or "install Ghidra to decompile binaries",
            "detail": (f"Ghidra {gd['version']}, JDK {gd['java_major']}, "
                       f"heap {gd['heap']}"
                       if gd["found"] and gd["java_ok"] else None),
        },
    ]

    saved = store.list_captures()
    usage = store.disk_usage()
    info = [
        f"saved captures: {len(saved)} host(s) under "
        f"{store.captures_dir()}",
        f"disk usage: {store.format_size(usage['total'])} total, "
        f"captures {store.format_size(usage['captures'])}, clients "
        f"{store.format_size(usage['clients'])}, assets "
        f"{store.format_size(usage['assets'])}, packs "
        f"{store.format_size(usage['packs'])}, presets "
        f"{store.format_size(usage['presets'])}",
        f"LAN IP for the phone proxy: {lan_ip()}:8080",
    ]
    # Measured specs (v0.5 item 17/U35): facts about this PC, no
    # settings shown — the backend adjusts, the user just watches.
    info.extend(machine.describe())

    return {
        "ok": all(c["ok"] for c in required),
        "required": required,
        "mitmweb_running": mitmweb_running,
        "optional": optional,
        "info": info,
    }
