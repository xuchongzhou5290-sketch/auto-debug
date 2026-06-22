from __future__ import annotations

import base64
import json
import socketserver
import sys
import threading
import time
from typing import Any

from autodbg.serial.runtime import (
    SerialTraceEntry,
    _acquire_port_lock,
    _append_trace_entry,
    _import_serial,
    remove_serial_broker_registry,
    write_serial_broker_registry,
)


class SerialBrokerStartError(RuntimeError):
    """Raised when a raw serial broker cannot open the physical COM port."""


class SerialBroker:
    def __init__(
        self,
        *,
        serial_port: str,
        baudrate: int,
        timeout: float = 0.2,
        host: str = "127.0.0.1",
        tcp_port: int = 0,
        owner: str | None = None,
        protected: bool = False,
    ) -> None:
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.timeout = timeout
        self.host = host
        self.tcp_port = tcp_port
        self.owner = owner
        self.protected = protected
        self._serial_module = None
        self._serial = None
        self._lock_context = None
        self._serial_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._clients_lock = threading.Lock()
        self._clients: set[_BrokerConnection] = set()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self._serial_module = _import_serial()
        self._lock_context = _acquire_port_lock(self.serial_port)
        self._lock_context.__enter__()
        try:
            self._serial = self._open_serial_handle()
        except Exception as exc:
            summary = _serial_error_summary(exc)
            _append_trace_entry(self.serial_port, "sys", f"SERIAL_OPEN_FAILED error={summary}")
            if self._lock_context is not None:
                self._lock_context.__exit__(type(exc), exc, exc.__traceback__)
                self._lock_context = None
            raise SerialBrokerStartError(f"Failed to open {self.serial_port}: {summary}") from exc
        self._broadcast_trace(_append_trace_entry(self.serial_port, "sys", f"BROKER_OPEN baudrate={self.baudrate}"))
        self._broadcast_trace(_append_trace_entry(self.serial_port, "sys", f"SERIAL_CONNECTED baudrate={self.baudrate}"))

        class _ThreadingServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def handle_error(self, request, client_address) -> None:  # type: ignore[override]
                exc = sys.exc_info()[1]
                if exc is not None and _is_benign_client_disconnect(exc):
                    return
                super().handle_error(request, client_address)

        self._server = _ThreadingServer((self.host, self.tcp_port), _BrokerRequestHandler)
        self._server.broker = self  # type: ignore[attr-defined]
        self.host = str(self._server.server_address[0])
        self.tcp_port = int(self._server.server_address[1])
        write_serial_broker_registry(
            self.serial_port,
            host=self.host,
            tcp_port=self.tcp_port,
            baudrate=self.baudrate,
            owner=self.owner,
            protected=self.protected,
        )

        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._reader_thread = threading.Thread(target=self._serial_reader_loop, daemon=True)
        self._server_thread.start()
        self._reader_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        remove_serial_broker_registry(self.serial_port)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=1.0)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.close()
        if self._serial is not None:
            self._broadcast_trace(_append_trace_entry(self.serial_port, "sys", "BROKER_CLOSE"))
            self._serial.close()
            self._serial = None
        if self._lock_context is not None:
            self._lock_context.__exit__(None, None, None)
            self._lock_context = None

    def add_client(self, connection: "_BrokerConnection") -> None:
        with self._clients_lock:
            self._clients.add(connection)

    def remove_client(self, connection: "_BrokerConnection") -> None:
        with self._clients_lock:
            self._clients.discard(connection)

    def write(self, data: bytes) -> None:
        serial_handle = self._ensure_serial_connection()
        text = data.decode("utf-8", errors="replace")
        chunks = text.splitlines()
        if not chunks:
            chunks = [text]
        for chunk in chunks:
            self._broadcast_trace(_append_trace_entry(self.serial_port, "tx", chunk.rstrip("\r\n")))
        with self._write_lock:
            try:
                serial_handle.write(data)
                serial_handle.flush()
            except Exception as exc:
                self._mark_serial_unavailable(exc)
                raise RuntimeError(f"Serial port {self.serial_port} is unavailable: {_serial_error_summary(exc)}") from exc

    def _serial_reader_loop(self) -> None:
        while not self._stop_event.is_set():
            serial_handle = self._get_serial_handle()
            if serial_handle is None:
                time.sleep(0.1)
                continue
            try:
                raw = serial_handle.readline()
            except Exception as exc:
                self._mark_serial_unavailable(exc)
                continue
            if not raw:
                continue
            entry = _append_trace_entry(
                self.serial_port,
                "rx",
                raw.decode("utf-8", errors="replace").rstrip("\r\n"),
            )
            self._broadcast_trace(entry)
            payload = {
                "type": "rx",
                "data_b64": base64.b64encode(raw).decode("ascii"),
            }
            with self._clients_lock:
                clients = list(self._clients)
            for client in clients:
                client.send(payload)

    def _broadcast_trace(self, entry: SerialTraceEntry) -> None:
        payload = {
            "type": "trace",
            "entry": entry.to_dict(),
        }
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            client.send(payload)

    def _open_serial_handle(self):
        if self._serial_module is None:
            self._serial_module = _import_serial()
        return self._serial_module.Serial(
            port=self.serial_port,
            baudrate=self.baudrate,
            timeout=self.timeout,
        )

    def _get_serial_handle(self):
        with self._serial_lock:
            return self._serial

    def _ensure_serial_connection(self):
        serial_handle = self._get_serial_handle()
        if serial_handle is not None:
            return serial_handle
        with self._serial_lock:
            if self._serial is not None:
                return self._serial
            try:
                self._serial = self._open_serial_handle()
            except Exception as exc:
                raise RuntimeError(f"Failed to reconnect {self.serial_port}: {_serial_error_summary(exc)}") from exc
        self._broadcast_trace(_append_trace_entry(self.serial_port, "sys", f"SERIAL_CONNECTED baudrate={self.baudrate}"))
        return self._serial

    def _mark_serial_unavailable(self, exc: BaseException) -> None:
        serial_handle = None
        with self._serial_lock:
            if self._serial is None:
                return
            serial_handle = self._serial
            self._serial = None
        try:
            if serial_handle is not None:
                serial_handle.close()
        except Exception:
            pass
        self._broadcast_trace(
            _append_trace_entry(
                self.serial_port,
                "sys",
                f"SERIAL_UNAVAILABLE error={_serial_error_summary(exc)}",
            )
        )


