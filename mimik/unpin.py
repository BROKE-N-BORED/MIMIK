"""Defeat certificate pinning so MIMIK can capture a pinned app.

Pinning doesn't stop replay, it stops *capture*. A pinned app rejects
mitmproxy's cert, so the proxy never sees the plaintext and there's
nothing to grab. The fix is app-side: make the app stop checking the
pin, using Frida to hook its TLS verification at runtime.

MIMIK doesn't write the hooks (that's a moving target across Android
versions and TLS stacks). It orchestrates the maintained upstream,
httptoolkit/frida-interception-and-unpinning, and owns the one part
that is genuinely MIMIK's job: baking *your* mitmproxy CA and proxy
address into the scripts, and emitting the exact command to run.

Three target paths:

  - Android, rooted (frida-server on the phone): attach over USB and
    spawn the app by package name. `mimik unpin com.example.app`
  - Android, stock (Frida gadget): inject the gadget into the APK,
    install the patched APK, attach to the gadget.
    `mimik unpin app.apk` runs `objection patchapk` for the inject.
  - Linux program: attach Frida to a local program.
    `mimik unpin /usr/bin/app` (or any command on PATH)

The script list is discovered from the fetched repo at run time
(config.js first, then the platform hooks, then the shared native
hooks), so an upstream rename fails clearly instead of silently
emitting a command that references a file that isn't there.

Honest limits: the Frida hooks cover OpenSSL/BoringSSL. Apps on
another TLS stack, and Flutter apps (private BoringSSL, proxy
ignored), still show no traffic. Live Frida execution needs a real
device or a real Linux app; it can't run in a headless test, so the
suite verifies the orchestration (fetch, patch, command) and the
pinning premise (a pinned client rejects a foreign cert), not the
hook itself.
"""
import os
import re
import shutil
import subprocess

from .doctor import lan_ip

SCRIPTS_REPO = ("https://github.com/httptoolkit/"
                "frida-interception-and-unpinning")

DEFAULT_CA = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")
PROXY_PORT = 8080  # mitmweb's proxy listener: the port `mimik record` uses


class UnpinError(RuntimeError):
    """Anything that stops `mimik unpin` preparing a target."""


def target_kind(target, platform=None):
    """'apk' | 'android' | 'linux' for a target string.

    platform forces it ('android'/'linux'); an .apk is always the
    gadget path. Otherwise: a path or a command on PATH is a Linux
    program, a dotted name is an Android package, anything else is
    ambiguous and must be disambiguated.
    """
    if target.lower().endswith(".apk"):
        return "apk"
    if platform in ("android", "linux"):
        return platform
    if "/" in target or shutil.which(target):
        return "linux"
    if "." in target:
        return "android"
    raise UnpinError(
        f"can't tell what {target!r} is: an Android package "
        "(com.example.app), an APK file, or a Linux program. Pass "
        "--android or --linux to say which.")


def fetch_scripts(dst):
    """Fetch (or refresh) the upstream Frida scripts into `dst`.

    False when git is missing or the clone fails; a failed refresh
    pull is not fatal (the scripts already there still work).
    """
    if not shutil.which("git"):
        return False
    if os.path.isdir(os.path.join(dst, ".git")):
        subprocess.run(["git", "-C", dst, "pull", "--quiet", "--ff-only"],
                       check=False)
        return True
    r = subprocess.run(["git", "clone", "--depth", "1", "--quiet",
                        SCRIPTS_REPO, dst], check=False)
    return r.returncode == 0


def _js_in(directory):
    if not os.path.isdir(directory):
        return []
    return [os.path.join(directory, name)
            for name in sorted(os.listdir(directory))
            if name.endswith(".js")]


def _found_list(scripts_dir):
    try:
        names = sorted(os.listdir(scripts_dir))
    except OSError:
        return "(nothing: the directory doesn't exist)"
    if not names:
        return "(nothing: the directory is empty)"
    return ", ".join(names)


