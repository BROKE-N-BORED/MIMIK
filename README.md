# MIMIK — any app, as a Python library

Intercept any app, program, or piece of software, then call it from Python
like a library.

```python
from hinge_client import Hinge

acc = Hinge()                 # reuses your captured session
recs = acc.get_recs()
acc.like(subject_id, comment="hi lol")
```

You don't write `hinge_client.py`. MIMIK captures your own app traffic and
generates the client from it — with a **deterministic, free generator** (no
LLM, no network, no API key). An optional polish pass can use your local
Ollama model or the `claude` / `opencode` CLI if you have one.

**Own accounts only.** MIMIK replays YOUR session, for YOUR apps and YOUR
accounts. It captures traffic you generate yourself, on your own machine.

MIMIK is a Linux port of [littledivy/mimic](https://github.com/littledivy/mimic)
(MIT) — see LICENSE-NOTICE. Upstream is iPhone→Mac with Claude-only codegen;
MIMIK targets Linux Mint, adds Android + Linux-app + file-based capture, a
persistent capture store (clients keep working after the proxy stops), and
the $0 deterministic generator.

## Install

Unzip, then double-click **Install MIMIK**. (If double-clicking asks you to
trust the launcher, right-click it → Allow Launching, then double-click.)

That creates a venv under `~/.local/share/mimik`, installs requests +
PySide6 + mitmproxy, and puts `mimik` and `mimik-gui` on your PATH
(`~/.local/bin`) plus a MIMIK menu entry.

Terminal way: `bash install.sh` (bash, not sh). Uninstall: `bash uninstall.sh`
— captures and generated clients are kept unless you pass `--purge`.

## Quick start

```bash
mimik doctor            # confirm everything is ready
mimik record            # start the proxy + Android setup steps
mimik hosts             # list captured hosts — pick your API host
mimik learn <host>      # see the endpoints MIMIK caught
mimik gen <host>        # generate <host>_client.py, free, no LLM needed
```

Or open the **MIMIK** GUI from the menu: Record → Hosts & Learn → Generate →
Clients → Doctor. Same flow, no terminal.

Captures are saved under `~/.local/share/mimik/captures/` as they're pulled,
so `hosts`, `learn`, `gen`, and generated clients keep working after the
proxy stops — and after a reboot.

## Capture targets

### Android phone (same Wi-Fi) — `mimik record`

1. Phone → Settings → Wi-Fi → your network → Proxy → Manual.
   Hostname: the LAN IP MIMIK prints. Port: 8080.
2. Phone browser → http://mitm.it → download the Android certificate.
3. Settings → Security → Install a certificate → CA certificate → pick it.
   (Newer Android: Security → Encryption & credentials → Install a
   certificate → CA certificate.)
4. Open the target app and use it normally.

Android 7+ apps only trust user-installed CAs if the app opts in — some apps
show no traffic because of that. Pinned apps (banks, Instagram) show none
at all. If `mimik hosts` shows the app's API host, you're good.

### Linux app — `mimik record --target linux-app`

Launch the app through the proxy:

```
http_proxy=http://127.0.0.1:8080 https_proxy=http://127.0.0.1:8080 <app>
```

or let MIMIK launch it: `mimik record --target linux-app --launch "<app>"`.

Apps using the system CA store need the mitmproxy CA installed — the cert is
at `~/.mitmproxy/mitmproxy-ca-cert.pem` after your first record; install it
with your distro's CA tool (on Mint: copy it to
`/usr/local/share/ca-certificates/` as `.crt` and run
`sudo update-ca-certificates`). Apps with their own cert store may refuse.

### Program from a file — `mimik record --target file --file <path>`

MIMIK captures programs that come as files, not just installed apps:

- **AppImage / executable / script** (`.sh`, `.py`): run directly with the
  proxy env vars. AppImages get `chmod +x` first if needed.
- **Archives** (`.zip`, `.tar.gz`, `.tgz`, …): extracted to
  `~/.cache/mimik/runs/<name>/`, main executable auto-picked (largest,
  top-level first). The GUI lets you pick a different file inside; the CLI
  takes `--pick N`.
- **Disk images** (`.iso`, `.img`): mounted **read-only, never read-write**
  (prefers `udisksctl loop-setup`, falls back to `fuseiso` / `gio`, else
  shows the manual Disk Image Mounter steps). MIMIK lists the executables
  inside and you pick one — nothing from an image ever auto-runs.
  Unmounted on Stop.
- **.deb**: default is `dpkg-deb -x` extraction (no sudo) and running the
  contained binary; `--deb-install` installs via apt with a password prompt
  (pkexec) and launches the installed app.
- **Windows `.exe` / `.msi`**: MIMIK never runs Windows programs — that's
  WinLinx's job. Picking one shows that message. To *capture* a Windows
  program's traffic, see the next section.

