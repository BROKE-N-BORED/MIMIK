"""Tests for mimik.wine — .reg generation, proxy commands, CA install,
revert logic. All mocked; no real wine runs in tests."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import wine


class RegTests(unittest.TestCase):
    def test_proxy_reg_keys(self):
        reg = wine.build_proxy_reg()
        self.assertIn("Windows Registry Editor Version 5.00", reg)
        self.assertIn(
            r"[HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion"
            r"\Internet Settings]", reg)
        self.assertIn('"ProxyEnable"=dword:00000001', reg)
        self.assertIn('"ProxyServer"="127.0.0.1:8080"', reg)
        self.assertIn('"ProxyOverride"="localhost;127.0.0.1;<local>"', reg)

    def test_proxy_reg_custom_port(self):
        reg = wine.build_proxy_reg(proxy_port=8888)
        self.assertIn('"ProxyServer"="127.0.0.1:8888"', reg)

    def test_disable_reg(self):
        reg = wine.build_disable_reg()
        self.assertIn('"ProxyEnable"=dword:00000000', reg)

    def test_restore_reg_restores_values(self):
        state = {"ProxyEnable": 1, "ProxyServer": "10.0.0.1:3128",
                 "ProxyOverride": "<local>"}
        reg = wine.build_restore_reg(state)
        self.assertIn('"ProxyEnable"=dword:00000001', reg)
        self.assertIn('"ProxyServer"="10.0.0.1:3128"', reg)
        self.assertIn('"ProxyOverride"="<local>"', reg)

    def test_restore_reg_deletes_absent_values(self):
        state = {"ProxyEnable": None, "ProxyServer": None,
                 "ProxyOverride": None}
        reg = wine.build_restore_reg(state)
        self.assertIn('"ProxyEnable"=-', reg)
        self.assertIn('"ProxyServer"=-', reg)
        self.assertIn('"ProxyOverride"=-', reg)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_read_proxy_state_from_user_reg(self):
        user_reg = os.path.join(self.tmp, "user.reg")
        with open(user_reg, "w") as f:
            f.write(
                "[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\"
                "Internet Settings]\n"
                '"ProxyEnable"=dword:00000001\n'
                '"ProxyServer"="10.0.0.1:3128"\n'
                '"ProxyOverride"="<local>"\n'
            )
        state = wine.read_proxy_state(self.tmp)
        self.assertEqual(state["ProxyEnable"], 1)
        self.assertEqual(state["ProxyServer"], "10.0.0.1:3128")
        self.assertEqual(state["ProxyOverride"], "<local>")

    def test_read_proxy_state_missing_file(self):
        state = wine.read_proxy_state(self.tmp)
        self.assertEqual(state, {"ProxyEnable": None, "ProxyServer": None,
                                 "ProxyOverride": None})

    def test_save_and_load_state(self):
        path = os.path.join(self.tmp, "state.json")
        state = {"ProxyEnable": 0, "ProxyServer": None, "ProxyOverride": None}
        wine.save_proxy_state(state, path)
        self.assertEqual(wine.load_proxy_state(path), state)

    def test_load_state_missing_returns_none(self):
        self.assertIsNone(wine.load_proxy_state(
            os.path.join(self.tmp, "nope.json")))


class CommandTests(unittest.TestCase):
    def test_regedit_command(self):
        cmd = wine.regedit_command("/tmp/x.reg")
        self.assertEqual(cmd, ["wine", "regedit", "/s", "/tmp/x.reg"])

    def test_ca_install_command(self):
        cmd = wine.ca_install_command("/home/u/.mitmproxy/mitmproxy-ca-cert.pem")
        self.assertEqual(cmd[:5],
                         ["wine", "certutil", "-addstore", "-f", "-user"])
        self.assertEqual(cmd[5], "Root")
        self.assertTrue(cmd[6].endswith("mitmproxy-ca-cert.pem"))

    def test_apply_reg_text_runs_regedit(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return mock.Mock()

        wine.apply_reg_text(wine.build_proxy_reg(), prefix="/tmp/prefix",
                            runner=runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], ["wine", "regedit", "/s"])
        self.assertTrue(calls[0][3].endswith(".reg"))

    def test_install_ca_missing_cert_raises(self):
        with self.assertRaises(wine.WineError):
            wine.install_ca(ca_path="/tmp/does-not-exist.pem",
                            prefix="/tmp/prefix", runner=mock.Mock())

    def test_install_ca_runs_certutil(self):
        with tempfile.NamedTemporaryFile(suffix=".pem",
                                         delete=False) as f:
            ca = f.name
        try:
            calls = []

            def runner(cmd, **kw):
                calls.append(cmd)
                return mock.Mock()

            wine.install_ca(ca_path=ca, prefix="/tmp/prefix", runner=runner)
            self.assertEqual(calls[0][1], "certutil")
            self.assertIn(ca, calls[0])
        finally:
            os.unlink(ca)

    def test_apply_proxy_saves_state_then_applies(self):
        prefix = tempfile.mkdtemp()
        state_file = os.path.join(prefix, "state.json")
        with mock.patch.object(wine, "state_path", return_value=state_file), \
             mock.patch.object(wine, "apply_reg_text") as apply_mock:
            wine.apply_proxy(prefix, runner=mock.Mock())
        self.assertTrue(os.path.exists(state_file))
        with open(state_file) as f:
            saved = json.load(f)
        self.assertIn("ProxyEnable", saved)
        apply_mock.assert_called_once()
        reg_text = apply_mock.call_args[0][0]
        self.assertIn('"ProxyEnable"=dword:00000001', reg_text)

    def test_revert_proxy_restores_saved_state(self):
        prefix = tempfile.mkdtemp()
        state_file = os.path.join(prefix, "state.json")
        with open(state_file, "w") as f:
            json.dump({"ProxyEnable": 1, "ProxyServer": "10.0.0.1:3128",
                       "ProxyOverride": None}, f)
        with mock.patch.object(wine, "state_path", return_value=state_file), \
             mock.patch.object(wine, "apply_reg_text") as apply_mock:
            wine.revert_proxy(prefix, runner=mock.Mock())
        reg_text = apply_mock.call_args[0][0]
        self.assertIn('"ProxyServer"="10.0.0.1:3128"', reg_text)
        self.assertFalse(os.path.exists(state_file))

    def test_revert_proxy_without_saved_state_disables(self):
        prefix = tempfile.mkdtemp()
        state_file = os.path.join(prefix, "missing.json")
        with mock.patch.object(wine, "state_path", return_value=state_file), \
             mock.patch.object(wine, "apply_reg_text") as apply_mock:
            wine.revert_proxy(prefix, runner=mock.Mock())
        reg_text = apply_mock.call_args[0][0]
        self.assertIn('"ProxyEnable"=dword:00000000', reg_text)

    def test_double_apply_preserves_original_state(self):
        # Review #3: a second apply must NOT overwrite the saved
        # ORIGINAL state with the proxy-on settings — otherwise the
        # prefix could never be restored to what it was.
        prefix = tempfile.mkdtemp()
        state_file = os.path.join(prefix, "state.json")
        user_reg = os.path.join(prefix, "user.reg")
        bs = chr(92)  # user.reg doubles every backslash
        section = ("[Software" + bs * 2 + "Microsoft" + bs * 2 +
                   "Windows" + bs * 2 + "CurrentVersion" + bs * 2 +
                   "Internet Settings]" + chr(10))
        original = ("10.0.0.1:3128", "<local>")
        with open(user_reg, "w") as f:
            f.write(section +
                    '"ProxyEnable"=dword:00000001\n'
                    f'"ProxyServer"="{original[0]}"\n'
                    f'"ProxyOverride"="{original[1]}"\n')
        with mock.patch.object(wine, "state_path",
                               return_value=state_file), \
             mock.patch.object(wine, "apply_reg_text"):
            wine.apply_proxy(prefix, runner=mock.Mock())
            # The first apply pointed the prefix at the proxy — the
            # second apply now reads the proxy-on state.
            with open(user_reg, "w") as f:
                f.write(section +
                        '"ProxyEnable"=dword:00000001\n'
                        '"ProxyServer"="127.0.0.1:8080"\n'
                        '"ProxyOverride"="localhost;127.0.0.1;<local>"\n')
            wine.apply_proxy(prefix, runner=mock.Mock())
        with open(state_file) as f:
            saved = json.load(f)
        self.assertEqual(saved["ProxyServer"], original[0])
        self.assertEqual(saved["ProxyOverride"], original[1])
        self.assertEqual(saved["ProxyEnable"], 1)

    def test_has_saved_state(self):
        prefix = tempfile.mkdtemp()
        state_file = os.path.join(prefix, "state.json")
        with mock.patch.object(wine, "state_path",
                               return_value=state_file):
            self.assertFalse(wine.has_saved_state())
            wine.save_proxy_state({"ProxyEnable": None,
                                   "ProxyServer": None,
                                   "ProxyOverride": None},
                                  path=state_file)
            self.assertTrue(wine.has_saved_state())


class ReadyTests(unittest.TestCase):
    def test_check_ready_no_wine(self):
        with mock.patch.object(wine, "wine_available", return_value=False):
            ok, msg = wine.check_ready()
        self.assertFalse(ok)
        self.assertIn("WinLinx", msg)

    def test_check_ready_no_winlinx(self):
        with mock.patch.object(wine, "wine_available", return_value=True), \
             mock.patch.object(wine, "winlinx_available", return_value=False):
            ok, msg = wine.check_ready()
        self.assertFalse(ok)
        self.assertIn("WinLinx", msg)

    def test_check_ready_ok(self):
        prefix = tempfile.mkdtemp()
        with mock.patch.object(wine, "wine_available", return_value=True), \
             mock.patch.object(wine, "winlinx_available", return_value=True), \
             mock.patch.object(wine, "wine_prefix", return_value=prefix):
            ok, _msg = wine.check_ready()
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
