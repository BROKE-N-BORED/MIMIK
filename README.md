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

From a git clone: every file arrives without exec bits (the GitHub push
path can't set file modes), so run `bash install.sh` — the installer
fixes the scripts' bits itself. The Release zip is the install path:
its scripts arrive executable, so double-clicking Install MIMIK works
straight from the unzip.

## Quick start

```bash
mimik doctor            # confirm everything is ready
mimik record            # start the proxy + Android setup steps
mimik hosts             # list captured hosts — pick your API host
mimik learn <host>      # see the endpoints MIMIK caught
mimik gen <host>        # generate <host>_client.py, free, no LLM needed
```

Or open the **MIMIK** GUI from the menu: Record → Hosts and Learn → Generate
→ Assets → Clients → Doctor → Presets. Same flow, no terminal.

Captures are saved under `~/.local/share/mimik/captures/` as they're pulled,
so `hosts`, `learn`, `gen`, and generated clients keep working after the
proxy stops — and after a reboot. Captures contain live session tokens:
capture files, preset files, and the mitmweb log are all written with
0600 permissions (owner only), so don't share them or loosen them.

Stop-time screenshot (v0.4): `mimik record --screenshot`, or the
"Screenshot the desktop when recording stops" checkbox on the Record
tab, takes one desktop screenshot when recording stops (while the
target app is still up) and saves it with the capture at
`~/.local/share/mimik/captures/screenshots/screenshot-<timestamp>.png`,
for reference. Off by default. The GUI uses Qt; the CLI uses
gnome-screenshot, scrot, or ImageMagick's import, whichever is
installed. A failed screenshot never loses the capture.

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
and compiles cleanly. If a call gets a 401, the client re-authenticates
from the live proxy (fresh headers from mitmweb) and retries once — so
keep the proxy running while you use it against a live app.

- `--polish ollama`: asks your local Ollama (localhost:11434) to improve
  names/docstrings. Uses a small model if one is already there (prefers
  `qwen3.5:2b`) — never downloads anything, never required. Falls back to
  the deterministic output if Ollama isn't running.
- `--polish claude|opencode`: same idea via the CLI, if installed.
- `--prompt-only`: prints the prompt instead of generating.

Generated clients are also copied to `~/.local/share/mimik/clients/`; the
GUI Clients tab lists them with a smoke-test button.

If a **preset** was learned for the host (see below), the generated client
uses it: the learned auth section (header/cookie names, sign-in endpoint)
appears in the class, and repeat endpoints collapse by their generalized
shape (`/api/users/123` and `/api/users/456` become one `/api/users/{id}`
method). With no preset, generation is exactly as before.

## Assets and Reblend packs (v0.2)

A clone gets its look, not just its code. When MIMIK captures an app or a
game, it also saves the assets the thing is made of — images, 3D models,
audio, fonts — and can build them into a **Reblend asset pack**: the
models converted to Reblend meshes, a scene file that opens in Reblend,
and the originals kept alongside.

Where assets come from:

- **Traffic**: image, model, audio, and font responses in a capture are
  saved as raw bytes when the proxy stops (live mitmweb preferred), and
  from saved captures or HAR imports on demand. Nothing is re-encoded.
- **Program files**: when MIMIK launches a program from a file, it scans
  the extracted archive, mounted disk image, or extracted `.deb` for the
  same asset types and copies them out (never moves, never runs
  anything). For ISOs the copy happens before the unmount.

```bash
mimik assets <host>              # list captured assets for a host
mimik assets --har session.har   # extract assets from a HAR file
mimik assets --scan ~/game-dir   # scan a program folder for assets
mimik build-pack <host>          # build a Reblend asset pack
```

Or use the GUI **Assets** tab: pick a capture, see its assets and their
conversion status, point MIMIK at your Reblend folder, and hit **Build
asset pack**. **Open in Reblend** opens the pack scene straight in
Reblend (`main.py --scene <pack scene>`).

A pack lives at `~/.local/share/mimik/packs/<app>_<timestamp>/`:

```
<app>.json     Reblend scene (v2) — one mesh object per converted model
assets/        converted .mesh.json files (via Reblend's own importer)
textures/      image originals
originals/     model / audio / font originals
pack.json      manifest: sources, conversions, warnings, materials
```

Pointing MIMIK at Reblend: the Assets tab has a folder picker, or
`mimik build-pack --reblend <path>`. MIMIK also auto-detects Reblend in
`~/.local/share/reblend*`, `~/Desktop/reblend*`, and `~/reblend*`. MIMIK
loads Reblend's own `light_engine` package by path and calls its own
importer — no Reblend code is copied into MIMIK, and Reblend's tree is
never touched. Without a Reblend install, capture and listing still work
and the pack still assembles with originals; conversion just waits until
Reblend is found.

Honest asset limits (these are Reblend's limits, not MIMIK hiding things):

- **Images are color-only materials.** Reblend materials use procedural
  textures only — there is no image-file texture support in scene v2.
  Each image becomes a material entry with its average color, and the
  original file is kept in the pack's `textures/` folder.
- **Audio and fonts are kept, not wired in.** Reblend has no audio or
  font support, so those originals travel in the pack for the clone but
  don't appear in the scene. The pack manifest says so plainly.
- **`.gltf` needs `.glb`.** Reblend's importer reads `.glb` and `.obj`
  only. A `.gltf` file is kept as an original with a note to convert it
  in Blender (File → Export → glTF 2.0). MIMIK writes no glTF parser.
- **Size guards**: 50 MB per file, 500 MB per capture group. Over-cap
  files are skipped with a note in the manifest, never a crash.
- **Heavy meshes warn, never refuse**: over 1M triangles gets a warning
  in the pack, and the import still goes through.

## Learned presets (v0.2)

MIMIK teaches itself rules from your captures, so its behavior gets
slowly programmed instead of needing AI for everything. A **preset** is
a small JSON file, learned by pure rule extraction — no LLM, no
network, no new dependency. There are two layers:

- **App presets** — rules for one app, host, or program, learned from
  its own captures and applied to that app. One JSON file per host or
  program under `~/.local/share/mimik/presets/`.
- **Pattern presets** — rules for a *shape* MIMIK has seen before,
  across apps. MIMIK fingerprints each app's captures — the sign-in
  flow, the API style, the asset shapes — and when a *different* app
  shows the same shape, the pattern preset applies there too. One
  JSON file per fingerprint under
  `~/.local/share/mimik/presets/patterns/`.

What a preset holds:

- **Auth**: the header and cookie NAMES that carry tokens (e.g.
  `Authorization` with scheme `Bearer`, cookie `session_id`), and the
  sign-in endpoint shape when one is visible. **Never the values** —
  token and cookie values are dropped at extraction time and can never
  appear in a preset file.
- **Endpoints**: generalized shapes with a seen count (`/api/users/123`
  and `/api/users/456` become `/api/users/{id}`; numeric, UUID, and
  long-hex segments generalize, short literal segments stay).
- **Asset patterns**: which URL/path patterns carry which asset kinds
  (e.g. `/m/*.glb` → 3D model).
- **Pack settings**: the model scale from that app's last built pack.
- **Pattern presets also hold**: which apps the pattern was learned
  from (`learned_from`, names only) and the fingerprint that defines
  it (id, plain name, signals observed).

### How fingerprint matching works

A fingerprint is a small set of traffic-derived signals with a stable
id and a plain name. The set MIMIK recognizes (`mimik/fingerprints.py`
documents each one with exactly what triggers it):

- **Auth algorithms**: `oauth2-code` (authorization-code flow),
  `oauth2-client-credentials`, `jwt-bearer` (Bearer tokens with the
  3-part JWT *shape* — shape only, the token itself is never stored),
  `basic-auth`, `api-key-header`, `session-cookie`.
- **Request shapes**: `json-rpc`, `graphql`, `cursor-pagination`,
  `offset-pagination`.
- **Framework/SDK signatures**, from unambiguous response headers
  only: `express`, `aspnet`, `php`, `stripe-api`.
- **Asset patterns**: `cdn-assets` (models/images served from
  CDN-shaped paths).

Scanned program files add a weak corroborating signal only — the
presence of a well-known engine/library file *by name* (e.g. a Unity
or Godot runtime) — and a code signal never creates a fingerprint on
its own.

**Matching is conservative.** A fingerprint needs at least two
independent signals, or one unambiguous one (`grant_type=`
`authorization_code` in a token request is one; a `jsonrpc` + `method`
body is another). One stray header never claims a pattern: a Bearer
token that is not JWT-shaped does not fingerprint `jwt-bearer`, a lone
API key header claims nothing alone, a single cursor value is not
pagination.

**Which preset wins.** App presets win, per section. A pattern preset
fills a section the app preset lacks — e.g. the app preset has
endpoints but no auth, so the pattern's auth applies. The generated
client carries a comment naming which presets were applied (app vs
pattern), and `pack.json` records `pattern_presets_applied`.

```bash
mimik presets                    # list app + pattern presets
mimik presets learn <host>       # learn (or refresh) app + pattern presets
mimik presets show <name>        # show a preset in plain language
mimik presets match <host>       # which patterns would apply (dry run)
mimik presets delete <name>      # delete a preset (asks first; --yes skips)
mimik presets --auto-learn on    # refresh presets when a capture stops
```

Learning is explicit by default — you run `learn` (or press **Learn
preset** on the GUI **Presets** tab) when you want it. Re-learning
merges: new endpoints are added, old ones are kept with updated counts,
and a pattern preset's `learned_from` list grows as more apps show
the same shape. The **Learn presets from captures** setting (default
OFF, in the GUI Presets tab or `mimik presets --auto-learn on`) also
refreshes app and pattern presets when a record run stops.

What a preset changes (nothing changes where no preset exists):

- `mimik gen` adds the learned auth section to the generated client
  and collapses repeat endpoints by their generalized shape.
- `mimik build-pack` reuses the learned scale unless `--scale` is
  given, and records the scale used plus "preset applied" in `pack.json`.
- The record summary says when a preset was used for a captured host
  ("Used the saved preset for \<host\>"), and names the patterns the
  traffic matched ("Matched patterns: jwt-bearer, cursor-pagination.").

## CLI reference

| Command | What it does |
|---|---|
| `mimik record [--target android\|linux-app\|file\|web\|windows]` | Start the proxy and capture a target |
| `mimik record --target file --file <path> [--pick N] [--deb-install]` | Capture a program from a file |
| `mimik record --target linux-app --launch "<cmd>"` | Launch an app through the proxy |
| `mimik record --screenshot` | Screenshot the desktop when recording stops (saved with the capture) |
| `mimik doctor` | Check python, requests, mitmproxy, Qt, Ollama, CA cert, wine/WinLinx |
| `mimik hosts [--har FILE] [--curl FILE]` | List captured hosts |
| `mimik learn <host> [--har FILE] [--curl FILE]` | Show endpoints for a host |
| `mimik gen <host> [...]` | Generate a Python client |
| `mimik clients` | List generated clients + saved captures |
| `mimik assets [host] [--har FILE] [--scan DIR] [--refresh]` | List captured assets (images, models, audio, fonts) |
| `mimik build-pack <host> [--reblend PATH] [--out DIR] [--scale N]` | Build a Reblend asset pack |
| `mimik presets [--auto-learn on\|off]` | List app + pattern presets + the auto-learn setting |
| `mimik presets learn <host\|group>` | Learn (or refresh) app + pattern presets from saved captures |
| `mimik presets show <name>` | Show a preset in plain language |
| `mimik presets match <host\|group>` | Show which pattern presets would apply (dry run) |
| `mimik presets delete <name> [--yes]` | Delete a preset |
| `mimik unpin <target> [--android\|--linux] [--ca PATH] [--proxy-host HOST] [--workdir DIR]` | Defeat cert pinning via Frida so a pinned app can be captured |

## Pinned apps — `mimik unpin` (v0.4)

A pinned app rejects the proxy's certificate, so it shows no traffic.
`mimik unpin` fixes that app-side: it fetches the maintained Frida
unpinning scripts (httptoolkit/frida-interception-and-unpinning),
bakes your mitmproxy CA + proxy address into them, and prints the
exact `frida` command to run. Three targets:

- **Android, rooted**: `mimik unpin com.example.app` — attaches over
  USB to frida-server on the phone and spawns the app.
- **Android, stock**: `mimik unpin app.apk` — injects the Frida gadget
  into the APK (`objection patchapk`), you install the patched APK,
  then attach to the gadget. Installing it replaces the original
  (different signature), so uninstall the original first.
- **Linux program**: `mimik unpin /usr/bin/app` — attaches Frida to a
  local program.

Also in the GUI: File → Unpin a pinned app… `mimik doctor` lists the
tools it needs (git, frida, objection) as optional checks. The hooks
cover OpenSSL/BoringSSL; Flutter apps and apps on another TLS stack
still show no traffic (see Honest limits). Your own apps on your own
devices only.

## Honest limits

- **Certificate pinning**: pinned apps show no traffic unless unpinned
  first — see `mimik unpin` above. The Frida hooks cover
  OpenSSL/BoringSSL; Flutter apps (private BoringSSL, proxy ignored)
  and apps on another TLS stack (NSS, GnuTLS, Rust) still show no
  traffic. Live Frida execution needs a real device or a real Linux
  app and hasn't been verified in a headless test.
- **DPoP / request signing**: APIs that bind requests to a device key or
  sign each request can't be replayed from captured headers alone.
- **Android 7+ user CAs**: apps that don't opt in won't trust the mitm CA.
- **Some desktop apps** ignore proxy env vars or use their own cert store.
- **Windows capture** needs WinLinx + wine and can't be verified without a
  real PC run — it needs your real-PC verdict.
- **Cloning in other languages**: MIMIK generates Python only. Other
  languages come later (multi-language codegen is queued, pending a
  language pick), not part of this release.
- **Asset packs**: images become color-only materials (Reblend has no
  image textures), audio/fonts are kept but not wired into scenes,
  `.gltf` needs a `.glb` conversion in Blender. See the Assets section.

## Files

- `~/.local/share/mimik/captures/` — saved captures (per host + timestamp)
- `~/.local/share/mimik/clients/` — copies of generated clients
- `~/.local/share/mimik/assets/` — captured assets (per host/program + timestamp)
- `~/.local/share/mimik/packs/` — built Reblend asset packs
- `~/.local/share/mimik/presets/` — learned presets (one JSON file per host/program; pattern presets under `patterns/`)
- `~/.local/share/mimik/settings.json` — settings (auto-learn presets: off by default)
- `~/.local/share/mimik/reblend.json` — remembered Reblend folder
- `~/.cache/mimik/runs/` — temp extractions from archives / .debs
- `~/.mitmproxy/mitmproxy-ca-cert.pem` — the mitmproxy CA cert

## Credits

MIMIK is released under the MIT License — see LICENSE. Linux port of
[littledivy/mimic](https://github.com/littledivy/mimic) (MIT, Copyright
(c) 2026 Divy Srivastava); see LICENSE-NOTICE for the full attribution.
