"""Tests for mimik.unpin — Frida certificate-unpinning (v0.4 item 14).

No network, no Frida, no device: the orchestration is tested with
fakes + fixtures, and the pinning premise with a real local TLS
exchange. The Frida hook itself can't run in a headless test — the
suite says so, and so does the README.
"""
import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import unpin

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "unpin-config.js")
CA_PEM = ("-----BEGIN CERTIFICATE-----\n"
          "MIIBtest-ca\n"
          "-----END CERTIFICATE-----\n")


def _scripts_dir(tmp, android=True, native=True, config=True):
    """A fake fetched scripts repo under tmp/frida-scripts."""
    d = os.path.join(tmp, "frida-scripts")
    os.makedirs(d, exist_ok=True)
    if config:
        shutil.copyfile(FIXTURE, os.path.join(d, "config.js"))
    if android:
        adir = os.path.join(d, "android")
        os.makedirs(adir, exist_ok=True)
        for name in ("android-connect-hook.js",
                     "android-disable-detection.js"):
            with open(os.path.join(adir, name), "w") as f:
                f.write("// fake hook\n")
    if native:
        for name in ("native-tls-hook.js", "native-connect-hook.js"):
            with open(os.path.join(d, name), "w") as f:
                f.write("// fake hook\n")
    return d


class TargetKindTests(unittest.TestCase):
    def test_apk_always_gadget(self):
        self.assertEqual(unpin.target_kind("app.apk"), "apk")
        self.assertEqual(unpin.target_kind("APP.APK"), "apk")
        self.assertEqual(unpin.target_kind("app.apk", "linux"), "apk")

    def test_dotted_name_is_android(self):
        self.assertEqual(unpin.target_kind("com.example.app"), "android")

    def test_path_is_linux(self):
        self.assertEqual(unpin.target_kind("/usr/bin/firefox"), "linux")
        self.assertEqual(unpin.target_kind("./app"), "linux")

    def test_command_on_path_is_linux(self):
        with mock.patch("mimik.unpin.shutil.which",
                        return_value="/usr/bin/firefox"):
            self.assertEqual(unpin.target_kind("firefox"), "linux")

    def test_platform_flag_forces_it(self):
        self.assertEqual(unpin.target_kind("myapp", "android"), "android")
        self.assertEqual(unpin.target_kind("myapp", "linux"), "linux")

    def test_bare_word_ambiguous(self):
        with mock.patch("mimik.unpin.shutil.which",
                        return_value=None):
            with self.assertRaises(unpin.UnpinError) as cm:
                unpin.target_kind("myapp")
            self.assertIn("--android or --linux", str(cm.exception))


class PatchConfigTests(unittest.TestCase):
    def test_bakes_ca_host_and_port(self):
        tmp = tempfile.mkdtemp()
        d = _scripts_dir(tmp)
        unpin.patch_config(d, CA_PEM, "192.168.1.5")
        with open(os.path.join(d, "config.js")) as f:
            src = f.read()
        self.assertIn("MIIBtest-ca", src)
        self.assertNotIn("MIIBplaceholder", src)
        self.assertIn("const PROXY_HOST = '192.168.1.5';", src)
        self.assertIn("const PROXY_PORT = 8080;", src)
        # The helper code survives untouched.
        self.assertIn("function derToPem(der)", src)

    def test_changed_shape_raises(self):
        tmp = tempfile.mkdtemp()
        d = os.path.join(tmp, "frida-scripts")
        os.makedirs(d)
        with open(os.path.join(d, "config.js"), "w") as f:
            f.write("// no consts here\n")
        with self.assertRaises(unpin.UnpinError) as cm:
            unpin.patch_config(d, CA_PEM, "192.168.1.5")
        self.assertIn("changed shape", str(cm.exception))


class DiscoverTests(unittest.TestCase):
    def test_android_order(self):
        tmp = tempfile.mkdtemp()
        d = _scripts_dir(tmp)
        scripts = unpin.discover_scripts(d, "android")
        names = [os.path.relpath(s, d) for s in scripts]
        self.assertEqual(names, [
            "config.js",
            os.path.join("android", "android-connect-hook.js"),
            os.path.join("android", "android-disable-detection.js"),
            "native-connect-hook.js",
            "native-tls-hook.js",
        ])

    def test_linux_is_config_plus_native(self):
        tmp = tempfile.mkdtemp()
        d = _scripts_dir(tmp)
        scripts = unpin.discover_scripts(d, "linux")
        names = [os.path.relpath(s, d) for s in scripts]
        self.assertEqual(names, ["config.js",
                                 "native-connect-hook.js",
                                 "native-tls-hook.js"])

    def test_apk_uses_android_hooks(self):
        tmp = tempfile.mkdtemp()
        d = _scripts_dir(tmp)
        names = [os.path.relpath(s, d)
                 for s in unpin.discover_scripts(d, "apk")]
        self.assertIn(os.path.join("android", "android-connect-hook.js"),
                      names)

    def test_missing_config_names_what_was_found(self):
        tmp = tempfile.mkdtemp()
        d = _scripts_dir(tmp, config=False)
        with self.assertRaises(unpin.UnpinError) as cm:
            unpin.discover_scripts(d, "android")
        self.assertIn("no config.js", str(cm.exception))
        self.assertIn("android", str(cm.exception))

    def test_missing_android_dir_raises(self):
        tmp = tempfile.mkdtemp()
        d = _scripts_dir(tmp, android=False)
        with self.assertRaises(unpin.UnpinError):
            unpin.discover_scripts(d, "android")

    def test_root_helpers_are_not_scripts(self):
        tmp = tempfile.mkdtemp()
        d = _scripts_dir(tmp)
        with open(os.path.join(d, "helper.js"), "w") as f:
            f.write("// not a hook\n")
        names = [os.path.relpath(s, d)
                 for s in unpin.discover_scripts(d, "linux")]
        self.assertNotIn("helper.js", names)


