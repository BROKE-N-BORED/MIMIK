"""Tests for the in-app update check (item 31)."""
import argparse
import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # for test_presets helpers

from mimik import cli, update
from test_presets import run_cmd


def _fake_response(payload):
    """A context-manager stand-in for urlopen's response."""
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class UpdateCheckTests(unittest.TestCase):
    def _check(self, payload, current="0.3.0"):
        with mock.patch("urllib.request.urlopen",
                        return_value=_fake_response(payload)):
            return update.check_for_update(current=current)

    def test_newer_release_available(self):
        r = self._check({
            "tag_name": "v0.4.0",
            "html_url": "https://github.com/BROKE-N-BORED/MIMIK/"
                        "releases/tag/v0.4.0"})
        self.assertTrue(r["update_available"])
        self.assertEqual(r["latest"], "0.4.0")
        self.assertIn("v0.4.0", r["url"])
        self.assertIsNone(r["error"])

    def test_same_version_is_current(self):
        r = self._check({"tag_name": "v0.3.0", "html_url": "https://x"})
        self.assertFalse(r["update_available"])
        self.assertEqual(r["latest"], "0.3.0")

    def test_numeric_not_lexicographic(self):
        # 0.10.0 > 0.9.0 numerically; "0.10.0" < "0.9.0" as strings.
        r = self._check({"tag_name": "v0.10.0", "html_url": "https://x"},
                        current="0.9.0")
        self.assertTrue(r["update_available"])
        r = self._check({"tag_name": "v0.9.0", "html_url": "https://x"},
                        current="0.10.0")
        self.assertFalse(r["update_available"])

    def test_tag_without_v_prefix(self):
        r = self._check({"tag_name": "0.4.0", "html_url": "https://x"})
        self.assertTrue(r["update_available"])

    def test_network_error_is_returned_not_raised(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("no route")):
            r = update.check_for_update(current="0.3.0")
        self.assertFalse(r["update_available"])
        self.assertIsNone(r["latest"])
        self.assertIn("no route", r["error"])

    def test_bad_json_is_returned_not_raised(self):
        resp = mock.MagicMock()
        resp.read.return_value = b"not json"
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with mock.patch("urllib.request.urlopen", return_value=resp):
            r = update.check_for_update(current="0.3.0")
        self.assertIsNotNone(r["error"])

    def test_missing_tag_is_an_error(self):
        r = self._check({"html_url": "https://x"})
        self.assertIsNotNone(r["error"])
        self.assertFalse(r["update_available"])

    def test_cli_reports_both_ways(self):
        newer = {"current": "0.3.0", "latest": "0.4.0",
                 "update_available": True,
                 "url": "https://github.com/x/releases/tag/v0.4.0",
                 "error": None}
        with mock.patch.object(update, "check_for_update",
                               return_value=newer):
            out, code = run_cmd(cli.cmd_update_check,
                                argparse.Namespace())
        self.assertEqual(code, 0)
        self.assertIn("0.4.0 is available", out)
        self.assertIn("https://github.com/x/releases/tag/v0.4.0", out)
        current = dict(newer, update_available=False, latest="0.3.0")
        with mock.patch.object(update, "check_for_update",
                               return_value=current):
            out, code = run_cmd(cli.cmd_update_check,
                                argparse.Namespace())
        self.assertIn("is the latest version", out)
        error = dict(newer, update_available=False, latest=None,
                     url=None, error="no route")
        with mock.patch.object(update, "check_for_update",
                               return_value=error):
            _out, code = run_cmd(cli.cmd_update_check,
                                 argparse.Namespace())
        self.assertNotEqual(code, 0)
        self.assertIn("no route", str(code))


