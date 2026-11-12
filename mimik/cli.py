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
    mimik assets [host]               list captured assets (images, models, ...)
    mimik build-pack <host>           build a Reblend asset pack for a host
    mimik presets                     list learned presets
    mimik presets learn <host>        learn a preset from saved captures
    mimik presets show <name>         show a preset in plain language
    mimik presets match <host>        which pattern presets would apply
    mimik presets delete <name>       delete a preset
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

from . import assets, codegen, launch, presets, reblend_bridge, store, wine
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
        # Assets inside the program's own files (archive extraction,
        # mounted ISO, extracted .deb): copy them out now, while the
        # files are there — for ISOs, before the unmount at the end.
        if plan.work_dir:
            try:
                program = os.path.basename(launch.run_dir_for(args.file))
                gdir, manifest = assets.scan_program_tree(
                    plan.work_dir, program)
                if gdir:
                    kinds = {}
                    for e in manifest["assets"]:
                        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
                    found = ", ".join(
                        f"{n} {assets.KIND_LABELS.get(k, k).lower()}"
                        for k, n in sorted(kinds.items()))
                    print(f"  found {len(manifest['assets'])} asset file(s) "
                          f"in the program ({found}) — saved for the "
                          "asset pack")
            except Exception as e:
                print(f"  (asset scan skipped: {e})")
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
    rows = []
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
        rows = session.run_forever()
    finally:
        launch.stop_process(app_proc)
        if plan is not None and plan.mount is not None:
            plan.mount.unmount()
            print("unmounted the disk image")
    _record_stop_summary(rows)
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
    rows = []
    try:
        rows = session.run_forever()
    finally:
        try:
            wine.revert_proxy(prefix)
            print("prefix proxy settings restored")
        except Exception as e:
            print(f"could not restore the prefix proxy: {e}")
    _record_stop_summary(rows)
    print("\ncapture saved — run `mimik hosts` to see what was caught.")


def _record_stop_summary(rows):
    """After a record run: refresh presets when auto-learn is on, and
    say which saved presets apply to the hosts just captured — app
    presets, and pattern presets the traffic matched."""
    hosts = [h for h, _n in rows]
    for host, refreshed, has in presets.after_record(hosts):
        if refreshed:
            print(f"Preset refreshed for {host}.")
        if has:
            print(f"Used the saved preset for {host}.")
        matched = presets.matched_pattern_ids(host)
        if matched:
            print(f"Matched patterns: {', '.join(matched)}.")


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
    rb = reblend_bridge.find_reblend()
    opt("Reblend (asset packs)",
        rb is not None,
        "point MIMIK at your Reblend folder (Assets tab, or "
        "`mimik build-pack --reblend PATH`)")
    if rb:
        print(f"          Reblend folder: {rb}")
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
    preset, applied, _pattern_ids, auth_label = presets.resolve_for_gen(
        args.host, flows=flows)
    print(f"generating a client from {len(eps)} endpoints "
          f"(polish: {args.polish})…", file=sys.stderr)
    if applied:
        print("presets applied: " + "; ".join(applied), file=sys.stderr)
    src = codegen.generate(args.host, eps, polish=args.polish,
                           model=args.model, preset=preset,
                           preset_label=auth_label, applied=applied)
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


# ---- assets / build-pack --------------------------------------------------------

def _fmt_size(size):
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def cmd_assets(args):
    if getattr(args, "har", None):
        try:
            groups = assets.extract_from_har(args.har)
        except (OSError, ValueError) as e:
            sys.exit(f"could not read that HAR file: {e}")
        if not groups:
            print("no assets in that HAR file")
        for gdir, manifest in groups:
            print(f"{manifest['host']}: saved "
                  f"{len(manifest['assets'])} asset(s) from the HAR")
    if getattr(args, "scan", None):
        if not os.path.isdir(args.scan):
            sys.exit(f"no such folder: {args.scan}")
        program = os.path.basename(os.path.abspath(args.scan)) or "folder"
        gdir, manifest = assets.scan_program_tree(args.scan, program)
        if not gdir:
            print(f"no asset files in {args.scan}")
        else:
            print(f"saved {len(manifest['assets'])} asset(s) from "
                  f"{args.scan}")
    if getattr(args, "refresh", None):
        if not args.host:
            sys.exit("assets --refresh needs a host")
        flows = store.load_host(args.host)
        gdir, manifest = assets.extract_from_flows(
            flows, args.host, host=args.host)
        if not gdir:
            print(f"no assets in the saved captures for {args.host}")
        else:
            print(f"saved {len(manifest['assets'])} asset(s) from the "
                  f"saved captures for {args.host}")

    found = assets.list_assets(**assets.resolve_scope(args.host))
    if not found:
        if args.host:
            print(f"\nno captured assets for {args.host} yet — run "
                  "`mimik record` and use the app, import a HAR with "
                  "`mimik assets --har FILE`, or scan a program folder "
                  "with `mimik assets --scan DIR`")
        else:
            print("no captured assets yet — run `mimik record` and use "
                  "the app, import a HAR with `mimik assets --har FILE`, "
                  "or scan a program folder with `mimik assets --scan DIR`")
        return
    print(f"\n{'size':>9}  kind      name")
    for e in found:
        where = e.get("url") or e.get("source_path") or ""
        print(f"{_fmt_size(e['size']):>9}  {e['kind']:<9} {e['name']}")
        if where:
            print(f"{'':>9}  {'':<9} {where[:100]}")
    print(f"\n{len(found)} asset(s). Build a Reblend pack with: "
          f"`mimik build-pack {args.host or '<host>'}`")


