"""Launch Linux programs from FILES, under MIMIK's proxy, for capture.

Handled in v0.1:
  AppImage / ELF executable / script (.sh, .py) — run directly with proxy env.
  Archives (.zip, .tar.gz, .tgz, ...) — extract, auto-find the main executable.
  Disk images (.iso, .img) — mount READ-ONLY, user picks the program inside.
  .deb — extract with dpkg-deb -x (no sudo) and run the contained binary,
         or install via apt with a pkexec password prompt.

Windows .exe/.msi are NOT launched here. WinLinx runs Windows programs —
that's its whole point. To capture a Windows program's traffic, use the
"Windows program (via WinLinx)" record target (see mimik.wine): MIMIK points
its already-running Wine prefix at the proxy and captures the traffic so the
program can be cloned as a native Linux client.
"""
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

from . import log

PROXY_URL = "http://127.0.0.1:8080"

# How long a launched program gets to prove it stays up. An instant
# failure (missing library, bad interpreter, missing dependency)
# exits in milliseconds; a real program is still running at 1 s.
LAUNCH_SETTLE_SECONDS = 1.0

# How many of the child's first stderr lines a launch failure shows.
_STDERR_HEAD_LINES = 5

# Extraction guards (review #19): a hostile or corrupt archive must not
# expand until the disk fills.
MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB total, all members
MAX_EXTRACT_FILES = 50000                    # file members, dirs excluded

WINLINX_MESSAGE = (
    "Windows programs are WinLinx's job, run it there.\n"
    "MIMIK never runs Windows programs. To capture a Windows program's\n"
    "traffic for cloning, launch it in WinLinx, then use:\n"
    "    mimik record --target windows"
)

ARCHIVE_EXTS = (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


class LaunchError(RuntimeError):
    pass


def proxy_env(proxy=PROXY_URL):
    """Proxy env for a launched program (both cases; localhost bypassed)."""
    return {
        "http_proxy": proxy,
        "https_proxy": proxy,
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "no_proxy": "localhost,127.0.0.1",
        "NO_PROXY": "localhost,127.0.0.1",
    }


def runs_dir():
    """Where temp extractions live (~/.cache/mimik/runs)."""
    base = os.environ.get("MIMIK_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "mimik"
    )
    return os.path.join(base, "runs")


def run_dir_for(path):
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", os.path.basename(path))
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz",
                ".zip", ".deb", ".iso", ".img", ".AppImage"):
        if stem.lower().endswith(ext.lower()):
            stem = stem[: -len(ext)]
            break
    return os.path.join(runs_dir(), stem or "run")


def _magic(path, n=16):
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def detect_kind(path):
    """Classify a file for launching: appimage/elf/script/archive/iso/deb/windows/unknown."""
    lower = path.lower()
    if lower.endswith((".exe", ".msi")):
        return "windows"
    if lower.endswith(".appimage"):
        return "appimage"
    if lower.endswith((".iso", ".img")):
        return "iso"
    if lower.endswith(".deb"):
        return "deb"
    if lower.endswith(ARCHIVE_EXTS):
        return "archive"
    if lower.endswith((".sh", ".py")):
        return "script"
    head = _magic(path, 16)
    if head.startswith(b"\x7fELF"):
        # AppImage type 2 carries "AI\x02" at offset 8.
        with open(path, "rb") as f:
            f.seek(8)
            if f.read(3) == b"AI\x02":
                return "appimage"
        return "elf"
    if head.startswith(b"PK\x03\x04"):
        return "archive"
    if head.startswith(b"\x1f\x8b"):
        return "archive"
    if head.startswith(b"#!"):
        return "script"
    if head.startswith(b"!<arch>"):
        return "deb"
    try:
        with open(path, "rb") as f:
            f.seek(32769)
            if f.read(5) == b"CD001":
                return "iso"
    except OSError:
        pass
    return "unknown"


# ---- extraction -------------------------------------------------------------

def _safe_members(names):
    for name in names:
        if name.startswith(("/", "\\")) or ".." in name.split("/"):
            raise LaunchError(f"unsafe path inside archive: {name!r}")


