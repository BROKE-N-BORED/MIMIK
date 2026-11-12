# MIMIK Changelog

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