MIMIK shows the exact command before running it, Stop kills the launched
program and the proxy, and the capture records which file was launched.
Temp extractions live under `~/.cache/mimik/runs/` (Clear button in the GUI).

### Windows program, via WinLinx — `mimik record --target windows`

**Division of labor:** WinLinx owns *running* Windows programs (Wine,
install, launch, Program Manager). MIMIK owns *capturing* a Windows
program's traffic so you can clone it and rebuild it natively for Linux —
the generated Python client is the first form of that clone.

1. Launch the Windows program in WinLinx (it owns running Windows programs).
2. MIMIK points its Wine prefix at the proxy: it writes the proxy into the
   Wine registry (`HKCU\...\Internet Settings`: ProxyEnable + ProxyServer
   `127.0.0.1:8080`) via a `.reg` file applied with `wine regedit`, and
   installs the mitmproxy CA into the prefix cert store so HTTPS capture
   works. Every command is shown before it runs.
3. Use the program normally — its API traffic lands in MIMIK.
4. Stop restores the prefix's original proxy settings (saved before capture).

Needs `wine` + WinLinx installed and a prefix (created when you first run a
Windows program in WinLinx). Some Windows programs ignore the system proxy
or pin their certificates — those show no traffic, same as pinned phone
apps.

### Web app — no proxy needed

Browser → DevTools (F12) → Network → use the app → right-click the log →
Save all as HAR. Then:

```
mimik hosts --har session.har
mimik gen <host> --har session.har
```

Or copy one request as cURL (right-click → Copy → Copy as cURL):

```
mimik gen <host> --curl request.txt
```

## Generating clients

```
mimik gen <host> [--har FILE] [--curl FILE] [--out FILE]
                 [--polish ollama|claude|opencode|none] [--prompt-only]
```

Default `--polish none`: the deterministic generator. Method names come from
method + path (`GET /rec/v2` → `get_recs`, `POST /like` → `like`), values
that vary per call (ids, text) become parameters, constants stay as
defaults, and **no token or secret ever appears in the output**. The file
subclasses `mimik.App`, pulls auth from live mitmweb or your saved capture,
and compiles cleanly.

- `--polish ollama`: asks your local Ollama (localhost:11434) to improve
  names/docstrings. Uses a small model if one is already there (prefers
  `qwen3.5:2b`) — never downloads anything, never required. Falls back to
  the deterministic output if Ollama isn't running.
- `--polish claude|opencode`: same idea via the CLI, if installed.
- `--prompt-only`: prints the prompt instead of generating.

Generated clients are also copied to `~/.local/share/mimik/clients/`; the
GUI Clients tab lists them with a smoke-test button.

## CLI reference

| Command | What it does |
|---|---|
| `mimik record [--target android\|linux-app\|file\|web\|windows]` | Start the proxy and capture a target |
| `mimik record --target file --file <path> [--pick N] [--deb-install]` | Capture a program from a file |
| `mimik record --target linux-app --launch "<cmd>"` | Launch an app through the proxy |
| `mimik doctor` | Check python, requests, mitmproxy, Qt, Ollama, CA cert, wine/WinLinx |
| `mimik hosts [--har FILE] [--curl FILE]` | List captured hosts |
| `mimik learn <host> [--har FILE] [--curl FILE]` | Show endpoints for a host |
| `mimik gen <host> [...]` | Generate a Python client |
| `mimik clients` | List generated clients + saved captures |

## Honest limits

- **Certificate pinning**: pinned apps show no traffic. MIMIK has no
  unpinning bypass in v0.1 (upstream's Frida `unpin` is not ported).
- **DPoP / request signing**: APIs that bind requests to a device key or
  sign each request can't be replayed from captured headers alone.
- **Android 7+ user CAs**: apps that don't opt in won't trust the mitm CA.
- **Some desktop apps** ignore proxy env vars or use their own cert store.
- **Windows capture** needs WinLinx + wine and can't be verified without a
  real PC run — it needs your real-PC verdict.
- **Cloning in other languages**: v0.1 generates Python only. Other
  languages are the next step after v0.1, not part of this release.

## Files

- `~/.local/share/mimik/captures/` — saved captures (per host + timestamp)
- `~/.local/share/mimik/clients/` — copies of generated clients
- `~/.cache/mimik/runs/` — temp extractions from archives / .debs
- `~/.mitmproxy/mitmproxy-ca-cert.pem` — the mitmproxy CA cert

## Credits

Linux port of [littledivy/mimic](https://github.com/littledivy/mimic)
(MIT, Copyright (c) 2026 Divy Srivastava). See LICENSE-NOTICE for the full
attribution and license text.
