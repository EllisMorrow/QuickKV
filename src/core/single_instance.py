# -*- coding: utf-8 -*-
"""Atomic single-instance ownership plus local activation IPC for QuickKV.

The ownership and IPC names are derived from the data root, so separate
portable copies do not block one another.  This module deliberately owns no
user data: callers must acquire it before opening configuration or libraries.
"""
import hashlib
import os
import tempfile
import time

try:
    from PySide6.QtCore import QLockFile
except ImportError:  # Allows isolated tests to inject an ownership fake.
    QLockFile = None

try:
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
except ImportError:  # Allows isolated non-GUI tests to inject IPC fakes.
    QLocalServer = None
    QLocalSocket = None


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _ERROR_ALREADY_EXISTS = 183
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL


class _WindowsNamedMutex:
    """Hold a session-local named kernel object for this process's lifetime."""

    def __init__(self, name):
        # Local keeps independent interactive sessions isolated.  The default
        # security descriptor comes from the process token and therefore keeps
        # access within the current user's normal named-object boundary.
        self._name = f"Local\\{name}"
        self._handle = None

    def try_acquire(self):
        ctypes.set_last_error(0)
        handle = _kernel32.CreateMutexW(None, False, self._name)
        error = ctypes.get_last_error()
        if not handle:
            return None
        if error == _ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self):
        if self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None


class _QtLockFileOwnership:
    """Best-effort non-Windows fallback using Qt's process-aware lock file."""

    def __init__(self, name):
        path = os.path.join(tempfile.gettempdir(), f"{name}.lock")
        self._lock = QLockFile(path)
        self._locked = False

    def try_acquire(self):
        try:
            self._locked = self._lock.tryLock(0)
            return self._locked
        except Exception:
            return None

    def release(self):
        if self._locked:
            self._lock.unlock()
            self._locked = False


def _create_ownership_guard(name):
    if os.name == "nt":
        return _WindowsNamedMutex(name)
    if QLockFile is not None:
        return _QtLockFileOwnership(name)
    return None


