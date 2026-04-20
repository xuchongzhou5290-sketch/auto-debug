from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import signal
import socket
import tempfile
import threading
import time
from typing import Protocol

if os.name == "nt":  # pragma: no cover - exercised in Windows runtime
    import ctypes
    from ctypes import wintypes


class SerialPortProtocol(Protocol):
    timeout: float | None

    def readline(self) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class SerialSupportError(RuntimeError):
    """Raised when serial runtime support is unavailable."""


class SerialPortBusyError(RuntimeError):
    """Raised when this tool already holds the same serial port lock."""


@dataclass(slots=True)
class SerialTraceEntry:
    timestamp: str
    port: str
    direction: str
    payload: str
    pid: int

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "port": self.port,
            "direction": self.direction,
            "payload": self.payload,
            "pid": self.pid,
        }

    @classmethod
    def from_json_line(cls, raw: str) -> "SerialTraceEntry":
        payload = json.loads(raw)
        return cls(
            timestamp=str(payload["timestamp"]),
            port=str(payload["port"]),
            direction=str(payload["direction"]),
            payload=str(payload["payload"]),
            pid=int(payload["pid"]),
        )


@dataclass(slots=True)
class SerialBrokerRegistry:
    host: str
    tcp_port: int
    pid: int
    serial_port: str
    baudrate: int

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "tcp_port": self.tcp_port,
            "pid": self.pid,
            "serial_port": self.serial_port,
            "baudrate": self.baudrate,
        }

    @classmethod
    def from_path(cls, path: Path) -> "SerialBrokerRegistry | None":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        try:
            return cls(
                host=str(payload["host"]),
                tcp_port=int(payload["tcp_port"]),
                pid=int(payload["pid"]),
                serial_port=str(payload["serial_port"]),
                baudrate=int(payload["baudrate"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


class SerialTraceStreamClient:
    def __init__(
        self,
        *,
        broker_registry: SerialBrokerRegistry,
        serial_port: str,
        timeout: float | None = 0.5,
    ) -> None:
        connect_timeout = max(timeout or 0.2, 1.0)
        self._timeout = timeout
        self._socket = socket.create_connection((broker_registry.host, broker_registry.tcp_port), timeout=connect_timeout)
        self._socket.settimeout(0.5)
        self._queue: queue.Queue[SerialTraceEntry] = queue.Queue()
        self._closed = threading.Event()
        self._send_lock = threading.Lock()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._send_json({"type": "hello", "role": "trace", "serial_port": serial_port})
        self._reader.start()

    def read_entry(self, timeout: float | None = None) -> SerialTraceEntry | None:
        if self._closed.is_set():
            return None
        wait_timeout = self._timeout if timeout is None else timeout
        deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
        while not self._closed.is_set():
            try:
                if deadline is None:
                    return self._queue.get(timeout=0.2)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                return self._queue.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
        return None

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        self._reader.join(timeout=1.0)

    def _send_json(self, payload: dict[str, object]) -> None:
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._send_lock:
            self._socket.sendall(raw)

    def _reader_loop(self) -> None:
        buffer = b""
        while not self._closed.is_set():
            try:
                chunk = self._socket.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line:
                    continue
                try:
                    payload = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if payload.get("type") != "trace":
                    continue
                entry_payload = payload.get("entry")
                if not isinstance(entry_payload, dict):
                    continue
                try:
                    entry = SerialTraceEntry(
                        timestamp=str(entry_payload["timestamp"]),
                        port=str(entry_payload["port"]),
                        direction=str(entry_payload["direction"]),
                        payload=str(entry_payload["payload"]),
                        pid=int(entry_payload["pid"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                self._queue.put(entry)


def _import_serial():
    try:
        import serial  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on local runtime
        raise SerialSupportError(
            "pyserial is required for real serial access. Install it with: "
            "python -m pip install pyserial. If this project already has .venv, "
            "prefer using .\\.venv\\Scripts\\python -m autodbg ..."
        ) from exc
    return serial


def list_serial_ports() -> list[dict[str, str]]:
    _import_serial()
    try:
        from serial.tools import list_ports  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on local runtime
        raise SerialSupportError("pyserial list_ports support is unavailable.") from exc
    ports = []
    for port in list_ports.comports():
        ports.append(
            {
                "device": str(port.device),
                "description": str(port.description),
                "hwid": str(port.hwid),
            }
        )
    return ports


@contextmanager
def open_serial_port(port: str, baudrate: int, timeout: float = 0.2):
    broker_registry = load_serial_broker_registry(port)
    if broker_registry is not None:
        with _acquire_control_lock(port):
            handle = _BrokerSerialPort(
                broker_registry=broker_registry,
                serial_port=port,
                timeout=timeout,
            )
            try:
                yield handle
            finally:
                handle.close()
        return
    with _open_direct_serial_port(port, baudrate, timeout) as handle:
        yield handle


@contextmanager
def _open_direct_serial_port(port: str, baudrate: int, timeout: float = 0.2):
    serial = _import_serial()
    with _acquire_port_lock(port):
        handle = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
        traced_handle = _TracedSerialPort(handle, port=port, baudrate=baudrate)
        _append_trace_entry(port, "sys", f"OPEN baudrate={baudrate}")
        try:
            yield traced_handle
        finally:
            _append_trace_entry(port, "sys", "CLOSE")
            handle.close()


@contextmanager
def _acquire_port_lock(port: str):
    lock_path = _serial_lock_path(port)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
        yield
    except FileExistsError as exc:
        if _remove_stale_lock(lock_path):
            with _acquire_port_lock(port):
                yield
            return
        raise SerialPortBusyError(
            f"Serial port {port} is already in use by another autodbg process."
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass


def _serial_lock_dir() -> Path:
    return Path(tempfile.gettempdir()) / "autodbg-serial-locks"


def _serial_lock_path(port: str) -> Path:
    return _serial_lock_dir() / f"{port.lower()}.lock"


@contextmanager
def _acquire_control_lock(port: str):
    lock_path = _serial_control_lock_path(port)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
        yield
    except FileExistsError as exc:
        if _remove_stale_lock(lock_path):
            with _acquire_control_lock(port):
                yield
            return
        raise SerialPortBusyError(
            f"Serial port {port} is already in use by another autodbg control session."
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass


def _serial_control_lock_dir() -> Path:
    return Path(tempfile.gettempdir()) / "autodbg-serial-control-locks"


def _serial_control_lock_path(port: str) -> Path:
    return _serial_control_lock_dir() / f"{port.lower()}.lock"


def serial_trace_log_path(port: str) -> Path:
    return _serial_trace_dir() / f"{_normalize_port_name(port)}.jsonl"


def serial_broker_registry_path(port: str) -> Path:
    return _serial_broker_dir() / f"{_normalize_port_name(port)}.json"


def write_serial_broker_registry(port: str, *, host: str, tcp_port: int, baudrate: int) -> Path:
    registry = SerialBrokerRegistry(
        host=host,
        tcp_port=tcp_port,
        pid=os.getpid(),
        serial_port=port,
        baudrate=baudrate,
    )
    path = serial_broker_registry_path(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return path


def load_serial_broker_registry(port: str) -> SerialBrokerRegistry | None:
    path = serial_broker_registry_path(port)
    registry = SerialBrokerRegistry.from_path(path)
    if registry is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None
    if not _pid_is_running(registry.pid):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None
    return registry


def remove_serial_broker_registry(port: str) -> None:
    try:
        serial_broker_registry_path(port).unlink()
    except FileNotFoundError:
        pass


def list_serial_broker_registries(*, serial_port: str | None = None) -> list[SerialBrokerRegistry]:
    broker_dir = _serial_broker_dir()
    if not broker_dir.exists():
        return []

    registries: list[SerialBrokerRegistry] = []
    normalized_filter = _normalize_port_name(serial_port) if serial_port else None
    for path in sorted(broker_dir.glob("*.json")):
        registry = SerialBrokerRegistry.from_path(path)
        if registry is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        if not _pid_is_running(registry.pid):
            _cleanup_serial_broker_artifacts(registry.serial_port)
            continue
        if normalized_filter and _normalize_port_name(registry.serial_port) != normalized_filter:
            continue
        registries.append(registry)
    return registries


def stop_serial_broker(port: str, *, wait_timeout: float = 2.0) -> SerialBrokerRegistry | None:
    registry = load_serial_broker_registry(port)
    if registry is None:
        _cleanup_serial_broker_artifacts(port)
        return None

    if not _pid_is_running(registry.pid):
        _cleanup_serial_broker_artifacts(port)
        return registry

    _terminate_pid(registry.pid)
    deadline = time.monotonic() + max(wait_timeout, 0.1)
    while time.monotonic() < deadline:
        if not _pid_is_running(registry.pid):
            _cleanup_serial_broker_artifacts(port)
            return registry
        time.sleep(0.05)

    if _pid_is_running(registry.pid):
        raise RuntimeError(f"Timed out while stopping raw serial broker pid {registry.pid} on {port}.")

    _cleanup_serial_broker_artifacts(port)
    return registry


def _cleanup_serial_broker_artifacts(port: str) -> None:
    remove_serial_broker_registry(port)
    _remove_stale_lock(_serial_lock_path(port))
    _remove_stale_lock(_serial_control_lock_path(port))


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    os.kill(pid, signal.SIGTERM)


def _remove_stale_lock(lock_path: Path) -> bool:
    pid = _read_lock_pid(lock_path)
    if pid is not None and _pid_is_running(pid):
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return True
    return True


def _read_lock_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, wintypes.DWORD(pid))
        if process:
            ctypes.windll.kernel32.CloseHandle(process)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _serial_trace_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "autodbg-serial-trace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _serial_broker_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "autodbg-serial-brokers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_port_name(port: str) -> str:
    return port.lower().replace(":", "_").replace("\\", "_").replace("/", "_")


def _append_trace_entry(port: str, direction: str, payload: str) -> SerialTraceEntry:
    entry = SerialTraceEntry(
        timestamp=datetime.now().isoformat(timespec="milliseconds"),
        port=port,
        direction=direction,
        payload=payload,
        pid=os.getpid(),
    )
    log_path = serial_trace_log_path(port)
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
    return entry


class _TracedSerialPort:
    def __init__(self, inner, *, port: str, baudrate: int) -> None:
        self._inner = inner
        self._port = port
        self._baudrate = baudrate

    @property
    def timeout(self) -> float | None:
        return self._inner.timeout

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        self._inner.timeout = value

    def readline(self) -> bytes:
        raw = self._inner.readline()
        if raw:
            _append_trace_entry(self._port, "rx", raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        return raw

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", errors="replace")
        chunks = text.splitlines()
        if not chunks:
            chunks = [text]
        for chunk in chunks:
            _append_trace_entry(self._port, "tx", chunk.rstrip("\r\n"))
        return self._inner.write(data)

    def flush(self) -> None:
        self._inner.flush()

    def close(self) -> None:
        self._inner.close()


class _BrokerSerialPort:
    def __init__(
        self,
        *,
        broker_registry: SerialBrokerRegistry,
        serial_port: str,
        timeout: float | None,
    ) -> None:
        self._serial_port = serial_port
        self._timeout = timeout
        connect_timeout = max(timeout or 0.2, 1.0)
        self._socket = socket.create_connection((broker_registry.host, broker_registry.tcp_port), timeout=connect_timeout)
        self._socket.settimeout(0.5)
        self._queue: queue.Queue[bytes] = queue.Queue()
        self._closed = threading.Event()
        self._send_lock = threading.Lock()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._send_json({"type": "hello", "role": "control", "serial_port": serial_port})
        self._reader.start()

    @property
    def timeout(self) -> float | None:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        self._timeout = value

    def readline(self) -> bytes:
        if self._closed.is_set():
            return b""
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while not self._closed.is_set():
            try:
                if deadline is None:
                    return self._queue.get(timeout=0.2)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                return self._queue.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
        return b""

    def write(self, data: bytes) -> int:
        self._send_json(
            {
                "type": "write",
                "data_b64": base64.b64encode(data).decode("ascii"),
            }
        )
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        self._reader.join(timeout=1.0)

    def _send_json(self, payload: dict[str, object]) -> None:
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._send_lock:
            self._socket.sendall(raw)

    def _reader_loop(self) -> None:
        buffer = b""
        while not self._closed.is_set():
            try:
                chunk = self._socket.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line:
                    continue
                try:
                    payload = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "rx":
                    encoded = payload.get("data_b64", "")
                    if not isinstance(encoded, str):
                        continue
                    try:
                        self._queue.put(base64.b64decode(encoded))
                    except Exception:
                        continue
