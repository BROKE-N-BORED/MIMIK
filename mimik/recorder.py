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
import tempfile
import time

from . import store
from .sources.mitm import Mitm, MitmError

TOKEN_RE = re.compile(r"token=([A-Za-z0-9_\-]+)")


def mitmweb_cmd():
    """Prefer mitmweb on PATH; otherwise run it ephemerally through uv."""
    if shutil.which("mitmweb"):
        return ["mitmweb"]
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
        self._log_fh = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            cmd, stdout=self._log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise MitmError(
                    f"mitmweb exited right away (code {self.proc.returncode}) "
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
        raise MitmError(
            f"mitmweb did not answer within {timeout}s (last error: {last_err})"
        )

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
        """Pull flows once, save them, report hosts. Returns (host, count) rows."""
        if self.mitm is None:
            return []
        flows = self.mitm.flows()
        if flows:
            try:
                store.save_flows(flows, mitm=self.mitm, meta=self.meta)
            except Exception:
                pass
        from .sources.mitm import hosts as _hosts

        rows = _hosts(flows)
        if self.on_update:
            self.on_update(rows)
        return rows

    def stop(self):
        """Final pull, then stop mitmweb."""
        if self.mitm is not None:
            try:
                self.poll()
            except Exception:
                pass
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
