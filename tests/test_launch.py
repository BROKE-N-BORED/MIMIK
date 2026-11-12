"""Tests for mimik.launch — file-type detection, command building,
archive extraction, ISO mount selection. No real mounts or execs."""
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import launch


def write(path, data=b"x", mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if mode is not None:
        os.chmod(path, mode)
    return path


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_detect_by_extension(self):
        cases = {
            "app.AppImage": "appimage",
            "disk.iso": "iso",
            "disk.img": "iso",
            "pkg.deb": "deb",
            "a.zip": "archive",
            "a.tar.gz": "archive",
            "a.tgz": "archive",
            "run.sh": "script",
            "run.py": "script",
            "setup.exe": "windows",
            "setup.msi": "windows",
        }
        for name, kind in cases.items():
            p = write(os.path.join(self.tmp, name), b"data")
            self.assertEqual(launch.detect_kind(p), kind, name)

    def test_detect_elf_magic(self):
        p = write(os.path.join(self.tmp, "bin"), b"\x7fELF" + b"\x00" * 60,
                  mode=0o755)
        self.assertEqual(launch.detect_kind(p), "elf")

    def test_detect_appimage_magic(self):
        p = write(os.path.join(self.tmp, "thing"),
                  b"\x7fELF\x02\x01\x01\x00AI\x02" + b"\x00" * 60,
                  mode=0o755)
        self.assertEqual(launch.detect_kind(p), "appimage")

    def test_detect_zip_magic(self):
        p = write(os.path.join(self.tmp, "thing"), b"PK\x03\x04" + b"\x00" * 20)
        self.assertEqual(launch.detect_kind(p), "archive")

    def test_detect_iso_magic(self):
        p = os.path.join(self.tmp, "thing")
        with open(p, "wb") as f:
            f.write(b"\x00" * 32769 + b"CD001")
        self.assertEqual(launch.detect_kind(p), "iso")

    def test_detect_shebang(self):
        p = write(os.path.join(self.tmp, "thing"), b"#!/bin/bash\necho hi\n",
                  mode=0o755)
        self.assertEqual(launch.detect_kind(p), "script")

    def test_detect_unknown(self):
        p = write(os.path.join(self.tmp, "thing"), b"just some text")
        self.assertEqual(launch.detect_kind(p), "unknown")


class ProxyEnvTests(unittest.TestCase):
    def test_proxy_env_vars(self):
        env = launch.proxy_env()
        self.assertEqual(env["http_proxy"], "http://127.0.0.1:8080")
        self.assertEqual(env["https_proxy"], "http://127.0.0.1:8080")
        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:8080")
        self.assertEqual(env["no_proxy"], "localhost,127.0.0.1")


class CommandTests(unittest.TestCase):
    def test_build_command_appimage_and_elf(self):
        self.assertEqual(launch.build_launch_command("/tmp/a.AppImage"),
                         ["/tmp/a.AppImage"])

    def test_build_command_shell_script(self):
        self.assertEqual(launch.build_launch_command("/tmp/run.sh"),
                         ["bash", "/tmp/run.sh"])

    def test_build_command_python_script(self):
        self.assertEqual(launch.build_launch_command("/tmp/run.py"),
                         [sys.executable, "/tmp/run.py"])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MIMIK_CACHE_DIR"] = self.tmp

    def tearDown(self):
        del os.environ["MIMIK_CACHE_DIR"]

    def _make_zip(self):
        zpath = os.path.join(self.tmp, "app.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("myapp", b"\x7fELF" + b"\x00" * 100)
            zf.writestr("README.txt", b"hello")
        return zpath

    def test_extract_zip(self):
        dest = launch.extract_archive(
            self._make_zip(), os.path.join(self.tmp, "out"))
        self.assertTrue(os.path.exists(os.path.join(dest, "myapp")))
        self.assertTrue(os.path.exists(os.path.join(dest, "README.txt")))

    def test_extract_rejects_traversal(self):
        zpath = os.path.join(self.tmp, "evil.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("../escape", b"x")
        with self.assertRaises(launch.LaunchError):
            launch.extract_archive(zpath, os.path.join(self.tmp, "out2"))

    def test_prepare_archive_autopicks_executable(self):
        plan = launch.prepare_file(self._make_zip())
        self.assertEqual(plan.kind, "archive")
        self.assertIsNotNone(plan.command)
        self.assertTrue(plan.command[0].endswith("myapp"))

    def test_prepare_archive_pick_another(self):
        zpath = os.path.join(self.tmp, "two.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("a", b"\x7fELF" + b"\x00" * 10)
            zf.writestr("b", b"\x7fELF" + b"\x00" * 200)
        plan = launch.prepare_file(zpath, pick=2)
        self.assertTrue(plan.command[0].endswith("a"))
        plan1 = launch.prepare_file(zpath, pick=1)
        self.assertTrue(plan1.command[0].endswith("b"))

    def test_find_executables_top_level_first(self):
        root = os.path.join(self.tmp, "root")
        write(os.path.join(root, "deep", "inner"), b"\x7fELF" + b"\x00" * 500,
              mode=0o755)
        write(os.path.join(root, "top"), b"\x7fELF" + b"\x00" * 10,
              mode=0o755)
        cands = launch.find_executables(root)
        self.assertTrue(cands[0].endswith("top"))

    def test_launch_process_uses_proxy_env(self):
        plan = launch.LaunchPlan("elf", "/tmp/x", command=["/bin/true"])
        fake_proc = mock.Mock()
        with mock.patch("subprocess.Popen",
                        return_value=fake_proc) as popen:
            launch.launch_process(plan)
        env = popen.call_args[1]["env"]
        self.assertEqual(env["http_proxy"], "http://127.0.0.1:8080")
        self.assertEqual(env["no_proxy"], "localhost,127.0.0.1")

    def test_clear_runs(self):
        os.makedirs(os.path.join(launch.runs_dir(), "a"), exist_ok=True)
        self.assertEqual(launch.clear_runs(), 1)
        self.assertEqual(launch.clear_runs(), 0)


class WindowsRedirectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_exe_redirects_to_winlinx(self):
        p = write(os.path.join(self.tmp, "setup.exe"), b"MZ" + b"\x00" * 20)
        with self.assertRaises(launch.LaunchError) as ctx:
            launch.prepare_file(p)
        self.assertIn("WinLinx", str(ctx.exception))
        self.assertIn("mimik record --target windows", str(ctx.exception))

    def test_msi_redirects_to_winlinx(self):
        p = write(os.path.join(self.tmp, "setup.msi"), b"data")
        with self.assertRaises(launch.LaunchError) as ctx:
            launch.prepare_file(p)
        self.assertIn("WinLinx", str(ctx.exception))


class MountTests(unittest.TestCase):
    def test_choose_mount_prefers_udisksctl(self):
        with mock.patch("shutil.which",
                        side_effect=lambda c: "/usr/bin/" + c):
            self.assertEqual(launch.choose_mount_method(), "udisksctl")

    def test_choose_mount_falls_back_to_fuseiso(self):
        def which(c):
            return "/usr/bin/fuseiso" if c == "fuseiso" else None

        with mock.patch("shutil.which", side_effect=which):
            self.assertEqual(launch.choose_mount_method(), "fuseiso")

    def test_choose_mount_falls_back_to_gio(self):
        def which(c):
            return "/usr/bin/gio" if c == "gio" else None

        with mock.patch("shutil.which", side_effect=which):
            self.assertEqual(launch.choose_mount_method(), "gio")

    def test_choose_mount_none_when_nothing(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(launch.choose_mount_method())

    def test_mount_udisksctl_commands(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            if "loop-setup" in cmd:
                return mock.Mock(stdout="Mapped file as /dev/loop7.\n")
            return mock.Mock(stdout="Mounted /dev/loop7 at /media/u/DISK.\n")

        m = launch.Mount("/tmp/disk.iso", method="udisksctl", runner=runner)
        mp = m.mount()
        self.assertEqual(mp, "/media/u/DISK")
        self.assertIn("--read-only", calls[0])
        self.assertIn("ro", calls[1])
        m.unmount()
        self.assertEqual(len(calls), 4)
        self.assertIn("loop-delete", calls[3])

    def test_mount_no_method_gives_manual_steps(self):
        m = launch.Mount("/tmp/disk.iso", method=None, runner=mock.Mock())
        with mock.patch.object(launch, "choose_mount_method",
                               return_value=None):
            m2 = launch.Mount("/tmp/disk.iso", runner=mock.Mock())
            with self.assertRaises(launch.LaunchError) as ctx:
                m2.mount()
            self.assertIn("Disk Image Mounter", str(ctx.exception))

    def test_iso_requires_pick_with_several(self):
        m = mock.Mock()
        m.mount.return_value = "/mnt/iso"
        with mock.patch.object(launch, "Mount", return_value=m), \
             mock.patch.object(launch, "find_executables",
                               return_value=["/mnt/iso/a", "/mnt/iso/b"]):
            with tempfile.NamedTemporaryFile(suffix=".iso",
                                             delete=False) as f:
                path = f.name
            try:
                plan = launch.prepare_file(path)
            finally:
                os.unlink(path)
        self.assertTrue(plan.needs_pick)
        self.assertIsNone(plan.command)
        self.assertEqual(len(plan.candidates), 2)


class DebTests(unittest.TestCase):
    def test_deb_install_plan(self):
        with tempfile.NamedTemporaryFile(suffix=".deb", delete=False) as f:
            path = f.name
        try:
            with mock.patch.object(launch, "deb_package_name",
                                   return_value="myapp"):
                plan = launch.prepare_file(path, deb_install=True)
            self.assertEqual(plan.command, ["myapp"])
            self.assertEqual(plan.install_command[0], "pkexec")
            self.assertIn("apt", plan.install_command)
        finally:
            os.unlink(path)

    def test_extract_deb_missing_tool(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(launch.LaunchError):
                launch.extract_deb("/tmp/x.deb", "/tmp/out")


if __name__ == "__main__":
    unittest.main()
