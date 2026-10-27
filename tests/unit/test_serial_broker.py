from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from autodbg.serial import broker
from autodbg.serial.runtime import SerialTraceEntry


def _trace_entry(port: str, direction: str, payload: str) -> SerialTraceEntry:
    return SerialTraceEntry(timestamp="2026-10-26T10:00:00.000", port=port, direction=direction, payload=payload, pid=1)


class _FakeFile:
    def __init__(self, *, abort_after_hello: bool = False, abort_on_readline: bool = False) -> None:
        self.abort_after_hello = abort_after_hello
        self.abort_on_readline = abort_on_readline
        self.closed = False
        self._hello_returned = False

    def readline(self) -> bytes:
        if self.abort_on_readline:
            raise ConnectionAbortedError(10053, "software caused connection abort")
        self._hello_returned = True
        return b'{"type":"hello","role":"control","serial_port":"COM20"}\n'

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        if self.abort_after_hello and self._hello_returned:
            raise ConnectionAbortedError(10053, "software caused connection abort")
        raise StopIteration

    def close(self) -> None:
        self.closed = True


class _FakeRequest:
    def __init__(self, file_obj: _FakeFile) -> None:
        self._file_obj = file_obj
        self.sent: list[bytes] = []
        self.closed = False

    def makefile(self, _mode: str):
        return self._file_obj

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def shutdown(self, _how: int) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeSerialHandle:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeLockContext:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.exited = True