class SingleInstanceManager:
    OWNER = "owner"
    NOTIFIED_OWNER = "notified_owner"
    UNAVAILABLE = "unavailable"

    _REQUEST = b"activate\n"

    def __init__(
        self,
        data_root,
        server_factory=None,
        socket_factory=None,
        ownership_factory=None,
        timeout_ms=500,
        notification_attempts=10,
        notification_retry_ms=50,
    ):
        normalized_root = os.path.normcase(os.path.realpath(os.path.abspath(data_root)))
        root_hash = hashlib.sha256(normalized_root.encode("utf-8")).hexdigest()[:20]
        self.server_name = f"QuickKV-{root_hash}"
        self.ownership_name = f"QuickKV-{root_hash}"
        self._server_factory = server_factory or QLocalServer
        self._socket_factory = socket_factory or QLocalSocket
        self._ownership_factory = ownership_factory or _create_ownership_guard
        self._timeout_ms = timeout_ms
        self._notification_attempts = max(1, notification_attempts)
        self._notification_retry_ms = max(0, notification_retry_ms)
        self._ownership = None
        self._server = None
        self._activation_handler = None
        self._pending_activations = 0
        self._client_sockets = []

    @property
    def owns_instance(self):
        return self._ownership is not None and self._server is not None

    def acquire(self):
        """Acquire atomic ownership, notify the owner, or fail closed."""
        if self.owns_instance:
            return self.OWNER
        if not self._can_create_ipc_objects():
            return self.UNAVAILABLE

        ownership = self._create_ownership()
        if ownership is None:
            return self.UNAVAILABLE
        try:
            acquired = ownership.try_acquire()
        except Exception:
            acquired = None

        if acquired is not True:
            try:
                ownership.release()
            except Exception:
                pass
            if acquired is False:
                return self._notify_owner_during_startup()
            return self.UNAVAILABLE

        self._ownership = ownership

        # Only the atomic ownership winner may remove a stale endpoint.  The
        # endpoint never participates in deciding who owns the instance.
        self._remove_stale_server()
        if self._listen():
            return self.OWNER

        self._release_ownership()
        return self.UNAVAILABLE

    def release(self):
        """Close activation IPC, then relinquish cross-process ownership."""
        server, self._server = self._server, None
        try:
            if server is not None:
                server.close()
        finally:
            self._client_sockets.clear()
            self._release_ownership()

    def set_activation_handler(self, handler):
        """Bind the UI callback and drain requests received during startup."""
        self._activation_handler = handler
        pending = self._pending_activations
        self._pending_activations = 0
        for _ in range(pending):
            handler()

    def _can_create_ipc_objects(self):
        return (
            self._server_factory is not None
            and self._socket_factory is not None
            and self._ownership_factory is not None
        )

    def _create_ownership(self):
        try:
            return self._ownership_factory(self.ownership_name)
        except Exception:
            return None

    def _release_ownership(self):
        ownership, self._ownership = self._ownership, None
        if ownership is not None:
            try:
                ownership.release()
            except Exception:
                pass

    def _listen(self):
        server = None
        try:
            server = self._server_factory()
            self._restrict_to_current_user(server)
            listening = server.listen(self.server_name)
        except Exception:
            listening = False
        if not listening:
            try:
                if server is not None:
                    server.close()
            except Exception:
                pass
            return False

        self._server = server
        try:
            server.newConnection.connect(self._accept_connections)
            self._accept_connections()
            return True
        except Exception:
            self._server = None
            try:
                server.close()
            except Exception:
                pass
            return False

    @staticmethod
    def _restrict_to_current_user(server):
        """Use Qt's user-only permission option when the binding exposes it."""
        options_type = getattr(server, "SocketOption", None)
        user_only = getattr(options_type, "UserAccessOption", None)
        if user_only is None:
            options_type = getattr(QLocalServer, "SocketOption", None)
            user_only = getattr(options_type, "UserAccessOption", None)
        if user_only is not None and hasattr(server, "setSocketOptions"):
            server.setSocketOptions(user_only)

    def _notify_owner_during_startup(self):
        for attempt in range(self._notification_attempts):
            if self._notify_existing_owner():
                return self.NOTIFIED_OWNER
            if attempt + 1 < self._notification_attempts and self._notification_retry_ms:
                time.sleep(self._notification_retry_ms / 1000)
        return self.UNAVAILABLE

    def _notify_existing_owner(self):
        try:
            socket = self._socket_factory()
        except Exception:
            return False
        try:
            socket.connectToServer(self.server_name)
            if not socket.waitForConnected(self._timeout_ms):
                return False
            bytes_written = socket.write(self._REQUEST)
            if bytes_written != len(self._REQUEST):
                return False
            if hasattr(socket, "flush"):
                socket.flush()
            if (
                hasattr(socket, "waitForBytesWritten")
                and not socket.waitForBytesWritten(self._timeout_ms)
            ):
                return False
            return True
        except Exception:
            return False
        finally:
            if hasattr(socket, "disconnectFromServer"):
                socket.disconnectFromServer()

    def _remove_stale_server(self):
        remove = getattr(self._server_factory, "removeServer", None)
        if remove is None:
            remove = getattr(QLocalServer, "removeServer", None)
        if remove is not None:
            try:
                remove(self.server_name)
            except Exception:
                pass

    def _accept_connections(self):
        while self._server is not None and self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._client_sockets.append(socket)
            socket.readyRead.connect(lambda client=socket: self._read_request(client))
            if hasattr(socket, "disconnected"):
                socket.disconnected.connect(lambda client=socket: self._discard_client(client))
            if not hasattr(socket, "bytesAvailable") or socket.bytesAvailable():
                self._read_request(socket)

    def _discard_client(self, socket):
        if socket in self._client_sockets:
            self._client_sockets.remove(socket)
        if hasattr(socket, "deleteLater"):
            socket.deleteLater()

    def _read_request(self, socket):
        data = bytes(socket.readAll())
        if self._REQUEST.strip() in data.splitlines():
            self._activate()
        if hasattr(socket, "disconnectFromServer"):
            socket.disconnectFromServer()

    def _activate(self):
        if self._activation_handler is None:
            self._pending_activations += 1
        else:
            self._activation_handler()
