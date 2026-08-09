import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.single_instance import SingleInstanceManager


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in list(self.callbacks):
            callback()


class FakeSocket:
    def __init__(
        self,
        connected=False,
        error="ServerNotFoundError",
        incoming=b"",
        write_result=None,
        bytes_written=True,
    ):
        self.connected = connected
        self.error_value = error
        self.incoming = incoming
        self.writes = []
        self.write_result = write_result
        self.bytes_written = bytes_written
        self.readyRead = FakeSignal()
        self.disconnected = FakeSignal()
        self.disconnected_called = False

    def connectToServer(self, _name):
        pass

    def waitForConnected(self, _timeout):
        return self.connected

    def error(self):
        return self.error_value

    def write(self, data):
        self.writes.append(data)
        return len(data) if self.write_result is None else self.write_result

    def flush(self):
        pass

    def waitForBytesWritten(self, _timeout):
        return self.bytes_written

    def readAll(self):
        result, self.incoming = self.incoming, b""
        return result

    def bytesAvailable(self):
        return len(self.incoming)

    def disconnectFromServer(self):
        self.disconnected_called = True


class FakeServer:
    SocketOption = type("SocketOption", (), {"UserAccessOption": "user-only"})
    listen_results = []
    removed_names = []
    instances = []

    def __init__(self):
        self.newConnection = FakeSignal()
        self.pending = []
        self.closed = False
        self.options = None
        type(self).instances.append(self)

    @classmethod
    def reset(cls, results):
        cls.listen_results = list(results)
        cls.removed_names = []
        cls.instances = []

    @classmethod
    def removeServer(cls, name):
        cls.removed_names.append(name)

    def setSocketOptions(self, options):
        self.options = options

    def listen(self, _name):
        return type(self).listen_results.pop(0)

    def hasPendingConnections(self):
        return bool(self.pending)

    def nextPendingConnection(self):
        return self.pending.pop(0)

    def close(self):
        self.closed = True


class SocketFactory:
    def __init__(self, sockets):
        self.sockets = list(sockets)

    def __call__(self):
        return self.sockets.pop(0)


class FakeOwnership:
    def __init__(self, acquired):
        self.acquired = acquired
        self.released = False

    def try_acquire(self):
        return self.acquired

    def release(self):
        self.released = True


class OwnershipFactory:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.guards = []
        self.names = []

    def __call__(self, name):
        self.names.append(name)
        guard = FakeOwnership(self.outcomes.pop(0))
        self.guards.append(guard)
        return guard


