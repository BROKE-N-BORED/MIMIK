"""MIMIK CLI — capture any app, generate a client.

    mimik record                      start the proxy + Android setup steps
    mimik record --target linux-app   capture a Linux desktop app
    mimik record --file app.AppImage  capture a program from a file
    mimik record --target windows     capture a Windows program via WinLinx
    mimik record --target web         web app: HAR import / cURL paste
    mimik hosts                       list captured hosts
    mimik learn <host>                show the endpoints MIMIK saw for a host
    mimik gen <host>                  generate a Python client for a host
    mimik clients                     list generated clients + saved captures
    mimik doctor                      check your setup

Own accounts only: MIMIK replays YOUR session, for YOUR apps and accounts.
"""
import argparse
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys

from . import codegen, launch, store, wine
from .recorder import ProxySession, mitmweb_cmd
from .sources import curl, har
from .sources import mitm as mitm_mod


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "<this-machine-ip>"
    finally:
        s.close()


# ---- flow resolution (HAR / cURL / live proxy / saved capture) --------------

def _flows_from_args(args):
    """(mitm_or_None, flows, source) — source is har/curl/live/saved."""
    if getattr(args, "har", None):
        return None, har.load(args.har), "har"
    if getattr(args, "curl", None):
        text = args.curl
        try:
            with open(text) as f:
                text = f.read()
        except OSError:
            pass  # not a file: treat the argument itself as the cURL text
        return None, curl.load(text), "curl"
    try:
        m = mitm_mod.Mitm()
        flows = m.flows()
        try:
            store.save_flows(flows, mitm=m)
        except Exception:
            pass
        return m, flows, "live"
    except mitm_mod.MitmError:
        flows = store.load_all()
        if flows:
            return None, flows, "saved"
        raise SystemExit(
            "no proxy running and no saved captures — run `mimik record` "
            "first, or pass --har FILE / --curl FILE"
        )


def _endpoints_from_args(args, m, flows, source):
    host = args.host
    if source == "har":
        return har.endpoints(args.har, host)
    if source == "live":
        return mitm_mod.endpoints(m, flows, host)
    return mitm_mod.endpoints_from_saved(flows, host)


# ---- record ------------------------------------------------------------------

ANDROID_STEPS = """
Android phone capture — do this once, then just reopen the app to add traffic:

  1. Phone → Settings → Wi-Fi → your network → Proxy → Manual
        Hostname: {ip}      Port: 8080
  2. Phone browser → http://mitm.it → download the Android certificate
  3. Settings → Security → Install a certificate → CA certificate → pick it
     (on newer Android: Settings → Security → Encryption & credentials →
      Install a certificate → CA certificate)
  4. open the target app and use it normally
  5. back here:   mimik hosts      then   mimik gen <api-host>

  mitmweb dashboard: http://127.0.0.1:8081   (MIMIK reads flows from here)

  Android 7+ apps only trust user-installed CAs if the app opts in — some
  apps show no traffic because of that, and pinned apps (banks, Instagram)
  show none at all. If `mimik hosts` shows the app's API host, you're good.
"""

LINUX_APP_STEPS = """
Linux app capture — run the app through the proxy:

  Launch it with the proxy env vars, for example:

      http_proxy=http://127.0.0.1:8080 https_proxy=http://127.0.0.1:8080 <app>

  Or let MIMIK launch it:   mimik record --target linux-app --launch "<app>"

  The app's own CA trust matters: apps using the system store need the
  mitmproxy CA installed (see README); apps with their own store may refuse.
  If `mimik hosts` shows the app's API host, you're good.
"""

WEB_STEPS = """
Web app capture — no proxy needed:

  1. Browser → DevTools (F12) → Network → use the web app normally
  2. Right-click the network log → Save all as HAR → save session.har
  3. mimik hosts --har session.har     then     mimik gen <host> --har session.har

  Or copy one request as cURL (right-click → Copy → Copy as cURL) and run:
      mimik gen <host> --curl request.txt
"""