def _check_extract_caps(n_files, total_bytes, max_bytes, max_files):
    """Refuse an archive over the extraction caps (review #19)."""
    if n_files > max_files:
        raise LaunchError(
            f"archive holds {n_files} files, over the "
            f"{max_files}-file extraction cap, refused")
    if total_bytes > max_bytes:
        raise LaunchError(
            f"archive expands to {total_bytes // (1024 * 1024)} MB, "
            f"over the {max_bytes // (1024 * 1024)} MB extraction cap, "
            "refused")


def _extract_zip(path, dest, max_bytes, max_files):
    with zipfile.ZipFile(path) as zf:
        _safe_members(zf.namelist())
        members = [i for i in zf.infolist() if not i.is_dir()]
        _check_extract_caps(
            len(members), sum(i.file_size for i in members),
            max_bytes, max_files)
        written = 0
        for info in members:
            zf.extract(info, dest)
            # Measure what actually landed — headers can lie, so the
            # running total comes from the disk, not the header.
            try:
                written += os.path.getsize(
                    os.path.join(dest, info.filename))
            except OSError:
                written += info.file_size
            if written > max_bytes:
                raise LaunchError(
                    f"archive expands past the "
                    f"{max_bytes // (1024 * 1024)} MB extraction cap, "
                    "refused, nothing extracted")


def _extract_tar(path, dest, max_bytes, max_files):
    with tarfile.open(path) as tf:
        _safe_members(tf.getnames())
        members = tf.getmembers()
        files = [m for m in members if m.isfile()]
        _check_extract_caps(
            len(files), sum(m.size for m in files),
            max_bytes, max_files)
        written = 0
        for member in members:
            tf.extract(member, dest, filter="data")
            if member.isfile():
                try:
                    written += os.path.getsize(
                        os.path.join(dest, member.name))
                except OSError:
                    written += member.size
                if written > max_bytes:
                    raise LaunchError(
                        f"archive expands past the "
                        f"{max_bytes // (1024 * 1024)} MB extraction cap, "
                        "refused, nothing extracted")


def extract_archive(path, dest, max_bytes=MAX_EXTRACT_BYTES,
                    max_files=MAX_EXTRACT_FILES):
    """Extract a zip/tar archive to dest (path-traversal safe). Returns dest.

    Size-guarded (review #19): the total uncompressed size and file
    count are checked against the caps before anything is written, and
    the bytes are measured again as each member lands (headers can
    lie). Over the cap, nothing is left behind — a partial extraction
    into a fresh dest is removed — and a LaunchError says which cap
    tripped. A dest that already existed is never removed.
    """
    created = not os.path.exists(dest)
    os.makedirs(dest, exist_ok=True)
    lower = path.lower()
    try:
        if lower.endswith(".zip"):
            _extract_zip(path, dest, max_bytes, max_files)
        else:
            _extract_tar(path, dest, max_bytes, max_files)
    except BaseException:
        if created:
            shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def extract_deb(path, dest):
    """Extract a .deb with dpkg-deb -x (no sudo). Returns dest."""
    if shutil.which("dpkg-deb") is None:
        raise LaunchError(
            "dpkg-deb not found, install it with: sudo apt install dpkg-dev"
        )
    os.makedirs(dest, exist_ok=True)
    subprocess.run(["dpkg-deb", "-x", path, dest], check=True)
    return dest