def discover_scripts(scripts_dir, kind):
    """Ordered script paths for a target kind.

    config.js first (it defines CERT_PEM/PROXY_* the hooks read),
    then the platform hooks, then the shared native hooks.
    """
    config = os.path.join(scripts_dir, "config.js")
    if not os.path.isfile(config):
        raise UnpinError(
            f"no config.js in {scripts_dir}: the scripts repo changed "
            f"shape. Found: {_found_list(scripts_dir)}")
    scripts = [config]
    platform_dir = {"android": "android", "apk": "android"}.get(kind)
    if platform_dir:
        found = _js_in(os.path.join(scripts_dir, platform_dir))
        if not found:
            raise UnpinError(
                f"no {platform_dir}/*.js hooks in {scripts_dir}: the "
                "scripts repo changed shape. Found: "
                f"{_found_list(scripts_dir)}")
        scripts += found
    # Root-level hooks are the shared native ones only; anything else
    # at the root (helpers, docs-as-js) is not a script to load.
    scripts += [s for s in _js_in(scripts_dir)
                if os.path.basename(s).startswith("native-")]
    return scripts


def patch_config(scripts_dir, ca_pem, proxy_host):
    """Bake the CA + proxy into a copy of upstream config.js, in place.

    Patches upstream's file rather than hand-writing one so all its
    helper code (the base64/DER machinery the hooks depend on) stays
    intact and current. Raises UnpinError if the file changed shape.
    """
    path = os.path.join(scripts_dir, "config.js")
    with open(path) as f:
        src = f.read()

    ca = ca_pem.strip()
    src, n_cert = re.subn(r"const CERT_PEM = `.*?`;",
                          f"const CERT_PEM = `{ca}`;", src, count=1,
                          flags=re.S)
    src, n_host = re.subn(r"const PROXY_HOST = '[^']*';",
                          f"const PROXY_HOST = '{proxy_host}';", src,
                          count=1)
    src, n_port = re.subn(r"const PROXY_PORT = \d+;",
                          f"const PROXY_PORT = {PROXY_PORT};", src,
                          count=1)
    if not (n_cert and n_host and n_port):
        raise UnpinError(
            "config.js from upstream changed shape: MIMIK couldn't patch "
            f"it (cert={n_cert} host={n_host} port={n_port}).")

    with open(path, "w") as f:
        f.write(src)


def frida_command(scripts, target, kind):
    """The exact frida command for a prepared target."""
    if kind == "linux":
        lines = ["frida \\"]
        flag, who = "-f", target
    elif kind == "apk":
        lines = ["frida -U \\"]
        flag, who = "-n", "Gadget"
    else:
        lines = ["frida -U \\"]
        flag, who = "-f", target
    for s in scripts:
        lines.append(f"    -l {s} \\")
    lines.append(f"    {flag} {who}")
    return "\n".join(lines)


def _patch_apk(target, workdir):
    """Inject the Frida gadget into an APK via objection patchapk."""
    if not shutil.which("objection"):
        raise UnpinError(
            "objection not found: it does the gadget injection.\n"
            "    pipx install objection      (or: uv tool install "
            "objection)\n"
            "then re-run `mimik unpin`.")
    out_apk = os.path.join(
        workdir, os.path.basename(target)[:-4] + "-patched.apk")
    r = subprocess.run(["objection", "patchapk", "--source", target,
                        "--output", out_apk], check=False)
    if r.returncode != 0:
        raise UnpinError(
            "objection patchapk failed: the APK must be one objection "
            "can unpack (an APK from your own build works; a Play Store "
            "APK may be split or protected).")
    return out_apk


def _steps(kind, target, host, patched_apk):
    if kind == "android":
        return [
            "Phone + PC on the same network; `mimik record` running here "
            "(or the proxy up in the app).",
            "Rooted phone with frida-server running, connected by USB. "
            "No USB? Use frida -H <phone-ip>:27042 instead of frida -U.",
            "Run this to launch the app with pinning disabled:",
            "Use the app, then: mimik hosts, then mimik gen <api-host>.",
        ]
    if kind == "apk":
        return [
            f"Install the patched APK on the phone: {patched_apk}. It "
            "replaces the original (different signature), so uninstall "
            "the original first: its local data goes with it.",
            "Launch the patched app once so the Frida gadget starts.",
            "Attach the scripts to the gadget:",
            "Use the app, then: mimik hosts, then mimik gen <api-host>.",
        ]
    return [
        "`mimik record` running here (or the proxy up in the app).",
        "Run this to launch the program with pinning disabled:",
        "Use the program, then: mimik hosts, then mimik gen <api-host>.",
    ]


