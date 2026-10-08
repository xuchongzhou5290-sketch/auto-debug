"""A broker registry may only be deleted when that broker is definitely gone.

Regression for a live observe-serial broker (pid alive, TCP port answering) whose %TEMP%/autodbg-serial-brokers
registry was deleted, after which exec/watch-serial could no longer find it.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from autodbg.serial import broker, runtime
from autodbg.utils.process import PID_ALIVE, PID_UNKNOWN


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _fake_broker(*, identity: dict | None = None, trace_line_first: bool = False):
    """TCP endpoint speaking the broker handshake; identity=None answers like an older broker (bare hello)."""

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            line = self.rfile.readline()
            try:
                hello = json.loads(line.decode("utf-8"))
            except ValueError:
                return
            if hello.get("type") != "hello":
                return
            if trace_line_first:
                self.wfile.write(b'{"type":"trace","entry":{"payload":"boot"}}\n')
            reply = {"type": "hello", "ok": True}
            reply.update(identity or {})
            self.wfile.write(json.dumps(reply).encode("utf-8") + b"\n")

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _dirs():
    """Private registry and lock directories (never the real %TEMP% ones the running brokers use)."""
    with tempfile.TemporaryDirectory() as broker_dir, tempfile.TemporaryDirectory() as lock_dir:
        broker_root, lock_root = Path(broker_dir), Path(lock_dir)
        with (
            patch.object(runtime, "_serial_broker_dir", return_value=broker_root),
            patch.object(runtime, "_serial_lock_dir", return_value=lock_root),
        ):
            yield broker_root, lock_root


def _write(port: str, *, tcp_port: int, pid: int) -> Path:
    return runtime.write_serial_broker_registry(port, host="127.0.0.1", tcp_port=tcp_port, baudrate=115200, pid=pid)


@contextmanager
def _pid(state_by_pid: dict):
    """Patch the liveness view: state_by_pid maps pid -> 'alive' | 'dead' | 'unknown'."""
    with (
        patch.object(runtime, "_pid_is_running", side_effect=lambda pid: state_by_pid.get(pid, "dead") != "dead"),
        patch.object(runtime, "_pid_state", side_effect=lambda pid: state_by_pid.get(pid, "dead")),
    ):
        yield


class RegistryLivenessTest(unittest.TestCase):
    def test_unknown_pid_kept_when_broker_identifies_itself(self) -> None:
        with _dirs(), _fake_broker(identity={"serial_port": "COM31", "pid": 27976}, trace_line_first=True) as port:
            path = _write("COM31", tcp_port=port, pid=27976)
            with _pid({27976: PID_UNKNOWN}):
                registry = runtime.load_serial_broker_registry("COM31")
            self.assertIsNotNone(registry)
            self.assertTrue(path.exists())

    def test_unknown_pid_dropped_when_another_ports_broker_answers(self) -> None:
        # the dead registry's TCP port now belongs to COM19's broker: must not route COM31 traffic there
        with _dirs(), _fake_broker(identity={"serial_port": "COM19", "pid": 30936}) as port:
            path = _write("COM31", tcp_port=port, pid=27976)
            with _pid({27976: PID_UNKNOWN}):
                registry = runtime.load_serial_broker_registry("COM31")
            self.assertIsNone(registry)
            self.assertFalse(path.exists())

    def test_unknown_pid_dropped_when_answering_broker_has_another_pid(self) -> None:
        with _dirs(), _fake_broker(identity={"serial_port": "com31", "pid": 11111}) as port:
            path = _write("COM31", tcp_port=port, pid=27976)
            with _pid({27976: PID_UNKNOWN}):
                self.assertIsNone(runtime.load_serial_broker_registry("COM31"))
            self.assertFalse(path.exists())

    def test_unknown_pid_older_broker_kept_when_port_lock_matches(self) -> None:
        with _dirs() as (_broker_root, lock_root), _fake_broker(identity=None) as port:
            (lock_root / "com31.lock").write_text("27976", encoding="ascii")
            path = _write("COM31", tcp_port=port, pid=27976)
            with _pid({27976: PID_UNKNOWN}):
                self.assertIsNotNone(runtime.load_serial_broker_registry("COM31"))
            self.assertTrue(path.exists())

    def test_unknown_pid_older_broker_dropped_when_port_lock_differs(self) -> None:
        with _dirs() as (_broker_root, lock_root), _fake_broker(identity=None) as port:
            (lock_root / "com31.lock").write_text("30936", encoding="ascii")
            path = _write("COM31", tcp_port=port, pid=27976)
            with _pid({27976: PID_UNKNOWN}):
                self.assertIsNone(runtime.load_serial_broker_registry("COM31"))
            self.assertFalse(path.exists())

    def test_unknown_pid_silent_port_kept_when_port_lock_matches(self) -> None:
        # e.g. a sandboxed caller that may neither open the process nor reach loopback: the lock still proves it
        with _dirs() as (_broker_root, lock_root):
            (lock_root / "com31.lock").write_text("27976", encoding="ascii")
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            with _pid({27976: PID_UNKNOWN}):
                self.assertIsNotNone(runtime.load_serial_broker_registry("COM31"))
            self.assertTrue(path.exists())

    def test_unknown_pid_dropped_when_port_silent(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            with _pid({27976: PID_UNKNOWN}):
                self.assertIsNone(runtime.load_serial_broker_registry("COM31"))
            self.assertFalse(path.exists())

    def test_dead_pid_is_conclusive_without_probing(self) -> None:
        with _dirs(), _fake_broker(identity={"serial_port": "COM31", "pid": 27976}) as port:
            path = _write("COM31", tcp_port=port, pid=27976)
            with _pid({}), patch.object(runtime, "_probe_serial_broker") as probe_mock:
                self.assertIsNone(runtime.load_serial_broker_registry("COM31"))
            probe_mock.assert_not_called()
            self.assertFalse(path.exists())

    def test_alive_pid_is_kept_without_probing(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            with _pid({27976: PID_ALIVE}), patch.object(runtime, "_probe_serial_broker") as probe_mock:
                self.assertIsNotNone(runtime.load_serial_broker_registry("COM31"))
            probe_mock.assert_not_called()
            self.assertTrue(path.exists())

    def test_load_keeps_registry_that_is_temporarily_unreadable(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            with patch.object(Path, "read_text", side_effect=PermissionError(13, "sharing violation")):
                registry = runtime.load_serial_broker_registry("COM31")
            self.assertIsNone(registry)
            self.assertTrue(path.exists())

    def test_load_rides_out_a_truncated_read(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            good = path.read_text(encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=["", good]), _pid({27976: PID_ALIVE}):
                registry = runtime.load_serial_broker_registry("COM31")
            self.assertIsNotNone(registry)
            self.assertTrue(path.exists())

    def test_load_removes_persistently_corrupt_registry(self) -> None:
        cases = {
            "not json": b"{not json",
            "not utf-8": b'{"host":"127.0.0.1","serial_port":"COM31\xff\xfe"}',
            "overflow": b'{"host":"127.0.0.1","tcp_port":1e400,"pid":1,"serial_port":"COM31","baudrate":115200}',
            "deep": b"[" * 100000 + b"]" * 100000,
        }
        for name, content in cases.items():
            with self.subTest(name), _dirs() as (broker_root, _lock_root):
                path = broker_root / "com31.json"
                path.write_bytes(content)
                self.assertIsNone(runtime.load_serial_broker_registry("COM31"))
                self.assertFalse(path.exists())

    def test_list_keeps_live_brokers_and_drops_only_dead_or_foreign_ones(self) -> None:
        with _dirs(), _fake_broker(identity={"serial_port": "COM31", "pid": 27976}) as com31_port:
            live = _write("COM31", tcp_port=com31_port, pid=27976)
            alive = _write("COM19", tcp_port=_free_port(), pid=30936)
            dead = _write("COM35", tcp_port=_free_port(), pid=18352)
            foreign = _write("COM27", tcp_port=com31_port, pid=4444)  # its old port is now COM31's broker
            with _pid({27976: PID_UNKNOWN, 30936: PID_ALIVE, 4444: PID_UNKNOWN}):
                ports = sorted(registry.serial_port for registry in runtime.list_serial_broker_registries())
            self.assertEqual(ports, ["COM19", "COM31"])
            self.assertTrue(live.exists())
            self.assertTrue(alive.exists())
            self.assertFalse(dead.exists())
            self.assertFalse(foreign.exists())

    def test_list_skips_unreadable_registry_without_deleting_it(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            with patch.object(Path, "read_text", side_effect=PermissionError(13, "sharing violation")):
                registries = runtime.list_serial_broker_registries()
            self.assertEqual(registries, [])
            self.assertTrue(path.exists())

    def test_remove_with_owner_pid_only_removes_own_registry(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            runtime.remove_serial_broker_registry("COM31", owner_pid=1111)
            self.assertTrue(path.exists())
            runtime.remove_serial_broker_registry("COM31", owner_pid=27976)
            self.assertFalse(path.exists())

    def test_cleanup_does_not_remove_registry_rewritten_by_a_new_broker(self) -> None:
        with _dirs():
            old_path = _write("COM31", tcp_port=_free_port(), pid=1111)
            old = runtime.SerialBrokerRegistry.from_path(old_path)
            new_path = _write("COM31", tcp_port=_free_port(), pid=2222)
            runtime._cleanup_serial_broker_artifacts("COM31", expected=old)
            self.assertTrue(new_path.exists())
            self.assertEqual(runtime.SerialBrokerRegistry.from_path(new_path).pid, 2222)

    def test_stop_cleans_up_when_broker_exits_right_after_load(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            registry = runtime.SerialBrokerRegistry.from_path(path)
            with (
                patch.object(runtime, "load_serial_broker_registry", return_value=registry),
                patch.object(runtime, "_pid_is_running", return_value=False),
                patch.object(runtime, "_terminate_pid") as terminate_mock,
            ):
                self.assertEqual(runtime.stop_serial_broker("COM31", allow_protected=True), registry)
            terminate_mock.assert_not_called()
            self.assertFalse(path.exists())

    def test_stop_does_not_kill_a_reused_pid_whose_port_answers_as_another_broker(self) -> None:
        with _dirs(), _fake_broker(identity={"serial_port": "COM19", "pid": 30936}) as port:
            path = _write("COM31", tcp_port=port, pid=27976)
            with _pid({27976: PID_ALIVE}), patch.object(runtime, "_terminate_pid") as terminate_mock:
                runtime.stop_serial_broker("COM31", allow_protected=True)
            terminate_mock.assert_not_called()
            self.assertFalse(path.exists())

    def test_stop_still_terminates_its_own_broker(self) -> None:
        with _dirs(), _fake_broker(identity={"serial_port": "COM31", "pid": 27976}) as port:
            _write("COM31", tcp_port=port, pid=27976)
            alive = {27976: PID_ALIVE}

            def kill(pid: int) -> None:
                alive.pop(pid, None)

            with _pid(alive), patch.object(runtime, "_terminate_pid", side_effect=kill) as terminate_mock:
                runtime.stop_serial_broker("COM31", wait_timeout=0.5, allow_protected=True)
            terminate_mock.assert_called_once_with(27976)

    def test_remove_stale_lock_tolerates_a_lock_still_held_open(self) -> None:
        with _dirs() as (_broker_root, lock_root):
            lock = lock_root / "com31.lock"
            lock.write_text("27976", encoding="ascii")
            with _pid({}), patch.object(Path, "unlink", side_effect=PermissionError(32, "in use")):
                self.assertFalse(runtime._remove_stale_lock(lock))

    def test_registry_delete_is_retried_while_a_reader_holds_it(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            expected = runtime.SerialBrokerRegistry.from_path(path)
            real_unlink = Path.unlink
            calls = {"n": 0}

            def flaky_unlink(self, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise PermissionError(32, "in use")
                return real_unlink(self, *args, **kwargs)

            with patch.object(Path, "unlink", flaky_unlink):
                runtime._unlink_registry_if_unchanged(path, expected)
            self.assertEqual(calls["n"], 3)
            self.assertFalse(path.exists())

    def test_protect_notifies_the_running_broker(self) -> None:
        received: list[dict] = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.rfile.readline()
                self.wfile.write(b'{"type":"hello","ok":true,"serial_port":"COM31","pid":27976}\n')
                for line in self.rfile:
                    received.append(json.loads(line.decode("utf-8")))

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with _dirs():
                _write("COM31", tcp_port=int(server.server_address[1]), pid=27976)
                with _pid({27976: PID_ALIVE}):
                    registry = runtime.protect_serial_broker_registry("COM31")
                self.assertTrue(registry.protected)
            deadline = threading.Event()
            for _ in range(50):
                if received:
                    break
                deadline.wait(0.02)
            self.assertIn({"type": "protect", "owner": "human-observe"}, received)
        finally:
            server.shutdown()
            server.server_close()

    def test_concurrent_rewrites_never_expose_a_partial_registry(self) -> None:
        with _dirs() as (root, _lock_root):
            _write("COM31", tcp_port=40000, pid=27976)
            stop = threading.Event()
            errors: list[BaseException] = []

            def writer() -> None:
                try:
                    i = 0
                    while not stop.is_set():
                        _write("COM31", tcp_port=40000 + (i % 50), pid=27976)
                        i += 1
                except BaseException as exc:  # pragma: no cover - reported below
                    errors.append(exc)

            thread = threading.Thread(target=writer, daemon=True)
            misses = 0
            with _pid({27976: PID_ALIVE}):
                thread.start()
                try:
                    for _ in range(300):
                        if runtime.load_serial_broker_registry("COM31") is None:
                            misses += 1
                finally:
                    stop.set()
                    thread.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(misses, 0)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["com31.json"])


class _BusyLock:
    def __enter__(self):
        raise runtime.SerialPortBusyError("Serial port COM31 is already in use by another autodbg process.")

    def __exit__(self, *_exc) -> bool:  # pragma: no cover - must never run
        raise AssertionError("exit called on a lock that was never entered")


class BrokerRegistryTest(unittest.TestCase):
    def test_failed_start_on_busy_port_keeps_the_running_brokers_registry(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            second = broker.SerialBroker(serial_port="COM31", baudrate=115200, owner="human-observe", protected=True)
            with (
                patch("autodbg.serial.broker._import_serial", return_value=object()),
                patch("autodbg.serial.broker._acquire_port_lock", return_value=_BusyLock()),
            ):
                with self.assertRaises(runtime.SerialPortBusyError):
                    second.start()
            self.assertIsNone(second._lock_context)
            second.stop()
            self.assertTrue(path.exists())
            self.assertEqual(runtime.SerialBrokerRegistry.from_path(path).pid, 27976)

    def test_stop_removes_own_registration(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=os.getpid())
            instance = broker.SerialBroker(serial_port="COM31", baudrate=115200)
            instance._registered = True
            instance.stop()
            self.assertFalse(path.exists())

    def test_stop_keeps_registration_rewritten_by_another_broker(self) -> None:
        with _dirs():
            path = _write("COM31", tcp_port=_free_port(), pid=27976)
            instance = broker.SerialBroker(serial_port="COM31", baudrate=115200)
            instance._registered = True
            instance.stop()
            self.assertTrue(path.exists())

    def test_stop_does_not_hang_when_server_never_served(self) -> None:
        instance = broker.SerialBroker(serial_port="COM31", baudrate=115200)
        server = Mock()
        server.shutdown.side_effect = AssertionError("shutdown() would block forever without serve_forever()")
        instance._server = server
        instance.stop()
        server.server_close.assert_called_once()

    def _registered_broker(self, tcp_port: int) -> broker.SerialBroker:
        instance = broker.SerialBroker(serial_port="COM31", baudrate=115200, tcp_port=tcp_port)
        instance._registered = True
        instance._broadcast_trace = Mock()
        return instance

    def test_ensure_registered_restores_a_deleted_registry(self) -> None:
        with _dirs() as (root, _lock_root), patch("autodbg.serial.broker._append_trace_entry"):
            instance = self._registered_broker(6730)
            self.assertTrue(instance._ensure_registered())
            restored = runtime.SerialBrokerRegistry.from_path(root / "com31.json")
            self.assertEqual((restored.pid, restored.tcp_port), (os.getpid(), 6730))
            self.assertFalse(instance._ensure_registered())  # already ours: nothing to do

    def test_ensure_registered_keeps_protection_granted_after_start(self) -> None:
        with _dirs() as (root, _lock_root), patch("autodbg.serial.broker._append_trace_entry"):
            instance = self._registered_broker(6730)
            runtime.write_serial_broker_registry(
                "COM31", host="127.0.0.1", tcp_port=6730, baudrate=115200, owner="human-observe", protected=True
            )
            self.assertFalse(instance._ensure_registered())
            (root / "com31.json").unlink()
            self.assertTrue(instance._ensure_registered())
            restored = runtime.SerialBrokerRegistry.from_path(root / "com31.json")
            self.assertEqual((restored.owner, restored.protected), ("human-observe", True))

    def test_ensure_registered_overwrites_a_foreign_entry(self) -> None:
        with _dirs() as (root, _lock_root), patch("autodbg.serial.broker._append_trace_entry"):
            instance = self._registered_broker(6730)
            _write("COM31", tcp_port=9999, pid=1111)
            self.assertTrue(instance._ensure_registered())
            self.assertEqual(runtime.SerialBrokerRegistry.from_path(root / "com31.json").pid, os.getpid())

    def test_protect_message_survives_a_deleted_registry(self) -> None:
        # the upgrade reaches the broker over TCP, so the self-heal restores it as protected
        with _dirs() as (root, _lock_root), patch("autodbg.serial.broker._append_trace_entry"):
            instance = self._registered_broker(6730)
            instance.set_protection("human-observe")
            self.assertTrue(instance._ensure_registered())
            restored = runtime.SerialBrokerRegistry.from_path(root / "com31.json")
            self.assertEqual((restored.owner, restored.protected), ("human-observe", True))

    def test_ensure_registered_repairs_tampered_port_host_or_baudrate(self) -> None:
        for field, value in (("serial_port", "COM35"), ("host", "10.255.255.1"), ("baudrate", 9600)):
            with self.subTest(field), _dirs() as (root, _lock_root), patch("autodbg.serial.broker._append_trace_entry"):
                instance = self._registered_broker(6730)
                self.assertTrue(instance._ensure_registered())
                path = root / "com31.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload[field] = value
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertTrue(instance._ensure_registered())
                repaired = runtime.SerialBrokerRegistry.from_path(path)
                self.assertEqual((repaired.serial_port, repaired.host, repaired.baudrate), ("COM31", "127.0.0.1", 115200))

    def test_handler_applies_protect_message(self) -> None:
        class TwoLineFile:
            def __init__(self) -> None:
                self._first = b'{"type":"hello","role":"protect"}\n'
                self._rest = [b'{"type":"protect","owner":"human-observe"}\n']

            def readline(self) -> bytes:
                line, self._first = self._first, b""
                return line

            def __iter__(self):
                return iter(self._rest)

            def close(self) -> None:
                return None

        target = Mock()
        target.serial_port = "COM31"
        request = SimpleNamespace(
            makefile=lambda _mode: TwoLineFile(),
            sendall=lambda _data: None,
            shutdown=lambda _how: None,
            close=lambda: None,
        )
        handler = broker._BrokerRequestHandler.__new__(broker._BrokerRequestHandler)
        handler.request = request
        handler.server = SimpleNamespace(broker=target)
        handler.handle()
        target.set_protection.assert_called_once_with("human-observe")

    def test_ensure_registered_does_nothing_after_stop(self) -> None:
        with _dirs() as (root, _lock_root):
            instance = self._registered_broker(6730)
            instance.stop()
            self.assertFalse(instance._ensure_registered())
            self.assertFalse((root / "com31.json").exists())

    def test_hello_reply_identifies_the_broker(self) -> None:
        class OneShotFile:
            def __init__(self) -> None:
                self._lines = [b'{"type":"hello","role":"probe"}\n']

            def readline(self) -> bytes:
                return self._lines.pop(0) if self._lines else b""

            def __iter__(self):
                return iter(())

            def close(self) -> None:
                return None

        sent: list[bytes] = []
        request = SimpleNamespace(
            makefile=lambda _mode: OneShotFile(),
            sendall=sent.append,
            shutdown=lambda _how: None,
            close=lambda: None,
        )
        handler = broker._BrokerRequestHandler.__new__(broker._BrokerRequestHandler)
        handler.request = request
        handler.server = SimpleNamespace(broker=SimpleNamespace(serial_port="COM31", add_client=Mock(), remove_client=Mock()))
        handler.handle()
        reply = json.loads(b"".join(sent).decode("utf-8").splitlines()[0])
        self.assertEqual((reply["type"], reply["ok"], reply["serial_port"], reply["pid"]), ("hello", True, "COM31", os.getpid()))


if __name__ == "__main__":
    unittest.main()
