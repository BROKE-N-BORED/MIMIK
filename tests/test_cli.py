"""Tests for the mimik CLI — hosts/learn/gen against the fixture HAR."""
import argparse
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import cli

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mimik.har")


def run_cmd(func, args):
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            func(args)
    except SystemExit as e:
        return out.getvalue(), e.code
    return out.getvalue(), 0


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = self.tmp
        self.orig_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_dir)
        del os.environ["MIMIK_DATA_DIR"]

    def test_hosts_from_har(self):
        args = argparse.Namespace(har=FIXTURE, curl=None)
        out, code = run_cmd(cli.cmd_hosts, args)
        self.assertEqual(code, 0)
        self.assertIn("api.example.com", out)
        self.assertIn("cdn.example.com", out)

    def test_learn_from_har(self):
        args = argparse.Namespace(host="api.example.com", har=FIXTURE,
                                  curl=None)
        out, code = run_cmd(cli.cmd_learn, args)
        self.assertEqual(code, 0)
        self.assertIn("4 endpoints", out)
        self.assertIn("POST  /v1/like", out)
        self.assertIn("GET   /v1/users", out)

    def test_learn_unknown_host_exits(self):
        args = argparse.Namespace(host="nope.example.com", har=FIXTURE,
                                  curl=None)
        out, code = run_cmd(cli.cmd_learn, args)
        self.assertNotEqual(code, 0)

    def test_gen_from_har_writes_client(self):
        out_path = os.path.join(self.tmp, "api_client.py")
        args = argparse.Namespace(host="api.example.com", har=FIXTURE,
                                  curl=None, out=out_path, model=None,
                                  polish="none", prompt_only=False)
        out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(out_path))
        with open(out_path) as f:
            src = f.read()
        compile(src, "<gen>", "exec")
        self.assertIn("class ExampleClient(App):", src)
        self.assertIn("def like(", src)
        self.assertNotIn("SECRETTOKEN123", src)
        self.assertIn("wrote", out)

    def test_gen_default_out_name(self):
        args = argparse.Namespace(host="api.example.com", har=FIXTURE,
                                  curl=None, out=None, model=None,
                                  polish="none", prompt_only=False)
        out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "api_client.py")))

    def test_gen_prompt_only(self):
        args = argparse.Namespace(host="api.example.com", har=FIXTURE,
                                  curl=None, out=None, model=None,
                                  polish="none", prompt_only=True)
        out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        self.assertIn("mimik.App", out)
        self.assertIn("### POST /v1/like", out)

    def test_gen_from_curl(self):
        curl_text = (
            "curl 'https://api.example.com/v1/like' -X POST "
            "-H 'Authorization: Bearer SECRETTOKEN123' "
            "--data '{\"subjectId\": \"abc\"}'"
        )
        args = argparse.Namespace(host="api.example.com", har=None,
                                  curl=curl_text, out=None, model=None,
                                  polish="none", prompt_only=False)
        out, code = run_cmd(cli.cmd_gen, args)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "api_client.py")))

    def test_hosts_from_curl(self):
        curl_text = "curl 'https://api.example.com/v1/x' -H 'A: b'"
        args = argparse.Namespace(har=None, curl=curl_text)
        out, code = run_cmd(cli.cmd_hosts, args)
        self.assertEqual(code, 0)
        self.assertIn("api.example.com", out)

    def test_clients_lists_generated_and_saved(self):
        from mimik import store

        store.save_client_copy("api_client.py", "x=1\n", base=self.tmp)
        store.save_flows([{
            "id": "1",
            "request": {"host": "api.example.com", "method": "GET",
                        "path": "/x", "scheme": "https", "port": 443,
                        "headers": []},
            "response": {"status_code": 200},
        }], base=self.tmp)
        out, code = run_cmd(cli.cmd_clients, argparse.Namespace())
        self.assertEqual(code, 0)
        self.assertIn("api_client.py", out)
        self.assertIn("api.example.com", out)

    def test_doctor_reports_missing_proxy(self):
        with mock.patch.object(cli, "mitmweb_cmd", return_value=None):
            out, code = run_cmd(cli.cmd_doctor, argparse.Namespace())
        self.assertEqual(code, 1)
        self.assertIn("MIMIK setup check", out)
        self.assertIn("MISSING", out)
        self.assertIn("LAN IP", out)

    def test_doctor_passes_when_ready(self):
        fake_pyside = mock.Mock()
        with mock.patch.dict(sys.modules, {"PySide6": fake_pyside}), \
             mock.patch.object(cli, "mitmweb_cmd",
                               return_value=["mitmweb"]), \
             mock.patch("mimik.sources.mitm.Mitm") as mock_mitm:
            mock_mitm.return_value.flows.return_value = []
            out, code = run_cmd(cli.cmd_doctor, argparse.Namespace())
        self.assertEqual(code, 0)

    def test_record_web_prints_steps_and_exits(self):
        args = argparse.Namespace(target="web", file=None, pick=None,
                                  deb_install=False, launch=None)
        out, code = run_cmd(cli.cmd_record, args)
        self.assertEqual(code, 0)
        self.assertIn("HAR", out)
        self.assertIn("cURL", out)

    def test_record_file_needs_file(self):
        args = argparse.Namespace(target="file", file=None, pick=None,
                                  deb_install=False, launch=None)
        out, code = run_cmd(cli.cmd_record, args)
        self.assertNotEqual(code, 0)

    def test_record_windows_without_wine_exits(self):
        args = argparse.Namespace(target="windows", file=None, pick=None,
                                  deb_install=False, launch=None)
        with mock.patch("mimik.wine.check_ready",
                        return_value=(False, "wine is not installed")):
            out, code = run_cmd(cli.cmd_record, args)
        self.assertNotEqual(code, 0)
        # sys.exit(msg) carries the message as the exit code, not stdout
        self.assertIn("wine is not installed", str(code))

    def test_main_parses_gen(self):
        with mock.patch.object(cli, "cmd_gen") as gen:
            cli.main(["gen", "api.example.com", "--har", FIXTURE,
                      "--polish", "none"])
        gen.assert_called_once()
        self.assertEqual(gen.call_args[0][0].host, "api.example.com")
        self.assertEqual(gen.call_args[0][0].polish, "none")


if __name__ == "__main__":
    unittest.main()
