"""Ghidra detection + headless driver (v0.5 items 3, 4, 11).

Ghidra is an OPTIONAL external engine, the same pattern as Reblend
(asset packs) and Ollama (polish): MIMIK never bundles it (544 MB
download + JDK is far too heavy) and never downloads it. The user
installs it once; MIMIK finds analyzeHeadless and drives it.

Everything here is stdlib-only. The analyzed binary is DATA, never
executed: analyzeHeadless imports it into a temp project, runs
auto-analysis, and MIMIK's export script writes JSON + per-function
files that MIMIK reads back.

Facts (verified 2026-11-13): Ghidra 12.2 (Oct 22, 2026), Apache-2.0,
NSA-maintained, GitHub NationalSecurityAgency/ghidra. Zip ~544 MB,
~885 MB extracted. Needs a full 64-bit JDK 25 for 12.2 (12.1.x was the
last JDK 21 line). 4 GB RAM minimum; the headless launcher defaults to
a 2 GB Java heap, overridden by GHIDRA_HEADLESS_MAXMEM. Built-in
Python is Jython 2.7; Ghidrathon bridges to CPython 3.8+.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile

from . import log, machine, store

GHIDRA_RELEASE_URL = (
    "https://github.com/NationalSecurityAgency/ghidra/releases")
GHIDRA_ZIP = "ghidra_12.2_PUBLIC_20261016.zip"
GHIDRA_VERSION = "12.2"
# analyzeHeadless default timeout: big binaries take a while, and the
# GUI/CLI both offer cancel. Ten minutes is the ceiling, not a target.
DEFAULT_TIMEOUT = 600


class GhidraError(Exception):
    """A plain-language Ghidra failure (missing install, bad JDK,
    timeout, crash)."""


def _is_exe(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _exe_in_home(home):
    """analyzeHeadless inside a Ghidra install dir, or None."""
    if not home:
        return None
    candidate = os.path.join(home, "support", "analyzeHeadless")
    return candidate if _is_exe(candidate) else None


def find_ghidra():
    """Locate analyzeHeadless. Search order: GHIDRA_HOME env, PATH,
    ~/opt/ghidra*, ~/.local/share/mimik/ghidra, /opt/ghidra*.
    Returns the executable path, or None when Ghidra is not found."""
    home = os.environ.get("GHIDRA_HOME")
    if home:
        if os.path.isfile(home):
            return home if _is_exe(home) else None
        found = _exe_in_home(home)
        if found:
            return found
    on_path = shutil.which("analyzeHeadless")
    if on_path:
        return on_path
    candidates = []
    candidates.extend(sorted(glob.glob(
        os.path.join(os.path.expanduser("~"), "opt", "ghidra*"))))
    candidates.append(os.path.join(store.data_dir(), "ghidra"))
    candidates.extend(sorted(glob.glob("/opt/ghidra*")))
    for cand in candidates:
        found = _exe_in_home(cand)
        if found:
            return found
    return None


def ghidra_home(exe=None):
    """The Ghidra install dir for an analyzeHeadless path."""
    exe = exe or find_ghidra()
    if not exe:
        return None
    # .../ghidra-12.2/support/analyzeHeadless -> .../ghidra-12.2
    parent = os.path.dirname(exe)
    if os.path.basename(parent) == "support":
        return os.path.dirname(parent)
    return parent


def ghidra_version(home=None):
    """The Ghidra version from Ghidra/application.properties, or
    None when it cannot be read."""
    home = home or ghidra_home()
    if not home:
        return None
    props = os.path.join(home, "Ghidra", "application.properties")
    try:
        with open(props, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("application.version"):
                    _key, _eq, value = line.partition("=")
                    return value.strip() or None
    except OSError:
        return None
    return None


def _parse_java_major(text):
    """Major version from `java -version` output. Handles both the
    modern form (openjdk version "25.0.1") and the legacy 1.x form
    (java version "1.8.0_392" -> 8)."""
    match = re.search(r'version "(\d+)(?:\.(\d+))?', text or "")
    if not match:
        return None
    major = int(match.group(1))
    if major == 1 and match.group(2):
        return int(match.group(2))
    return major


def java_version():
    """The major JDK version Ghidra would use (JAVA_HOME/bin/java,
    else java on PATH), or None when no Java is found."""
    candidates = []
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(os.path.join(java_home, "bin", "java"))
    on_path = shutil.which("java")
    if on_path:
        candidates.append(on_path)
    for java in candidates:
        if not _is_exe(java):
            continue
        try:
            proc = subprocess.run(
                [java, "-version"], capture_output=True, text=True,
                timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        major = _parse_java_major(proc.stderr)
        if major is not None:
            return major
    return None


def required_jdk(version=None):
    """The JDK major Ghidra needs: 25 for 12.2 and later, 21 for the
    12.1.x line and older. Unknown version -> 25 (the current line)."""
    version = version or ghidra_version()
    if not version:
        return 25
    match = re.match(r"(\d+)\.(\d+)", version)
    if not match:
        return 25
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) >= (12, 2):
        return 25
    return 21


def detect():
    """Everything Doctor and the GUI need to know about Ghidra.

    Returns a dict: found, exe, home, version, java_major,
    required_jdk, java_ok, heap (the RAM-sized heap from item 17),
    and fix (plain-language text when something is missing).
    Never raises."""
    exe = find_ghidra()
    heap = machine.ghidra_heap()
    if not exe:
        return {
            "found": False, "exe": None, "home": None, "version": None,
            "java_major": java_version(),
            "required_jdk": required_jdk(None), "java_ok": False,
            "heap": heap,
            "fix": ("install Ghidra (Doctor tab, 'Ghidra setup "
                    "steps', or `mimik ghidra-setup`)"),
        }
    home = ghidra_home(exe)
    version = ghidra_version(home)
    need = required_jdk(version)
    java_major = java_version()
    java_ok = java_major is not None and java_major >= need
    if java_ok:
        fix = None
    elif java_major is None:
        fix = (f"install JDK {need} (64-bit, full JDK): "
               f"`sudo apt install openjdk-{need}-jdk`, or Temurin "
               "if apt does not carry it")
    else:
        fix = (f"Ghidra {version or ''} needs JDK {need}+, but Java "
               f"{java_major} was found: install JDK {need}")
    return {
        "found": True, "exe": exe, "home": home, "version": version,
        "java_major": java_major, "required_jdk": need,
        "java_ok": java_ok, "heap": heap, "fix": fix,
    }


def setup_steps():
    """The exact setup steps for his Mint PC, as plain lines. Prints
    steps; downloads nothing. The SHA-256 is checked against the
    release page itself — MIMIK never states a hash it has not
    verified."""
    return [
        "Install Ghidra (NSA's free reverse-engineering tool, "
        "Apache-2.0) so MIMIK can decompile binaries.",
        "",
        "1. Download the release zip (about 544 MB):",
        f"   {GHIDRA_RELEASE_URL}",
        f"   File: {GHIDRA_ZIP}",
        "",
        "2. Check the download: the release page lists a SHA-256 "
        "checksum for the zip. Compare it with:",
        f"   sha256sum ~/{GHIDRA_ZIP}",
        "   (the two must match; if they do not, download again)",
        "",
        "3. Extract it (about 885 MB on disk):",
        "   mkdir -p ~/opt",
        f"   unzip ~/{GHIDRA_ZIP} -d ~/opt",
        "",
        "4. Install Java 25 (64-bit, full JDK, not just the "
        "runtime):",
        "   sudo apt install openjdk-25-jdk",
        "   If Mint's apt does not carry openjdk-25, use Temurin "
        "from adoptium.net, or unpack a JDK 25 tarball and set "
        "JAVA_HOME to it.",
        "",
        "5. Check it works:",
        "   ~/opt/ghidra-12.2/support/analyzeHeadless",
        "   (with no arguments it prints its usage; that means it "
        "runs)",
        "",
        "6. Back in MIMIK, open the Doctor tab and press Re-check: "
        "the Ghidra line should be green.",
        "",
        "MIMIK finds Ghidra in ~/opt/ghidra*, on PATH, or where "
        "GHIDRA_HOME points. No restart needed.",
    ]


def script_dir():
    """The dir holding MimikExport.py (ships inside the package)."""
    return os.path.join(os.path.dirname(__file__), "ghidra_scripts")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_analysis(binary_path, out_dir, heap=None, timeout=DEFAULT_TIMEOUT,
                 cancelled=None, on_log=None):
    """Run analyzeHeadless on a binary and read the export back.

    binary_path: the file to analyze (DATA, never executed).
    out_dir: where MimikExport writes functions.json, callgraph.json,
    xrefs.json, and functions/*.c + *.asm. Created if needed.
    heap: Java heap for the run (default: RAM-sized, item 17).
    timeout: seconds before the run is killed (default 600).
    cancelled: callable returning True to stop the run early.
    on_log: callable(line) fed progress lines from the run.

    Returns a dict: ok, functions (list), callgraph (dict),
    xrefs (dict), log_path, heap, version. A cancelled run returns
    ok False with cancelled True (not an error). Raises GhidraError
    for a missing install, a timeout, or a failed run.
    """
    exe = find_ghidra()
    if not exe:
        raise GhidraError(
            "Ghidra is not installed (run `mimik ghidra-setup` "
            "for the steps)")
    if not os.path.isfile(binary_path):
        raise GhidraError(f"no such file: {binary_path}")
    heap = heap or machine.ghidra_heap()
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "ghidra.log")
    project_root = store.ensure_private_dir(
        os.path.join(store.data_dir(), "ghidra-projects"))
    project_dir = tempfile.mkdtemp(prefix="mimik-", dir=project_root)
    log.log_info("ghidra", f"analyzing {binary_path} (heap {heap})")
    env = dict(os.environ)
    env["GHIDRA_HEADLESS_MAXMEM"] = heap
    cmd = [exe, project_dir, "mimik",
           "-import", binary_path,
           "-postScript", "MimikExport", out_dir,
           "-scriptPath", script_dir(),
           "-deleteProject"]
    proc = None
    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                cmd, stdout=log_file, stderr=subprocess.STDOUT,
                env=env)
            waited = 0.0
            poll = 0.25
            while True:
                rc = proc.poll()
                if rc is not None:
                    break
                if cancelled is not None and cancelled():
                    proc.kill()
                    proc.wait()
                    log.log_info("ghidra", "run cancelled")
                    return {"ok": False, "cancelled": True,
                            "functions": [], "callgraph": {},
                            "xrefs": {}, "log_path": log_path,
                            "heap": heap,
                            "version": ghidra_version()}
                waited += poll
                if waited >= timeout:
                    proc.kill()
                    proc.wait()
                    raise GhidraError(
                        f"Ghidra took longer than {timeout} seconds "
                        "and was stopped (try a smaller binary, or "
                        "run it again)")
                # Feed the last log line to the progress callback.
                if on_log is not None:
                    try:
                        log_file.flush()
                        with open(log_path, "r", encoding="utf-8",
                                  errors="replace") as reader:
                            lines = reader.read().splitlines()
                        if lines:
                            on_log(lines[-1])
                    except OSError:
                        pass
                _sleep(poll)
        if rc != 0:
            tail = ""
            try:
                with open(log_path, "r", encoding="utf-8",
                          errors="replace") as f:
                    tail = "\n".join(f.read().splitlines()[-5:])
            except OSError:
                pass
            raise GhidraError(
                "Ghidra failed (exit code "
                f"{rc})" + (f":\n{tail}" if tail else ""))
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        shutil.rmtree(project_dir, ignore_errors=True)
    functions = _read_json(os.path.join(out_dir, "functions.json"))
    callgraph = _read_json(os.path.join(out_dir, "callgraph.json"))
    xrefs = _read_json(os.path.join(out_dir, "xrefs.json"))
    log.log_info(
        "ghidra", f"done, {len(functions or [])} functions exported")
    return {
        "ok": True, "cancelled": False,
        "functions": functions or [],
        "callgraph": callgraph or {},
        "xrefs": xrefs or {},
        "log_path": log_path, "heap": heap,
        "version": ghidra_version(),
    }


def _sleep(seconds):
    """Small indirection so tests can patch the poll loop's sleep."""
    import time

    time.sleep(seconds)
