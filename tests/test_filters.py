"""Tests for capture filtering — exclude hosts from a capture (item 26)."""
import argparse
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import cli, filters, presets, recorder, store


def flow(fid, host):
    return {
        "id": fid,
        "request": {"host": host, "method": "GET", "path": "/v1/x",
                    "scheme": "https", "port": 443, "headers": []},
        "response": {"status_code": 200, "headers": []},
    }


class MatchTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(filters.is_excluded(
            "google-analytics.com", ["google-analytics.com"]))

    def test_subdomain_matches(self):
        self.assertTrue(filters.is_excluded(
            "ssl.google-analytics.com", ["google-analytics.com"]))
        self.assertTrue(filters.is_excluded(
            "a.b.sentry.io", ["sentry.io"]))

    def test_dot_boundary(self):
        # A longer name that merely ENDS with the pattern is not a
        # subdomain of it.
        self.assertFalse(filters.is_excluded(
            "evilgoogle-analytics.com", ["google-analytics.com"]))
        self.assertFalse(filters.is_excluded(
            "mysentry.io", ["sentry.io"]))

    def test_case_and_whitespace(self):
        self.assertTrue(filters.is_excluded(
            "  SSL.Google-Analytics.COM ", ["google-analytics.com"]))

    def test_no_match_and_empty(self):
        self.assertFalse(filters.is_excluded(
            "api.example.com", ["google-analytics.com"]))
        self.assertFalse(filters.is_excluded("", ["google-analytics.com"]))
        self.assertFalse(filters.is_excluded("google-analytics.com", []))
        self.assertFalse(filters.is_excluded("google-analytics.com", None))


class FilterFlowsTests(unittest.TestCase):
    def test_mixed_flows(self):
        flows = [flow("1", "game.example.com"),
                 flow("2", "google-analytics.com"),
                 flow("3", "ssl.google-analytics.com"),
                 flow("4", "api.example.com")]
        kept, dropped = filters.filter_flows(
            flows, ["google-analytics.com"])
        self.assertEqual([f["id"] for f in kept], ["1", "4"])
        self.assertEqual(dropped, {"google-analytics.com",
                                   "ssl.google-analytics.com"})


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_defaults_on_first_use(self):
        self.assertEqual(presets.excluded_hosts(),
                         list(filters.DEFAULT_EXCLUDED))
        self.assertEqual(len(filters.DEFAULT_EXCLUDED), 12)

    def test_set_round_trip_normalizes_and_dedupes(self):
        got = presets.set_excluded_hosts(
            ["  Telemetry.Example.COM ", "telemetry.example.com",
             "other.example.com", ""])
        self.assertEqual(got, ["telemetry.example.com",
                               "other.example.com"])
        self.assertEqual(presets.excluded_hosts(), got)

    def test_explicitly_empty_stays_empty(self):
        presets.set_excluded_hosts([])
        self.assertEqual(presets.excluded_hosts(), [])

    def test_toggle_defaults_on(self):
        self.assertTrue(presets.filtering_enabled())
        presets.set_filtering_enabled(False)
        self.assertFalse(presets.filtering_enabled())
        presets.set_filtering_enabled(True)
        self.assertTrue(presets.filtering_enabled())

    def test_apply_honors_toggle(self):
        flows = [flow("1", "game.example.com"),
                 flow("2", "google-analytics.com")]
        kept, dropped = filters.apply_capture_filter(flows)
        self.assertEqual([f["id"] for f in kept], ["1"])
        self.assertEqual(dropped, {"google-analytics.com"})
        presets.set_filtering_enabled(False)
        kept, dropped = filters.apply_capture_filter(flows)
        self.assertEqual([f["id"] for f in kept], ["1", "2"])
        self.assertEqual(dropped, set())

    def test_settings_preserve_other_keys(self):
        presets.set_auto_learn(True)
        presets.set_excluded_hosts(["x.example.com"])
        self.assertTrue(presets.auto_learn_enabled())
        presets.set_filtering_enabled(False)
        self.assertEqual(presets.excluded_hosts(), ["x.example.com"])


class RecorderFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def test_poll_drops_excluded_hosts_from_store_and_rows(self):
        # The verify line: an excluded host's flows never reach the
        # store or the host list.
        s = recorder.ProxySession()
        s.mitm = mock.Mock()
        s.mitm.flows.return_value = [
            flow("1", "game.example.com"),
            flow("2", "google-analytics.com"),
            flow("3", "ssl.google-analytics.com"),
        ]
        s.mitm.body.return_value = b""
        rows = s.poll()
        self.assertEqual(rows, [("game.example.com", 1)])
        hosts = [h for h, _n, _t in store.list_captures()]
        self.assertEqual(hosts, ["game.example.com"])
        self.assertEqual(store.load_host("google-analytics.com"), [])

    def test_poll_logs_dropped_hosts_once_per_change(self):
        from mimik import log as mimik_log

        s = recorder.ProxySession()
        s.mitm = mock.Mock()
        s.mitm.flows.return_value = [flow("2", "google-analytics.com")]
        s.poll()
        s.poll()
        text = mimik_log.read_tail()
        self.assertEqual(text.count("excluded 1 host(s)"), 1)
        self.assertIn("google-analytics.com", text)


class ExcludeCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(argv)
        return out.getvalue()

    def test_list_add_remove_reset(self):
        out = self._run(["exclude", "list"])
        self.assertIn("Capture filtering: on", out)
        self.assertIn("google-analytics.com", out)
        out = self._run(["exclude", "add", "telemetry.example.com"])
        self.assertIn("Excluded telemetry.example.com", out)
        self.assertIn("telemetry.example.com", presets.excluded_hosts())
        out = self._run(["exclude", "add", "telemetry.example.com"])
        self.assertIn("already excluded", out)
        out = self._run(["exclude", "remove", "telemetry.example.com"])
        self.assertIn("will be captured again", out)
        self.assertNotIn("telemetry.example.com",
                         presets.excluded_hosts())
        out = self._run(["exclude", "remove", "telemetry.example.com"])
        self.assertIn("not on the exclusion list", out)
        presets.set_excluded_hosts(["only.example.com"])
        out = self._run(["exclude", "reset"])
        self.assertIn("restored to the 12 defaults", out)
        self.assertEqual(presets.excluded_hosts(),
                         list(filters.DEFAULT_EXCLUDED))

    def test_on_off(self):
        self._run(["exclude", "off"])
        self.assertFalse(presets.filtering_enabled())
        self._run(["exclude", "on"])
        self.assertTrue(presets.filtering_enabled())
