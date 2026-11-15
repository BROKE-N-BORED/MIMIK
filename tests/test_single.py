"""Tests for mimik/single.py — the single-instance guard (v0.5
item 14, U27). Qt-gated: skipped when PySide6 is not importable."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from PySide6.QtWidgets import QApplication

    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "PySide6 not installed")
class SingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        from mimik.single import SingleInstance

        self.tmp = tempfile.mkdtemp()
        self.guards = []
        self.SingleInstance = SingleInstance

    def tearDown(self):
        for guard in self.guards:
            guard.release()
        self.app.processEvents()

    def _guard(self):
        guard = self.SingleInstance(base=self.tmp)
        self.guards.append(guard)
        return guard

    def test_second_guard_cannot_take_lock(self):
        first = self._guard()
        self.assertTrue(first.try_take())
        second = self._guard()
        self.assertFalse(second.try_take())
        first.release()
        self.assertTrue(second.try_take())

    def test_notify_raises_running_copy(self):
        raised = []
        first = self._guard()
        self.assertTrue(first.try_take())
        self.assertTrue(first.start_server(lambda: raised.append(1)))
        second = self._guard()
        self.assertFalse(second.try_take())
        self.assertTrue(second.notify_running())
        deadline = time.time() + 5
        while not raised and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.05)
        self.assertEqual(raised, [1])

    def test_notify_fails_when_nobody_listens(self):
        # No first instance at all: notify finds no server.
        guard = self._guard()
        self.assertFalse(guard.notify_running())

    def test_stale_lock_takeover(self):
        # A crashed owner (SIGKILL, no cleanup) leaves a lock file
        # behind; a new launch takes it over past the stale window.
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                            ".."))
        script = (
            "import sys, time; sys.path.insert(0, %r); "
            "from mimik.single import SingleInstance; "
            "g = SingleInstance(base=%r); "
            "assert g.try_take(); time.sleep(30)"
            % (repo, self.tmp))
        proc = subprocess.Popen([sys.executable, "-c", script])
        try:
            deadline = time.time() + 10
            lock_path = os.path.join(self.tmp, "app.lock")
            while not os.path.exists(lock_path) \
                    and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(lock_path),
                            "the holder never took the lock")
            proc.kill()
            proc.wait()
            time.sleep(1.5)  # past the 1 s stale window
            guard = self._guard()
            self.assertTrue(guard.try_take())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_server_name_differs_per_data_dir(self):
        other = tempfile.mkdtemp()
        a = self._guard()
        b = self.SingleInstance(base=other)
        self.guards.append(b)
        self.assertNotEqual(a.server_name, b.server_name)

    def test_bring_to_front_shows_window(self):
        from mimik.gui import MainWindow

        with mock.patch.dict(os.environ, {"MIMIK_DATA_DIR": self.tmp}):
            win = MainWindow()
            win.hide()
            self.app.processEvents()
            self.assertFalse(win.isVisible())
            win.bring_to_front()
            self.app.processEvents()
            self.assertTrue(win.isVisible())
            win.close()


if __name__ == "__main__":
    unittest.main()
