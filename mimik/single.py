"""Single instance (v0.5 item 14, design rule U27).

Re-clicking the menu icon focuses the running app; it never starts
a second copy. Adapted from the proven Dataset Scout pattern.

A QLockFile in the data dir says who owns the window; a QLocalServer
lets a second launch ask the running copy to come forward instead of
starting a second window. A stale lock from a crashed run is taken
over: the lock file is judged stale 1 second after it was written
when its owner is dead (a live owner stays protected by the pid
check, a dead owner is taken over).

The CLI never touches this guard: it does not import this module, so
`mimik` commands keep working while the window is open.

Quirks of this PySide6 build, probed 2026-11-13: bare
QLockFile.tryLock() raises TypeError, use tryLock(0); call
QLocalServer.removeServer(name) before listen or a crashed server
leaves its socket behind. And MIMIK's heap rule: no lambdas on Qt
signals (they corrupt the heap at teardown) — the socket slots use
bound methods + sender() instead.
"""
import hashlib
import os
import time

from PySide6.QtCore import QLockFile, QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from . import log, store


class SingleInstance(QObject):
    """The single-instance guard for one process."""

    def __init__(self, base=None, parent=None):
        super().__init__(parent)
        self.base = base or store.data_dir()
        os.makedirs(self.base, exist_ok=True)
        self.lock_path = os.path.join(self.base, "app.lock")
        self.lock = QLockFile(self.lock_path)
        self.lock.setStaleLockTime(1000)
        digest = hashlib.md5(
            self.base.encode("utf-8")).hexdigest()
        self.server_name = f"mimik-{digest[:12]}"
        self.server = None
        self._sockets = []
        self._on_raise = None

    def try_take(self):
        """True if this process now owns the single-instance lock."""
        if self.lock.tryLock(0):
            return True
        # The owner may have crashed: drop a stale lock and retry.
        # (Only safe to call while NOT holding the lock.)
        if self.lock.removeStaleLockFile():
            return self.lock.tryLock(0)
        return False

    def release(self):
        if self.server is not None:
            self.server.close()
            self.server = None
        self.lock.unlock()

    def start_server(self, on_raise):
        """Listen for raise requests from later launches."""
        self._on_raise = on_raise
        # A crashed run leaves its socket behind; remove it first or
        # listen fails with "Address in use".
        QLocalServer.removeServer(self.server_name)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept_connection)
        if not self.server.listen(self.server_name):
            log.log_info("single instance",
                         "could not listen for raise requests: "
                         + self.server.errorString())
            return False
        return True

    def _accept_connection(self):
        if self.server is None:
            return
        while True:
            sock = self.server.nextPendingConnection()
            if sock is None:
                return
            self._sockets.append(sock)
            sock.readyRead.connect(self._read_message)
            sock.disconnected.connect(self._drop_socket)
            self._read_message_for(sock)

    def _read_message(self):
        """A raise request arrived on one of the accepted sockets."""
        sock = self.sender()
        if sock is not None:
            self._read_message_for(sock)

    def _read_message_for(self, sock):
        data = bytes(sock.readAll())
        if b"raise" in data and self._on_raise is not None:
            self._on_raise()

    def _drop_socket(self):
        sock = self.sender()
        if sock is None:
            return
        if sock in self._sockets:
            self._sockets.remove(sock)
        sock.deleteLater()

    def notify_running(self):
        """Ask the running copy to raise its window.

        True if it answered. Retries for a few seconds: a first
        instance that has taken the lock but not yet started its
        server answers once it is up.
        """
        for _ in range(10):
            sock = QLocalSocket()
            sock.connectToServer(self.server_name)
            if sock.waitForConnected(200):
                sock.write(b"raise")
                sock.flush()
                sock.waitForBytesWritten(200)
                sock.disconnectFromServer()
                return True
            time.sleep(0.2)
        return False