class AssetPickingTests(unittest.TestCase):
    """v0.7: the check also returns the release's zip + sidecar URLs."""

    def _check(self, payload, current="0.3.0"):
        with mock.patch("urllib.request.urlopen",
                        return_value=_fake_response(payload)):
            return update.check_for_update(current=current)

    def test_assets_found(self):
        r = self._check({
            "tag_name": "v0.4.0",
            "html_url": "https://x",
            "assets": [
                {"name": "mimik-v0.4.zip",
                 "browser_download_url": "https://dl/x.zip"},
                {"name": "mimik-v0.4.zip.sha256",
                 "browser_download_url": "https://dl/x.zip.sha256"},
                {"name": "Source code (zip)",
                 "browser_download_url": "https://dl/src.zip"},
            ]})
        self.assertEqual(r["zip_url"], "https://dl/x.zip")
        self.assertEqual(r["zip_name"], "mimik-v0.4.zip")
        self.assertEqual(r["sha_url"], "https://dl/x.zip.sha256")
        self.assertEqual(r["sha_name"], "mimik-v0.4.zip.sha256")

    def test_no_assets_is_none_not_error(self):
        r = self._check({"tag_name": "v0.4.0", "html_url": "https://x"})
        self.assertTrue(r["update_available"])
        self.assertIsNone(r["zip_url"])
        self.assertIsNone(r["sha_url"])
        self.assertIsNone(r["error"])

    def test_zip_without_sidecar(self):
        r = self._check({
            "tag_name": "v0.4.0", "html_url": "https://x",
            "assets": [{"name": "mimik-v0.4.zip",
                        "browser_download_url": "https://dl/x.zip"}]})
        self.assertEqual(r["zip_url"], "https://dl/x.zip")
        self.assertIsNone(r["sha_url"])


class _RouteHandler(http.server.BaseHTTPRequestHandler):
    """Serves bytes from the server's routes dict (path -> bytes),
    404 otherwise. Quiet: no request logging."""

    def do_GET(self):
        body = self.server.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeReleaseServer:
    """A local HTTP server standing in for GitHub's download host."""

    def __init__(self, routes):
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _RouteHandler)
        self.httpd.routes = routes
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _no_proxy_env():
    """urllib honours proxy env vars; a test server on localhost must
    be reached directly, so the proxies go away for these tests."""
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


