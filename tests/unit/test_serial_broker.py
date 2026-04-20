from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from autodbg.serial import broker


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


if __name__ == "__main__":
    unittest.main()
