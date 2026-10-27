from pathlib import Path
from contextlib import contextmanager
import json
import tempfile
import threading
import time
import unittest
import unittest.mock
from unittest.mock import patch

from autodbg.serial import runtime


@contextmanager
def _null_context():
    yield


class SerialRuntimeTest(unittest.TestCase):
    def test_open_serial_port_launches_observe_serial_when_missing(self) -> None:
        class FakeBrokerSerialPort:
            def __init__(self, *, broker_registry, serial_port: str, timeout: float) -> None:
                self.broker_registry = broker_registry
                self.serial_port = serial_port
                self.timeout = timeout
                self.closed = False

            def close(self) -> None:
                self.closed = True

        registry = runtime.SerialBrokerRegistry(
            host="127.0.0.1",
            tcp_port=9001,
            pid=1234,
            serial_port="COM19",
            baudrate=115200,
        )

        with (
            patch.object(runtime, "load_serial_broker_registry", return_value=None),
            patch.object(runtime, "ensure_observe_serial_broker", return_value=registry) as ensure_mock,
            patch.object(runtime, "_start_local_serial_broker") as start_mock,
            patch.object(runtime, "_BrokerSerialPort", FakeBrokerSerialPort),
            patch.object(runtime, "_acquire_control_lock", return_value=_null_context()),
        ):
            with runtime.open_serial_port("COM19", 115200, timeout=0.2) as handle:
                self.assertEqual(handle.serial_port, "COM19")
                self.assertEqual(handle.broker_registry.tcp_port, 9001)

        ensure_mock.assert_called_once_with("COM19", baudrate=115200)
        start_mock.assert_not_called()
        self.assertTrue(handle.closed)

    def test_open_serial_port_can_disable_broker_first_for_direct_access(self) -> None:
        fake_handle = object()

        @contextmanager
        def fake_direct(_port: str, _baudrate: int, _timeout: float):
            yield fake_handle

        with (
            patch.object(runtime, "load_serial_broker_registry", return_value=None),
            patch.object(runtime, "_start_local_serial_broker") as start_mock,
            patch.object(runtime, "_open_direct_serial_port", side_effect=fake_direct) as direct_mock,
        ):
            with runtime.open_serial_port("COM19", 115200, timeout=0.2, broker_first=False) as handle:
                self.assertIs(handle, fake_handle)

        start_mock.assert_not_called()
        direct_mock.assert_called_once()

    def test_serial_trace_stream_client_reads_trace_entries(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.sent: list[bytes] = []
                self._chunks = [
                    (
                        b'{"type":"hello","ok":true}\n'
                        b'{"type":"trace","entry":{"timestamp":"2026-04-17T04:40:00.123","port":"COM19","direction":"rx","payload":"boot","pid":321}}\n'
                    ),
                    b"",
                ]

            def settimeout(self, _value: float) -> None:
                return None

            def sendall(self, payload: bytes) -> None:
                self.sent.append(payload)

            def recv(self, _size: int) -> bytes:
                if self._chunks:
                    return self._chunks.pop(0)
                return b""

            def shutdown(self, _how: int) -> None:
                return None

            def close(self) -> None:
                return None

        fake_socket = FakeSocket()
        registry = runtime.SerialBrokerRegistry(
            host="127.0.0.1",
            tcp_port=9001,
            pid=1234,
            serial_port="COM19",
            baudrate=115200,
        )

        with patch.object(runtime.socket, "create_connection", return_value=fake_socket):
            client = runtime.SerialTraceStreamClient(
                broker_registry=registry,
                serial_port="COM19",
                timeout=0.1,
            )
            try:
                entry = client.read_entry(timeout=0.5)
            finally:
                client.close()

        self.assertIsNotNone(entry)
        self.assertEqual(entry.port, "COM19")
        self.assertEqual(entry.direction, "rx")
        self.assertEqual(entry.payload, "boot")
        self.assertTrue(fake_socket.sent)

    def test_read_lock_pid_handles_empty_and_invalid_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "com19.lock"
            lock_path.write_text("", encoding="ascii")
            self.assertIsNone(runtime._read_lock_pid(lock_path))

            lock_path.write_text("not-a-pid", encoding="ascii")
            self.assertIsNone(runtime._read_lock_pid(lock_path))

            lock_path.write_text("1234", encoding="ascii")
            self.assertEqual(runtime._read_lock_pid(lock_path), 1234)

    def test_remove_stale_lock_deletes_dead_pid_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "com19.lock"
            lock_path.write_text("9052", encoding="ascii")

            with patch.object(runtime, "_pid_is_running", return_value=False):
                removed = runtime._remove_stale_lock(lock_path)

            self.assertTrue(removed)
            self.assertFalse(lock_path.exists())

    def test_remove_stale_lock_preserves_live_pid_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "com19.lock"
            lock_path.write_text("9052", encoding="ascii")

            with patch.object(runtime, "_pid_is_running", return_value=True):
                removed = runtime._remove_stale_lock(lock_path)

            self.assertFalse(removed)
            self.assertTrue(lock_path.exists())

    def test_acquire_pid_lock_does_not_remove_replaced_lock_on_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "com19.lock"

            with runtime._acquire_pid_lock(lock_path, busy_message="busy"):
                lock_path.write_text("999999", encoding="ascii")

            self.assertTrue(lock_path.exists())
            self.assertEqual(lock_path.read_text(encoding="ascii"), "999999")

    def test_busy_port_error_names_the_process_holding_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as lock_dir:
            lock_root = Path(lock_dir)
            (lock_root / "com19.lock").write_text("5555", encoding="ascii")
            with (
                patch.object(runtime, "_serial_lock_dir", return_value=lock_root),
                patch.object(runtime, "_pid_is_running", return_value=True),
                patch.object(runtime, "pid_process_name", return_value="python.exe"),
            ):
                with self.assertRaises(runtime.SerialPortBusyError) as busy:
                    with runtime._acquire_port_lock("COM19"):
                        pass

        self.assertIn("pid 5555", str(busy.exception))
        self.assertIn("python.exe", str(busy.exception))

    def test_append_trace_entry_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(runtime, "_serial_trace_dir", return_value=Path(temp_dir)):
                runtime._append_trace_entry("COM19", "tx", "root")
                trace_path = runtime.serial_trace_log_path("COM19")
                payload = trace_path.read_text(encoding="utf-8")

        self.assertIn('"port": "COM19"', payload)
        self.assertIn('"direction": "tx"', payload)
        self.assertIn('"payload": "root"', payload)
        self.assertEqual(trace_path.parent.name, "COM19")
        self.assertEqual(trace_path.name, "trace.jsonl")

    def test_serial_trace_log_path_defaults_under_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            with patch.dict(runtime.os.environ, {"AUTO_DBG_PROJECT_ROOT": str(project_root)}, clear=False):
                trace_path = runtime.serial_trace_log_path("COM22")

        self.assertEqual(trace_path, project_root / "autodbg" / "serial-log" / "COM22" / "trace.jsonl")

    def test_ensure_observe_serial_broker_launches_terminal_until_protected(self) -> None:
        registry = runtime.SerialBrokerRegistry(
            host="127.0.0.1",
            tcp_port=9001,
            pid=1234,
            serial_port="COM19",
            baudrate=115200,
            owner="human-observe",
            protected=True,
        )

        with (
            tempfile.TemporaryDirectory() as broker_dir,
            patch.object(runtime, "_serial_broker_dir", return_value=Path(broker_dir)),
            patch.object(runtime, "load_serial_broker_registry", side_effect=[None, registry]),
            patch.object(runtime, "_launch_observe_serial_window") as launch_mock,
            patch.object(runtime.time, "sleep"),
        ):
            launch_mock.return_value.poll.return_value = None
            launch_mock.return_value.pid = 4242
            observed = runtime.ensure_observe_serial_broker("COM19", baudrate=115200, wait_timeout=0.5)

        self.assertEqual(observed, registry)
        launch_mock.assert_called_once()
        self.assertEqual(launch_mock.call_args.args, ("COM19",))
        self.assertEqual(launch_mock.call_args.kwargs["baudrate"], 115200)

    def test_protect_serial_broker_registry_updates_existing_owner(self) -> None:
        with tempfile.TemporaryDirectory() as broker_dir:
            broker_root = Path(broker_dir)
            broker_path = broker_root / "com19.json"
            broker_path.write_text(
                '{"host":"127.0.0.1","tcp_port":9001,"pid":1111,"serial_port":"COM19","baudrate":115200}\n',
                encoding="utf-8",
                newline="\n",
            )
            with patch.object(runtime, "_serial_broker_dir", return_value=broker_root):
                with patch.object(runtime, "_pid_is_running", return_value=True):
                    registry = runtime.protect_serial_broker_registry("COM19")

        self.assertIsNotNone(registry)
        self.assertEqual(registry.pid, 1111)
        self.assertEqual(registry.owner, "human-observe")
        self.assertTrue(registry.protected)

    def test_load_serial_broker_registry_returns_live_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            broker_dir = Path(temp_dir)
            with patch.object(runtime, "_serial_broker_dir", return_value=broker_dir):
                path = runtime.write_serial_broker_registry("COM19", host="127.0.0.1", tcp_port=9001, baudrate=115200)
                payload = runtime.SerialBrokerRegistry.from_path(path)
                with patch.object(runtime, "_pid_is_running", return_value=True):
                    registry = runtime.load_serial_broker_registry("COM19")

        self.assertIsNotNone(payload)
        self.assertIsNotNone(registry)
        self.assertEqual(registry.host, "127.0.0.1")
        self.assertEqual(registry.tcp_port, 9001)

    def test_load_serial_broker_registry_removes_stale_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            broker_dir = Path(temp_dir)
            with patch.object(runtime, "_serial_broker_dir", return_value=broker_dir):
                path = runtime.write_serial_broker_registry("COM19", host="127.0.0.1", tcp_port=9001, baudrate=115200)
                with patch.object(runtime, "_pid_is_running", return_value=False):
                    registry = runtime.load_serial_broker_registry("COM19")

        self.assertIsNone(registry)
        self.assertFalse(path.exists())

    def test_list_serial_broker_registries_skips_stale_entries(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            tempfile.TemporaryDirectory() as lock_dir,
            tempfile.TemporaryDirectory() as control_lock_dir,
        ):
            broker_dir = Path(temp_dir)
            lock_root = Path(lock_dir)
            control_root = Path(control_lock_dir)
            (broker_dir / "com19.json").write_text(
                '{"host":"127.0.0.1","tcp_port":9001,"pid":1111,"serial_port":"COM19","baudrate":115200}\n',
                encoding="utf-8",
                newline="\n",
            )
            stale_path = broker_dir / "com20.json"
            stale_path.write_text(
                '{"host":"127.0.0.1","tcp_port":9002,"pid":2222,"serial_port":"COM20","baudrate":115200}\n',
                encoding="utf-8",
                newline="\n",
            )
            with patch.object(runtime, "_serial_broker_dir", return_value=broker_dir):
                with patch.object(runtime, "_serial_lock_dir", return_value=lock_root):
                    with patch.object(runtime, "_serial_control_lock_dir", return_value=control_root):
                        with patch.object(runtime, "_pid_is_running", side_effect=lambda pid: pid == 1111):
                            registries = runtime.list_serial_broker_registries()

        self.assertEqual([registry.serial_port for registry in registries], ["COM19"])
        self.assertFalse(stale_path.exists())

    def test_stop_serial_broker_terminates_process_and_cleans_locks(self) -> None:
        with tempfile.TemporaryDirectory() as broker_dir, tempfile.TemporaryDirectory() as lock_dir, tempfile.TemporaryDirectory() as control_lock_dir:
            broker_root = Path(broker_dir)
            lock_root = Path(lock_dir)
            control_root = Path(control_lock_dir)
            broker_path = broker_root / "com19.json"
            broker_path.write_text(
                '{"host":"127.0.0.1","tcp_port":9001,"pid":1111,"serial_port":"COM19","baudrate":115200}\n',
                encoding="utf-8",
                newline="\n",
            )
            (lock_root / "com19.lock").write_text("1111", encoding="ascii")
            (control_root / "com19.lock").write_text("1111", encoding="ascii")
            alive = {"value": True}

            def fake_pid_is_running(pid: int) -> bool:
                return pid == 1111 and alive["value"]

            def fake_kill(pid: int, _sig: int) -> None:
                self.assertEqual(pid, 1111)
                alive["value"] = False

            with patch.object(runtime, "_serial_broker_dir", return_value=broker_root):
                with patch.object(runtime, "_serial_lock_dir", return_value=lock_root):
                    with patch.object(runtime, "_serial_control_lock_dir", return_value=control_root):
                        with patch.object(runtime, "_pid_is_running", side_effect=fake_pid_is_running):
                            with patch.object(runtime.os, "kill", side_effect=fake_kill) as kill_mock:
                                registry = runtime.stop_serial_broker("COM19", wait_timeout=0.1)

        self.assertIsNotNone(registry)
        self.assertEqual(registry.serial_port, "COM19")
        kill_mock.assert_called_once()
        self.assertFalse(broker_path.exists())
        self.assertFalse((lock_root / "com19.lock").exists())
        self.assertFalse((control_root / "com19.lock").exists())

    def test_stop_serial_broker_refuses_protected_human_observer(self) -> None:
        with tempfile.TemporaryDirectory() as broker_dir:
            broker_root = Path(broker_dir)
            broker_path = broker_root / "com19.json"
            broker_path.write_text(
                (
                    '{"host":"127.0.0.1","tcp_port":9001,"pid":1111,'
                    '"serial_port":"COM19","baudrate":115200,'
                    '"owner":"human-observe","protected":true}\n'
                ),
                encoding="utf-8",
                newline="\n",
            )

            with patch.object(runtime, "_serial_broker_dir", return_value=broker_root):
                with patch.object(runtime, "_pid_is_running", return_value=True):
                    with patch.object(runtime.os, "kill") as kill_mock:
                        with self.assertRaises(runtime.SerialBrokerProtectedError):
                            runtime.stop_serial_broker("COM19", wait_timeout=0.1)

                        kill_mock.assert_not_called()
            self.assertTrue(broker_path.exists())

    def test_stop_serial_broker_can_force_protected_human_observer(self) -> None:
        with tempfile.TemporaryDirectory() as broker_dir, tempfile.TemporaryDirectory() as lock_dir, tempfile.TemporaryDirectory() as control_lock_dir:
            broker_root = Path(broker_dir)
            lock_root = Path(lock_dir)
            control_root = Path(control_lock_dir)
            broker_path = broker_root / "com19.json"
            broker_path.write_text(
                (
                    '{"host":"127.0.0.1","tcp_port":9001,"pid":1111,'
                    '"serial_port":"COM19","baudrate":115200,'
                    '"owner":"human-observe","protected":true}\n'
                ),
                encoding="utf-8",
                newline="\n",
            )
            alive = {"value": True}

            def fake_pid_is_running(pid: int) -> bool:
                return pid == 1111 and alive["value"]

            def fake_kill(pid: int, _sig: int) -> None:
                self.assertEqual(pid, 1111)
                alive["value"] = False

            with patch.object(runtime, "_serial_broker_dir", return_value=broker_root):
                with patch.object(runtime, "_serial_lock_dir", return_value=lock_root):
                    with patch.object(runtime, "_serial_control_lock_dir", return_value=control_root):
                        with patch.object(runtime, "_pid_is_running", side_effect=fake_pid_is_running):
                            with patch.object(runtime.os, "kill", side_effect=fake_kill) as kill_mock:
                                registry = runtime.stop_serial_broker(
                                    "COM19",
                                    wait_timeout=0.1,
                                    allow_protected=True,
                                )

        self.assertIsNotNone(registry)
        kill_mock.assert_called_once()
        self.assertFalse(broker_path.exists())


class _FakeClock:
    """Stands in for runtime.time: sleeping advances both clocks instantly."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.01)


_PROTECTED_COM19 = runtime.SerialBrokerRegistry(
    host="127.0.0.1",
    tcp_port=9001,
    pid=1234,
    serial_port="COM19",
    baudrate=115200,
    owner="human-observe",
    protected=True,
)


class ControlLockWaitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _FakeClock()
        lock_dir = tempfile.TemporaryDirectory()
        self.addCleanup(lock_dir.cleanup)
        self.lock_root = Path(lock_dir.name)
        self.holder_lock = self.lock_root / "com19.lock"
        self.holder_lock.write_text("5555", encoding="ascii")
        for target, kwargs in (
            ("time", {"new": self.clock}),
            ("_serial_control_lock_dir", {"return_value": self.lock_root}),
            ("_pid_is_running", {"side_effect": lambda pid: pid in {5555, runtime.os.getpid()}}),
        ):
            patcher = patch.object(runtime, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_waits_for_a_parallel_command_to_release_the_port(self) -> None:
        started = self.clock.now
        original_sleep = self.clock.sleep

        def release_after_three_seconds(seconds: float) -> None:
            original_sleep(seconds)
            if self.clock.now - started >= 3.0 and self.holder_lock.exists():
                self.holder_lock.unlink()

        self.clock.sleep = release_after_three_seconds
        with runtime._acquire_control_lock("COM19"):
            self.assertEqual(self.holder_lock.read_text(encoding="ascii"), str(runtime.os.getpid()))

        self.assertGreaterEqual(self.clock.now - started, 3.0)

    def test_gives_up_after_the_deadline_and_names_the_holder(self) -> None:
        started = self.clock.now

        with self.assertRaises(runtime.SerialPortBusyError) as busy:
            with runtime._acquire_control_lock("COM19", wait_timeout=10.0):
                self.fail("must not acquire a lock held by a live process")

        self.assertGreaterEqual(self.clock.now - started, 10.0)
        self.assertLess(self.clock.now - started, 12.0)
        self.assertIn("pid 5555", str(busy.exception))

    def test_observe_window_input_does_not_freeze_while_an_ai_command_holds_the_port(self) -> None:
        from autodbg.cli.watch import _send_watch_serial_text

        started = self.clock.now
        with patch.object(runtime, "load_serial_broker_registry", return_value=_PROTECTED_COM19):
            ok, message = _send_watch_serial_text("COM19", 115200, "ls\n")

        self.assertFalse(ok)
        self.assertIn("already in use", message)
        self.assertLessEqual(self.clock.now - started, 2.5)

    def test_a_busy_error_raised_while_holding_the_lock_is_not_retried(self) -> None:
        self.holder_lock.unlink()

        with self.assertRaises(runtime.SerialPortBusyError) as raised:
            with runtime._acquire_control_lock("COM19"):
                raise runtime.SerialPortBusyError("from the command body")

        self.assertEqual(str(raised.exception), "from the command body")
        self.assertFalse(self.holder_lock.exists())


class EnsureObserveSerialBrokerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _FakeClock()
        self.broker_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.broker_dir.cleanup)
        self.ready_at: float | None = None
        self.window = unittest.mock.Mock(pid=4242)
        self.window.poll.return_value = None
        self.status_files: list[Path] = []
        self.launch_mock = unittest.mock.Mock(side_effect=self._launch)
        self.alive_pids = {4242, runtime.os.getpid()}
        for target, kwargs in (
            ("time", {"new": self.clock}),
            ("_serial_broker_dir", {"return_value": Path(self.broker_dir.name)}),
            ("load_serial_broker_registry", {"side_effect": self._registry}),
            ("_launch_observe_serial_window", {"new": self.launch_mock}),
            ("_pid_is_running", {"side_effect": lambda pid: pid in self.alive_pids}),
        ):
            patcher = patch.object(runtime, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _launch(self, _port: str, *, baudrate: int, status_file: Path):
        self.status_files.append(status_file)
        return self.window

    def _registry(self, _port: str):
        if self.ready_at is not None and self.clock.now >= self.ready_at:
            return _PROTECTED_COM19
        return None

    def _after(self, seconds: float, action) -> None:
        """Run action once the fake clock has advanced `seconds` past now (it advances only while ensure sleeps)."""
        due = self.clock.now + seconds
        original_sleep = self.clock.sleep
        done = {"value": False}

        def sleep(step: float) -> None:
            original_sleep(step)
            if not done["value"] and self.clock.now >= due:
                done["value"] = True
                action()

        self.clock.sleep = sleep

    def _write_marker(self, **fields) -> Path:
        marker_path = runtime._observe_launch_marker_path("COM19")
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps(fields), encoding="utf-8")
        return marker_path

    def test_waits_for_an_observe_window_that_needs_twenty_seconds(self) -> None:
        self.ready_at = self.clock.now + 20.0

        observed = runtime.ensure_observe_serial_broker("COM19", baudrate=115200)

        self.assertEqual(observed, _PROTECTED_COM19)
        self.launch_mock.assert_called_once()
        self.assertFalse(runtime._observe_launch_marker_path("COM19").exists())

    def test_a_retry_waits_for_the_window_already_starting_instead_of_opening_another(self) -> None:
        with self.assertRaises(RuntimeError) as first:
            runtime.ensure_observe_serial_broker("COM19", baudrate=115200)
        self.assertIn("still starting", str(first.exception))
        self.assertIn("4242", str(first.exception))

        self.ready_at = self.clock.now + 5.0
        observed = runtime.ensure_observe_serial_broker("COM19", baudrate=115200)

        self.assertEqual(observed, _PROTECTED_COM19)
        self.launch_mock.assert_called_once()

    def test_fails_fast_with_the_windows_reason_when_it_cannot_open_the_port(self) -> None:
        started = self.clock.now

        def window_records_its_error() -> None:
            with patch.dict(runtime.os.environ, {"AUTO_DBG_OBSERVE_STATUS_FILE": str(self.status_files[-1])}):
                runtime.record_observe_start_error(
                    "COM19",
                    "Failed to open COM19: Cannot configure port ... WinError 31",
                    ["Windows reported the serial device is not functioning; unplug/replug the USB serial adapter"],
                )

        self._after(2.0, window_records_its_error)
        with self.assertRaises(RuntimeError) as failure:
            runtime.ensure_observe_serial_broker("COM19", baudrate=115200)

        self.assertLess(self.clock.now - started, 5.0)
        self.assertIn("WinError 31", str(failure.exception))
        self.assertIn("unplug/replug", str(failure.exception))
        self.assertFalse(runtime._observe_launch_marker_path("COM19").exists())

    def test_fails_fast_when_the_paused_window_reports_its_exit_code(self) -> None:
        # python died before opening the port (no error recorded); observe-serial.ps1 then keeps the window open
        # for the user to read, so the process never exits: the exit code it writes is the only signal
        started = self.clock.now
        self._after(3.0, lambda: self.status_files[-1].write_text('{"exit_code": 1}', encoding="ascii"))

        with self.assertRaises(RuntimeError) as failure:
            runtime.ensure_observe_serial_broker("COM19", baudrate=115200)

        self.assertLess(self.clock.now - started, 5.0)
        self.assertIn("exited with code 1", str(failure.exception))

    def test_fails_fast_when_the_window_process_exits(self) -> None:
        started = self.clock.now
        self.window.poll.side_effect = lambda: 1 if self.clock.now - started >= 3.0 else None

        with self.assertRaises(RuntimeError) as failure:
            runtime.ensure_observe_serial_broker("COM19", baudrate=115200)

        self.assertLess(self.clock.now - started, 5.0)
        self.assertIn("exited with code 1", str(failure.exception))
        self.assertFalse(runtime._observe_launch_marker_path("COM19").exists())

    def test_another_windows_failure_is_not_taken_for_ours(self) -> None:
        # e.g. a second window the user opened by hand, failing with "already in use" while ours starts
        stale = Path(self.broker_dir.name) / "com19.launch-someoneelse.status"
        stale.write_text(json.dumps({"error": "Serial port COM19 is already in use", "hints": []}), encoding="utf-8")
        self.ready_at = self.clock.now + 3.0

        observed = runtime.ensure_observe_serial_broker("COM19", baudrate=115200)

        self.assertEqual(observed, _PROTECTED_COM19)

    def test_a_waiter_reports_the_launchers_failure_and_leaves_its_marker_alone(self) -> None:
        status_file = Path(self.broker_dir.name) / "com19.launch-theirs.status"
        marker_path = self._write_marker(
            token="theirs",
            launcher_pid=777,
            window_pid=4242,
            started_at=self.clock.now - 1.0,
            status_file=str(status_file),
        )
        marker_text = marker_path.read_text(encoding="utf-8")
        self._after(1.0, lambda: status_file.write_text(json.dumps({"error": "WinError 31", "hints": []}), encoding="utf-8"))

        with self.assertRaises(RuntimeError) as failure:
            runtime.ensure_observe_serial_broker("COM19", baudrate=115200)

        self.assertIn("WinError 31", str(failure.exception))
        self.launch_mock.assert_not_called()
        self.assertEqual(marker_path.read_text(encoding="utf-8"), marker_text)

    def test_a_waiting_caller_returns_the_broker_its_launcher_just_saw(self) -> None:
        marker_path = self._write_marker(token="theirs", launcher_pid=777, window_pid=4242, started_at=self.clock.now - 1.0)
        polls = {"count": 0}

        def registry_appears_after_the_marker_is_cleared(_port: str):
            polls["count"] += 1
            if polls["count"] == 2:
                # the launcher saw the broker and removed its marker between our two looks
                marker_path.unlink()
            return _PROTECTED_COM19 if polls["count"] >= 3 else None

        with patch.object(runtime, "load_serial_broker_registry", side_effect=registry_appears_after_the_marker_is_cleared):
            observed = runtime.ensure_observe_serial_broker("COM19", baudrate=115200)

        self.assertEqual(observed, _PROTECTED_COM19)
        self.launch_mock.assert_not_called()

    def test_an_abandoned_launch_claim_stops_blocking_after_a_short_grace(self) -> None:
        # an older autodbg (or a crash between two writes) can leave an empty marker behind
        marker_path = runtime._observe_launch_marker_path("COM19")
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("", encoding="utf-8")
        runtime.os.utime(marker_path, (self.clock.now - 30.0, self.clock.now - 30.0))
        self.ready_at = self.clock.now + 2.0

        observed = runtime.ensure_observe_serial_broker("COM19", baudrate=115200)

        self.assertEqual(observed, _PROTECTED_COM19)
        self.launch_mock.assert_called_once()

    def test_relaunches_when_the_previous_window_is_gone(self) -> None:
        self._write_marker(token="old", launcher_pid=777, window_pid=778, started_at=self.clock.now - 30.0)
        self.ready_at = self.clock.now + 2.0

        observed = runtime.ensure_observe_serial_broker("COM19", baudrate=115200)

        self.assertEqual(observed, _PROTECTED_COM19)
        self.launch_mock.assert_called_once()


class EnsureObserveSerialBrokerConcurrencyTest(unittest.TestCase):
    def test_simultaneous_callers_finding_a_stale_marker_open_one_window(self) -> None:
        # real threads and clock: each caller judges the stale marker and would replace it
        broker_dir = tempfile.TemporaryDirectory()
        self.addCleanup(broker_dir.cleanup)
        launched: list[float] = []
        launch_lock = threading.Lock()
        window = unittest.mock.Mock(pid=4242)
        window.poll.return_value = None

        def launch(_port: str, *, baudrate: int, status_file: Path):
            with launch_lock:
                launched.append(time.monotonic())
            return window

        def registry(_port: str):
            return _PROTECTED_COM19 if launched and time.monotonic() - launched[0] > 0.3 else None

        with (
            patch.object(runtime, "_serial_broker_dir", return_value=Path(broker_dir.name)),
            patch.object(runtime, "load_serial_broker_registry", side_effect=registry),
            patch.object(runtime, "_launch_observe_serial_window", side_effect=launch),
            patch.object(runtime, "_pid_is_running", side_effect=lambda pid: pid in {4242, runtime.os.getpid()}),
            patch.object(runtime, "_OBSERVE_LAUNCH_POLL_SECONDS", 0.02),
        ):
            runtime._observe_launch_marker_path("COM19").write_text(
                json.dumps({"token": "dead", "launcher_pid": 777, "window_pid": 778, "started_at": time.time() - 30}),
                encoding="utf-8",
            )
            results: list[object] = []
            callers = [
                threading.Thread(
                    target=lambda: results.append(runtime.ensure_observe_serial_broker("COM19", baudrate=115200, wait_timeout=5.0))
                )
                for _ in range(6)
            ]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(timeout=10.0)

        self.assertEqual(len(launched), 1)
        self.assertEqual(results, [_PROTECTED_COM19] * 6)


if __name__ == "__main__":
    unittest.main()