class _BrokerConnection:
    def __init__(self, request) -> None:
        self._request = request
        self._send_lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._send_lock:
            try:
                self._request.sendall(raw)
            except OSError:
                pass

    def close(self) -> None:
        try:
            self._request.shutdown(2)
        except OSError:
            pass
        try:
            self._request.close()
        except OSError:
            pass

    def __hash__(self) -> int:
        return id(self)


class _BrokerRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        broker: SerialBroker = self.server.broker  # type: ignore[attr-defined]
        file_obj = self.request.makefile("rb")
        connection: _BrokerConnection | None = None
        try:
            try:
                first_line = file_obj.readline()
            except Exception as exc:
                if _is_benign_client_disconnect(exc):
                    return
                raise
            if not first_line:
                return
            try:
                hello = json.loads(first_line.decode("utf-8"))
            except json.JSONDecodeError:
                return
            if hello.get("type") != "hello":
                return
            connection = _BrokerConnection(self.request)
            broker.add_client(connection)
            connection.send({"type": "hello", "ok": True})
            try:
                for raw_line in file_obj:
                    if not raw_line:
                        break
                    try:
                        payload = json.loads(raw_line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") == "write":
                        encoded = payload.get("data_b64", "")
                        if not isinstance(encoded, str):
                            continue
                        try:
                            broker.write(base64.b64decode(encoded))
                        except Exception:
                            continue
            except Exception as exc:
                if not _is_benign_client_disconnect(exc):
                    raise
        finally:
            if connection is not None:
                broker.remove_client(connection)
                connection.close()
            try:
                file_obj.close()
            except OSError:
                pass


def _is_benign_client_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)):
        return True
    if not isinstance(exc, OSError):
        return False
    winerror = getattr(exc, "winerror", None)
    errno = getattr(exc, "errno", None)
    return winerror in {64, 995, 10053, 10054, 10058} or errno in {54, 104, 107}


def _serial_error_summary(exc: BaseException) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return message.replace("\r", " ").replace("\n", " ")


def serial_open_recovery_hints(serial_port: str, exc: BaseException | str) -> list[str]:
    summary = _serial_error_summary(exc) if isinstance(exc, BaseException) else str(exc)
    summary_lower = summary.lower()
    hints: list[str] = []
    if (
        "permissionerror" in summary_lower
        or "access is denied" in summary_lower
        or "permission denied" in summary_lower
        or "拒绝访问" in summary
        or "权限" in summary
    ):
        hints.append(f"Close other tools or stale observe-serial windows that may still hold {serial_port}.")
    if (
        "cannot configure port" in summary_lower
        or "winerror 31" in summary_lower
        or ", 31)" in summary_lower
        or "没有发挥作用" in summary
        or "device attached to the system is not functioning" in summary_lower
    ):
        hints.append(
            f"Windows reported the serial device is not functioning; unplug/replug the USB serial adapter for {serial_port} "
            "or disable/enable it in Device Manager."
        )
    if "could not open port" in summary_lower or "file not found" in summary_lower or "找不到" in summary:
        hints.append(f"Confirm {serial_port} still exists in Device Manager and retry with the actual COM number.")
    if not hints:
        hints.append(f"Confirm {serial_port} exists, is not held by another program, then retry observe-serial.")
    hints.append(f"After the device is healthy, rerun: observe-serial -SerialPort {serial_port}")
    return hints