WINDOWS_STEPS = """
Windows program capture (via WinLinx) — MIMIK captures, WinLinx runs:

  1. Launch the Windows program in WinLinx (it owns running Windows programs)
  2. MIMIK points its Wine prefix at this proxy and installs the mitmproxy
     CA into the prefix, so the program's HTTPS traffic can be captured
  3. Use the program normally — its API traffic lands in MIMIK
  4. Stop here restores the prefix's original proxy settings

  Some Windows programs ignore the system proxy or pin their certificates —
  those show no traffic, same as pinned phone apps.
"""


def _print_hosts(rows):
    if not rows:
        print("  (no traffic yet — use the app, then check again)")
        return
    for host, n in rows:
        print(f"  {n:>6}  {host}")


def cmd_record(args):
    target = args.target
    if target == "web":
        print(WEB_STEPS)
        return
    if target == "windows":
        _record_windows(args)
        return

    meta = {}
    plan = None
    app_proc = None
    if target == "file":
        if not args.file:
            sys.exit("record --target file needs --file <path>")
        try:
            plan = launch.prepare_file(args.file, pick=args.pick,
                                       deb_install=args.deb_install)
        except launch.LaunchError as e:
            sys.exit(str(e))
        while plan.needs_pick:
            print(f"\n{plan.message}:")
            for i, c in enumerate(plan.candidates, 1):
                print(f"  {i}. {c}")
            try:
                choice = input("pick a number: ").strip()
            except (EOFError, KeyboardInterrupt):
                sys.exit("\nno program picked — nothing launched")
            try:
                plan = launch.prepare_file(args.file, pick=choice,
                                           deb_install=args.deb_install)
            except launch.LaunchError as e:
                print(f"  {e}")
        print(f"\nwill run:  {plan.command_display}")
        if plan.message:
            print(f"  ({plan.message})")
        meta = {"launched_file": args.file, "kind": plan.kind}
        print("\nUse the program normally. Ctrl-C here stops capture "
              "(and the program).")
    elif target == "linux-app":
        print(LINUX_APP_STEPS)
    else:
        print(ANDROID_STEPS.format(ip=_lan_ip()))

    session = ProxySession(meta=meta, on_update=_print_hosts)
    try:
        session.start()
    except mitm_mod.MitmError as e:
        sys.exit(str(e))
    print("\nproxy up — capturing. Ctrl-C to stop.\n")
    try:
        if plan is not None:
            try:
                app_proc = launch.launch_process(plan)
            except launch.LaunchError as e:
                print(f"could not launch the program: {e}")
            except subprocess.SubprocessError as e:
                print(f"could not launch the program: {e}")
        elif target == "linux-app" and args.launch:
            env = dict(os.environ)
            env.update(launch.proxy_env())
            try:
                app_proc = subprocess.Popen(
                    shlex.split(args.launch), env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                print(f"launched: {args.launch}")
            except (OSError, ValueError) as e:
                print(f"could not launch {args.launch!r}: {e}")
        session.run_forever()
    finally:
        launch.stop_process(app_proc)
        if plan is not None and plan.mount is not None:
            plan.mount.unmount()
            print("unmounted the disk image")
    print("\ncapture saved — run `mimik hosts` to see what was caught.")


def _record_windows(args):
    print(WINDOWS_STEPS)
    ok, msg = wine.check_ready()
    if not ok:
        sys.exit(msg)
    prefix = wine.wine_prefix()
    print(f"Wine prefix: {prefix}")
    print("\ncommands MIMIK will run:")
    print("  wine regedit /s <proxy.reg>     (point prefix at 127.0.0.1:8080)")
    print("  wine certutil -addstore -f -user Root "
          "~/.mitmproxy/mitmproxy-ca-cert.pem")
    print("  (on Stop: the prefix's original proxy settings are restored)")
    session = ProxySession(meta={"target": "windows", "prefix": prefix},
                           on_update=_print_hosts)
    try:
        session.start()
    except mitm_mod.MitmError as e:
        sys.exit(str(e))
    print("\nproxy up — configuring the Wine prefix…")
    try:
        wine.apply_proxy(prefix)
        print("  prefix now points at the proxy")
    except Exception as e:
        print(f"  could not set the prefix proxy: {e}")
    try:
        wine.install_ca(prefix=prefix)
        print("  mitmproxy CA installed in the prefix")
    except Exception as e:
        print(f"  could not install the CA in the prefix: {e}")
    print("\nNow launch the Windows program in WinLinx and use it normally.")
    print("Ctrl-C here stops capture and restores the prefix proxy.\n")
    try:
        session.run_forever()
    finally:
        try:
            wine.revert_proxy(prefix)
            print("prefix proxy settings restored")
        except Exception as e:
            print(f"could not restore the prefix proxy: {e}")
    print("\ncapture saved — run `mimik hosts` to see what was caught.")


# ---- doctor ------------------------------------------------------------------

def cmd_doctor(args):
    ok = True

    def check(name, present, fix):
        nonlocal ok
        mark = "ok " if present else "MISSING"
        print(f"  [{mark}] {name}")
        if not present:
            ok = False
            print(f"          → {fix}")

    print("MIMIK setup check:\n")
    check(f"python {sys.version_info.major}.{sys.version_info.minor} "
          f"(3.10+ needed)", sys.version_info >= (3, 10),
          "install Python 3.12 from your distro")
    try:
        import requests  # noqa: F401

        has_requests = True
    except ImportError:
        has_requests = False
    check("requests library", has_requests, "pip install requests")
    check("proxy (mitmweb or uvx)", mitmweb_cmd() is not None,
          "pip install mitmproxy  — or install uv: "
          "https://astral.sh/uv")
    try:
        import PySide6  # noqa: F401

        has_qt = True
    except ImportError:
        has_qt = False
    check("Qt GUI (PySide6)", has_qt, "pip install PySide6")
    reachable = False
    try:
        mitm_mod.Mitm().flows()
        reachable = True
    except mitm_mod.MitmError:
        pass
    print(f"  [{'ok ' if reachable else '  -'}] mitmweb running + reachable")
    if not reachable:
        print("          → run `mimik record` in another terminal")

    def opt(name, present, fix):
        print(f"  [{'ok ' if present else '  -'}] {name}")
        if not present:
            print(f"          → {fix}")

    print("\noptional:")
    opt("Ollama (optional polish)", bool(codegen.ollama_models()),
        "start Ollama if you want local-AI polish: ollama serve")
    ca = os.path.join(os.path.expanduser("~"), ".mitmproxy",
                      "mitmproxy-ca-cert.pem")
    opt("mitmproxy CA cert", os.path.exists(ca),
        "created automatically the first time you run `mimik record`")
    opt("wine (Windows capture target)", wine.wine_available(),
        "WinLinx brings Wine — install WinLinx to capture Windows programs")
    opt("WinLinx (Windows capture target)", wine.winlinx_available(),
        "install WinLinx to run Windows programs for capture")
    saved = store.list_captures()
    print(f"\nsaved captures: {len(saved)} host(s) under "
          f"{store.captures_dir()}")
    print(f"LAN IP for the phone proxy: {_lan_ip()}:8080")
    sys.exit(0 if ok else 1)


# ---- hosts / learn / gen / clients -------------------------------------------

def cmd_hosts(args):
    _m, flows, source = _flows_from_args(args)
    rows = mitm_mod.hosts(flows)
    if not rows:
        if source in ("har", "curl"):
            sys.exit("no entries in that file")
        sys.exit("no traffic captured yet — run `mimik record` and use the app")
    if source == "saved":
        print("(proxy is off — showing saved captures)\n")
    print(f"{'requests':>9}  host")
    for host, n in rows:
        print(f"{n:>9}  {host}")
    print("\nPick your API host (usually the one with JSON, not media/cdn).")


def cmd_learn(args):
    m, flows, source = _flows_from_args(args)
    eps = _endpoints_from_args(args, m, flows, source)
    if not eps:
        sys.exit(f"no requests to {args.host} found")
    print(f"{args.host}: {len(eps)} endpoints\n")
    for e in eps:
        print(f"  {e['method']:5s} {e['path']}   -> {e['status']}")


def _default_out(host):
    stem = re.sub(r"[^a-z0-9]+", "_", host.split(".")[0].lower()).strip("_")
    return f"{stem or 'app'}_client.py"


def _class_name(source):
    m = re.search(r"class\s+(\w+)\s*\(", source)
    return m.group(1) if m else None


def cmd_gen(args):
    m, flows, source = _flows_from_args(args)
    eps = _endpoints_from_args(args, m, flows, source)
    if not eps:
        sys.exit(f"no requests to {args.host} found")

    if args.prompt_only:
        print(codegen.build_prompt(args.host, eps))
        return

    out = args.out or _default_out(args.host)
    print(f"generating a client from {len(eps)} endpoints "
          f"(polish: {args.polish})…", file=sys.stderr)
    src = codegen.generate(args.host, eps, polish=args.polish,
                           model=args.model)
    import os

    parent = os.path.dirname(os.path.abspath(out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out, "w") as f:
        f.write(src)
    store.save_client_copy(os.path.basename(out), src)
    cls = _class_name(src)
    print(f"\nwrote {out}")
    print(f"\n    from {out[:-3] if out.endswith('.py') else out} "
          f"import {cls or 'Client'}")
    print(f"    acc = {cls or 'Client'}()")
    print("    # then call the generated methods\n")


def cmd_clients(args):
    clients = store.list_clients()
    caps = store.list_captures()
    print("generated clients:")
    if not clients:
        print("  (none yet — run `mimik gen <host>`)")
    for name, path, size in clients:
        print(f"  {name}  ({size} bytes)  {path}")
    print("\nsaved captures:")
    if not caps:
        print("  (none yet — run `mimik record`)")
    for host, n, stamp in caps:
        print(f"  {host}: {n} requests, last saved {stamp}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="mimik", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("record", help="start the proxy + capture a target")
    rp.add_argument("--target", default="android",
                    choices=["android", "linux-app", "file", "web", "windows"],
                    help="what to capture (default: android)")
    rp.add_argument("--file", metavar="PATH",
                    help="program file to launch (with --target file)")
    rp.add_argument("--pick", metavar="N",
                    help="which program to run when a file holds several")
    rp.add_argument("--deb-install", action="store_true",
                    help="install a .deb with apt instead of extracting it")
    rp.add_argument("--launch", metavar="CMD",
                    help="command to launch with proxy env (--target linux-app)")
    rp.set_defaults(func=cmd_record)

    sub.add_parser("doctor", help="check your setup").set_defaults(func=cmd_doctor)

    hp = sub.add_parser("hosts", help="list captured hosts")
    hp.add_argument("--har", metavar="FILE", help="read from a HAR file")
    hp.add_argument("--curl", metavar="FILE",
                    help="read from a cURL paste (file or text)")
    hp.set_defaults(func=cmd_hosts)

    lp = sub.add_parser("learn", help="show endpoints for a host")
    lp.add_argument("host")
    lp.add_argument("--har", metavar="FILE", help="read from a HAR file")
    lp.add_argument("--curl", metavar="FILE",
                    help="read from a cURL paste (file or text)")
    lp.set_defaults(func=cmd_learn)

    gp = sub.add_parser("gen", help="generate a client for a host")
    gp.add_argument("host")
    gp.add_argument("-o", "--out", help="output .py path")
    gp.add_argument("--model", default=None,
                    help="Ollama model for --polish ollama")
    gp.add_argument("--polish", default="none",
                    choices=["none", "ollama", "claude", "opencode"],
                    help="optional polish pass (default: none, deterministic)")
    gp.add_argument("--prompt-only", action="store_true",
                    help="print the prompt instead of generating")
    gp.add_argument("--har", metavar="FILE", help="read from a HAR file")
    gp.add_argument("--curl", metavar="FILE",
                    help="read from a cURL paste (file or text)")
    gp.set_defaults(func=cmd_gen)

    sub.add_parser("clients",
                   help="list generated clients + saved captures"
                   ).set_defaults(func=cmd_clients)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
