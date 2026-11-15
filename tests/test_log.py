"""Tests for mimik.log — the log file + swallowed-error recording (item 23)."""
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import log, recorder, store


class LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def _read_log(self):
        with open(log.log_path(), encoding="utf-8") as f:
            return f.read()

    def test_log_error_writes_timestamped_entry_with_traceback(self):
        # The verify line: a forced error lands in the log.
        try:
            raise ValueError("forced-boom-123")
        except ValueError as e:
            log.log_error("test site", e)
        text = self._read_log()
        self.assertIn("ERROR test site: forced-boom-123", text)
        self.assertIn("ValueError: forced-boom-123", text)
        self.assertIn("test_log_error_writes_timestamped_entry", text)
        # Timestamped: [YYYY-MM-DD HH:MM:SS]
        self.assertRegex(text, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")

    def test_log_file_is_0600(self):
        log.log_info("test", "hello")
        mode = stat.S_IMODE(os.stat(log.log_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_log_info_writes_entry(self):
        log.log_info("startup", "MIMIK 0.3.0 started")
        self.assertIn("INFO startup: MIMIK 0.3.0 started", self._read_log())

    def test_read_tail_returns_last_lines(self):
        for i in range(5):
            log.log_info("test", f"line {i}")
        tail = log.read_tail(lines=2)
        self.assertIn("line 3", tail)
        self.assertIn("line 4", tail)
        self.assertNotIn("line 0", tail)

    def test_read_tail_empty_when_no_log(self):
        self.assertEqual(log.read_tail(), "")

    def test_rotation_caps_the_file(self):
        # A >1 MB log is cut to its newest ~512 KB on the next write.
        path = log.log_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("x\n" * ((log.MAX_BYTES + 100) // 2))
        log.log_info("test", "after rotation")
        size = os.path.getsize(path)
        self.assertGreater(size, log.KEEP_BYTES - 10)
        self.assertLess(size, log.KEEP_BYTES + 1024)
        self.assertIn("after rotation", self._read_log())

    def test_ring_holds_last_200(self):
        # v0.5 item 15 (T21): the in-memory ring keeps the newest
        # 200 entries.
        log._RING.clear()
        for i in range(250):
            log.log_info("test", f"ring line {i}")
        ring = log.ring_lines()
        self.assertEqual(len(ring), 200)
        self.assertIn("ring line 249", ring[-1])
        self.assertIn("ring line 50", ring[0])
        self.assertFalse(any("ring line 49" in line for line in ring))
        log._RING.clear()

    def test_ring_records_errors_too(self):
        log._RING.clear()
        try:
            raise ValueError("ring-boom-456")
        except ValueError as e:
            log.log_error("test site", e)
        ring = log.ring_lines()
        self.assertEqual(len(ring), 1)
        self.assertIn("ring-boom-456", ring[0])
        log._RING.clear()

    def test_scrub_redacts_secrets(self):
        # v0.5 item 15 (T21): tokens never leave the machine raw.
        text = log.scrub(
            "get /x?token=abc123secret "
            "Authorization: Bearer xyz789token "
            "api_key=key456secret done")
        self.assertNotIn("abc123secret", text)
        self.assertNotIn("xyz789token", text)
        self.assertNotIn("key456secret", text)
        self.assertIn("token=<redacted>", text)
        self.assertIn("Authorization: <redacted>", text)
        self.assertIn("done", text)

    def test_scrub_leaves_plain_text_alone(self):
        self.assertEqual(log.scrub("nothing secret here"),
                         "nothing secret here")

    def test_debug_report_is_scrubbed_and_complete(self):
        # v0.5 item 15 (T21): version, platform, specs, recent log —
        # with secrets redacted.
        from mimik import __version__

        log._RING.clear()
        log.log_info("test", "report-marker-789 token=leakme123")
        report = log.debug_report()
        self.assertIn(f"MIMIK {__version__}", report)
        self.assertIn("Python", report)
        self.assertIn("this PC:", report)
        self.assertIn("report-marker-789", report)
        self.assertNotIn("leakme123", report)
        self.assertIn("<redacted>", report)
        log._RING.clear()

    def test_logging_never_raises(self):
        # The data dir is unusable (a file sits where the dir goes) —
        # logging still must not raise.
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        os.environ["MIMIK_DATA_DIR"] = os.path.join(blocker, "sub")
        log.log_error("test", ValueError("unwritable"))
        log.log_info("test", "unwritable")
        self.assertEqual(log.read_tail(), "")

    def test_swallowed_save_error_lands_in_log(self):
        # A swallowed site in action: recorder.poll's save fails, the
        # poll still returns its rows, and the error is in the log.
        flow = {"id": "p1",
                "request": {"host": "log.example.com", "method": "GET",
                            "path": "/x", "scheme": "https", "port": 443,
                            "headers": []},
                "response": {"status_code": 200, "headers": []}}
        s = recorder.ProxySession()
        s.mitm = mock.Mock()
        s.mitm.flows.return_value = [flow]
        with mock.patch.object(store, "save_flows",
                               side_effect=OSError("disk full (test)")):
            rows = s.poll()
        self.assertEqual(rows, [("log.example.com", 1)])
        text = self._read_log()
        self.assertIn("ERROR recorder poll save_flows: disk full (test)",
                      text)