def cmd_build_pack(args):
    scope = assets.resolve_scope(args.host)
    if not assets.list_assets(**scope):
        # Nothing extracted yet — try the saved captures first.
        flows = store.load_host(args.host)
        if flows:
            assets.extract_from_flows(flows, args.host, host=args.host)
            scope = assets.resolve_scope(args.host)
    if not assets.list_assets(**scope):
        sys.exit(f"no captured assets for {args.host} — run `mimik record` "
                 "and use the app, import a HAR with "
                 "`mimik assets --har FILE`, or scan a program folder "
                 "with `mimik assets --scan DIR`")
    print(f"building an asset pack for {args.host}…")
    result = reblend_bridge.build_pack(
        reblend=args.reblend, out=args.out, scale=args.scale, **scope)
    print(f"\npack: {result['pack_dir']}")
    counts = result["counts"]
    print("assets: " + ", ".join(f"{n} {k}"
                                 for k, n in sorted(counts.items()) if n))
    if result.get("preset_applied"):
        print(f"preset: used the saved scale {result['scale']} for "
              f"{args.host}")
    if result.get("pattern_presets_applied"):
        print(f"patterns: used the saved scale {result['scale']} from "
              f"{', '.join(result['pattern_presets_applied'])}")
    if result["reblend_path"]:
        print(f"Reblend: {result['reblend_path']} "
              f"(v{result['reblend_version']})")
        print(f"models converted: {result['models_converted']}")
        if result["scene_path"]:
            print(f"scene: {result['scene_path']}")
    for note in result["notes"]:
        print(f"  note: {note}")
    for w in result["warnings"]:
        print(f"  warning: {w}")
    if result["scene_path"]:
        print("\nOpen it with: "
              f"python3 {result['reblend_path']}/main.py --scene "
              f"{result['scene_path']}")


# ---- presets -------------------------------------------------------------------

def cmd_presets(args):
    """`mimik presets` — list saved presets (+ the auto-learn setting).

    App presets and pattern presets are listed in separate sections:
    app presets are the rules one app taught MIMIK; pattern presets
    are the shapes learned across apps.
    """
    if getattr(args, "auto_learn", None):
        on = args.auto_learn == "on"
        presets.set_auto_learn(on)
        print("Learn presets from captures: "
              + ("on — presets refresh when a capture stops"
                 if on else "off — learn with "
                 "`mimik presets learn <name>`"))
    found = presets.list_presets()
    patterns = presets.list_pattern_presets()
    if not found and not patterns:
        print("no presets yet — record an app, then run "
              "`mimik presets learn <host>`")
    else:
        if found:
            print("App presets:")
            for p in found:
                print(f"  {p['name']} ({p['scope']})")
                print(f"    {presets.summary(p)}")
                learned = p.get("learned_at", "").replace("T", " ")
                if learned:
                    print(f"    last learned {learned}")
        if patterns:
            print("Pattern presets:")
            for p in patterns:
                learned_from = p.get("learned_from") or []
                print(f"  {p['name']} (pattern) — "
                      f"{p.get('display_name') or p['name']}")
                print(f"    learned from {len(learned_from)} "
                      f"app{'s' if len(learned_from) != 1 else ''}")
                print(f"    {presets.summary(p)}")
                learned = p.get("learned_at", "").replace("T", " ")
                if learned:
                    print(f"    last learned {learned}")
    state = "on" if presets.auto_learn_enabled() else "off"
    print(f"\nLearn presets from captures: {state}")


def cmd_presets_learn(args):
    result = presets.learn(args.name)
    patterns = presets.learn_patterns(args.name)
    if result is None:
        if presets.load_preset(args.name) is not None:
            print(f"no new captures for {args.name} — the saved preset "
                  "is unchanged")
        else:
            sys.exit(f"no captures for {args.name} yet — record the app, "
                     "import a HAR, or scan a program folder first, "
                     "then learn")
        return
    preset, _path, created = result
    verb = "Learned" if created else "Refreshed"
    print(f"{verb} a preset for {args.name} ({preset['scope']}): "
          f"{presets.summary(preset)}")
    if patterns:
        names = ", ".join(p["name"] for p, _created in patterns)
        print(f"Learned/updated {len(patterns)} pattern(s): {names}")


