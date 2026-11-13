"""One shared doctor (item 12): structured results, both front-ends
render the same data — the GUI no longer hardcodes the Qt check."""
import builtins
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import doctor
from mimik.sources.mitm import MitmError


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_DATA_DIR"]

    def _run(self):
        with mock.patch("mimik.sources.mitm.Mitm") as mock_mitm:
            mock_mitm.return_value.flows.side_effect = MitmError("down")
            return doctor.doctor()

    def test_structure(self):
        result = self._run()
        self.assertEqual(set(result),
                         {"ok", "required", "mitmweb_running",
                          "optional", "info"})
        self.assertEqual(len(result["required"]), 4)
        self.assertEqual(len(result["optional"]), 8)
        self.assertEqual(len(result["info"]), 3)
        for c in result["required"] + result["optional"]:
            self.assertIn("name", c)
            self.assertIn("ok", c)
            self.assertIn("fix", c)
        names = [c["name"] for c in result["required"]]
        self.assertTrue(any("python" in n for n in names))
        self.assertIn("requests library", names)
        self.assertIn("proxy (mitmweb or uvx)", names)
        self.assertIn("Qt GUI (PySide6)", names)
        # v0.4 item 14: the unpin tools are optional checks.
        opt_names = [c["name"] for c in result["optional"]]
        self.assertIn("git (fetch unpinning scripts)", opt_names)
        self.assertIn("frida (defeat pinning in the app)", opt_names)
        self.assertIn("objection (patch APKs for the Frida gadget)",
                      opt_names)
        self.assertFalse(result["mitmweb_running"])
        self.assertIn("disk usage:", result["info"][1])
        self.assertIn("LAN IP", result["info"][2])

    def test_qt_check_actually_imports(self):
        # Fake PySide6 present -> the check passes.
        fake = mock.Mock()
        with mock.patch.dict(sys.modules, {"PySide6": fake}):
            result = self._run()
        qt = [c for c in result["required"]
              if c["name"] == "Qt GUI (PySide6)"][0]
        self.assertTrue(qt["ok"])
        # PySide6 unimportable -> the check fails (no hardcoding).
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "PySide6":
                raise ImportError("no PySide6 here")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__",
                        side_effect=fake_import):
            result = self._run()
        qt = [c for c in result["required"]
              if c["name"] == "Qt GUI (PySide6)"][0]
        self.assertFalse(qt["ok"])
        self.assertEqual(qt["fix"], "pip install PySide6")
        self.assertFalse(result["ok"])

    def test_missing_proxy_fails_ok(self):
        with mock.patch("mimik.doctor.mitmweb_cmd", return_value=None):
            result = self._run()
        proxy = [c for c in result["required"]
                 if c["name"] == "proxy (mitmweb or uvx)"][0]
        self.assertFalse(proxy["ok"])
        self.assertFalse(result["ok"])

    def test_reblend_detail_when_found(self):
        with mock.patch("mimik.doctor.reblend_bridge.find_reblend",
                        return_value="/home/u/reblend"):
            result = self._run()
        rb = [c for c in result["optional"]
              if c["name"] == "Reblend (asset packs)"][0]
        self.assertTrue(rb["ok"])
        self.assertEqual(rb["detail"], "Reblend folder: /home/u/reblend")

    def test_lan_ip_returns_something(self):
        ip = doctor.lan_ip()
        self.assertIsInstance(ip, str)
        self.assertTrue(ip)  # an address, or the placeholder


if __name__ == "__main__":
    unittest.main()
