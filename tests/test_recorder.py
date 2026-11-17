"""Tests for mimik.recorder — mitmweb discovery."""
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import recorder


class MitmwebCmdTests(unittest.TestCase):
    def test_finds_venv_mitmweb_when_not_on_path(self):
        # Regression: the installer puts mitmproxy in MIMIK's venv, whose
        # bin dir is not on PATH — record must still find it there.
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "mitmweb")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
            with mock.patch.object(recorder.shutil, "which",
                                   return_value=None), \
                    mock.patch.object(recorder.sys, "executable",
                                      os.path.join(tmp, "python")):
                self.assertEqual(recorder.mitmweb_cmd(), [fake])

    def test_none_when_mitmweb_nowhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(recorder.shutil, "which",
                                   return_value=None), \
                    mock.patch.object(recorder.sys, "executable",
                                      os.path.join(tmp, "python")):
                self.assertIsNone(recorder.mitmweb_cmd())

    def test_path_mitmweb_wins(self):
        with mock.patch.object(
                recorder.shutil, "which",
                side_effect=lambda c: "/usr/bin/mitmweb"
                if c == "mitmweb" else None):
            self.assertEqual(recorder.mitmweb_cmd(), ["mitmweb"])


class PollDroppedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_poll_reports_dropped_hosts(self):
        # v0.6 T9: poll calls on_dropped with the sorted hosts the
        # filter dropped, when the set changes (including back to
        # empty); an unchanged set calls nothing.
        from mimik import presets

        presets.set_filtering_enabled(True)
        presets.set_excluded_hosts(["google-analytics.com"])
        kept = {"id": "1",
                "request": {"host": "game.example.com", "method": "GET",
                            "path": "/v1/x", "scheme": "https",
                            "port": 443, "headers": []},
                "response": {"status_code": 200, "headers": []}}
        dropped = {"id": "2",
                   "request": {"host": "google-analytics.com",
                               "method": "GET", "path": "/g/collect",
                               "scheme": "https", "port": 443,
                               "headers": []},
                   "response": {"status_code": 200, "headers": []}}
        seen = []
        s = recorder.ProxySession(on_dropped=seen.append)
        s.mitm = mock.Mock()
        s.mitm.flows.return_value = [kept, dropped]
        s.poll()
        self.assertEqual(seen, [["google-analytics.com"]])
        s.poll()
        self.assertEqual(len(seen), 1)
        s.mitm.flows.return_value = [kept]
        s.poll()
        self.assertEqual(seen[-1], [])


class ProxySessionStartTests(unittest.TestCase):
    def test_start_passes_unbuffered_env(self):
        # Regression: mitmweb buffers its startup lines when stdout is a
        # file, so the token line never reached the log and /flows 403'd.
        with mock.patch.object(recorder, "mitmweb_cmd",
                               return_value=["mitmweb"]), \
             mock.patch.object(recorder.subprocess, "Popen") as popen, \
             mock.patch.object(recorder, "Mitm") as mitm_cls:
            proc = mock.Mock()
            proc.poll.return_value = None
            popen.return_value = proc
            mitm_cls.return_value.flows.return_value = []
            s = recorder.ProxySession()
            self.assertTrue(s.start(timeout=5))
            _, kwargs = popen.call_args
            self.assertEqual(kwargs["env"].get("PYTHONUNBUFFERED"), "1")
            s._log_fh.close()
            s.proc = None

    def test_start_writes_log_0600(self):
        # Review #13: the mitmweb log holds the web-UI token — owner only.
        with mock.patch.object(recorder, "mitmweb_cmd",
                               return_value=["mitmweb"]), \
             mock.patch.object(recorder.subprocess, "Popen") as popen, \
             mock.patch.object(recorder, "Mitm") as mitm_cls:
            proc = mock.Mock()
            proc.poll.return_value = None
            popen.return_value = proc
            mitm_cls.return_value.flows.return_value = []
            s = recorder.ProxySession()
            self.assertTrue(s.start(timeout=5))
            mode = stat.S_IMODE(os.stat(s.log_path).st_mode)
            self.assertEqual(mode, 0o600)
            s._log_fh.close()
            s.proc = None

    def test_failed_start_kills_orphan(self):
        with mock.patch.object(recorder, "mitmweb_cmd",
                               return_value=["mitmweb"]), \
             mock.patch.object(recorder.subprocess, "Popen") as popen, \
             mock.patch.object(recorder, "Mitm") as mitm_cls:
            proc = mock.Mock()
            proc.poll.return_value = None
            popen.return_value = proc
            mitm_cls.return_value.flows.side_effect = recorder.MitmError(
                "403")
            s = recorder.ProxySession()
            with self.assertRaises(recorder.MitmError):
                s.start(timeout=1)
            proc.terminate.assert_called_once()
            self.assertIsNone(s.proc)


if __name__ == "__main__":
    unittest.main()
