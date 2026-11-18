"""Update check, verified download, staging, and self-update (v0.7).

The GUI checks on startup (silent when current) and on demand (Help menu,
Doctor tab). When a newer release is out, the user is asked; only on their
tap does the app download it, and the download is verified against the
release's .sha256 sidecar before anything is staged. No sidecar means no
install, never a blind download.

Staging lives under the data dir (updates/pending/): the verified zip plus
a pending.json marker. Restarting applies it: a small updater script waits
for the old GUI to exit, runs the staged zip's install.sh, and relaunches
MIMIK. The staged zip is verified again at apply time.

Nothing here raises for a network or parsing problem: check_for_update and
download_update return their failure as data, so the GUI can say so in
plain words instead of crashing. Test seam: MIMIK_UPDATE_API overrides
the releases API base URL, so the whole flow can run against a local fake
release server. It is test-only; the app never sets it.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from . import __version__
from .store import data_dir

REPO = "BROKE-N-BORED/MIMIK"
RELEASES_URL = f"https://github.com/{REPO}/releases"
_CHECK_TIMEOUT = 10
_DOWNLOAD_TIMEOUT = 60


def _api_base():
    """The releases API base URL (MIMIK_UPDATE_API overrides it,
    test-only, so the full flow can run against a fake server)."""
    return os.environ.get("MIMIK_UPDATE_API") or "https://api.github.com"


def _latest_url():
    return f"{_api_base()}/repos/{REPO}/releases/latest"


# The default latest-release URL, kept for callers that read it.
LATEST_URL = "https://api.github.com/repos/BROKE-N-BORED/MIMIK/releases/latest"


def _parse_version(text):
    """("v0.10.0" | "0.10.0") -> (0, 10, 0), or None when the tag
    isn't a dotted-number version."""
    parts = (text or "").strip().lstrip("v").split(".")
    nums = []
    for part in parts:
        if not part.isdigit():
            return None
        nums.append(int(part))
    return tuple(nums) if nums else None


def _pick_assets(data):
    """The release's zip and checksum sidecar, from its asset list.
    Returns (zip_url, zip_name, sha_url, sha_name), each None when the
    release does not carry that file."""
    zip_url = zip_name = sha_url = sha_name = None
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        url = asset.get("browser_download_url")
        if not url:
            continue
        if name.endswith(".zip.sha256") and sha_url is None:
            sha_url, sha_name = url, name
        elif name.endswith(".zip") and zip_url is None:
            zip_url, zip_name = url, name
    return zip_url, zip_name, sha_url, sha_name


