from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autodbg.serial import runtime


class SerialRuntimeTest(unittest.TestCase):
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

    def test_append_trace_entry_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(runtime, "_serial_trace_dir", return_value=Path(temp_dir)):
                runtime._append_trace_entry("COM19", "tx", "root")
                trace_path = runtime.serial_trace_log_path("COM19")
                payload = trace_path.read_text(encoding="utf-8")

        self.assertIn('"port": "COM19"', payload)
        self.assertIn('"direction": "tx"', payload)
        self.assertIn('"payload": "root"', payload)

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


if __name__ == "__main__":
    unittest.main()