class FridaCommandTests(unittest.TestCase):
    SCRIPTS = ["/w/frida-scripts/config.js",
               "/w/frida-scripts/native-tls-hook.js"]

    def test_android_spawn(self):
        cmd = unpin.frida_command(self.SCRIPTS, "com.example.app",
                                  "android")
        self.assertIn("frida -U \\", cmd)
        self.assertIn("-l /w/frida-scripts/config.js \\", cmd)
        self.assertTrue(cmd.endswith("-f com.example.app"))

    def test_apk_gadget_attach(self):
        cmd = unpin.frida_command(self.SCRIPTS, "app.apk", "apk")
        self.assertIn("frida -U \\", cmd)
        self.assertTrue(cmd.endswith("-n Gadget"))

    def test_linux_spawn_no_usb_flag(self):
        cmd = unpin.frida_command(self.SCRIPTS, "/usr/bin/app", "linux")
        self.assertTrue(cmd.startswith("frida \\"))
        self.assertNotIn("-U", cmd)
        self.assertTrue(cmd.endswith("-f /usr/bin/app"))


class FetchTests(unittest.TestCase):
    def test_no_git_is_false(self):
        with mock.patch("mimik.unpin.shutil.which",
                        return_value=None):
            self.assertFalse(unpin.fetch_scripts("/tmp/x"))

    def test_clone_success(self):
        with mock.patch("mimik.unpin.shutil.which",
                        return_value="/usr/bin/git"), \
             mock.patch("mimik.unpin.subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(unpin.fetch_scripts("/tmp/x"))
            self.assertIn("clone", run.call_args.args[0])

    def test_clone_failure_is_false(self):
        with mock.patch("mimik.unpin.shutil.which",
                        return_value="/usr/bin/git"), \
             mock.patch("mimik.unpin.subprocess.run",
                        return_value=mock.Mock(returncode=128)):
            self.assertFalse(unpin.fetch_scripts("/tmp/x"))

    def test_existing_checkout_pulls(self):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, ".git"))
        with mock.patch("mimik.unpin.shutil.which",
                        return_value="/usr/bin/git"), \
             mock.patch("mimik.unpin.subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(unpin.fetch_scripts(tmp))
            self.assertIn("pull", run.call_args.args[0])


class PrepareTests(unittest.TestCase):
    def _ca(self, tmp):
        path = os.path.join(tmp, "ca.pem")
        with open(path, "w") as f:
            f.write(CA_PEM)
        return path

    def _workdir(self, tmp):
        workdir = os.path.join(tmp, "mimik-unpin")
        _scripts_dir(workdir)
        return workdir

    def test_android_plan(self):
        tmp = tempfile.mkdtemp()
        plan = unpin.prepare("com.example.app",
                             ca_path=self._ca(tmp),
                             proxy_host="192.168.1.5",
                             workdir=self._workdir(tmp))
        self.assertEqual(plan["kind"], "android")
        self.assertIsNone(plan["patched_apk"])
        self.assertIn("-f com.example.app", plan["command"])
        self.assertIn("frida -U", plan["command"])
        text = unpin.format_plan(plan, "com.example.app")
        self.assertIn("192.168.1.5:8080", text)
        self.assertIn("frida -U", text)
        self.assertNotIn("—", text)  # U33: the GUI shows this text

    def test_linux_plan(self):
        tmp = tempfile.mkdtemp()
        plan = unpin.prepare("/usr/bin/app",
                             ca_path=self._ca(tmp),
                             proxy_host="192.168.1.5",
                             workdir=self._workdir(tmp))
        self.assertEqual(plan["kind"], "linux")
        self.assertTrue(plan["command"].startswith("frida \\"))
        self.assertNotIn("-U", plan["command"])

    def test_missing_ca_raises(self):
        tmp = tempfile.mkdtemp()
        with self.assertRaises(unpin.UnpinError) as cm:
            unpin.prepare("com.example.app",
                          ca_path=os.path.join(tmp, "nope.pem"),
                          workdir=self._workdir(tmp))
        self.assertIn("no mitmproxy CA", str(cm.exception))

    def test_missing_git_and_scripts_raises_with_clone_hint(self):
        tmp = tempfile.mkdtemp()
        workdir = os.path.join(tmp, "mimik-unpin")
        with mock.patch("mimik.unpin.shutil.which",
                        return_value=None):
            with self.assertRaises(unpin.UnpinError) as cm:
                unpin.prepare("com.example.app",
                              ca_path=self._ca(tmp),
                              workdir=workdir)
        self.assertIn("git clone", str(cm.exception))

    def test_apk_plan_runs_objection(self):
        tmp = tempfile.mkdtemp()
        apk = os.path.join(tmp, "app.apk")
        with open(apk, "wb") as f:
            f.write(b"fake apk")
        with mock.patch("mimik.unpin.shutil.which",
                        return_value="/usr/bin/objection"), \
             mock.patch("mimik.unpin.subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run:
            plan = unpin.prepare(apk, ca_path=self._ca(tmp),
                                 proxy_host="192.168.1.5",
                                 workdir=self._workdir(tmp))
        self.assertEqual(plan["kind"], "apk")
        self.assertTrue(plan["patched_apk"].endswith("-patched.apk"))
        self.assertIn("-n Gadget", plan["command"])
        self.assertIn("patchapk", run.call_args.args[0])

    def test_apk_without_objection_raises(self):
        tmp = tempfile.mkdtemp()
        apk = os.path.join(tmp, "app.apk")
        with open(apk, "wb") as f:
            f.write(b"fake apk")
        with mock.patch("mimik.unpin.shutil.which",
                        return_value=None):
            with self.assertRaises(unpin.UnpinError) as cm:
                unpin.prepare(apk, ca_path=self._ca(tmp),
                              workdir=self._workdir(tmp))
        self.assertIn("objection not found", str(cm.exception))


@unittest.skipUnless(shutil.which("openssl"), "openssl not available")
class PinnedStubTests(unittest.TestCase):
    """The pinning premise, proved with real TLS: a client pinned to
    one cert rejects a server presenting another. That is the exact
    wall MIMIK's unpin targets: the proxy's cert is never the pinned
    one, so no proxy-side fix can capture a pinned app. This test
    proves the premise, not the Frida hook (which needs a device).
    """

    def _cert(self, tmp, name):
        cert = os.path.join(tmp, name + ".pem")
        key = os.path.join(tmp, name + "-key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey",
                        "rsa:2048", "-keyout", key, "-out", cert,
                        "-days", "1", "-nodes", "-subj",
                        "/CN=127.0.0.1"],
                       check=True, capture_output=True)
        return cert, key

    def _serve_once(self, cert, key):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def _run():
            conn, _ = srv.accept()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            try:
                wrapped = ctx.wrap_socket(conn, server_side=True)
                wrapped.recv(1024)
                wrapped.sendall(b"ok")
                wrapped.close()
            except ssl.SSLError:
                pass
            finally:
                srv.close()

        threading.Thread(target=_run, daemon=True).start()
        return port

    def _pinned_fetch(self, port, pinned_cert):
        with open(pinned_cert, "rb") as f:
            pinned_der = ssl.PEM_cert_to_DER_cert(f.read().decode())
        pinned_fp = hashlib.sha256(pinned_der).hexdigest()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the pin is the check
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            wrapped = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
            presented = wrapped.getpeercert(binary_form=True)
            got_fp = hashlib.sha256(presented).hexdigest()
            if got_fp != pinned_fp:
                raise ssl.SSLCertVerificationError(
                    "pin mismatch: presented cert is not the pinned one")
            wrapped.sendall(b"hi")
            return wrapped.recv(1024)
        finally:
            raw.close()

    def test_pinned_client_rejects_a_foreign_cert(self):
        tmp = tempfile.mkdtemp()
        real_cert, _ = self._cert(tmp, "real")
        proxy_cert, proxy_key = self._cert(tmp, "proxy")
        # The "proxy" presents its own cert; the client pins the real
        # server's. The handshake must fail: this is why a pinned app
        # shows no traffic through mitmproxy.
        port = self._serve_once(proxy_cert, proxy_key)
        with self.assertRaises(ssl.SSLCertVerificationError):
            self._pinned_fetch(port, real_cert)

    def test_pinned_client_accepts_the_pinned_cert(self):
        tmp = tempfile.mkdtemp()
        cert, key = self._cert(tmp, "real")
        port = self._serve_once(cert, key)
        self.assertEqual(self._pinned_fetch(port, cert), b"ok")


if __name__ == "__main__":
    unittest.main()