class DownloadTests(unittest.TestCase):
    """v0.7: verified download against a local fake release server."""

    ZIP_BYTES = b"fake zip bytes for the update test"
    ZIP_SHA = hashlib.sha256(ZIP_BYTES).hexdigest()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.server = FakeReleaseServer({
            "/zip": self.ZIP_BYTES,
            "/good.sha256": f"{self.ZIP_SHA}  mimik-v9.9.zip\n".encode(),
            "/bad.sha256": ("0" * 64 + "  mimik-v9.9.zip\n").encode(),
            "/garbage.sha256": b"not a checksum\n",
        })
        self.addCleanup(self.server.close)
        self.addCleanup(lambda: os.path.exists(self.tmp) and
                        __import__("shutil").rmtree(self.tmp,
                                                    ignore_errors=True))
        self._proxy = mock.patch.dict(os.environ, _no_proxy_env(),
                                      clear=True)
        self._proxy.start()
        self.addCleanup(self._proxy.stop)

    def _download(self, sha_path="/good.sha256", zip_path="/zip"):
        dest = os.path.join(self.tmp, "pending")
        return update.download_update(
            self.server.base + zip_path if zip_path else None,
            self.server.base + sha_path if sha_path else None,
            dest)

    def test_good_download_verifies(self):
        seen = []
        dest = os.path.join(self.tmp, "pending")
        r = update.download_update(
            self.server.base + "/zip", self.server.base + "/good.sha256",
            dest, progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["sha256"], self.ZIP_SHA)
        self.assertEqual(r["size"], len(self.ZIP_BYTES))
        with open(r["path"], "rb") as f:
            self.assertEqual(f.read(), self.ZIP_BYTES)
        self.assertFalse(os.path.exists(r["path"] + ".part"))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], len(self.ZIP_BYTES))
        self.assertEqual(seen[-1][1], len(self.ZIP_BYTES))

    def test_wrong_checksum_is_refused_and_cleaned(self):
        r = self._download(sha_path="/bad.sha256")
        self.assertFalse(r["ok"])
        self.assertIn("did not match its checksum", r["error"])
        dest = os.path.join(self.tmp, "pending")
        self.assertEqual(os.listdir(dest), [])

    def test_missing_sidecar_is_refused_without_downloading(self):
        r = self._download(sha_path=None)
        self.assertFalse(r["ok"])
        self.assertIn("no checksum file", r["error"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pending")))

    def test_garbage_sidecar_is_refused(self):
        r = self._download(sha_path="/garbage.sha256")
        self.assertFalse(r["ok"])
        self.assertIn("checksum", r["error"])

    def test_download_failure_is_returned_and_cleaned(self):
        r = self._download(zip_path="/no-such-file")
        self.assertFalse(r["ok"])
        self.assertIn("download failed", r["error"])
        dest = os.path.join(self.tmp, "pending")
        self.assertEqual(os.listdir(dest), [])

    def test_bare_hex_sidecar_parses(self):
        self.assertEqual(update._parse_sidecar(self.ZIP_SHA), self.ZIP_SHA)
        self.assertEqual(
            update._parse_sidecar(f"{self.ZIP_SHA}  mimik-v9.9.zip\n"),
            self.ZIP_SHA)
        self.assertIsNone(update._parse_sidecar("garbage"))
        self.assertIsNone(update._parse_sidecar(""))


class StagingTests(unittest.TestCase):
    """v0.7: pending-marker round-trip, verify, clear."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.base, ignore_errors=True))

    def test_round_trip(self):
        zip_path = os.path.join(self.base, "mimik-v9.9.zip")
        with open(zip_path, "wb") as f:
            f.write(b"zip")
        sha = hashlib.sha256(b"zip").hexdigest()
        marker = update.write_pending("9.9", zip_path, sha, base=self.base)
        self.assertEqual(marker["version"], "9.9")
        self.assertEqual(marker["zip"], "mimik-v9.9.zip")
        self.assertEqual(marker["sha256"], sha)
        self.assertEqual(update.read_pending(base=self.base), marker)
        mode = os.stat(update.pending_dir(base=self.base)).st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_verify_file(self):
        path = os.path.join(self.base, "f.bin")
        with open(path, "wb") as f:
            f.write(b"abc")
        sha = hashlib.sha256(b"abc").hexdigest()
        self.assertTrue(update.verify_file(path, sha))
        self.assertTrue(update.verify_file(path, sha.upper()))
        self.assertFalse(update.verify_file(path, "0" * 64))
        self.assertFalse(update.verify_file(path, ""))
        self.assertFalse(update.verify_file(
            os.path.join(self.base, "missing"), sha))

    def test_corrupt_marker_reads_as_none(self):
        os.makedirs(update.pending_dir(base=self.base), exist_ok=True)
        with open(os.path.join(update.pending_dir(base=self.base),
                               "pending.json"), "w") as f:
            f.write("{not json")
        self.assertIsNone(update.read_pending(base=self.base))

    def test_clear_pending(self):
        zip_path = os.path.join(self.base, "mimik-v9.9.zip")
        with open(zip_path, "wb") as f:
            f.write(b"zip")
        update.write_pending("9.9", zip_path, "0" * 64, base=self.base)
        update.clear_pending(base=self.base)
        self.assertFalse(os.path.exists(
            update.pending_dir(base=self.base)))

    def test_failed_note_round_trip(self):
        self.assertIsNone(update.read_failed(base=self.base))
        os.makedirs(update.pending_dir(base=self.base), exist_ok=True)
        with open(update.failed_path(base=self.base), "w") as f:
            f.write("the installer failed\n")
        self.assertEqual(update.read_failed(base=self.base),
                         "the installer failed")


class IsInstalledTests(unittest.TestCase):
    def test_installed_and_source_runs(self):
        base = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            base, ignore_errors=True))
        venv_python = os.path.join(base, "venv", "bin", "python")
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": base}):
            with mock.patch.object(sys, "executable", venv_python):
                self.assertTrue(update.is_installed())
            with mock.patch.object(sys, "executable", "/usr/bin/python3"):
                self.assertFalse(update.is_installed())

    def test_venv_python_is_a_symlink(self):
        """A venv's python is a symlink to the system python: the
        check must still say installed (the v0.7 live test caught
        realpath resolving it out of the venv)."""
        base = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            base, ignore_errors=True))
        bin_dir = os.path.join(base, "venv", "bin")
        os.makedirs(bin_dir)
        link = os.path.join(bin_dir, "python")
        os.symlink(sys.executable, link)
        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": base}):
            with mock.patch.object(sys, "executable", link):
                self.assertTrue(update.is_installed())


class UpdaterScriptTests(unittest.TestCase):
    """v0.7: the generated updater script's content, syntax, and a
    live run against a fake install."""

    def test_script_contents(self):
        text = update.updater_script_text(1234, "/tmp/x/mimik-v9.9.zip",
                                          "9.9", base="/tmp/x/data")
        self.assertIn("PID=1234", text)
        self.assertIn("kill -0 \"$PID\"", text)
        # A zombie still answers kill -0, so the wait also checks for
        # the zombie state, and gives up after 2 minutes instead of
        # waiting forever.
        self.assertIn("Z\" \"/proc/$PID/status\"", text)
        self.assertIn("WAITED\" -ge 120", text)
        self.assertIn("install.sh\" < /dev/null", text)
        # install.sh needs BASH (set -euo pipefail dies under dash):
        # the updater must run it with bash, never sh.
        self.assertIn("bash \"$INSTALL_DIR/install.sh\"", text)
        # A stray PYTHONPATH would make the venv's pip import the
        # system pip's internals and crash, so the updater unsets it.
        self.assertIn("unset PYTHONPATH PYTHONHOME", text)
        self.assertIn("mimik-gui", text)
        self.assertIn("failed.txt", text)
        self.assertIn("rm -rf \"$PENDING\"", text)
        self.assertIn("/tmp/x/mimik-v9.9.zip", text)
        self.assertIn("PID=0",
                      update.updater_script_text(0, "/tmp/x.zip", "9.9",
                                                 base="/tmp/x/data"))

    def test_script_quotes_paths_with_spaces(self):
        text = update.updater_script_text(
            0, "/tmp/a b/mimik-v9.9.zip", "9.9", base="/tmp/a b/data")
        self.assertIn("'/tmp/a b/mimik-v9.9.zip'", text)
        self.assertIn("'/tmp/a b/data/updates'", text)

    def test_script_passes_shell_syntax_check(self):
        text = update.updater_script_text(0, "/tmp/x.zip", "9.9",
                                          base="/tmp/x/data")
        with tempfile.NamedTemporaryFile("w", suffix=".sh",
                                         delete=False) as f:
            f.write(text)
            path = f.name
        try:
            r = subprocess.run(["sh", "-n", path],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            os.unlink(path)

    def _fake_install(self, home):
        """A fake HOME with a fake mimik-gui launcher, and a fake
        update zip whose install.sh appends to a marker file."""
        bindir = os.path.join(home, ".local", "bin")
        os.makedirs(bindir, exist_ok=True)
        gui = os.path.join(bindir, "mimik-gui")
        with open(gui, "w") as f:
            f.write("#!/bin/sh\necho relaunched >> "
                    + os.path.join(home, "gui.log") + "\n")
        os.chmod(gui, 0o755)
        zippath = os.path.join(home, "mimik-v9.9.zip")
        with zipfile.ZipFile(zippath, "w") as zf:
            # The fake installer uses set -euo pipefail like the real
            # one, so running it under sh (dash) fails the live test.
            zf.writestr("mimik-v9.9/install.sh",
                        "#!/usr/bin/env bash\nset -euo pipefail\n"
                        "echo installed >> "
                        + os.path.join(home, "install.log") + "\n")
        return zippath

    def test_script_runs_install_and_relaunches(self):
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            home, ignore_errors=True))
        base = os.path.join(home, ".local", "share", "mimik")
        zippath = self._fake_install(home)
        pdir = update.pending_dir(base=base)
        os.makedirs(pdir, exist_ok=True)
        staged = os.path.join(pdir, "mimik-v9.9.zip")
        with open(zippath, "rb") as src, open(staged, "wb") as dst:
            dst.write(src.read())
        script = os.path.join(home, "update.sh")
        with open(script, "w") as f:
            f.write(update.updater_script_text(0, staged, "9.9",
                                               base=base))
        os.chmod(script, 0o700)
        env = dict(os.environ, HOME=home)
        env.pop("MIMIK_DATA_DIR", None)
        r = subprocess.run([script], capture_output=True, text=True,
                           timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(home, "install.log")) as f:
            self.assertIn("installed", f.read())
        with open(os.path.join(home, "gui.log")) as f:
            self.assertIn("relaunched", f.read())
        self.assertFalse(os.path.exists(pdir))
        log = os.path.join(base, "updates", "update.log")
        with open(log) as f:
            self.assertIn("installed 9.9", f.read())

    def test_script_waits_for_the_old_process(self):
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            home, ignore_errors=True))
        base = os.path.join(home, ".local", "share", "mimik")
        zippath = self._fake_install(home)
        pdir = update.pending_dir(base=base)
        os.makedirs(pdir, exist_ok=True)
        staged = os.path.join(pdir, "mimik-v9.9.zip")
        with open(zippath, "rb") as src, open(staged, "wb") as dst:
            dst.write(src.read())
        sleeper = subprocess.Popen(["sleep", "3"])
        self.addCleanup(sleeper.wait)
        script = os.path.join(home, "update.sh")
        with open(script, "w") as f:
            f.write(update.updater_script_text(sleeper.pid, staged,
                                               "9.9", base=base))
        os.chmod(script, 0o700)
        env = dict(os.environ, HOME=home)
        env.pop("MIMIK_DATA_DIR", None)
        proc = subprocess.Popen([script], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        time.sleep(1)
        # The sleeper is still alive, so the install must not have run.
        self.assertFalse(os.path.exists(
            os.path.join(home, "install.log")))
        proc.wait(timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(
            os.path.join(home, "install.log")))

    def test_script_failure_writes_failed_note(self):
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            home, ignore_errors=True))
        base = os.path.join(home, ".local", "share", "mimik")
        pdir = update.pending_dir(base=base)
        os.makedirs(pdir, exist_ok=True)
        staged = os.path.join(pdir, "mimik-v9.9.zip")
        with open(staged, "wb") as f:
            f.write(b"not a zip at all")
        script = os.path.join(home, "update.sh")
        with open(script, "w") as f:
            f.write(update.updater_script_text(0, staged, "9.9",
                                               base=base))
        os.chmod(script, 0o700)
        env = dict(os.environ, HOME=home)
        env.pop("MIMIK_DATA_DIR", None)
        r = subprocess.run([script], capture_output=True, text=True,
                           timeout=60, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(update.read_failed(base=base),
                         "could not unpack the downloaded update")
        self.assertFalse(os.path.exists(staged))


class ApplyPendingTests(unittest.TestCase):
    """v0.7: the launch-time hook."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.base, ignore_errors=True))

    def _stage(self, content=b"zip bytes"):
        zip_path = os.path.join(self.base, "mimik-v9.9.zip")
        with open(zip_path, "wb") as f:
            f.write(content)
        sha = hashlib.sha256(content).hexdigest()
        pdir = update.pending_dir(base=self.base)
        os.makedirs(pdir, exist_ok=True)
        staged = os.path.join(pdir, "mimik-v9.9.zip")
        with open(zip_path, "rb") as src, open(staged, "wb") as dst:
            dst.write(src.read())
        update.write_pending("9.9", staged, sha, base=self.base)
        return staged, sha

    def test_nothing_pending(self):
        self.assertEqual(update.apply_pending(base=self.base), "none")

    def test_valid_pending_launches_updater(self):
        staged, _sha = self._stage()
        launched = []
        result = update.apply_pending(launcher=launched.append,
                                      base=self.base)
        self.assertEqual(result, "apply")
        self.assertEqual(len(launched), 1)
        script = launched[0][0]
        self.assertTrue(os.path.isfile(script))
        self.assertTrue(os.access(script, os.X_OK))
        with open(script) as f:
            text = f.read()
        self.assertIn("PID=0", text)
        self.assertIn(staged, text)

    def test_tampered_pending_is_cleared(self):
        staged, _sha = self._stage()
        with open(staged, "wb") as f:
            f.write(b"tampered")
        self.assertEqual(update.apply_pending(base=self.base), "cleared")
        self.assertFalse(os.path.exists(
            update.pending_dir(base=self.base)))

    def test_leftover_junk_is_cleared(self):
        pdir = update.pending_dir(base=self.base)
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "orphan.zip"), "wb") as f:
            f.write(b"junk")
        self.assertEqual(update.apply_pending(base=self.base), "cleared")
        self.assertFalse(os.path.exists(pdir))

    def test_launcher_failure_is_none_not_raise(self):
        self._stage()

        def boom(argv):
            raise OSError("cannot launch")

        self.assertEqual(update.apply_pending(launcher=boom,
                                              base=self.base), "none")


if __name__ == "__main__":
    unittest.main()
