# MIMIK Changelog

## v0.4 — 2026-11-13

Every fix from the fresh-clone repo audit (see
`~/workspace/mimik-review/task-list-repo-audit.md`): the suite and the
installer are green from a fresh clone, not just from the Release zip.
Items in build order:

- Icon PNGs green from a fresh clone (audit #1): the repo holds only
  `icons/mimik.svg` — the GitHub push path can't carry binary, so the
  PNGs are rendered from the SVG at install time (Qt `QSvgRenderer`,
  `rsvg-convert` fallback) and the icon loop never fails the install;
  tests generate their PNG fixture when the icon file is absent.
- Exec bits (audit #2): the push path can't set file modes either, so
  `install.sh` chmods its own scripts (fresh clone: 660 → 770,
  verified) and the Release zip stays the install path — a new test
  pins the zip's stored modes (scripts 0o100755, files 0o100644).
- Capture store: 0700 data dir + atomic writes (audit #3): the data
  dir and its subdirs are created 0700 (existing ones tighten on
  next use); every save — captures, presets, settings, pack/asset
  manifests — writes to a temp file + fsync + atomic rename, so an
  interrupted save leaves the previous good file intact, never a
  partial one (16 new tests, incl. a simulated kill mid-save).
- Front door (audit #4): a LICENSE file with the MIT text (both
  copyright lines — Joshua Lofland + Divy Srivastava), carried in the
  release zip; repo description/topics/homepage are set on the GitHub
  repo page at ship time.
- Docs accuracy + one version source (audit #5): README's 3 stale
  "v0.1" mentions fixed; CHANGELOG's "6th Assets tab" → 4th, the
  wrong "324/324 (252…)" breakdown dropped, and the fresh-unzip claim
  now says what's actually verified (zip, not clone); pyproject.toml
  is the one version source (`__version__` and `build_zip.py` read
  it); the zip default is now `./dist/`; the vestigial
  capture/gui/all extras are gone.
- CI (audit #6): `.github/workflows/ci.yml` runs the suite from a
  fresh checkout on every push to main, in both envs (system Python
  + Qt offscreen) — the guard that would have caught the fresh-clone
  icon defect. NOTE: the snapshot push can't carry workflow files
  (403s), so this file goes in via the GitHub web UI at ship time.
- Window icon set in code (audit #7): the main window (and every
  dialog, via the application icon) carries the MIMIK icon — PNG
  sizes 48/128/256 when present, SVG fallback; verified by rendering
  the window icon's pixmap and matching its pixels to the source PNG.
  Flake fix in passing: the LaunchCheckedTests used a Python child
  that can outlast the 0.3 s settle window under load (3 failures in
  the Qt env) — they now use /bin/sh children (3/3 clean).
- Capture diff (audit #12): `mimik diff A B` (CLI) and File →
  Compare captures (GUI) diff two captures — hosts, capture files,
  or HAR files. New/removed endpoints (paths generalized, so a new
  id isn't a new endpoint) and changed responses (status, or JSON
  top-level keys added/removed — same shape with different values
  is not a change). Verified by screenshot.
- Single-sample flagging (audit #11): endpoints built from one
  captured call are marked in the generated client — a "# Single
  sample" comment on the method, and the module docstring counts
  them ("2 of 3 endpoints come from a single captured sample").
  Sample counts come from the endpoint builders (live, saved, HAR)
  and sum when paths generalize (/users/123 + /users/456 = 2).
- GUI replay button (audit #10): the Hosts and Learn tab has a
  Replay button — pick an endpoint, re-send its captured request
  (same method/URL/headers/body, minus per-request noise), and see
  the response in a dialog next to the original status. State-changing
  methods ask first. Verified against a local stub (200 OK + body
  round-trip, headers forwarded, noise dropped) and by screenshot.
- Dark-mode toggle (audit #9): View → Dark theme switches the whole
  app between the dark theme and a light one (same layout, light
  backgrounds, violet accent); the choice persists across restarts.
  Verified by screenshot — light theme renders with readable dark
  text on light backgrounds.
- Geometry persistence (audit #8): the window reopens where it was
  left — size + position saved to QSettings on close, restored on
  start, with a fallback to a visible spot when the saved geometry
  is entirely off the current screen.
- WebSocket capture + replay (audit #13): mitmproxy sees WS traffic
  and MIMIK now captures it — messages are fetched from mitmweb
  (text exact, binary exact via the hex-dump view), saved with the
  capture, and replayed from the generated client (`*_ws` methods
  calling `Session.ws_replay`, stdlib-only, no new dependency). The
  GUI marks WS endpoints "101 (WS)" and replays them with a
  sent/received transcript. Verified live end-to-end against real
  mitmweb 12.3.1 and a real echo server. Defects fixed in passing:
  the WS client swallowed a frame arriving in the same packet as
  the 101 handshake (now stashed and replayed); an IP host produced
  the invalid class name `127Client` (now `Host127Client`).
- UI copy (U33, design-rules.md): em-dashes removed from all 55
  visible GUI strings (commas, colons, periods instead), pinned by
  a guard test that fails the suite if one comes back.
- Frida unpin port (audit #14): `mimik unpin <target>` and File →
  Unpin a pinned app… defeat certificate pinning so pinned apps can
  be captured — rooted Android (frida-server), stock Android (Frida
  gadget via objection patchapk), and Linux programs. MIMIK fetches
  the maintained upstream scripts, bakes in your mitmproxy CA +
  proxy address, and prints the exact frida command; the script list
  is discovered from the repo at run time, not hardcoded. Doctor
  lists git/frida/objection as optional checks. Honest limits: the
  hooks cover OpenSSL/BoringSSL (Flutter + other TLS stacks still
  show no traffic), and live Frida execution needs a real device or
  app — the suite verifies the orchestration and the pinning premise
  (real-TLS stub), not the hook itself.
- Stop-time screenshot (audit #15): `mimik record --screenshot`, or
  the Record tab checkbox, takes one desktop screenshot when recording
  stops (while the target app is still up) and saves it with the
  capture at `captures/screenshots/screenshot-<timestamp>.png`, for
  reference. Off by default. The GUI grabs with Qt (GUI thread only);
  the CLI uses gnome-screenshot, scrot, or ImageMagick's import. A
  failed screenshot never loses the capture.
- Release-zip fix (found in final verification): the zip excluded
  `tools/` + `.github/`, which left the fresh-unzip suite red (9
  errors: the builder, icon-renderer, and CI tests need those files).
  The zip now carries them, pinned by a test; the fresh-unzip suite
  is green.

## v0.3 — 2026-11-12

Every fix from the v0.2 full review (see `~/workspace/mimik-review/report.md`):
recording is incremental instead of re-saving everything, codegen
generalizes paths by default, the window closes cleanly mid-record, GUI
work runs on worker threads, and everything learned from the review lands
here. Items in build order:

- Incremental poll saving (review #1, #5): the recorder no longer
  re-saves every flow every 5 seconds — only flows whose id isn't
  already saved are written, and only their bodies are fetched
  (3 polls of a 1,501-flow fixture: the captures dir stays flat
  instead of growing 3.6×; a repeat save costs ~40–110 ms of local
  id scanning instead of 4–43 s of HTTP). New `mimik prune` command
  drops old saves, keeping the N most recent per host (default 20,
  asks first unless --yes). Same-second saves get _2/_3 suffixes
  instead of overwriting each other. `list_captures` counts now agree
  with `load_host` (deduped, capped at 5,000/host), and the 5,000 cap
  is visible: `mimik hosts` and the Hosts tab say when a host is
  truncated.
- Generalize paths in codegen by default (review #2): endpoints now
  collapse by generalized shape whether or not a preset exists — a
  first run (no preset learned yet) gets a 1-method client for 1,200
  id-varying flows instead of a 1,200-method, 502 KB monster. The
  preset keeps its role (auth/scale/asset-mapping). `generalize_path`
  moved to shared `mimik/paths.py` (codegen, presets, and fingerprints
  all import it; `codegen.generalize_path` still works).
- Close safety (review #3): the window now has a closeEvent — closing
  mid-record asks "Stop and close" or "Keep recording", then stops the
  worker (proxy, launched program, prefix, mount) with a 10 s wait and
  a terminate + clean-up-from-the-GUI-thread force path, so no orphan
  mitmweb is left on ports 8080/8081. `wine.apply_proxy` no longer
  overwrites an already-saved proxy state, so a double apply preserves
  the ORIGINAL prefix settings; on startup the GUI warns if a saved
  state was never restored and offers to restore it.
- Worker threads + progress/cancel (review #4, #22, #24): host-select,
  Generate (including up to 300 s Ollama polish), Build pack, Learn
  preset, HAR import, program-folder scan, smoke test, and Doctor all
  run off the UI thread now, with a progress label and a Cancel button
  at the bottom of the window; cancelling drops the result. Tabs load
  their data on first show instead of fetching everything at startup,
  and flow fetches are cached (`_flows_cache`, invalidated on Refresh
  and record-stop) instead of re-pulled per action. Closing mid-task
  cancels it and waits briefly; a blocking task defers the close until
  it ends. Two teardown hazards found and fixed along the way: never
  connect a lambda to a Qt signal (heap corruption at teardown), and
  the GUI tests collect garbage deterministically between tests (a
  mid-task cyclic GC freeing QObject wrappers segfaulted the suite).
- Delete actions + disk usage (review #5): captures, clients, asset
  groups, and packs can all be deleted now — `mimik delete --host /
  --client / --group / --pack` (asks first unless --yes; --client
  takes the name with or without the _client.py suffix) and Delete
  buttons on the Hosts, Assets, and Clients tabs (each asks with the
  host/flows/size first). Deleting a host drops the flow cache and
  reloads both host lists; deleting a pack re-wires the Open buttons
  to the next-newest pack. The Doctor (CLI and GUI) now reports disk
  usage per area (captures/clients/assets/packs/presets), and the
  Hosts tab shows each host's saved size. All deletes are guarded to
  their own directory (no traversal out of clients/, assets/, or
  packs/).
- Empty states + first-run order + shorter intros (review #16): every
  list and text area now shows one line of guidance when it's empty
  instead of a blank black box ("No captures yet — record an app, or
  import a HAR on the Generate tab", "No clients yet — generate one
  on the Generate tab", and so on); the Record tab opens with the
  start-here order (Record → Hosts → Generate). The Presets wall of
  text is one short line now; the Generate and Assets intros are
  trimmed to one line each. (QComboBox's own placeholderText never
  reaches the line edit in this Qt build — the Generate host hint is
  set on the line edit directly.)
- Status bar (review #18): a real status bar at the bottom of the
  window, visible on every tab. While recording it shows elapsed
  time plus live flow/host counts ("● Recording — 02:05", "142 flows
  · 3 hosts"), updated every second and on each live-hosts poll;
  idle it shows "Proxy stopped" and the saved-flows total (refreshed
  at startup, record-stop, and host delete). Default window height
  grew 620 → 650 so the bar doesn't squeeze the Record tab.
- HAR import into the capture store + friendly errors (review #6):
  new `mimik import --har FILE` / `--curl FILE` saves the traffic
  into the store like a capture, so the hosts persist for hosts /
  learn / gen / presets after a restart (re-importing the same file
  is a no-op — flow ids are content-hashed, so different files never
  collide and the same file dedupes). The Generate tab's Import HAR
  does the same save. Malformed HAR/cURL now gets a plain message
  instead of a raw traceback (CLI) or silent nothing (GUI): a new
  `har.HarError` covers missing files, bad JSON, and non-HAR JSON,
  and the cURL import warns on a paste with no URL. Folded in: the
  HAR file is parsed once and shared — `har.parse()` returns the
  dict, `load`/`hosts`/`endpoints` all accept it, `Session.from_har`
  parses once, and the CLI reads HAR endpoints from the already
  loaded flows instead of re-parsing the file.
- Collision-safe client filenames + overwrite warning (review #7):
  the default output name now uses the full host, sanitized —
  `api.foo.com` → `api_foo_com_client.py`, `192.168.1.5` →
  `192_168_1_5_client.py` (the old first-label-only name made
  `api.foo.com` and `api.bar.com` silently overwrite each other).
  The CLI numbers on collision (`api_example_com_client_2.py`);
  the GUI asks when the file already exists — Overwrite, Save as
  the numbered copy, or Cancel (cancel still shows the generated
  source so the work isn't lost). Folded in: the redundant
  `import os` inside `cmd_gen` is gone (it made `os` a local and
  broke the new code — item 32a, pulled forward because item 9
  needed it).
- Asset preview + double-click + Hosts→Generate jump (review #15):
  double-click an asset to preview it — images open in a viewer
  (scaled, with dimensions + size), audio opens in the default
  player, models show an info dialog (new `assets.model_info`:
  .obj vertex/face counts, .glb mesh/primitive counts, anything
  else format + size), other kinds show kind + size + path; every
  preview dialog has an Open button. Assets and Clients both get
  "Preview/Open selected…" and "Reveal in folder" buttons, and
  double-clicking a client opens it in the default editor. The
  Hosts tab gets a "Generate client…" button that jumps to the
  Generate tab with the host already picked (it waits for the
  host combo to load if the tab hasn't opened yet).
- Prepare the file plan once (review #9): new
  `launch.pick_candidate(plan, pick)` picks from an already-prepared
  plan's candidates with no re-extraction and no re-mount (the
  docstring promised it; it didn't exist). The CLI pick loop uses it
  instead of calling `prepare_file` again, and the GUI passes its
  plan into the RecordWorker instead of the worker preparing the
  file a second time — the first mount no longer leaks. A stale plan
  (image unmounted after a previous record, or the extraction
  cleared — new `LaunchPlan.is_stale`) is re-prepared once at Start,
  with the user's pick kept. Folded in: `Mount.mount` releases the
  loop device (`loop-delete`) when `udisksctl mount` fails after
  loop-setup succeeded.
- One shared doctor + colored lines (review #10): new `mimik/doctor.py`
  — one `doctor()` returning structured results (required checks,
  mitmweb status, optional checks, info lines), rendered by the CLI
  as plain text and by the GUI as colored HTML. The Qt check now
  really imports PySide6 in both (the GUI hardcoded it True before —
  the same interpreter now gives the same answer in both front-ends,
  verified live). The Doctor tab colors its lines — green passing,
  red missing with its fix, grey optional — so the "green is good,
  red has a fix" header is true at last. `_lan_ip` moved into the
  shared module as `doctor.lan_ip()` (it was the one helper the
  shared check needed from cli.py, and a shared module can't import
  the CLI).
- Default request timeout (review #11): every request from a
  generated client now times out after 30 s (`Session.DEFAULT_TIMEOUT`),
  so a hung server can't hang the user's script forever. Override per
  session (`Session(..., timeout=60)` / `App(timeout=60)`) or per call
  (`session.get("/x", timeout=120)`); `timeout=None` disables it.
  Generated clients inherit it from `App` and their header docstring
  says so.
- No personal strings baked as literals (review #12): only known-safe
  constant shapes are baked into generated clients now (None, booleans,
  small ints, the empty string). Every non-empty string from a request
  body becomes an optional parameter with default None — a body
  `{"name": "Josh", "city": "Dover"}` produces
  `create_profile(self, name=None, city=None, role=None)` with no trace
  of the samples anywhere in the file, so a shared client can't leak
  the user's name or city. Secret-keyed values were already safe and
  still are; the generated header docstring states the guarantee.
- 0600 permissions on sensitive files (review #13): capture files, the
  mitmweb log, and preset files are written owner-only now via a shared
  `store.open_private` (`os.open` with 0600 + a chmod after, so a file
  left 0644 by an older version tightens on the next write to the same
  path). The README notes that captures contain live tokens. (Capture
  files from v0.2 stay 0644 until pruned or deleted — captures are
  append-only, so old files are never re-written.)
- Launch liveness checks (review #14): launched programs and Reblend
  are checked after a 1 s settle now — a program that dies instantly
  comes back as a failure carrying its first 5 stderr lines, never as
  "launched" (new `launch.launch_checked`; stderr goes to a temp file,
  never an unread pipe). `launch_process` raises `LaunchError` with the
  exit code and stderr, which the CLI and the Record worker surface;
  `open_in_reblend` returns (ok, message) the same way and the GUI
  shows it in a warning dialog, opening the pack folder as a fallback.
  Reblend launches under its own `.venv`/`venv` Python when one exists
  (MIMIK's Python may lack Reblend's dependencies); an exit code of 0
  still counts as launched, since some launchers fork and exit.
- Extraction + scan size guards (review #19): `extract_archive` refuses
  an archive over 2 GiB total or 50,000 files before anything is
  written, and measures the bytes again as each member lands (headers
  can lie) — a refused extraction leaves nothing behind, and a
  pre-existing dest is never removed. `scan_program_tree` stops
  descending past 12 levels and stops entirely after 50,000 files
  visited, with a "scan_note" in the manifest saying the result is
  partial; the group cap is checked BEFORE a file is read, so a file
  the group has no room for is never loaded into memory. The GUI's
  folder scan and the Record worker's program scan are both
  cancellable now (Cancel stops the walk and saves what was found so
  far, with a "scan cancelled" note).
- `convert_model` keeps its contract (review #20): the except around
  Reblend's `import_mesh` now catches `Exception`, not just
  `OSError`/`ValueError` — a malformed `.glb` raising `struct.error`,
  `KeyError`, or `IndexError` is a recorded failure, and `build_pack`
  completes with the model marked failed instead of dying on a raw
  traceback.
- Fingerprint once per scope (review #21): learn + pattern learn +
  match used to resolve the scope and load the flows 3 times and
  fingerprint them twice per host. New `presets.learn_all` does the
  whole learn run — app preset, pattern presets, matched ids — in one
  scope resolution, one flow load, one fingerprint pass (measured on
  200 flows: 0.011 s vs 0.061 s for the old separate calls), and
  `after_record` returns the matched ids with the refresh so the
  record-stop summary no longer re-fingerprints per host. The CLI
  learn command and the GUI Learn button use it; `learn` /
  `learn_patterns` / `match` keep working unchanged. Folded in:
  `_Evidence.observe` extracts and parses each flow body once per
  side (it re-parsed request bodies 3–4 times) — a flow with JSON
  request + response bodies is now 2 `json.loads` calls, not 3.
- `--version` + About (review #26): `mimik --version` prints
  `mimik 0.3.0`; the window title shows the version and the Doctor
  tab has an About button opening an About dialog with the version,
  a one-paragraph description, and the littledivy/mimic attribution.
  Both read `mimik.__version__` (which was still 0.2.0 — fixed), and
  a test pins it to pyproject's version so they can't drift again.
- Record-tab wording points at GUI tabs (review #17): the Record
  instructions no longer send a GUI user to the terminal — Android
  and Linux-app steps end at "open the Hosts and Learn tab", the
  web steps point at the Generate tab's Import HAR… / Import cURL…
  buttons, and the Linux-app launch line points at the "Linux app
  to launch" box above it. The CLI keeps its own terminal wording.
  Same fix on the Doctor tab's mitmweb hint (now "press Start proxy
  on the Record tab"). A test cycles all five targets and asserts no
  `mimik` command appears in any of them.
- Search/filter on lists (review "missing entirely"): a Filter box
  above the Hosts endpoints table, the Assets table, and the Clients
  list — typing filters live (case-insensitive substring over every
  visible column), with an "N of M" count while a filter is active.
  Empty-state rows stay visible so a filtered list never goes
  silently blank, and the filter re-applies after every refresh.
- Log file + crash handler (review #27): a new `mimik.log` module
  writes timestamped entries to `<data>/mimik.log` (0600, capped at
  ~1 MB with oldest-half rotation, and logging itself never raises).
  All 17 truly-swallowed `except Exception: pass/continue` sites now
  log their error with a traceback instead of dropping it — the only
  bare swallows left are inside the logger itself, which has nowhere
  to log to. The GUI installs a crash handler: an uncaught exception
  is logged and the user gets a dialog naming the error and the log
  path, instead of the failure vanishing (a menu launch has no
  stderr). The Doctor tab has a View log button showing the log's
  tail in-app.
- Extra asset kinds (review #25): video (`.mp4`, `.webm`), game
  textures (`.dds`, `.ktx2`), and more model formats (`.fbx`, `.dae`)
  are now detected by extension and Content-Type, shown with their
  own Kind labels, and kept. In a pack, video and textures are kept
  as originals with honest notes (no Reblend video support; convert
  textures to PNG for materials), and `.fbx`/`.dae` models are kept
  as originals with the Blender import-then-export-.glb route —
  meshio can't import them, so they no longer produce a spurious
  conversion failure. Video previews open in the default player,
  like audio.
- Fixed a racy scan-cancel test: it cancelled on a bare event-loop
  turn, which doesn't prove the worker thread started — under load
  the cancel landed before the first file and the scan correctly
  returned no group. It now waits for the first copied file before
  cancelling.
- Client construction no longer re-saves flows (review #23):
  `Session.from_mitm` used to save every flow it pulled, so each
  `Hinge()` rewrote captures the recorder had already saved on its
  last poll. Construction is now cheap and side-effect-free.
- Capture filtering (review "missing entirely"): noisy hosts can be
  excluded from a capture, so telemetry and analytics never reach
  the store or the host list. A new `mimik.filters` module matches
  exact hosts and subdomains (with a dot boundary, so
  `evilgoogle-analytics.com` is not a match); the list lives in
  settings.json with 12 conservative telemetry/analytics/crash/ads
  defaults — CDNs are deliberately not defaulted, since they serve
  real assets. The filter applies to every live-flow path (recorder
  poll + asset extraction, GUI live fetch, CLI live fetch) before
  saving and before the host list is derived; the recorder logs the
  dropped hosts when the set changes. Per-record control is the
  Record tab's "Exclude telemetry + analytics hosts" checkbox
  (on by default) plus a Manage dialog (add/remove/restore
  defaults); `mimik exclude list/add/remove/reset/on/off` does the
  same from the CLI. HAR/cURL imports are not filtered — an explicit
  import is the user's choice.
- Pack/preset export-import (review "missing entirely"): a backup /
  restore story that doesn't need copying files by hand. A new
  `mimik.export` module writes zip exports carrying a
  `mimik-export.json` marker (kind + version, so import can validate
  the file is a MIMIK export and route it). A preset exports as its
  JSON file; a pack exports as its whole directory. Import refuses
  to overwrite an existing preset/pack unless asked (the GUI asks
  "replace?"; the CLI takes --force, and a forced pack import
  replaces the directory wholesale so stale files don't survive).
  Extraction is traversal-checked — no absolute paths, no `..`,
  nothing lands outside its target dir — and the payload is
  validated (preset JSON parses and names itself; a pack holds a
  valid pack.json). GUI: Export selected… / Import… on the Presets
  tab, Export pack… / Import pack… on the Assets tab. CLI:
  `mimik presets export/import`, `mimik pack list/export/import`
  (export by pack dir name, app, or host — newest when several
  match). Export -> delete -> import round-trips a preset and a
  pack byte-for-byte, covered by tests.
- First-run onboarding (review "missing entirely"): a sample capture
  + guided first client, so a new user can try the whole flow
  without recording anything first. A new `mimik.sample` module
  holds a 6-flow capture of a fictional game API
  (demo.example.com — a reserved name, all values plainly fake):
  sign-in POST, id-varying user/item GETs (so generalization shows),
  and a 1x1 PNG asset. Loading it saves the flows, extracts the
  asset, and learns a preset; loading twice is a no-op (flows
  dedupe by id, no duplicate asset group). On a fresh data dir the
  app offers the sample once at startup ("Load the sample capture"
  / "No thanks" — declining marks it offered, it never pops again);
  accepting shows the guide, whose "Take me to Generate" jumps to
  the Generate tab with the sample host pre-selected, so one press
  of Generate produces the first working client. The Doctor tab's
  "Load sample capture" button reloads it any time. `mimik sample`
  does the same from the CLI. The guided client is covered by a
  test that imports the generated DemoClient and calls its methods
  against a stubbed session. (Also fixed while here: the status-bar
  flow count now re-counts after a sample load — it used to sit at
  0 until a record-stop.)
- Smoke-test exec hardening (review security notes): the Clients tab
  smoke test used to `exec_module` whatever Python file was selected
  from clients/ — a foreign file dropped there would run with the
  user's privileges. Generated clients now carry a marker
  (`codegen.GENERATED_MARKER`, the header line the generator writes)
  and `codegen.is_generated_client` verifies it; a file without the
  marker gets an explicit confirmation dialog naming the file and
  saying the smoke test runs its code — No refuses (logged in the
  smoke log), Yes runs it. Unreadable or binary files read as
  foreign. Tests prove a declined foreign file never executes (its
  marker file never appears) while a generated client still
  smoke-tests with no dialog at all.
- Keyboard shortcuts + menu bar (review "missing entirely"): the app
  had no menus at all, only buttons. Now File / View / Help:
  File has Import HAR… / Import cURL… (the Generate tab imports,
  callable from any tab) and Quit (Ctrl+Q, through the same
  closeEvent that stops a recording cleanly); View has a jump to
  every tab (Ctrl+1..7) and Refresh (F5), which re-reads whichever
  tab is showing; Help has About MIMIK (the item-20 dialog) and
  View log (item 23). All actions connect to bound methods — the
  tab jumps read their index from the action's data, no lambdas on
  signals.
- In-app update check (review "missing entirely"): a new
  `mimik.update` module asks GitHub's releases API for the latest
  release and compares versions numerically (0.10.0 > 0.9.0 — not
  a string compare). Manual/opt-in only — nothing runs on startup
  or on a timer; the user asks from Help > Check for updates… or
  the Doctor tab's button. The check runs off the UI thread; a
  newer release offers to open the releases page in the browser,
  up-to-date says so, and every failure (no network, bad JSON, no
  version tag) is a warning, never a crash. `mimik update-check`
  does the same from the CLI. Verified live against the real API
  (latest v0.2, current 0.3.0, correctly no update) as well as
  against mocked fixtures.
- Cleanup pass (review code smells + small security/docs notes):
  (a) `cmd_gen`'s re-import of `os` was already gone — verified by
  grep + an AST scan of cli.py, no change needed. (b) One shared
  flow fetch: a new `mimik.flows.fetch_flows` holds the live-first /
  saved-fallback logic (capture filter + store save + log labels
  preserved); the CLI adds its HAR/cURL branches and empty-store
  error, the GUI adds its cache — and the CLI's now-unused `log`
  import went with it. (c) One header normalizer:
  `extract.header_pairs` / `header_dict` / `flow_header_pairs`
  replace the duplicated `_header_pairs` (presets), `_pairs`
  (fingerprints), and `_headers_dict` (assets); mitm's copy was
  dead code with no callers and is deleted. `session_headers`
  uses the normalizer too, so a malformed header pair can't crash
  it anymore. (d) The launch-command box is a QLineEdit — a
  one-line shell command, not a multi-line editor (the 30 px height
  hack went with the QTextEdit). (e) `session_headers` no longer
  replays request-scoped x- headers: x-request-id,
  x-correlation-id, x-trace-id, x-span-id, x-parent-span-id,
  x-amzn-trace-id, x-b3-*, x-forwarded-*, x-real-ip join the DROP
  list; identity-ish x- headers (x-device-id, x-session-id,
  x-api-key) still replay. (f) Preset files carry a validated
  version: `PRESET_VERSION = 1` is the single source (all five
  literal stamps use it), all four loaders read through
  `_read_preset_file`, which migrates a missing version to 1 in
  memory and refuses any other version — a read never rewrites a
  user's file. (g) Mitm passes `proxies={"http": None,
  "https": None, "all": None}` on every request instead of relying
  on NO_PROXY — per-request, not session-level, because requests
  strips None values when merging session proxies (probed on
  2.31.0; the session attribute would silently do nothing), and
  with "all" included because requests' select_proxy falls back to
  the "all" key, so an ALL_PROXY env var would otherwise still
  route localhost through the proxy. (h) README: one
  line that a generated client's 401-refresh re-authenticates from
  the live proxy and retries once.

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
- GUI: new 4th "Assets" tab — per-capture asset list with conversion
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
- 324/324 tests green, verified from a fresh unzip of the Release zip
  (not from a fresh clone — the repo's missing icon PNGs broke 18
  tests from a clone, fixed in v0.4); pack scenes load through
  Reblend's own scene loader.
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
