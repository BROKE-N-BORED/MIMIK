"""Tests for the shared flow fetch (item 32b)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import flows, store
from mimik.sources import mitm as mitm_mod


def _flow(fid, host):
    return {"id": fid,
            "request": {"host": host, "method": "GET", "path": "/",
                        "scheme": "https", "port": 443, "headers": []},
            "response": {"status_code": 200, "headers": []}}


class FetchFlowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def test_live_path_filters_and_saves(self):
        live = [_flow("f1", "game.example.com"),
                _flow("f2", "google-analytics.com")]
        fake = mock.Mock()
        fake.flows.return_value = live
        fake.body.return_value = b""  # else _embed_bodies can't serialize
        with mock.patch.object(mitm_mod, "Mitm", return_value=fake):
            m, got, source = flows.fetch_flows()
        self.assertIs(m, fake)
        self.assertEqual(source, "live")
        # The excluded host never reaches the returned flows or the
        # store (item 26, via the shared fetch).
        self.assertEqual([f["id"] for f in got], ["f1"])
        self.assertEqual(len(store.load_host("game.example.com")), 1)
        self.assertEqual(store.load_host("google-analytics.com"), [])

    def test_saved_path_when_proxy_down(self):
        store.save_flows([_flow("f1", "game.example.com")], base=self.tmp)
        with mock.patch.object(mitm_mod, "Mitm",
                               side_effect=mitm_mod.MitmError("down")):
            m, got, source = flows.fetch_flows()
        self.assertIsNone(m)
        self.assertEqual(source, "saved")
        self.assertEqual([f["id"] for f in got], ["f1"])

    def test_saved_path_empty_when_nothing_saved(self):
        with mock.patch.object(mitm_mod, "Mitm",
                               side_effect=mitm_mod.MitmError("down")):
            m, got, source = flows.fetch_flows()
        self.assertIsNone(m)
        self.assertEqual(source, "saved")
        self.assertEqual(got, [])

    def test_failed_save_is_logged_not_fatal(self):
        fake = mock.Mock()
        fake.flows.return_value = [_flow("f1", "game.example.com")]
        with mock.patch.object(mitm_mod, "Mitm", return_value=fake), \
             mock.patch.object(store, "save_flows",
                               side_effect=OSError("disk full")), \
             mock.patch("mimik.flows.log") as log_mod:
            m, got, source = flows.fetch_flows(where="test")
        self.assertEqual(source, "live")
        self.assertEqual([f["id"] for f in got], ["f1"])
        log_mod.log_error.assert_called_once()
        self.assertIn("test save_flows",
                      log_mod.log_error.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