class SerialBrokerTest(unittest.TestCase):
    def test_is_benign_client_disconnect_accepts_expected_socket_abort_errors(self) -> None:
        self.assertTrue(broker._is_benign_client_disconnect(ConnectionAbortedError(10053, "aborted")))
        self.assertTrue(broker._is_benign_client_disconnect(ConnectionResetError(10054, "reset")))
        self.assertTrue(broker._is_benign_client_disconnect(BrokenPipeError()))
        self.assertFalse(broker._is_benign_client_disconnect(RuntimeError("boom")))

    def test_request_handler_swallows_benign_disconnect_after_hello(self) -> None:
        fake_file = _FakeFile(abort_after_hello=True)
        fake_request = _FakeRequest(fake_file)
        fake_broker = Mock()
        handler = broker._BrokerRequestHandler.__new__(broker._BrokerRequestHandler)
        handler.request = fake_request
        handler.server = SimpleNamespace(broker=fake_broker)

        handler.handle()

        fake_broker.add_client.assert_called_once()
        fake_broker.remove_client.assert_called_once()
        self.assertTrue(fake_request.closed)
        self.assertTrue(fake_file.closed)
        self.assertTrue(fake_request.sent)

    def test_request_handler_swallows_benign_disconnect_during_handshake(self) -> None:
        fake_file = _FakeFile(abort_on_readline=True)
        fake_request = _FakeRequest(fake_file)
        fake_broker = Mock()
        handler = broker._BrokerRequestHandler.__new__(broker._BrokerRequestHandler)
        handler.request = fake_request
        handler.server = SimpleNamespace(broker=fake_broker)

        handler.handle()

        fake_broker.add_client.assert_not_called()
        fake_broker.remove_client.assert_not_called()
        self.assertFalse(fake_request.closed)
        self.assertTrue(fake_file.closed)

    def test_serial_error_summary_normalizes_empty_and_multiline_messages(self) -> None:
        self.assertEqual(broker._serial_error_summary(RuntimeError("")), "RuntimeError")
        self.assertEqual(
            broker._serial_error_summary(RuntimeError("line1\nline2")),
            "line1 line2",
        )

    def test_write_reconnects_when_serial_handle_is_missing(self) -> None:
        fake_handle = _FakeSerialHandle()
        broker_instance = broker.SerialBroker(serial_port="COM19", baudrate=115200)
        broker_instance._serial_module = SimpleNamespace(Serial=Mock(return_value=fake_handle))
        with patch.object(broker_instance, "_broadcast_trace") as broadcast_mock:
            broker_instance.write(b"\n")

        self.assertEqual(fake_handle.writes, [b"\n"])
        self.assertTrue(
            any(
                call.args[0].direction == "sys" and call.args[0].payload == "SERIAL_CONNECTED baudrate=115200"
                for call in broadcast_mock.call_args_list
            )
        )

    def test_lost_link_is_reopened_with_backoff_without_waiting_for_a_write(self) -> None:
        reopened = _FakeSerialHandle()
        serial_ctor = Mock(side_effect=[FileNotFoundError(2, "port gone"), reopened])
        broker_instance = broker.SerialBroker(serial_port="COM19", baudrate=115200)
        broker_instance._serial_module = SimpleNamespace(Serial=serial_ctor)
        broker_instance._serial = _FakeSerialHandle()
        clock = {"now": 100.0}
        with (
            patch.object(broker.time, "monotonic", side_effect=lambda: clock["now"]),
            patch("autodbg.serial.broker._append_trace_entry", side_effect=_trace_entry),
            patch.object(broker_instance, "_broadcast_trace") as broadcast_mock,
        ):
            broker_instance._mark_serial_unavailable(OSError("device disconnected"))
            self.assertFalse(broker_instance._reconnect_when_due())  # right after the drop: give the device a moment
            clock["now"] += 1.0
            self.assertFalse(broker_instance._reconnect_when_due())  # first attempt: the port is still gone
            clock["now"] += 0.5
            self.assertFalse(broker_instance._reconnect_when_due())  # backing off
            serial_ctor_calls_while_backing_off = serial_ctor.call_count
            clock["now"] += 2.0
            self.assertTrue(broker_instance._reconnect_when_due())

        self.assertEqual(serial_ctor_calls_while_backing_off, 1)
        self.assertIs(broker_instance._serial, reopened)
        self.assertTrue(
            any(call.args[0].payload == "SERIAL_CONNECTED baudrate=115200" for call in broadcast_mock.call_args_list)
        )

    def test_a_link_that_keeps_dropping_right_after_reopening_backs_off_further(self) -> None:
        # e.g. a wedged USB-serial adapter: the port opens, then the first read fails
        broker_instance = broker.SerialBroker(serial_port="COM19", baudrate=115200)
        broker_instance._serial_module = SimpleNamespace(Serial=Mock(side_effect=lambda **_kwargs: _FakeSerialHandle()))
        broker_instance._serial = _FakeSerialHandle()
        clock = {"now": 100.0}
        pauses: list[float] = []
        with (
            patch.object(broker.time, "monotonic", side_effect=lambda: clock["now"]),
            patch("autodbg.serial.broker._append_trace_entry", side_effect=_trace_entry),
            patch.object(broker_instance, "_broadcast_trace"),
        ):
            for _ in range(5):
                broker_instance._mark_serial_unavailable(OSError("ClearCommError failed"))
                dropped_at = clock["now"]
                while not broker_instance._reconnect_when_due():
                    clock["now"] += 0.125
                pauses.append(round(clock["now"] - dropped_at, 1))
                clock["now"] += 0.25  # and it fails again at once
            clock["now"] += 60.0  # then it stays healthy for a while
            broker_instance._mark_serial_unavailable(OSError("device rebooted"))
            dropped_at = clock["now"]
            while not broker_instance._reconnect_when_due():
                clock["now"] += 0.125
            pause_after_healthy_link = round(clock["now"] - dropped_at, 1)

        self.assertEqual(pauses, [1.0, 2.0, 4.0, 8.0, 16.0])
        self.assertEqual(pause_after_healthy_link, 1.0)

    def test_reader_loop_resumes_reading_after_the_device_comes_back(self) -> None:
        class _FlakyHandle(_FakeSerialHandle):
            def readline(self) -> bytes:
                raise OSError("ClearCommError failed (device disconnected)")

        class _ReadableHandle(_FakeSerialHandle):
            def __init__(self) -> None:
                super().__init__()
                self.lines = [b"U-Boot 2026.10\r\n"]

            def readline(self) -> bytes:
                return self.lines.pop(0) if self.lines else b""

        reopened = _ReadableHandle()
        broker_instance = broker.SerialBroker(serial_port="COM19", baudrate=115200)
        broker_instance._serial_module = SimpleNamespace(Serial=Mock(return_value=reopened))
        broker_instance._serial = _FlakyHandle()
        received: list[str] = []

        def capture(entry) -> None:
            received.append(entry.payload)
            if entry.payload == "U-Boot 2026.10":
                broker_instance._stop_event.set()

        with (
            patch.object(broker, "_RECONNECT_MIN_DELAY", 0.01),
            patch.object(broker, "_RECONNECT_MAX_DELAY", 0.02),
            patch("autodbg.serial.broker._append_trace_entry", side_effect=_trace_entry),
            patch.object(broker_instance, "_broadcast_trace", side_effect=capture),
        ):
            reader = threading.Thread(target=broker_instance._serial_reader_loop, daemon=True)
            reader.start()
            reader.join(timeout=5.0)
            broker_instance._stop_event.set()

        self.assertFalse(reader.is_alive())
        self.assertEqual(
            [payload.split(" error=")[0] for payload in received],
            ["SERIAL_UNAVAILABLE", "SERIAL_CONNECTED baudrate=115200", "U-Boot 2026.10"],
        )

    def test_mark_serial_unavailable_closes_handle_and_broadcasts_status(self) -> None:
        fake_handle = _FakeSerialHandle()
        broker_instance = broker.SerialBroker(serial_port="COM19", baudrate=115200)
        broker_instance._serial = fake_handle
        with patch.object(broker_instance, "_broadcast_trace") as broadcast_mock:
            broker_instance._mark_serial_unavailable(PermissionError(13, "Access is denied"))

        self.assertTrue(fake_handle.closed)
        self.assertIsNone(broker_instance._serial)
        self.assertTrue(
            any(
                call.args[0].direction == "sys" and call.args[0].payload.startswith("SERIAL_UNAVAILABLE error=")
                for call in broadcast_mock.call_args_list
            )
        )

    def test_start_releases_lock_and_records_trace_when_open_fails(self) -> None:
        lock_context = _FakeLockContext()
        serial_error = PermissionError(13, "连接到系统上的设备没有发挥作用。")
        broker_instance = broker.SerialBroker(serial_port="COM4", baudrate=115200)
        with (
            patch("autodbg.serial.broker._import_serial", return_value=SimpleNamespace(Serial=Mock(side_effect=serial_error))),
            patch("autodbg.serial.broker._acquire_port_lock", return_value=lock_context),
            patch("autodbg.serial.broker._append_trace_entry") as append_mock,
        ):
            with self.assertRaises(broker.SerialBrokerStartError) as exc_context:
                broker_instance.start()

        self.assertTrue(lock_context.entered)
        self.assertTrue(lock_context.exited)
        self.assertIsNone(broker_instance._lock_context)
        self.assertIn("Failed to open COM4", str(exc_context.exception))
        self.assertTrue(
            any(
                call.args[0] == "COM4"
                and call.args[1] == "sys"
                and call.args[2].startswith("SERIAL_OPEN_FAILED error=")
                for call in append_mock.call_args_list
            )
        )

    def test_serial_open_recovery_hints_explain_windows_device_error(self) -> None:
        hints = broker.serial_open_recovery_hints(
            "COM4",
            "Cannot configure port. PermissionError(13, '连接到系统上的设备没有发挥作用。', None, 31)",
        )

        self.assertTrue(any("unplug/replug" in hint for hint in hints))
        self.assertTrue(any("observe-serial -SerialPort COM4" in hint for hint in hints))

    def test_device_not_functioning_is_not_blamed_on_another_program(self) -> None:
        # pyserial wraps WinError 31 in PermissionError(13, ...): it is a device fault, not a port held elsewhere
        hints = broker.serial_open_recovery_hints(
            "COM4",
            "Failed to open COM4: Cannot configure port, something went wrong. "
            "Original message: PermissionError(13, '连接到系统上的设备没有发挥作用。', None, 31)",
        )

        self.assertTrue(any("unplug/replug" in hint for hint in hints))
        self.assertFalse(any("Close other tools" in hint for hint in hints))

    def test_access_denied_points_at_the_program_holding_the_port(self) -> None:
        hints = broker.serial_open_recovery_hints(
            "COM4",
            "Failed to open COM4: could not open port 'COM4': PermissionError(13, '拒绝访问。', None, 5)",
        )

        self.assertTrue(any("Close other tools" in hint for hint in hints))
        self.assertFalse(any("Device Manager" in hint for hint in hints))

    def test_missing_port_points_at_device_manager(self) -> None:
        hints = broker.serial_open_recovery_hints(
            "COM4",
            "Failed to open COM4: could not open port 'COM4': FileNotFoundError(2, '系统找不到指定的文件。', None, 2)",
        )

        self.assertTrue(any("Device Manager" in hint for hint in hints))
        self.assertFalse(any("Close other tools" in hint for hint in hints))

    def test_port_held_by_another_autodbg_points_at_that_process(self) -> None:
        hints = broker.serial_open_recovery_hints(
            "COM4",
            "Serial port COM4 is already in use by another autodbg process (pid 5555 python.exe).",
        )

        self.assertTrue(any("already owns COM4" in hint for hint in hints))
        self.assertFalse(any("Device Manager" in hint for hint in hints))


if __name__ == "__main__":
    unittest.main()