def _notes(kind):
    notes = [
        "This defeats pinning in your own apps on your own devices: "
        "the same own-accounts-only rule as the rest of MIMIK.",
    ]
    if kind in ("android", "apk"):
        notes.append(
            "Flutter apps ignore the proxy and use private BoringSSL: "
            "this path won't capture them without reFlutter.")
    else:
        notes.append(
            "The hooks cover OpenSSL/BoringSSL. Apps on another TLS "
            "stack (NSS, GnuTLS, Rust) still show no traffic: that is "
            "the hook's limit, not the proxy's. If Frida refuses to "
            "attach, it usually needs root.")
    return notes


def prepare(target, ca_path=None, proxy_host=None, workdir=None,
            platform=None):
    """Prepare an unpinning run. Returns a plan dict:

        {"kind", "scripts", "command", "steps", "notes",
         "patched_apk", "proxy_host"}

    Raises UnpinError when anything stops it. The only third-party
    tools it shells out to are git (fetch the scripts) and, for an
    APK target, objection (inject the gadget).
    """
    kind = target_kind(target, platform)
    ca_path = ca_path or DEFAULT_CA
    if not os.path.exists(ca_path):
        raise UnpinError(
            f"no mitmproxy CA at {ca_path}: run `mimik record` once so "
            "mitmproxy generates it, or pass --ca <path>.")
    with open(ca_path) as f:
        ca_pem = f.read()

    workdir = workdir or "mimik-unpin"
    scripts_dir = os.path.join(workdir, "frida-scripts")
    os.makedirs(workdir, exist_ok=True)
    have_git = os.path.isdir(os.path.join(scripts_dir, ".git"))
    have_scripts = os.path.isfile(os.path.join(scripts_dir, "config.js"))
    if have_git:
        fetch_scripts(scripts_dir)  # refresh; a failed pull is not fatal
    elif not have_scripts:
        if not fetch_scripts(scripts_dir):
            raise UnpinError(
                "couldn't fetch the Frida scripts (need git + network). "
                "Clone them yourself:\n"
                f"    git clone {SCRIPTS_REPO} {scripts_dir}\n"
                "then re-run `mimik unpin`.")

    host = proxy_host or lan_ip()
    patch_config(scripts_dir, ca_pem, host)
    scripts = discover_scripts(scripts_dir, kind)
    patched_apk = _patch_apk(target, workdir) if kind == "apk" else None
    return {
        "kind": kind,
        "scripts": scripts,
        "command": frida_command(scripts, target, kind),
        "steps": _steps(kind, target, host, patched_apk),
        "notes": _notes(kind),
        "patched_apk": patched_apk,
        "proxy_host": host,
    }


def format_plan(plan, target):
    """Render a plan as plain text (CLI + GUI share it). No em-dashes:
    the GUI shows this text (U33)."""
    titles = {
        "android": f"Android app, rooted (frida-server): {target}",
        "apk": f"Android app, patched APK (Frida gadget): {target}",
        "linux": f"Linux program: {target}",
    }
    lines = [titles[plan["kind"]],
             f"Proxy: {plan['proxy_host']}:{PROXY_PORT} "
             f"(baked into config.js with your mitmproxy CA)",
             ""]
    for i, step in enumerate(plan["steps"], 1):
        lines.append(f"  {i}. {step}")
        if step.endswith(":"):
            lines.append("")
            lines.append(plan["command"])
            lines.append("")
    lines.append("")
    lines.append("Notes:")
    for note in plan["notes"]:
        lines.append(f"  - {note}")
    return "\n".join(lines)
