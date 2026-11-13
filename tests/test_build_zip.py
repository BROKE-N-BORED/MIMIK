"""Tests for the release-zip builder (v0.4 item 2).

Zip modes matter: GUI extractors (Thunar's) drop the execute bit unless
external_attr carries the full regular-file type bits — 0o100755 for
scripts/launchers, 0o100644 for plain files. Bare 0o755 is NOT enough.
These tests pin the stored modes of the built zip.
"""
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
BUILD_ZIP = os.path.join(ROOT, "tools", "build_zip.py")

spec = importlib.util.spec_from_file_location("build_zip", BUILD_ZIP)
build_zip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_zip)


class ZipModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.zip_path = os.path.join(cls.tmp, "mimik-test.zip")
        cls.paths = build_zip.build(cls.zip_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def test_scripts_are_100755_in_the_zip(self):
        with zipfile.ZipFile(self.zip_path) as zf:
            for info in zf.infolist():
                mode = info.external_attr >> 16
                name = os.path.basename(info.filename)
                if name in build_zip.EXECUTABLES:
                    self.assertEqual(
                        mode, 0o100755, f"{info.filename}: {oct(mode)}")
                else:
                    self.assertEqual(
                        mode, 0o100644, f"{info.filename}: {oct(mode)}")

    def test_every_shell_and_desktop_file_is_executable(self):
        # A future script added to the tree must not silently ship
        # without its exec bit.
        for full in self.paths:
            name = os.path.basename(full)
            if name.endswith((".sh", ".desktop")):
                self.assertIn(name, build_zip.EXECUTABLES,
                              f"{name} is collected but not in EXECUTABLES")

    def test_extracted_scripts_stay_executable(self):
        out = os.path.join(self.tmp, "extracted")
        subprocess.run(["unzip", "-q", self.zip_path, "-d", out],
                       check=True)
        prefix = build_zip.PREFIX
        for name in ("install.sh", "uninstall.sh",
                     "Install MIMIK.desktop"):
            path = os.path.join(out, prefix, name)
            self.assertTrue(os.path.exists(path), f"{name} missing")
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertTrue(mode & stat.S_IXUSR,
                            f"{name} lost its exec bit: {oct(mode)}")
            self.assertTrue(os.access(path, os.X_OK))

    def test_plain_files_are_not_executable(self):
        out = os.path.join(self.tmp, "extracted2")
        subprocess.run(["unzip", "-q", self.zip_path, "-d", out],
                       check=True)
        prefix = build_zip.PREFIX
        for rel in ("pyproject.toml", "README.md", "CHANGELOG.md",
                    "mimik/cli.py"):
            path = os.path.join(out, prefix, rel)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertFalse(mode & 0o111,
                             f"{rel} is unexpectedly executable: {oct(mode)}")


class LicenseTests(unittest.TestCase):
    """v0.4 item 4: a LICENSE file with the MIT text, in the repo and
    in the release zip. LICENSE-NOTICE stays the attribution record."""

    def test_license_file_present_and_mit(self):
        path = os.path.join(ROOT, "LICENSE")
        self.assertTrue(os.path.exists(path), "LICENSE missing from tree")
        with open(path) as fh:
            text = fh.read()
        self.assertIn("MIT License", text)
        self.assertIn("Permission is hereby granted, free of charge",
                      text)
        self.assertIn("Copyright (c) 2026 Joshua Lofland", text)
        self.assertIn("Copyright (c) 2026 Divy Srivastava", text)

    def test_zip_carries_the_license(self):
        zip_path = os.path.join(tempfile.mkdtemp(), "mimik-lic.zip")
        build_zip.build(zip_path)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertIn(build_zip.PREFIX + "LICENSE", names)
            self.assertIn(build_zip.PREFIX + "LICENSE-NOTICE", names)
        finally:
            os.remove(zip_path)

    def test_zip_carries_the_dev_tools_the_suite_needs(self):
        # The fresh-unzip suite was red (9 errors) when the zip
        # excluded tools/ + .github/: test_build_zip imports the
        # builder, test_icons runs the renderer, test_ci reads the
        # workflow. The zip is the testable tree, so it carries them.
        zip_path = os.path.join(tempfile.mkdtemp(), "mimik-tools.zip")
        build_zip.build(zip_path)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertIn(build_zip.PREFIX + "tools/build_zip.py", names)
            self.assertIn(build_zip.PREFIX + "tools/render_icons.py",
                          names)
            self.assertIn(build_zip.PREFIX + ".github/workflows/ci.yml",
                          names)
        finally:
            os.remove(zip_path)


class VersionSourceTests(unittest.TestCase):
    """v0.4 item 5: pyproject.toml is the one version source."""

    def _pyproject_version(self):
        import re

        with open(os.path.join(ROOT, "pyproject.toml")) as fh:
            text = fh.read()
        in_project = False
        for line in text.splitlines():
            if line.startswith("["):
                in_project = line.strip() == "[project]"
                continue
            if in_project:
                match = re.match(r'\s*version\s*=\s*"([^"]+)"', line)
                if match:
                    return match.group(1)
        raise AssertionError("no version in pyproject.toml")

    def test_zip_version_is_pyproject_major_minor(self):
        full = self._pyproject_version()
        short = ".".join(full.split(".")[:2])
        self.assertEqual(build_zip.FULL_VERSION, full)
        self.assertEqual(build_zip.VERSION, short)
        self.assertEqual(build_zip.PREFIX, f"mimik-v{short}/")

    def test_runtime_version_matches_pyproject(self):
        from mimik import __version__

        self.assertEqual(__version__, self._pyproject_version())

    def test_default_output_is_cwd_dist(self):
        out = build_zip.default_out_path()
        self.assertEqual(out, os.path.join(
            os.getcwd(), "dist", f"mimik-v{build_zip.VERSION}.zip"))
        # The old default was a hardcoded sandbox path — never again.
        self.assertNotIn("your_files", out)


if __name__ == "__main__":
    unittest.main()