def check_for_update(current=None, timeout=_CHECK_TIMEOUT):
    """Ask GitHub for the latest release. Returns a dict:
    {"current", "latest", "update_available", "url", "error",
     "zip_url", "zip_name", "sha_url", "sha_name"}.
    "latest"/"url" are None when the check failed; "error" is None
    on success. The zip/sidecar URLs are None when the release does
    not carry those files. Never raises."""
    current = current or __version__
    current_v = _parse_version(current)
    base = {"current": current, "latest": None,
            "update_available": False, "url": None,
            "zip_url": None, "zip_name": None,
            "sha_url": None, "sha_name": None}
    try:
        req = urllib.request.Request(
            _latest_url(), headers={"User-Agent": f"MIMIK/{current}",
                                    "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        base["error"] = str(e)
        return base
    latest = _parse_version(data.get("tag_name", ""))
    if latest is None:
        base["error"] = "the release had no version tag"
        return base
    latest_s = ".".join(str(n) for n in latest)
    zip_url, zip_name, sha_url, sha_name = _pick_assets(data)
    base.update({"latest": latest_s,
                 "update_available": current_v is not None and latest > current_v,
                 "url": data.get("html_url") or RELEASES_URL,
                 "zip_url": zip_url, "zip_name": zip_name,
                 "sha_url": sha_url, "sha_name": sha_name,
                 "error": None})
    return base


def _parse_sidecar(text):
    """The expected SHA-256 from a sidecar file: the first token of
    sha256sum output ("<sha>  <name>") or a bare hex digest. None
    when the sidecar does not carry a 64-hex digest."""
    token = (text or "").split()[0] if (text or "").split() else ""
    if len(token) == 64 and all(c in "0123456789abcdefABCDEF"
                                for c in token):
        return token.lower()
    return None


def _sha256_file(path):
    """The SHA-256 hex digest of a file, streamed. None on any error."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def verify_file(path, expected_sha):
    """True when the file's SHA-256 matches. False for a missing or
    unreadable file, or a mismatch."""
    if not expected_sha:
        return False
    return _sha256_file(path) == expected_sha.lower()


def download_update(zip_url, sha_url, dest_dir, progress=None,
                    timeout=_DOWNLOAD_TIMEOUT):
    """Download a release zip and verify it against its sidecar.
    The sidecar comes first; without one the download is refused,
    never installed blind. Streams the zip to a temp file with a
    running hash, so a 400 KB download never holds the whole file in
    memory twice. progress(done, total) is called per chunk when
    given (total is None when the server did not send a length).
    Returns {"ok": True, "path", "sha256", "size"} or
    {"ok": False, "error": "<plain words>"}. Never raises."""
    if not sha_url:
        return {"ok": False, "error":
                "the release has no checksum file, so MIMIK will "
                "not install it blind"}
    if not zip_url:
        return {"ok": False, "error":
                "the release has no download file"}
    try:
        req = urllib.request.Request(
            sha_url, headers={"User-Agent": f"MIMIK/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            sidecar = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error":
                f"could not fetch the checksum: {e}"}
    expected = _parse_sidecar(sidecar)
    if expected is None:
        return {"ok": False, "error":
                "the release's checksum file did not carry a usable "
                "checksum, so the update was refused"}
    try:
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": f"could not stage the update: {e}"}
    name = os.path.basename(zip_url.split("?", 1)[0]) or "mimik-update.zip"
    final = os.path.join(dest_dir, name)
    part = final + ".part"
    digest = hashlib.sha256()
    done = 0
    total = None
    try:
        req = urllib.request.Request(
            zip_url, headers={"User-Agent": f"MIMIK/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            total = int(length) if (length or "").isdigit() else None
            with open(part, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
    except (urllib.error.URLError, OSError, ValueError) as e:
        try:
            os.unlink(part)
        except OSError:
            pass
        return {"ok": False, "error": f"the download failed: {e}"}
    if digest.hexdigest() != expected:
        try:
            os.unlink(part)
        except OSError:
            pass
        return {"ok": False, "error":
                "the download did not match its checksum, so it was "
                "thrown away and nothing was installed"}
    try:
        os.replace(part, final)
    except OSError as e:
        return {"ok": False, "error": f"could not stage the update: {e}"}
    return {"ok": True, "path": final, "sha256": expected, "size": done}


# ---- staging ---------------------------------------------------------------

def updates_dir(base=None):
    return os.path.join(base or data_dir(), "updates")


def pending_dir(base=None):
    return os.path.join(updates_dir(base), "pending")


def _pending_marker(base=None):
    return os.path.join(pending_dir(base), "pending.json")


def write_pending(version, zip_path, sha256, base=None):
    """Record a staged update: the marker names the version, the zip
    file, and the checksum it must still match at apply time. The
    marker write is atomic; the dir is 0700."""
    pdir = pending_dir(base)
    os.makedirs(pdir, mode=0o700, exist_ok=True)
    marker = {"version": version,
              "zip": os.path.basename(zip_path),
              "sha256": sha256,
              "staged_at": time.time()}
    fd, tmp = tempfile.mkstemp(dir=pdir, prefix=".pending-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(marker, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _pending_marker(base))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(_pending_marker(base), 0o600)
    except OSError:
        pass
    return marker


def read_pending(base=None):
    """The staged update's marker, or None when nothing is staged or
    the marker does not parse (an untrusted marker is no marker)."""
    try:
        with open(_pending_marker(base)) as f:
            marker = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict):
        return None
    if not marker.get("version") or not marker.get("zip") \
            or not marker.get("sha256"):
        return None
    return marker


def clear_pending(base=None):
    """Remove the whole pending dir (staged zip, marker, failure
    note). Never raises."""
    shutil.rmtree(pending_dir(base), ignore_errors=True)


def failed_path(base=None):
    return os.path.join(pending_dir(base), "failed.txt")


def read_failed(base=None):
    """The install-failure note the updater left, or None."""
    try:
        with open(failed_path(base)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def is_installed():
    """True when the running copy is the installed one: the Python
    running this code lives under the data dir's venv (the install.sh
    layout). A copy run from a source tree reports False, and the GUI
    offers the releases page instead of an automatic install.
    NOTE: compare with abspath, NOT realpath. A venv's python is a
    symlink to the system python, so realpath resolves the
    executable out of the venv and this returns False on every real
    install (caught by the v0.7 live test)."""
    exe = os.path.abspath(sys.executable or "")
    venv = os.path.abspath(os.path.join(data_dir(), "venv"))
    return exe.startswith(venv + os.sep)


# ---- the updater script ----------------------------------------------------

def _sh_quote(text):
    """A shell single-quoted string (an embedded quote becomes '\'' )."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def updater_script_text(pid, zip_path, version, base=None):
    """The POSIX updater script, as text. It waits for the old GUI
    process (pid 0 = no wait, for a pending update applied at launch),
    unpacks the staged zip, runs its install.sh with stdin closed so
    it never blocks on the "Press Enter" prompt, then relaunches
    mimik-gui. On success the staging is cleaned; on failure the zip
    is dropped and failed.txt names the reason, so the next launch
    reports it once instead of retrying forever."""
    udir = updates_dir(base)
    log = os.path.join(udir, "update.log")
    pdir = pending_dir(base)
    lines = [
        "#!/bin/sh",
        "# MIMIK self-updater - written by the app when an update is",
        "# staged. Do not run it by hand; the app launches it.",
        "set -u",
        "# A stray PYTHONPATH in the inherited environment makes the",
        "# venv's pip import the SYSTEM pip's internals and crash,",
        "# so the updater starts from a clean Python environment.",
        "unset PYTHONPATH PYTHONHOME",
        f"PID={int(pid)}",
        f"ZIP={_sh_quote(zip_path)}",
        f"VERSION={_sh_quote(version)}",
        f"UPDATES={_sh_quote(udir)}",
        f"PENDING={_sh_quote(pdir)}",
        f"LOG={_sh_quote(log)}",
        "mkdir -p \"$UPDATES\"",
        "{",
        "echo \"MIMIK updater: installing $VERSION\"",
        "if [ \"$PID\" -gt 0 ]; then",
        "  WAITED=0",
        "  while kill -0 \"$PID\" 2>/dev/null; do",
        "    # A zombie still answers kill -0: its parent has not reaped",
        "    # it yet, but it is dead for our purposes.",
        "    if grep -q \"State:[[:space:]]*Z\" \"/proc/$PID/status\" "
        "2>/dev/null; then",
        "      break",
        "    fi",
        "    WAITED=$((WAITED + 1))",
        "    if [ \"$WAITED\" -ge 120 ]; then",
        "      echo \"MIMIK updater: the old MIMIK did not close\"",
        "      rm -f \"$ZIP\"",
        "      echo \"MIMIK was still running after 2 minutes, so the "
        "update was not applied\" > \"$PENDING/failed.txt\"",
        "      exit 1",
        "    fi",
        "    sleep 1",
        "  done",
        "fi",
        "TMP=\"$(mktemp -d)\"",
        "trap 'rm -rf \"$TMP\"' EXIT",
        "if command -v unzip >/dev/null 2>&1; then",
        "  UNPACK_OK=0; unzip -q -o \"$ZIP\" -d \"$TMP\" && UNPACK_OK=1",
        "elif command -v python3 >/dev/null 2>&1; then",
        "  UNPACK_OK=0; python3 -m zipfile -e \"$ZIP\" \"$TMP\" "
        "&& UNPACK_OK=1",
        "else",
        "  UNPACK_OK=0",
        "fi",
        "if [ \"$UNPACK_OK\" -ne 1 ]; then",
        "  echo \"MIMIK updater: could not unpack the update\"",
        "  rm -f \"$ZIP\"",
        "  echo \"could not unpack the downloaded update\" "
        "> \"$PENDING/failed.txt\"",
        "  exit 1",
        "fi",
        "INSTALL_DIR=\"$(find \"$TMP\" -maxdepth 2 -name install.sh "
        "-print -quit | xargs dirname 2>/dev/null)\"",
        "if [ -z \"$INSTALL_DIR\" ]; then",
        "  echo \"MIMIK updater: no installer in the update\"",
        "  rm -f \"$ZIP\"",
        "  echo \"the downloaded update had no installer in it\" "
        "> \"$PENDING/failed.txt\"",
        "  exit 1",
        "fi",
        "if ! bash \"$INSTALL_DIR/install.sh\" < /dev/null; then",
        "  echo \"MIMIK updater: the installer failed\"",
        "  rm -f \"$ZIP\"",
        "  echo \"the installer failed - see update.log for the reason\" "
        "> \"$PENDING/failed.txt\"",
        "  exit 1",
        "fi",
        "rm -rf \"$PENDING\"",
        "echo \"MIMIK updater: installed $VERSION, relaunching\"",
        "if [ -x \"$HOME/.local/bin/mimik-gui\" ]; then",
        "  exec \"$HOME/.local/bin/mimik-gui\"",
        "fi",
        "exec \"$(dirname \"$UPDATES\")/venv/bin/mimik-gui\"",
        f"}} >> {_sh_quote(log)} 2>&1",
    ]
    return "\n".join(lines) + "\n"


def _default_launcher(argv):
    """Run the updater detached, so it outlives the quitting GUI."""
    subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def apply_pending(launcher=None, base=None):
    """The launch-time hook (see main()). Returns one of:
    "apply" — a pending update verified, its updater was launched
      (the caller exits at once; the updater applies and relaunches);
    "cleared" — something was staged but did not verify, so it was
      dropped (the caller starts normally; the next check re-offers);
    "none" — nothing staged.
    The launcher is injected for tests; the default detaches the
    updater script. Never raises."""
    marker = read_pending(base)
    pdir = pending_dir(base)
    if marker is None:
        try:
            leftover = os.path.isdir(pdir) and bool(os.listdir(pdir))
        except OSError:
            leftover = False
        if leftover:
            clear_pending(base)
            return "cleared"
        return "none"
    zip_path = os.path.join(pdir, marker["zip"])
    if not os.path.isfile(zip_path) \
            or not verify_file(zip_path, marker["sha256"]):
        clear_pending(base)
        return "cleared"
    try:
        udir = updates_dir(base)
        os.makedirs(udir, mode=0o700, exist_ok=True)
        script = os.path.join(udir, "update.sh")
        with open(script, "w") as f:
            f.write(updater_script_text(0, zip_path, marker["version"],
                                        base))
        os.chmod(script, 0o700)
    except OSError:
        return "none"
    launch = launcher or _default_launcher
    try:
        launch([script])
    except OSError:
        return "none"
    return "apply"
