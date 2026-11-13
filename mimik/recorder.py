"""Run mitmweb and keep saving what it captures.

The proxy runs as a child process with its log in a temp file (so MIMIK can
discover mitmweb's web-UI token from the startup lines). Flows are pulled and
saved to the capture store every few seconds, so Ctrl-C / Stop never loses a
session — the proxy stopping is not the capture ending.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import assets, filters, log, store
from .sources.mitm import Mitm, MitmError

TOKEN_RE = re.compile(r"token=([A-Za-z0-9_\-]+)")


def mitmweb_cmd():
    """Prefer mitmweb on PATH; otherwise the copy in MIMIK's own venv
    (next to this Python — the installer puts mitmproxy there, and the
    venv's bin is not on PATH); otherwise run it ephemerally through uv.
    """
    if shutil.which("mitmweb"):
        return ["mitmweb"]
    venv_mitmweb = os.path.join(os.path.dirname(sys.executable), "mitmweb")
    if os.path.isfile(venv_mitmweb) and os.access(venv_mitmweb, os.X_OK):
        return [venv_mitmweb]
    if shutil.which("uvx"):
        return ["uvx", "--from", "mitmproxy", "mitmweb"]
    return None


class ProxySession:
    """A mitmweb run with periodic capture saving.

    meta: dict stored with every capture (e.g. {"launched_file": ...}).
    on_update: callback(hosts) called after each successful poll, where
    hosts is [(host, count)] most frequent first.
    """

    def __init__(self, meta=None, on_update=None):
        self.meta = meta or {}
        self.on_update = on_update
        self.proc = None
        self.mitm = None
        # The last set of hosts the capture filter dropped — the log
        # line fires only when the set changes, not every 5 s poll.
        self._last_dropped = set()
        self.log_path = os.path.join(
            tempfile.gettempdir(), "mimik-mitmweb.log"
        )
        self._log_fh = None
        self._token = os.environ.get("MITM_TOKEN", "test")

    # ---- lifecycle ----------------------------------------------------------
    def start(self, timeout=30):
        cmd = mitmweb_cmd()
        if not cmd:
            raise MitmError(
                "no proxy available — install mitmproxy "
                "(`pip install mitmproxy`) or uv (https://astral.sh/uv) "
                "so MIMIK can run mitmweb for you"
            )
        self._log_fh = store.open_private(self.log_path, "w")
        # Unbuffered: mitmweb's startup lines (including the web-UI token)
        # sit in a block buffer forever when stdout is a file, so the
        # token was never discovered and /flows answered 403.
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            cmd, stdout=self._log_fh, stderr=subprocess.STDOUT,
            start_new_session=True, env=env,
        )
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                code = self.proc.returncode
                self._cleanup_failed_start()
                raise MitmError(
                    f"mitmweb exited right away (code {code}) "
                    f"— see {self.log_path}"
                )
            self._discover_token()
            try:
                self.mitm = Mitm(token=self._token)
                self.mitm.flows()
                return True
            except MitmError as e:
                last_err = e
                time.sleep(1)
        self._cleanup_failed_start()
        raise MitmError(
            f"mitmweb did not answer within {timeout}s (last error: {last_err})"
        )

    def _cleanup_failed_start(self):
        """Stop a mitmweb that started but never answered, so a failed
        record does not leave an orphan proxy behind."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None

    def _discover_token(self):
        """mitmweb prints its web-UI token at startup; pick it up if present."""
        try:
            with open(self.log_path, errors="replace") as f:
                for line in f:
                    m = TOKEN_RE.search(line)
                    if m:
                        self._token = m.group(1)
                        return
        except OSError:
            pass

    def poll(self):
        """Pull flows once, save them, report hosts. Returns (host, count) rows.

        The save is incremental — store.save_flows only writes flows it
        hasn't seen, so re-pulling the same flows costs a local id scan
        instead of a full body re-fetch + rewrite.
        """
        if self.mitm is None:
            return []
        flows = self.mitm.flows()
        # Capture filter (item 26): excluded hosts never reach the
        # store or the host list — filter before saving AND before
        # deriving the rows.
        flows, dropped = filters.apply_capture_filter(flows)
        if dropped and dropped != self._last_dropped:
            log.log_info("recorder poll",
                         "excluded %d host(s): %s"
                         % (len(dropped), ", ".join(sorted(dropped))))
        self._last_dropped = dropped
        if flows:
            try:
                store.save_flows(flows, mitm=self.mitm, meta=self.meta)
            except Exception as e:
                log.log_error("recorder poll save_flows", e)
        from .sources.mitm import hosts as _hosts

        rows = _hosts(flows)
        if self.on_update:
            self.on_update(rows)
        return rows

    def extract_assets(self):
        """Save asset responses (images, models, audio, fonts) from the
        live capture, per host. Raw bytes come from mitmweb while it
        is still running. Returns [(host, group_dir, count)]. Never
        raises — a failed extraction is a lost bonus, not a lost
        capture."""
        if self.mitm is None:
            return []
        try:
            flows = self.mitm.flows()
        except Exception:
            return []
        # Capture filter (item 26): an excluded host's assets are not
        # extracted either.
        flows, _dropped = filters.apply_capture_filter(flows)
        by_host = {}
        for f in flows:
            req = f.get("request") or {}
            if req.get("host"):
                by_host.setdefault(req["host"], []).append(f)
        out = []
        for host, host_flows in sorted(by_host.items()):
            try:
                gdir, manifest = assets.extract_from_flows(
                    host_flows, host, mitm=self.mitm, host=host)
            except Exception as e:
                log.log_error("recorder extract_assets", e)
                continue
            if gdir:
                out.append((host, gdir, len(manifest["assets"])))
        return out

    def stop(self):
        """Final pull, asset extraction, then stop mitmweb."""
        if self.mitm is not None:
            try:
                self.poll()
            except Exception as e:
                log.log_error("recorder stop poll", e)
            try:
                self.extract_assets()
            except Exception as e:
                log.log_error("recorder stop extract_assets", e)
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        self.proc = None
        self.mitm = None

    def run_forever(self, interval=5):
        """Poll until Ctrl-C, then stop. Returns the last host rows."""
        rows = []
        try:
            while True:
                rows = self.poll()
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return rows
