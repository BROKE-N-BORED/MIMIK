"""Capture traffic from a Windows program already running under WinLinx/Wine.

Division of labor — do not blur it:
  * WinLinx owns RUNNING Windows programs (Wine, install, launch, Program
    Manager). MIMIK never installs, runs, or manages a Windows program, and
    builds no Wine layer.
  * MIMIK owns CAPTURING traffic from a Windows program that is already
    running under WinLinx/Wine, and GENERATING the client/clone from it.

This module points the user's own Wine prefix at MIMIK's proxy (a .reg file
applied with `wine regedit`) and installs the mitmproxy CA into the prefix's
cert store so HTTPS capture works. Every command is shown before it runs, the
prior proxy settings are saved, and Stop restores them.

Honest limits: some Windows programs ignore the system proxy or pin their
certificates — those show no traffic, same as pinned phone apps. Live Wine
capture cannot be verified in a sandbox; it needs a real-PC verdict.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8080

INTERNET_SETTINGS = (
    r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion"
    r"\Internet Settings"
)

# Where WinLinx (the Windows layer) may live — checked best-effort.
WINLINX_HINTS = (
    os.path.join(os.path.expanduser("~"), ".local", "share", "winlinx"),
    os.path.join(os.path.expanduser("~"), ".local", "share", "WinLinx"),
    os.path.join(os.path.expanduser("~"), "Windows-Apps"),
)


class WineError(RuntimeError):
    pass


def wine_prefix():
    """The user's Wine prefix (WINEPREFIX or ~/.wine)."""
    return os.environ.get("WINEPREFIX") or os.path.join(
        os.path.expanduser("~"), ".wine"
    )


def wine_available():
    return shutil.which("wine") is not None


def winlinx_available():
    """WinLinx present? (binary on PATH or one of its install dirs)."""
    if shutil.which("winlinx"):
        return True
    return any(os.path.isdir(p) for p in WINLINX_HINTS)


def check_ready():
    """(ok, message) — wine + WinLinx + prefix present?"""
    if not wine_available():
        return False, (
            "wine is not installed, WinLinx runs Windows programs, so "
            "install WinLinx (it brings Wine) and launch the program there "
            "first, then come back to MIMIK to capture it."
        )
    if not winlinx_available():
        return False, (
            "WinLinx not found, WinLinx runs Windows programs. Install it "
            "and launch the program there first, then use MIMIK to capture "
            "its traffic."
        )
    if not os.path.isdir(wine_prefix()):
        return False, (
            f"no Wine prefix at {wine_prefix()} yet, launch a Windows "
            "program in WinLinx once to create it."
        )
    return True, ""


# ---- .reg generation ---------------------------------------------------------

def build_proxy_reg(proxy_host=PROXY_HOST, proxy_port=PROXY_PORT):
    """Registry text enabling the system proxy for the prefix."""
    return (
        "Windows Registry Editor Version 5.00\r\n\r\n"
        f"[{INTERNET_SETTINGS}]\r\n"
        '"ProxyEnable"=dword:00000001\r\n'
        f'"ProxyServer"="{proxy_host}:{proxy_port}"\r\n'
        '"ProxyOverride"="localhost;127.0.0.1;<local>"\r\n'
    )


def build_restore_reg(state):
    """Registry text restoring saved proxy settings (or disabling the proxy).

    state: {"ProxyEnable": 0|1|None, "ProxyServer": str|None,
            "ProxyOverride": str|None} — None means the value was absent,
    which a .reg expresses as a deletion ("Name"=-).
    """
    lines = ["Windows Registry Editor Version 5.00\r\n\r\n",
             f"[{INTERNET_SETTINGS}]\r\n"]
    enable = state.get("ProxyEnable")
    if enable is None:
        lines.append('"ProxyEnable"=-\r\n')
    else:
        lines.append(f'"ProxyEnable"=dword:{int(enable):08x}\r\n')
    for key in ("ProxyServer", "ProxyOverride"):
        val = state.get(key)
        if val is None:
            lines.append(f'"{key}"=-\r\n')
        else:
            lines.append(f'"{key}"="{val}"\r\n')
    return "".join(lines)


def build_disable_reg():
    """Registry text that simply turns the proxy off."""
    return (
        "Windows Registry Editor Version 5.00\r\n\r\n"
        f"[{INTERNET_SETTINGS}]\r\n"
        '"ProxyEnable"=dword:00000000\r\n'
    )


# ---- reading + saving the current proxy state --------------------------------