def cmd_presets_show(args):
    preset = presets.load_preset(args.name)
    if preset is None:
        preset = presets.load_pattern_preset(args.name)
    if preset is None:
        sys.exit(f"no preset named {args.name} — run `mimik presets` "
                 "to see them")
    print("\n".join(presets.describe(preset)))


def cmd_presets_match(args):
    """`mimik presets match <host|group>` — dry run: which pattern
    presets would apply to this app, and why. Changes nothing."""
    matches, code_signals = presets.match(args.name)
    if not matches and not code_signals:
        print(f"no patterns matched for {args.name} — nothing to apply "
              "yet")
        return
    if matches:
        print(f"patterns {args.name} matches:\n")
        for m, preset, corroborated in matches:
            if preset is not None:
                learned_from = preset.get("learned_from") or []
                line = (f"  {m['id']} ({m['name']}) — saved preset, "
                        f"learned from {len(learned_from)} "
                        f"app{'s' if len(learned_from) != 1 else ''}")
            else:
                line = (f"  {m['id']} ({m['name']}) — no saved preset "
                        "yet (learn one to apply it)")
            print(line)
            for signal in m["signals"]:
                print(f"      signal: {signal}")
            if corroborated:
                print("      corroborated by shared code: "
                      + ", ".join(corroborated))
    if code_signals:
        from . import fingerprints as fp_mod

        print("\ncode signals seen in the program files:")
        for sid in sorted(code_signals):
            name = fp_mod.CODE_SIGNALS.get(sid, {}).get("name", sid)
            files = ", ".join(code_signals[sid])
            print(f"  {name}: {files}")


def cmd_presets_delete(args):
    preset = presets.load_preset(args.name)
    is_pattern = False
    if preset is None:
        preset = presets.load_pattern_preset(args.name)
        is_pattern = preset is not None
    if preset is None:
        sys.exit(f"no preset named {args.name} — run `mimik presets` "
                 "to see them")
    what = f"the pattern preset {args.name}" if is_pattern \
        else f"the preset for {args.name}"
    if not args.yes:
        try:
            answer = input(f"Delete {what}? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print("kept it.")
            return
        if answer.strip().lower() not in ("y", "yes"):
            print("kept it.")
            return
    if is_pattern:
        presets.delete_pattern_preset(args.name)
        print(f"Deleted the pattern preset {args.name}.")
    else:
        presets.delete_preset(args.name)
        print(f"Deleted the preset for {args.name}.")


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

    ap = sub.add_parser("assets", help="list captured assets")
    ap.add_argument("host", nargs="?", default=None,
                    help="host or program group to list assets for "
                    "(default: all)")
    ap.add_argument("--har", metavar="FILE",
                    help="extract assets from a HAR file first")
    ap.add_argument("--scan", metavar="DIR",
                    help="scan a program folder for asset files first")
    ap.add_argument("--refresh", action="store_true",
                    help="re-extract assets from saved captures for the host")
    ap.set_defaults(func=cmd_assets)

    bp = sub.add_parser("build-pack",
                        help="build a Reblend asset pack for a host or "
                        "program group")
    bp.add_argument("host", help="host or program group")
    bp.add_argument("--reblend", metavar="PATH",
                    help="Reblend folder (else auto-detected / saved)")
    bp.add_argument("--out", metavar="DIR",
                    help="pack output dir (default: under the data dir)")
    bp.add_argument("--scale", type=float, default=None,
                    help="model scale in cm per unit (default: the saved "
                    "preset's scale, else 100)")
    bp.set_defaults(func=cmd_build_pack)

    pp = sub.add_parser(
        "presets",
        help="learned presets: rules MIMIK teaches itself from captures")
    pp.add_argument("--auto-learn", choices=["on", "off"],
                    help="turn learning from captures on or off "
                    "(default: off)")
    pp.set_defaults(func=cmd_presets)
    psub = pp.add_subparsers(dest="preset_cmd")
    lp = psub.add_parser("learn",
                         help="learn a preset from saved captures")
    lp.add_argument("name", help="host or program group")
    lp.set_defaults(func=cmd_presets_learn)
    sp = psub.add_parser("show",
                         help="show a preset in plain language")
    sp.add_argument("name", help="host or program group")
    sp.set_defaults(func=cmd_presets_show)
    mp = psub.add_parser("match",
                         help="show which pattern presets would apply "
                         "to an app (dry run, changes nothing)")
    mp.add_argument("name", help="host or program group")
    mp.set_defaults(func=cmd_presets_match)
    dp = psub.add_parser("delete", help="delete a preset")
    dp.add_argument("name", help="host or program group")
    dp.add_argument("--yes", action="store_true",
                    help="skip the confirmation question")
    dp.set_defaults(func=cmd_presets_delete)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