def deb_package_name(path):
    """The Package: field of a .deb (used as the installed binary name)."""
    if shutil.which("dpkg-deb") is None:
        return None
    try:
        out = subprocess.run(
            ["dpkg-deb", "-f", path, "Package"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


# ---- finding the program inside ----------------------------------------------

def _is_executable_file(path):
    if not os.path.isfile(path):
        return False
    if os.access(path, os.X_OK):
        return True
    return detect_kind(path) in ("appimage", "elf", "script")


def find_executables(root):
    """Candidate programs under root: top-level first, then largest first."""
    found = []
    for dirpath, _dirs, files in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        for name in files:
            full = os.path.join(dirpath, name)
            if _is_executable_file(full):
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                found.append((depth, -size, full))
    found.sort()
    return [f for _d, _s, f in found]


def pick_main_executable(root):
    """Best guess at the main program: first candidate, or None."""
    cands = find_executables(root)
    return cands[0] if cands else None


# ---- disk images (read-only mounts) ------------------------------------------

def choose_mount_method():
    """Best available read-only ISO mount method, or None (manual steps)."""
    if shutil.which("udisksctl"):
        return "udisksctl"
    if shutil.which("fuseiso"):
        return "fuseiso"
    if shutil.which("gio"):
        return "gio"
    return None


MOUNT_MANUAL_STEPS = (
    "No automatic mount tool found (udisksctl, fuseiso, gio).\n"
    "Mount it by hand: right-click the image in Files → "
    "Open With Disk Image Mounter, then point MIMIK at the mounted folder\n"
    "with:  mimik record --file /media/$USER/<name>/"
)


class Mount:
    """A read-only mount of a disk image. Never mounts read-write."""

    def __init__(self, image, method=None, runner=None):
        self.image = image
        self.method = method or choose_mount_method()
        self.runner = runner or subprocess.run
        self.mountpoint = None
        self.loopdev = None

    def mount(self):
        """Mount read-only. Returns the mountpoint path."""
        if self.method == "udisksctl":
            r = self.runner(
                ["udisksctl", "loop-setup", "-f", self.image, "--read-only"],
                capture_output=True, text=True,
            )
            m = re.search(r"(/dev/loop\d+)", r.stdout or "")
            if not m:
                raise LaunchError(
                    "udisksctl loop-setup gave no loop device: "
                    f"{(r.stdout or '') + (r.stderr or '')}"
                )
            self.loopdev = m.group(1)
            r = self.runner(
                ["udisksctl", "mount", "-b", self.loopdev, "--options", "ro"],
                capture_output=True, text=True,
            )
            m = re.search(r"at\s+(\S+)", r.stdout or "")
            if not m:
                # The loop device is already set up — release it before
                # raising, or it leaks (nothing else knows about it).
                try:
                    self.runner(
                        ["udisksctl", "loop-delete", "-b", self.loopdev],
                        capture_output=True, text=True)
                except Exception as e:
                    log.log_error("launch loop-delete", e)
                self.loopdev = None
                raise LaunchError(
                    "udisksctl mount failed: "
                    f"{(r.stdout or '') + (r.stderr or '')}"
                )
            # udisksctl ends the line with a period: "... at /media/u/DISK."
            self.mountpoint = m.group(1).rstrip(".")
        elif self.method == "fuseiso":
            self.mountpoint = os.path.join(run_dir_for(self.image), "mnt")
            os.makedirs(self.mountpoint, exist_ok=True)
            self.runner(["fuseiso", "-n", self.image, self.mountpoint],
                        capture_output=True, text=True, check=True)
        elif self.method == "gio":
            self.runner(["gio", "mount", f"archive://{self.image}"],
                        capture_output=True, text=True, check=True)
            gvfs = os.path.join(
                "/run/user", str(os.getuid()), "gvfs"
            )
            cands = [os.path.join(gvfs, d) for d in os.listdir(gvfs)
                     if d.startswith("archive:")] if os.path.isdir(gvfs) else []
            if not cands:
                raise LaunchError("gio mounted the image but no gvfs path appeared")
            self.mountpoint = sorted(cands)[-1]
        else:
            raise LaunchError(MOUNT_MANUAL_STEPS)
        return self.mountpoint

    def unmount(self):
        """Unmount + release the loop device (best effort, never raises)."""
        try:
            if self.method == "udisksctl" and self.loopdev:
                self.runner(["udisksctl", "unmount", "-b", self.loopdev],
                            capture_output=True, text=True)
                self.runner(["udisksctl", "loop-delete", "-b", self.loopdev],
                            capture_output=True, text=True)
            elif self.method == "fuseiso" and self.mountpoint:
                self.runner(["fusermount", "-u", self.mountpoint],
                            capture_output=True, text=True)
            elif self.method == "gio":
                self.runner(["gio", "mount", "-u", f"archive://{self.image}"],
                            capture_output=True, text=True)
        except Exception as e:
            log.log_error("launch unmount", e)
        self.mountpoint = None
        self.loopdev = None


# ---- launch plans ------------------------------------------------------------

class LaunchPlan:
    """Everything needed to run a program from a file, decided up front."""

    def __init__(self, kind, source, command=None, work_dir=None,
                 mount=None, candidates=None, needs_pick=False,
                 message="", install_command=None):
        self.kind = kind
        self.source = source
        self.command = command  # argv list to run (None until picked)
        self.work_dir = work_dir
        self.mount = mount
        self.candidates = candidates or []
        self.needs_pick = needs_pick
        self.message = message
        self.install_command = install_command  # .deb --install only

    @property
    def command_display(self):
        if self.install_command:
            return " ".join(_shell_quote(a) for a in self.install_command)
        if self.command:
            return " ".join(_shell_quote(a) for a in self.command)
        return "(pick a program first)"

    @property
    def is_stale(self):
        """True when the plan's files are gone — the image was unmounted
        after a previous record, or the extraction dir was deleted — so
        prepare_file must run again before launching."""
        if self.mount is not None and self.mount.mountpoint is None:
            return True
        if self.work_dir and not os.path.isdir(self.work_dir):
            return True
        return False


def _shell_quote(arg):
    return arg if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", arg) else f"'{arg}'"


def build_launch_command(executable):
    """argv to run an executable/script (AppImage chmod +x handled in prepare)."""
    lower = executable.lower()
    if lower.endswith(".sh"):
        return ["bash", executable]
    if lower.endswith(".py"):
        return [sys.executable, executable]
    return [executable]


def prepare_file(path, pick=None, deb_install=False):
    """Decide how to launch `path`. Never runs anything itself.

    pick: for archives/ISOs with several candidates, the chosen executable
    (full path or 1-based index into the candidate list). Returns a
    LaunchPlan; when needs_pick is True the caller must ask the user and
    pick from the plan's candidates with pick_candidate(plan, pick) —
    never call prepare_file again for the same file (that would
    re-extract the archive or re-mount the ISO and leak the first mount).
    """
    if not os.path.exists(path):
        raise LaunchError(f"no such file: {path}")
    if os.path.isdir(path):
        # A mounted folder (e.g. user mounted an ISO by hand): treat like an
        # extraction dir — find the program inside.
        cands = find_executables(path)
        return _plan_from_candidates("folder", path, path, cands, pick)
    kind = detect_kind(path)
    if kind == "windows":
        raise LaunchError(WINLINX_MESSAGE)
    if kind in ("appimage", "elf", "script"):
        if kind == "appimage" and not os.access(path, os.X_OK):
            os.chmod(path, 0o755)
        return LaunchPlan(kind, path, command=build_launch_command(path))
    if kind == "archive":
        dest = extract_archive(path, run_dir_for(path))
        cands = find_executables(dest)
        return _plan_from_candidates(kind, path, dest, cands, pick)
    if kind == "iso":
        mount = Mount(path)
        mp = mount.mount()
        cands = find_executables(mp)
        return _plan_from_candidates(kind, path, mp, cands, pick, mount=mount)
    if kind == "deb":
        if deb_install:
            pkg = deb_package_name(path)
            if not pkg:
                raise LaunchError("could not read the package name from the .deb")
            install = ["pkexec", "apt", "install", "-y", os.path.abspath(path)]
            return LaunchPlan(kind, path, command=[pkg],
                              install_command=install,
                              message=f"install {pkg} with apt (password prompt), "
                                      f"then launch `{pkg}`")
        dest = extract_deb(path, run_dir_for(path))
        cands = find_executables(dest)
        return _plan_from_candidates(kind, path, dest, cands, pick)
    raise LaunchError(
        f"don't know how to launch {os.path.basename(path)}, MIMIK handles "
        "Linux programs, AppImages, executables, scripts, archives (.zip, "
        ".tar.gz), disk images (.iso, .img), and .deb packages."
    )


def _plan_from_candidates(kind, source, work_dir, cands, pick, mount=None):
    if not cands:
        raise LaunchError(
            f"no program found inside {os.path.basename(source)}, nothing "
            "executable in there to launch"
        )
    if pick is not None:
        exe = _resolve_pick(cands, pick)
        return LaunchPlan(kind, source, command=build_launch_command(exe),
                          work_dir=work_dir, mount=mount, candidates=cands)
    if len(cands) == 1:
        return LaunchPlan(kind, source,
                          command=build_launch_command(cands[0]),
                          work_dir=work_dir, mount=mount, candidates=cands,
                          message=f"only one program inside: {cands[0]}")
    if kind == "iso":
        # Never auto-run from a disk image: the user must pick.
        return LaunchPlan(kind, source, work_dir=work_dir, mount=mount,
                          candidates=cands, needs_pick=True,
                          message="pick the program to run from the image")
    # Archives/.deb: auto-pick the best guess, but show it plainly.
    return LaunchPlan(kind, source, command=build_launch_command(cands[0]),
                      work_dir=work_dir, mount=mount, candidates=cands,
                      message=f"auto-picked {cands[0]} "
                              f"({len(cands)} programs found, pick another "
                              "with --pick N)")


def _resolve_pick(cands, pick):
    if isinstance(pick, int) or (isinstance(pick, str) and pick.isdigit()):
        idx = int(pick) - 1
        if 0 <= idx < len(cands):
            return cands[idx]
        raise LaunchError(f"pick {pick} is out of range (1–{len(cands)})")
    for c in cands:
        if c == pick or c.endswith(pick):
            return c
    raise LaunchError(f"{pick!r} is not one of the programs found")


def pick_candidate(plan, pick):
    """Pick a program from an ALREADY-PREPARED plan's candidates — no
    re-extraction, no re-mount. Sets plan.command, clears needs_pick.
    Returns the plan."""
    exe = _resolve_pick(plan.candidates, pick)
    plan.command = build_launch_command(exe)
    plan.needs_pick = False
    return plan


def _stderr_head(path, lines=_STDERR_HEAD_LINES):
    """First `lines` lines of a stderr file, or "" when unreadable."""
    try:
        with open(path, errors="replace") as fh:
            out = []
            for _ in range(lines):
                line = fh.readline()
                if not line:
                    break
                out.append(line.rstrip("\n"))
            return "\n".join(out)
    except OSError:
        return ""


def launch_checked(argv, *, env=None, cwd=None,
                   settle=LAUNCH_SETTLE_SECONDS, label="the program"):
    """Start a child process and check it stays up. Returns the Popen object.

    stderr goes to a temp file — never a pipe, which nobody reads and
    would block the child once it fills. After `settle` seconds the
    child is polled: if it exited with an error, LaunchError names the
    exit code and carries its first stderr lines, so a failure is never
    reported as a launch (review #14). An exit code of 0 counts as
    launched — some launchers fork and exit 0 once the real program is
    running. If it is still running (or exited 0), the temp file is
    unlinked (the child's own fd stays valid) and the Popen object is
    returned.
    """
    fd, path = tempfile.mkstemp(prefix="mimik-launch-", suffix=".log")
    try:
        proc = subprocess.Popen(argv, env=env, cwd=cwd,
                                stdout=subprocess.DEVNULL,
                                stderr=fd, start_new_session=True)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    os.close(fd)  # our copy; the child's dup stays open
    try:
        time.sleep(settle)
        rc = proc.poll()
    except BaseException:
        # Interrupted during the settle window — don't orphan the child.
        stop_process(proc)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    if rc:
        head = _stderr_head(path)
        try:
            os.unlink(path)
        except OSError:
            pass
        detail = f" (exit code {rc})"
        if head:
            detail += f":\n{head}"
        else:
            detail += " with no error output"
        raise LaunchError(f"{label} exited right away{detail}")
    try:
        os.unlink(path)
    except OSError:
        pass
    return proc


def launch_process(plan, proxy=PROXY_URL, settle=LAUNCH_SETTLE_SECONDS):
    """Start the planned program with proxy env. Returns the Popen object.

    Raises LaunchError when the program exits with an error within
    `settle` seconds, with its first stderr lines — a program that
    dies instantly is reported as failed, never as launched.
    """
    if plan.install_command:
        subprocess.run(plan.install_command, check=True)
    if not plan.command:
        raise LaunchError("no program chosen yet, pick one first")
    env = dict(os.environ)
    env.update(proxy_env(proxy))
    return launch_checked(plan.command, env=env, cwd=plan.work_dir or None,
                          settle=settle, label=plan.command_display)


def stop_process(proc):
    """Stop a launched program (best effort, never raises)."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as e:
        log.log_error("launch stop_process", e)


def clear_runs():
    """Delete temp extractions under ~/.cache/mimik/runs. Returns count."""
    base = runs_dir()
    if not os.path.isdir(base):
        return 0
    n = 0
    for name in os.listdir(base):
        shutil.rmtree(os.path.join(base, name), ignore_errors=True)
        n += 1
    return n