def read_proxy_state(prefix=None):
    """Current proxy settings, parsed from the prefix's user.reg.

    Returns {"ProxyEnable": int|None, "ProxyServer": str|None,
    "ProxyOverride": str|None}. Missing prefix/user.reg -> all None.
    """
    state = {"ProxyEnable": None, "ProxyServer": None, "ProxyOverride": None}
    user_reg = os.path.join(prefix or wine_prefix(), "user.reg")
    try:
        with open(user_reg, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return state
    # user.reg escapes backslashes: [Software\\Microsoft\\...]
    section = re.search(
        r"\[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\"
        r"Internet Settings\](.*?)(?:\n\[|\Z)",
        text, re.S,
    )
    if not section:
        return state
    body = section.group(1)
    m = re.search(r'"ProxyEnable"=dword:([0-9a-fA-F]+)', body)
    if m:
        state["ProxyEnable"] = int(m.group(1), 16)
    for key in ("ProxyServer", "ProxyOverride"):
        m = re.search(r'"%s"="((?:[^"\\]|\\.)*)"' % key, body)
        if m:
            state[key] = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    return state


def state_path():
    """Where MIMIK keeps the saved pre-capture proxy state."""
    from . import store

    return os.path.join(store.data_dir(), "wine-proxy-state.json")


def save_proxy_state(state, path=None):
    path = path or state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f)
    return path


def load_proxy_state(path=None):
    path = path or state_path()
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def has_saved_state(path=None):
    """True if a pre-capture proxy state is saved — i.e. a capture ended
    without restoring the prefix (a crash, or the window closed
    mid-record before v0.3)."""
    return load_proxy_state(path) is not None


# ---- applying ---------------------------------------------------------------

def regedit_command(reg_path, prefix=None):
    """argv to apply a .reg file to a prefix (WINEPREFIX via env)."""
    return ["wine", "regedit", "/s", reg_path]


def apply_reg_text(reg_text, prefix=None, runner=None):
    """Write reg_text to a temp .reg and apply it. Returns the argv run."""
    runner = runner or subprocess.run
    with tempfile.NamedTemporaryFile(
        "w", suffix=".reg", delete=False, encoding="utf-8", newline=""
    ) as f:
        f.write(reg_text)
        reg_path = f.name
    env = dict(os.environ)
    env["WINEPREFIX"] = prefix or wine_prefix()
    try:
        runner(regedit_command(reg_path, prefix), env=env,
               capture_output=True, text=True, check=True)
    finally:
        try:
            os.unlink(reg_path)
        except OSError:
            pass
    return regedit_command(reg_path, prefix)


def mitm_ca_path():
    """The mitmproxy CA cert (created on first mitmproxy run)."""
    return os.path.join(
        os.path.expanduser("~"), ".mitmproxy", "mitmproxy-ca-cert.pem"
    )


def ca_install_command(ca_path=None, prefix=None):
    """argv installing the mitmproxy CA into the prefix's trusted roots.

    Uses Wine's certutil. If certutil is missing in the prefix, the manual
    fallback is: wine control → Internet Options → Content → Certificates →
    Trusted Root Certification Authorities → Import.
    """
    ca = ca_path or mitm_ca_path()
    return ["wine", "certutil", "-addstore", "-f", "-user", "Root", ca]


def install_ca(ca_path=None, prefix=None, runner=None):
    """Install the mitmproxy CA into the prefix. Returns the argv run."""
    runner = runner or subprocess.run
    ca = ca_path or mitm_ca_path()
    if not os.path.exists(ca):
        raise WineError(
            f"no mitmproxy CA cert at {ca} yet, run `mimik record` once "
            "(any target) so mitmproxy creates it, then retry"
        )
    env = dict(os.environ)
    env["WINEPREFIX"] = prefix or wine_prefix()
    cmd = ca_install_command(ca, prefix)
    runner(cmd, env=env, capture_output=True, text=True, check=True)
    return cmd


def apply_proxy(prefix=None, runner=None):
    """Save current proxy state, then point the prefix at MIMIK's proxy.

    The saved state is the ORIGINAL pre-capture state: if a state is
    already saved (a previous capture never restored it), it is NOT
    overwritten — saving again would record the proxy-on settings as
    "original", and the prefix could never be restored to what it was.

    Returns the list of argv commands that were (or will be) run, for display.
    """
    prefix = prefix or wine_prefix()
    if load_proxy_state() is None:
        save_proxy_state(read_proxy_state(prefix))
    cmds = [regedit_command("<proxy.reg>", prefix)]
    apply_reg_text(build_proxy_reg(), prefix, runner=runner)
    return cmds


def revert_proxy(prefix=None, runner=None):
    """Restore the saved proxy state (or disable the proxy if none saved)."""
    prefix = prefix or wine_prefix()
    state = load_proxy_state()
    reg = build_restore_reg(state) if state is not None else build_disable_reg()
    apply_reg_text(reg, prefix, runner=runner)
    try:
        os.unlink(state_path())
    except OSError:
        pass
    return regedit_command("<restore.reg>", prefix)
