"""Tests for mimik/ghidra.py — detection, setup steps, headless
driver, and the MimikExport script (v0.5 items 3-6, 11).

Ghidra is NOT installed in the sandbox, so the driver is verified
against a stub analyzeHeadless (a small script that writes the same
files the real MimikExport script writes). The one test that needs a
real Ghidra install skips cleanly when it is absent.
"""
import ast
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import ghidra

STUB = '''#!/usr/bin/env python3
import json
import os
import sys
import time

outdir = sys.argv[sys.argv.index("MimikExport") + 1]
mode = os.environ.get("MIMIK_STUB_MODE", "ok")
if mode == "fail":
    print("stub: boom", file=sys.stderr, flush=True)
    sys.exit(3)
if mode == "sleep":
    time.sleep(30)
if mode == "slow":
    time.sleep(0.3)
    print("stub: working...", flush=True)
    # A long window after the progress line: the driver's 0.25 s
    # poll loop must see "working..." even on a loaded machine
    # (a 0.2 s window flaked under load).
    time.sleep(1.5)
os.makedirs(os.path.join(outdir, "functions"), exist_ok=True)
with open(os.path.join(outdir, "heap.txt"), "w") as f:
    f.write(os.environ.get("GHIDRA_HEADLESS_MAXMEM", ""))
with open(os.path.join(outdir, "functions.json"), "w") as f:
    json.dump([{"address": "0x401000", "name": "main", "size": 42,
                "decompiled": True, "disassembly": True}], f)
with open(os.path.join(outdir, "callgraph.json"), "w") as f:
    json.dump({"edges": [["0x401000", "0x401020"]],
               "names": {"0x401000": "main",
                         "0x401020": "helper"}}, f)
with open(os.path.join(outdir, "xrefs.json"), "w") as f:
    json.dump({"strings": [{"address": "0x402000",
                            "text": "HelloMIMIK",
                            "referenced_by": ["0x401000"]}]}, f)
with open(os.path.join(outdir, "functions", "401000_main.c"), "w") as f:
    f.write("int main(void) { return 42; }\\n")
with open(os.path.join(outdir, "functions", "401000_main.asm"),
          "w") as f:
    f.write("0x401000: ret\\n")
print("MimikExport: done, 1 functions, 1 call edges, 1 strings",
      flush=True)
'''


def _make_executable(path, content):
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o755)


def make_fake_ghidra(root, version="12.2"):
    """A fake Ghidra install dir with the stub as analyzeHeadless."""
    home = os.path.join(root, "ghidra-" + version)
    support = os.path.join(home, "support")
    os.makedirs(support)
    _make_executable(os.path.join(support, "analyzeHeadless"), STUB)
    props_dir = os.path.join(home, "Ghidra")
    os.makedirs(props_dir)
    with open(os.path.join(props_dir, "application.properties"),
              "w") as f:
        f.write("application.version=" + version + "\n")
    return home


def make_fake_java(root, version_line):
    """A fake java that prints version_line to stderr."""
    bindir = os.path.join(root, "bin")
    os.makedirs(bindir, exist_ok=True)
    _make_executable(
        os.path.join(bindir, "java"),
        "#!/bin/sh\necho '%s' >&2\n" % version_line)
    return bindir


class FindGhidraTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = make_fake_ghidra(self.tmp)
        self.exe = os.path.join(self.home, "support", "analyzeHeadless")

    def test_via_ghidra_home_dir(self):
        with mock.patch.dict(os.environ, {"GHIDRA_HOME": self.home}):
            self.assertEqual(ghidra.find_ghidra(), self.exe)

    def test_via_ghidra_home_exe(self):
        with mock.patch.dict(os.environ, {"GHIDRA_HOME": self.exe}):
            self.assertEqual(ghidra.find_ghidra(), self.exe)

    def test_via_path(self):
        bindir = os.path.join(self.tmp, "pathbin")
        os.makedirs(bindir)
        _make_executable(os.path.join(bindir, "analyzeHeadless"), STUB)
        with mock.patch.dict(os.environ, {"GHIDRA_HOME": "",
                                          "PATH": bindir}):
            self.assertEqual(
                ghidra.find_ghidra(),
                os.path.join(bindir, "analyzeHeadless"))

    def test_via_glob(self):
        with mock.patch.dict(os.environ, {"GHIDRA_HOME": "",
                                          "PATH": self.tmp}), \
                mock.patch("glob.glob", return_value=[self.home]):
            self.assertEqual(ghidra.find_ghidra(), self.exe)

    def test_absent(self):
        with mock.patch.dict(os.environ, {"GHIDRA_HOME": "",
                                          "PATH": self.tmp}), \
                mock.patch("glob.glob", return_value=[]):
            self.assertIsNone(ghidra.find_ghidra())

    def test_home_and_version(self):
        self.assertEqual(ghidra.ghidra_home(self.exe), self.home)
        self.assertEqual(ghidra.ghidra_version(self.home), "12.2")
        self.assertIsNone(ghidra.ghidra_version(self.tmp))


class JavaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_modern_version(self):
        bindir = make_fake_java(
            self.tmp, 'openjdk version "25.0.1" 2025-09-16')
        with mock.patch.dict(os.environ, {"PATH": bindir,
                                          "JAVA_HOME": ""}):
            self.assertEqual(ghidra.java_version(), 25)

    def test_legacy_1x_version(self):
        bindir = make_fake_java(self.tmp, 'java version "1.8.0_392"')
        with mock.patch.dict(os.environ, {"PATH": bindir,
                                          "JAVA_HOME": ""}):
            self.assertEqual(ghidra.java_version(), 8)

    def test_java_home_wins(self):
        jdk = os.path.join(self.tmp, "jdk25")
        make_fake_java(jdk, 'openjdk version "25"')
        other = make_fake_java(os.path.join(self.tmp, "other"),
                               'openjdk version "17"')
        with mock.patch.dict(os.environ, {"PATH": other,
                                          "JAVA_HOME": jdk}):
            self.assertEqual(ghidra.java_version(), 25)

    def test_no_java(self):
        with mock.patch.dict(os.environ, {"PATH": self.tmp,
                                          "JAVA_HOME": ""}):
            self.assertIsNone(ghidra.java_version())


class RequiredJdkTests(unittest.TestCase):
    def test_current_line_needs_25(self):
        self.assertEqual(ghidra.required_jdk("12.2"), 25)
        self.assertEqual(ghidra.required_jdk("12.3"), 25)
        self.assertEqual(ghidra.required_jdk("13.0"), 25)

    def test_older_line_needs_21(self):
        self.assertEqual(ghidra.required_jdk("12.1.1"), 21)
        self.assertEqual(ghidra.required_jdk("11.3"), 21)

    def test_unknown_needs_25(self):
        with mock.patch.object(ghidra, "ghidra_version",
                               return_value=None):
            self.assertEqual(ghidra.required_jdk(None), 25)
        self.assertEqual(ghidra.required_jdk("garbage"), 25)


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = make_fake_ghidra(self.tmp)

    def _env(self, java_line):
        bindir = make_fake_java(self.tmp, java_line)
        return {"GHIDRA_HOME": self.home, "PATH": bindir,
                "JAVA_HOME": ""}

    def test_found_with_good_jdk(self):
        with mock.patch.dict(os.environ, self._env(
                'openjdk version "25.0.1"')):
            det = ghidra.detect()
        self.assertTrue(det["found"])
        self.assertEqual(det["version"], "12.2")
        self.assertEqual(det["java_major"], 25)
        self.assertEqual(det["required_jdk"], 25)
        self.assertTrue(det["java_ok"])
        self.assertIsNone(det["fix"])
        self.assertTrue(det["heap"].endswith("G"))

    def test_found_with_old_jdk(self):
        with mock.patch.dict(os.environ, self._env(
                'openjdk version "17.0.9"')):
            det = ghidra.detect()
        self.assertTrue(det["found"])
        self.assertFalse(det["java_ok"])
        self.assertIn("JDK 25", det["fix"])

    def test_not_found(self):
        with mock.patch.dict(os.environ, {"GHIDRA_HOME": "",
                                          "PATH": self.tmp,
                                          "JAVA_HOME": ""}), \
                mock.patch("glob.glob", return_value=[]):
            det = ghidra.detect()
        self.assertFalse(det["found"])
        self.assertIn("ghidra-setup", det["fix"])


class SetupStepsTests(unittest.TestCase):
    def test_steps_are_plain_and_complete(self):
        text = "\n".join(ghidra.setup_steps())
        self.assertIn(ghidra.GHIDRA_RELEASE_URL, text)
        self.assertIn(ghidra.GHIDRA_ZIP, text)
        self.assertIn("JDK 25", text)
        self.assertIn("openjdk-25-jdk", text)
        self.assertIn("~/opt", text)
        self.assertIn("sha256sum", text)

    def test_no_unverified_hash_stated(self):
        # MIMIK must never state a SHA-256 it has not verified.
        text = "\n".join(ghidra.setup_steps())
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", text))


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(ghidra.script_dir(), "MimikExport.py")
        with open(self.path, "r") as f:
            self.source = f.read()

    def test_parses_as_python(self):
        # Jython 2.7-compatible syntax must also parse under CPython.
        ast.parse(self.source)

    def test_no_f_strings(self):
        # Jython 2.7 has no f-strings.
        self.assertNotIn('f"', self.source)
        self.assertNotIn("f'", self.source)

    def test_uses_the_expected_apis(self):
        for token in ("DecompInterface", "decompileFunction",
                      "getInstructions", "getCalledFunctions",
                      "getReferencesTo", "functions.json",
                      "callgraph.json", "xrefs.json",
                      "MAX_FUNCTIONS"):
            self.assertIn(token, self.source)


class RunAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = make_fake_ghidra(self.tmp)
        self.binary = os.path.join(self.tmp, "tiny.elf")
        with open(self.binary, "wb") as f:
            f.write(b"\x7fELF" + b"\x00" * 100)
        self.outdir = os.path.join(self.tmp, "out")
        self.env = {"GHIDRA_HOME": self.home,
                    "MIMIK_DATA_DIR": self.tmp,
                    "MIMIK_STUB_MODE": "ok"}

    def test_success_reads_the_export_back(self):
        with mock.patch.dict(os.environ, self.env):
            result = ghidra.run_analysis(self.binary, self.outdir,
                                         heap="1G")
        self.assertTrue(result["ok"])
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["version"], "12.2")
        self.assertEqual(result["heap"], "1G")
        self.assertEqual(len(result["functions"]), 1)
        self.assertEqual(result["functions"][0]["name"], "main")
        self.assertEqual(result["callgraph"]["edges"],
                         [["0x401000", "0x401020"]])
        self.assertEqual(result["xrefs"]["strings"][0]["text"],
                         "HelloMIMIK")
        # The heap reached the child through the environment.
        with open(os.path.join(self.outdir, "heap.txt")) as f:
            self.assertEqual(f.read(), "1G")
        # Per-function files landed.
        self.assertTrue(os.path.isfile(os.path.join(
            self.outdir, "functions", "401000_main.c")))
        self.assertTrue(os.path.isfile(os.path.join(
            self.outdir, "functions", "401000_main.asm")))
        # The run log exists.
        self.assertTrue(os.path.isfile(result["log_path"]))
        # The temp project was cleaned up.
        projects = os.path.join(self.tmp, "ghidra-projects")
        self.assertEqual(os.listdir(projects), [])

    def test_on_log_gets_progress(self):
        lines = []
        env = dict(self.env, MIMIK_STUB_MODE="slow")
        with mock.patch.dict(os.environ, env):
            result = ghidra.run_analysis(
                self.binary, self.outdir, on_log=lines.append)
        self.assertTrue(result["ok"])
        self.assertTrue(any("working" in line for line in lines),
                        lines)

    def test_missing_ghidra(self):
        with mock.patch.object(ghidra, "find_ghidra",
                               return_value=None):
            with self.assertRaises(ghidra.GhidraError) as ctx:
                ghidra.run_analysis(self.binary, self.outdir)
        self.assertIn("not installed", str(ctx.exception))

    def test_missing_binary(self):
        with mock.patch.dict(os.environ, self.env):
            with self.assertRaises(ghidra.GhidraError):
                ghidra.run_analysis(os.path.join(self.tmp, "nope"),
                                    self.outdir)

    def test_failed_run_reports_exit_code(self):
        env = dict(self.env, MIMIK_STUB_MODE="fail")
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(ghidra.GhidraError) as ctx:
                ghidra.run_analysis(self.binary, self.outdir)
        self.assertIn("exit code 3", str(ctx.exception))

    def test_cancel_stops_the_run(self):
        env = dict(self.env, MIMIK_STUB_MODE="sleep")
        with mock.patch.dict(os.environ, env):
            result = ghidra.run_analysis(
                self.binary, self.outdir, cancelled=lambda: True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])

    def test_timeout_kills_the_run(self):
        env = dict(self.env, MIMIK_STUB_MODE="sleep")
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(ghidra.GhidraError) as ctx:
                ghidra.run_analysis(self.binary, self.outdir,
                                    timeout=1)
        self.assertIn("longer than 1 seconds", str(ctx.exception))


@unittest.skipUnless(ghidra.find_ghidra(), "Ghidra not installed")
class RealGhidraTests(unittest.TestCase):
    """Runs only on a machine with a real Ghidra install (his PC,
    not the sandbox)."""

    def test_real_install_detects(self):
        det = ghidra.detect()
        self.assertTrue(det["found"])
        self.assertIsNotNone(det["version"])


if __name__ == "__main__":
    unittest.main()
