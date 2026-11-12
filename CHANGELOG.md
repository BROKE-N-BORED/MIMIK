# MIMIK Changelog

## v0.2 — 2026-11-12

MIMIK–Reblend bridge: a clone gets its look, not just its code.

- Asset capture from traffic: image, model, audio, and font responses
  are saved as raw bytes (per-host groups under
  `~/.local/share/mimik/assets/`, with an assets.json manifest).
  Works live (raw bytes from mitmweb), from saved captures, and from
  HAR imports. Dedupe by SHA-256; 50 MB per-file / 500 MB per-group
  caps, over-cap files skipped with a note, never a crash.
- Asset capture from program files: when MIMIK launches a program from
  a file, the extracted archive / mounted ISO / extracted .deb is
  scanned for assets and matches are copied out (never moved, nothing
  executed; ISOs copied before unmount).
- Reblend conversion (`mimik/reblend_bridge.py`): finds Reblend
  (picker / `--reblend` / saved setting / auto-detect), loads its own
  `light_engine` by path — no Reblend code copied, Reblend's tree
  untouched. Models (.glb/.obj) convert to `.mesh.json` via Reblend's
  own importer; images become color-only materials (average color)
  with originals kept; audio/fonts kept as originals; `.gltf` kept
  with a "needs .glb" note. Without Reblend, capture and listing
  still work and the pack assembles with originals.
- Asset packs (`mimik build-pack <host>`): scene file (v2, loads in
  Reblend), `assets/`, `textures/`, `originals/`, `pack.json` under
  `~/.local/share/mimik/packs/`. "Open in Reblend" launches
  `main.py --scene <pack scene>`.
- CLI scope: `mimik assets` and `mimik build-pack` accept a host OR a
  program group name (a scanned program folder has no host — its group
  is named after the program). Host matches win, then group name,
  then group folder name.
- GUI: new 6th "Assets" tab — per-capture asset list with conversion
  status, Reblend picker, Build asset pack, Open pack folder, Open in
  Reblend. Doctor (CLI + GUI) reports the Reblend install.
- Learned presets (`mimik/presets.py`): MIMIK teaches itself rules
  from your captures — auth header/cookie NAMES (never values),
  generalized endpoint shapes (ids become {id}), asset URL patterns,
  and the pack scale from the last build. Learned explicitly
  (`mimik presets learn <host|group>`, or the new 7th "Presets" GUI
  tab) or, with the "Learn presets from captures" setting on, when a
  record run stops. A preset changes behavior only where it exists:
  `mimik gen` adds the learned auth section and collapses repeat
  endpoints, `mimik build-pack` reuses the learned scale unless
  `--scale` is given, and the record summary says when a preset was
  used. No LLM, no network, no new dependency; re-learning merges.
- Pattern presets (`mimik/fingerprints.py`): the generalization —
  MIMIK fingerprints a *shape* (OAuth2 flows, JWT bearer auth, API
  keys, session cookies, JSON-RPC, GraphQL, cursor/offset
  pagination, framework/SDK signatures from unambiguous response
  headers, CDN asset shapes) and carries the rules for that shape to
  the next app that shows it, under `presets/patterns/`. Matching is
  conservative: at least two independent signals, or one unambiguous
  one. App presets still win per-section; patterns fill the gaps;
  the generated client names which presets were applied, and
  `pack.json` records `pattern_presets_applied`. New CLI:
  `mimik presets match <host|group>` (dry run). The Presets tab has
  a Pattern presets section (fingerprint name + learned-from count).
- 324/324 tests green (252 app presets + 67 pattern presets + 5
  recorder), verified
  from a fresh unzip; pack scenes load through Reblend's own scene
  loader.
- Installer fix (reported from a real PC): v0.1.x installed WITHOUT
  PySide6 or mitmproxy — they lived in optional extras nothing ever
  requested — so the menu icon silently did nothing (`mimik-gui`
  crashed on import, with no terminal to show the error). PySide6 and
  mitmproxy are now core dependencies, and the installer verifies the
  packages are actually installed before reporting success (with a
  warning, naming the missing library, if the GUI cannot start on the
  machine's system Qt libraries), so a broken install can never look
  finished again. Installing v0.2 over v0.1.x repairs the venv
  in place.
- Record fix (same install family): `mimik record` only looked for
  mitmweb on PATH, but the installer puts mitmproxy in the venv,
  whose bin dir is not on PATH — so record would have failed even
  with mitmproxy installed. MIMIK now also looks next to its own
  Python, so the venv's mitmweb is found.
- Record fix (token discovery): mitmweb buffers its startup lines
  when stdout is a file, so the web-UI token never reached the log
  and every /flows call answered 403 — record could never start.
  mitmweb is now launched unbuffered, so the token is discovered at
  once. A failed start also kills the orphaned mitmweb instead of
  leaving it running.

## v0.1.1 — 2026-11-12

- Fixed the Hosts tab label: Qt renders a bare `&` in tab text as a
  mnemonic marker, so "Hosts & Learn" showed as "Hosts_Learn" in the
  window (the stored string was right, the pixels were wrong). The tab
  is now "Hosts and Learn", and the GUI test forbids `&` in tab titles.

## v0.1 — 2026-11-12

First release. Linux port of littledivy/mimic (MIT, see LICENSE-NOTICE).

- Capture any app, program, or software: Android phone over Wi-Fi,
  Linux desktop app, launch-from-file (AppImage/executable/script,
  archives, read-only ISO mount, .deb), browser HAR export, cURL paste,
  and Windows programs running under WinLinx (Wine prefix proxy + mitm
  CA, restored on Stop — MIMIK never runs Windows programs itself).
- Persistent capture store: hosts/learn/gen and generated clients keep
  working after the proxy stops.
- Deterministic $0 client generator: no LLM, no network, no API key.
  Optional polish via local Ollama or claude/opencode CLI.
- Session/App replay runtime with 401 refresh for idempotent requests.
- Native Qt desktop GUI (Record / Hosts & Learn / Generate / Clients /
  Doctor), dark theme, violet accent.
- Double-click installer + menu launcher + icon; no terminal needed.
- 161/161 tests green from a fresh unzip.