class SingleInstanceTests(unittest.TestCase):
    def manager(
        self,
        ownership_outcomes,
        listen_results,
        sockets=(),
        notification_attempts=3,
    ):
        FakeServer.reset(listen_results)
        ownership_factory = OwnershipFactory(ownership_outcomes)
        manager = SingleInstanceManager(
            "C:/temporary/QuickKV/用户数据",
            server_factory=FakeServer,
            socket_factory=SocketFactory(sockets),
            ownership_factory=ownership_factory,
            notification_attempts=notification_attempts,
            notification_retry_ms=0,
        )
        return manager, ownership_factory

    def test_atomic_lock_winner_acquires_user_only_server(self):
        manager, ownership_factory = self.manager([True], [True])

        self.assertEqual(manager.acquire(), SingleInstanceManager.OWNER)
        self.assertTrue(manager.owns_instance)
        self.assertEqual(ownership_factory.names, [manager.ownership_name])
        self.assertEqual(FakeServer.instances[-1].options, "user-only")
        self.assertEqual(FakeServer.removed_names, [manager.server_name])

    def test_atomic_lock_loser_notifies_existing_owner(self):
        socket = FakeSocket(connected=True)
        manager, ownership_factory = self.manager([False], [], [socket])

        self.assertEqual(manager.acquire(), SingleInstanceManager.NOTIFIED_OWNER)
        self.assertEqual(socket.writes, [b"activate\n"])
        self.assertFalse(manager.owns_instance)
        self.assertTrue(ownership_factory.guards[0].released)
        self.assertEqual(FakeServer.instances, [])
        self.assertEqual(FakeServer.removed_names, [])

    def test_lock_loser_retries_notification_during_owner_startup(self):
        sockets = [FakeSocket(), FakeSocket(), FakeSocket(connected=True)]
        manager, _ = self.manager(
            [False], [], sockets, notification_attempts=3
        )

        self.assertEqual(manager.acquire(), SingleInstanceManager.NOTIFIED_OWNER)
        self.assertEqual(sockets[-1].writes, [b"activate\n"])

    def test_lock_loser_treats_unsent_or_unflushed_ipc_as_unavailable(self):
        for socket in (
            FakeSocket(connected=True, write_result=-1),
            FakeSocket(connected=True, write_result=len(b"activate") - 1),
            FakeSocket(connected=True, bytes_written=False),
        ):
            with self.subTest(socket=socket):
                manager, _ = self.manager([False], [], [socket], notification_attempts=1)

                self.assertEqual(manager.acquire(), SingleInstanceManager.UNAVAILABLE)
                self.assertTrue(socket.disconnected_called)

    def test_lock_loser_fails_closed_when_owner_ipc_never_becomes_ready(self):
        manager, _ = self.manager(
            [False],
            [],
            [FakeSocket(), FakeSocket(), FakeSocket()],
            notification_attempts=3,
        )

        self.assertEqual(manager.acquire(), SingleInstanceManager.UNAVAILABLE)
        self.assertFalse(manager.owns_instance)
        self.assertEqual(FakeServer.instances, [])
        self.assertEqual(FakeServer.removed_names, [])

    def test_ipc_listen_failure_releases_atomic_lock(self):
        manager, ownership_factory = self.manager([True], [False])

        self.assertEqual(manager.acquire(), SingleInstanceManager.UNAVAILABLE)
        self.assertFalse(manager.owns_instance)
        self.assertTrue(ownership_factory.guards[0].released)
        self.assertTrue(FakeServer.instances[0].closed)
        self.assertEqual(FakeServer.removed_names, [manager.server_name])

    def test_atomic_lock_backend_error_fails_closed_without_ipc(self):
        manager, ownership_factory = self.manager([None], [])

        self.assertEqual(manager.acquire(), SingleInstanceManager.UNAVAILABLE)
        self.assertTrue(ownership_factory.guards[0].released)
        self.assertEqual(FakeServer.instances, [])

    def test_activate_before_handler_is_queued(self):
        manager, _ = self.manager([], [])
        client = FakeSocket(incoming=b"activate\n")

        manager._read_request(client)
        activations = []
        manager.set_activation_handler(lambda: activations.append("activate"))

        self.assertEqual(activations, ["activate"])

    def test_release_then_reacquire_supports_failed_restart_recovery(self):
        manager, ownership_factory = self.manager([True, True], [True, True])
        self.assertEqual(manager.acquire(), SingleInstanceManager.OWNER)
        owner_server = FakeServer.instances[-1]
        first_guard = ownership_factory.guards[-1]

        manager.release()
        self.assertTrue(owner_server.closed)
        self.assertTrue(first_guard.released)
        self.assertFalse(manager.owns_instance)

        # When detached launch fails, the stopped process can safely recover.
        self.assertEqual(manager.acquire(), SingleInstanceManager.OWNER)
        self.assertIsNot(ownership_factory.guards[-1], first_guard)
        self.assertFalse(ownership_factory.guards[-1].released)
        manager.release()

    def test_failed_restart_recovery_fails_closed_after_handoff(self):
        manager, ownership_factory = self.manager(
            [True, False], [True], [FakeSocket(connected=True)]
        )
        self.assertEqual(manager.acquire(), SingleInstanceManager.OWNER)
        manager.release()

        # A successfully started replacement owns the mutex before recovery.
        self.assertEqual(manager.acquire(), SingleInstanceManager.NOTIFIED_OWNER)
        self.assertFalse(manager.owns_instance)
        self.assertTrue(ownership_factory.guards[-1].released)

    def test_real_qt_child_exit_still_delivers_activate(self):
        """Exercise the immediate-child-exit timing with a fresh Qt process.

        The repository's lightweight test Python does not necessarily include
        PySide6, so that environment skips this test.  A packaged/development
        runtime with PySide6 runs the complete parent/child IPC exchange.
        """
        script = textwrap.dedent(
            """
            import subprocess
            import sys
            import tempfile
            import time

            try:
                from PySide6.QtCore import QCoreApplication
            except ImportError:
                print("SKIP_NO_PYSIDE6")
                raise SystemExit(0)

            sys.path.insert(0, sys.argv[1])
            from core.single_instance import SingleInstanceManager

            app = QCoreApplication([])
            with tempfile.TemporaryDirectory(prefix="quickkv-ipc-test-") as data_root:
                primary = SingleInstanceManager(data_root, timeout_ms=1000)
                activations = []
                primary.set_activation_handler(lambda: activations.append("activate"))
                assert primary.acquire() == SingleInstanceManager.OWNER

                child_code = (
                    "import sys\\n"
                    "from PySide6.QtCore import QCoreApplication\\n"
                    "sys.path.insert(0, sys.argv[1])\\n"
                    "from core.single_instance import SingleInstanceManager\\n"
                    "app = QCoreApplication([])\\n"
                    "print(SingleInstanceManager(sys.argv[2], timeout_ms=1000).acquire(), flush=True)\\n"
                )
                child = subprocess.Popen(
                    [sys.executable, "-c", child_code, sys.argv[1], data_root],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline and (child.poll() is None or not activations):
                    app.processEvents()
                    time.sleep(0.01)
                stdout, stderr = child.communicate(timeout=1)
                primary.release()
                assert stdout.strip() == SingleInstanceManager.NOTIFIED_OWNER, (stdout, stderr)
                assert len(activations) == 1, (stdout, stderr)
                print("PASS_REAL_QT")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(SRC_ROOT)],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.stdout.strip() == "SKIP_NO_PYSIDE6":
            self.skipTest("当前解释器未安装 PySide6")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS_REAL_QT", result.stdout)

    @unittest.skipUnless(sys.platform == "win32", "Windows named mutex regression")
    def test_real_windows_eight_process_atomic_ownership_multiple_rounds(self):
        """A synchronized race must produce exactly one overlapping owner."""
        pyside_probe = subprocess.run(
            [sys.executable, "-c", "import PySide6"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if pyside_probe.returncode != 0:
            self.skipTest("当前解释器未安装 PySide6")

        child_code = textwrap.dedent(
            """
            import os
            import sys
            import time

            from PySide6.QtCore import QCoreApplication

            sys.path.insert(0, sys.argv[1])
            from core.single_instance import SingleInstanceManager

            data_root, barrier = sys.argv[2], sys.argv[3]
            app = QCoreApplication([])
            manager = SingleInstanceManager(
                data_root,
                timeout_ms=200,
                notification_attempts=12,
                notification_retry_ms=25,
            )
            print("READY", flush=True)
            deadline = time.monotonic() + 10
            while not os.path.exists(barrier):
                if time.monotonic() >= deadline:
                    print("BARRIER_TIMEOUT", flush=True)
                    raise SystemExit(2)
                time.sleep(0.005)

            result = manager.acquire()
            print(result, flush=True)
            if result == SingleInstanceManager.OWNER:
                hold_until = time.monotonic() + 0.6
                while time.monotonic() < hold_until:
                    app.processEvents()
                    time.sleep(0.005)
            manager.release()
            """
        )

        rounds = 8
        contenders = 8
        with tempfile.TemporaryDirectory(prefix="quickkv-mutex-race-") as root:
            for round_index in range(rounds):
                round_root = Path(root) / f"round-{round_index}"
                round_root.mkdir()
                data_root = round_root / "data"
                barrier = round_root / "go"
                children = [
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            child_code,
                            str(SRC_ROOT),
                            str(data_root),
                            str(barrier),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                    )
                    for _ in range(contenders)
                ]
                try:
                    ready = [child.stdout.readline().strip() for child in children]
                    self.assertEqual(ready, ["READY"] * contenders)
                    barrier.touch()

                    results = []
                    diagnostics = []
                    for child in children:
                        stdout, stderr = child.communicate(timeout=8)
                        results.append(stdout.strip())
                        diagnostics.append((child.returncode, stdout, stderr))
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                            child.communicate()

                self.assertTrue(
                    all(returncode == 0 for returncode, _, _ in diagnostics),
                    diagnostics,
                )
                self.assertEqual(
                    results.count(SingleInstanceManager.OWNER),
                    1,
                    (round_index, results, diagnostics),
                )
                self.assertTrue(
                    all(
                        result
                        in {
                            SingleInstanceManager.OWNER,
                            SingleInstanceManager.NOTIFIED_OWNER,
                            SingleInstanceManager.UNAVAILABLE,
                        }
                        for result in results
                    ),
                    (round_index, results, diagnostics),
                )


if __name__ == "__main__":
    unittest.main()
