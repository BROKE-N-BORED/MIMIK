"""How to Use index (v0.5 item 13, design rule U22).

Plain-language index of every tab and flow, shown in its own window
from the Help menu. No manual needed (U19). The text lives here, in
one place, so the GUI and any future help surface share it.

U33: no em-dashes in any of these strings — they are shown in the UI.
"""
TOPICS = [
    ("start",
     "Start here: Record, pick, generate",
     "MIMIK turns an app you use into a Python library you can call "
     "from your own scripts. Three steps:\n\n"
     "1. Record the app (Record tab). MIMIK watches its network "
     "traffic while you use it.\n"
     "2. Pick a host (Hosts and Learn tab). The host is the server "
     "the app talks to, usually the one with JSON, not media or CDN.\n"
     "3. Generate a client (Generate tab). MIMIK writes a Python "
     "file that calls the same API with your real login.\n\n"
     "Your own apps and accounts only. MIMIK replays YOUR session, "
     "for YOUR apps."),
    ("record",
     "Record tab",
     "Pick a target, press Start, use the app, press Stop.\n\n"
     "Android phone: point the phone's Wi-Fi proxy at this computer "
     "(the tab shows the address), install the MIMIK certificate on "
     "the phone once, then use the app.\n\n"
     "Linux app: pick a running desktop app, or launch a program "
     "from a file (AppImage, executable, or script).\n\n"
     "Windows program: MIMIK captures traffic from a Windows program "
     "already running under WinLinx. MIMIK never installs or runs "
     "Windows programs itself.\n\n"
     "Web app: import a HAR file or paste a cURL command instead of "
     "recording live.\n\n"
     "The live strip shows flows captured, hosts seen, bytes, and "
     "elapsed time while you record. Stop saves everything, and the "
     "proxy settings are put back the way they were."),
    ("hosts",
     "Hosts and Learn tab",
     "Every server MIMIK saw traffic for, live from the proxy or "
     "from saved captures.\n\n"
     "Select a host to see its endpoints: the requests the app made, "
     "with method, path, and status.\n\n"
     "Generate client jumps to the Generate tab with that host "
     "already picked.\n\n"
     "Link binary pairs a host with an analyzed program file (Binary "
     "tab): the same app, two views, traffic and code. If you "
     "recorded by launching a file, the link is made automatically; "
     "otherwise Link binary does it by hand.\n\n"
     "Replay selected re-sends one request and shows the response. "
     "Delete selected host removes that host's saved captures for "
     "good."),
    ("generate",
     "Generate tab",
     "Pick a host and press Generate. MIMIK writes a Python client "
     "for that host's API: one method per endpoint, with your real "
     "login headers baked in, so the server cannot tell your script "
     "from the app.\n\n"
     "The client is deterministic: same traffic, same file, no AI, "
     "no network, no key needed. An optional polish pass (Ollama, or "
     "Claude if you have it) can tidy the code afterwards.\n\n"
     "Learn preset teaches MIMIK the host's patterns (how it pages, "
     "how it names things) so later clients for similar hosts come "
     "out better.\n\n"
     "Import HAR and Import cURL bring in traffic captured "
     "elsewhere, so you can generate a client without recording."),
    ("assets",
     "Assets tab",
     "Images, models, audio, and other files MIMIK saw the app "
     "download, grouped by host.\n\n"
     "Select a group to see its files, preview an image or play a "
     "sound, and save the ones you want.\n\n"
     "Build pack turns a host's assets into a Reblend pack (a scene "
     "file Reblend can open), if you have Reblend installed."),
    ("clients",
     "Clients tab",
     "Every Python client MIMIK has generated, plus your saved "
     "captures.\n\n"
     "Open a client to read it, or run a smoke test: MIMIK imports "
     "the client and calls one harmless read-only method to prove "
     "it works.\n\n"
     "A client file is plain Python. Copy it into your own project "
     "and use it like any library."),
    ("binary",
     "Binary tab",
     "Analyze a program file: what it is, what it calls, what words "
     "it contains. MIMIK reads the file, it never runs it.\n\n"
     "Analyze a file runs the built-in pass: format, sections, "
     "imports and exports, strings, and packed-section warnings. "
     "That works with nothing else installed.\n\n"
     "Run Ghidra pass adds decompiled C, disassembly, functions, "
     "and a call graph, using Ghidra (free, from the NSA). Install "
     "Ghidra first: Doctor tab, Ghidra setup steps. Without it, "
     "those views say so honestly instead of pretending.\n\n"
     "Strings shows every readable word in the file and, with "
     "Ghidra, which functions use each one.\n\n"
     "Delete analysis removes MIMIK's saved analysis. The program "
     "file itself is never touched."),
    ("doctor",
     "Doctor tab",
     "The setup check: green is good, red has a fix written next "
     "to it.\n\n"
     "Required lines must pass for MIMIK to work (Python, requests, "
     "the proxy, Qt). Optional lines are extras: Ollama, Wine and "
     "WinLinx, Reblend, the unpinning tools, and Ghidra.\n\n"
     "Re-check runs it again. View log shows the recent log. "
     "Load sample capture loads a demo capture so you can try the "
     "whole flow with no recording. Ghidra setup steps shows how "
     "to install Ghidra.\n\n"
     "Check for updates asks GitHub whether a newer MIMIK is out."),
    ("presets",
     "Presets tab",
     "Presets are what MIMIK has learned about a host: its URL "
     "patterns, paging, and naming. A preset makes the next client "
     "for a similar host better.\n\n"
     "Learn a preset from a host on the Generate tab, or from a "
     "saved capture here.\n\n"
     "Presets never hold passwords or tokens: only shapes and "
     "patterns. Export a preset to share it, import one to use it.\n\n"
     "Pattern presets apply across hosts (for example, every host "
     "that pages the same way)."),
    ("ghidra",
     "Binary analysis with Ghidra",
     "Ghidra is the NSA's free reverse-engineering tool. MIMIK "
     "drives it headless (no Ghidra window) as an optional engine, "
     "the same way it uses Reblend for asset packs and Ollama for "
     "polish.\n\n"
     "Install it once: Doctor tab, Ghidra setup steps, or run "
     "`mimik ghidra-setup` in a terminal. It is a 544 MB download "
     "plus Java 25. MIMIK never downloads it for you.\n\n"
     "MIMIK finds Ghidra in ~/opt/ghidra*, on PATH, or where "
     "GHIDRA_HOME points. The Doctor tab shows green when it is "
     "found with the right Java.\n\n"
     "The analyzed binary is data, never executed: Ghidra reads "
     "it, MIMIK reads back the decompiled code, disassembly, call "
     "graph, and string references."),
]


def topics():
    """[(key, title), ...] in display order."""
    return [(key, title) for key, title, _body in TOPICS]


def body(key):
    """The plain-language text for one topic key, or None."""
    for k, _title, text in TOPICS:
        if k == key:
            return text
    return None
