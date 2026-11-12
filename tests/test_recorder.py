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
