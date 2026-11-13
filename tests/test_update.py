"""Tests for the in-app update check (item 31)."""
import argparse
import json
import os
import sys
import unittest
import urllib.error
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


if __name__ == "__main__":
    unittest.main()
