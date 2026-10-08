from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import random
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
from typing import Protocol

from autodbg.utils.process import PID_UNKNOWN, pid_is_running, pid_state


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


class SerialBrokerProtectedError(RuntimeError):
    """Raised when a protected human observer broker would be stopped."""


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
    owner: str | None = None
    protected: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "tcp_port": self.tcp_port,
            "pid": self.pid,
            "serial_port": self.serial_port,
            "baudrate": self.baudrate,
            "owner": self.owner,
            "protected": self.protected,
        }

    @classmethod
    def from_path(cls, path: Path) -> "SerialBrokerRegistry | None":
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):  # ValueError: not UTF-8
            return None
        return cls.from_text(text)

    @classmethod
    def from_text(cls, text: str) -> "SerialBrokerRegistry | None":
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                host=str(payload["host"]),
                tcp_port=int(payload["tcp_port"]),
                pid=int(payload["pid"]),
                serial_port=str(payload["serial_port"]),
                baudrate=int(payload["baudrate"]),
                owner=str(payload["owner"]) if payload.get("owner") else None,
                protected=bool(payload.get("protected", False)),
            )
        except (KeyError, TypeError, ValueError, OverflowError):  # OverflowError: int(1e400)
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
def open_serial_port(port: str, baudrate: int, timeout: float = 0.2, *, broker_first: bool = True):
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
    if broker_first:
        broker_registry = ensure_observe_serial_broker(port, baudrate=baudrate)
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


def _start_local_serial_broker(port: str, *, baudrate: int, timeout: float):
    from autodbg.serial.broker import SerialBroker

    broker = SerialBroker(
        serial_port=port,
        baudrate=baudrate,
        timeout=timeout,
    )
    broker.start()
    return broker


def ensure_observe_serial_broker(
    port: str,
    *,
    baudrate: int,
    wait_timeout: float = 8.0,
) -> SerialBrokerRegistry:
    registry = load_serial_broker_registry(port)
    if is_observe_serial_broker(registry):
        return registry

    _launch_observe_serial_window(port, baudrate=baudrate)
    deadline = time.monotonic() + max(wait_timeout, 0.1)
    last_registry: SerialBrokerRegistry | None = registry
    while time.monotonic() < deadline:
        registry = load_serial_broker_registry(port)
        if registry is not None:
            last_registry = registry
            if is_observe_serial_broker(registry):
                return registry
        time.sleep(0.1)

    if last_registry is not None:
        return protect_serial_broker_registry(port) or last_registry
    raise RuntimeError(
        f"observe-serial did not start a protected broker for {port}. "
        "Open observe-serial manually and retry."
    )


def is_observe_serial_broker(registry: SerialBrokerRegistry | None) -> bool:
    return bool(registry and registry.owner == "human-observe" and registry.protected)


def protect_serial_broker_registry(
    port: str,
    *,
    owner: str = "human-observe",
) -> SerialBrokerRegistry | None:
    registry = load_serial_broker_registry(port)
    if registry is None:
        return None
    path = write_serial_broker_registry(
        registry.serial_port,
        host=registry.host,
        tcp_port=registry.tcp_port,
        baudrate=registry.baudrate,
        owner=owner,
        protected=True,
        pid=registry.pid,
    )
    # tell the broker too: its registry self-heal rewrites from what it holds in memory and would otherwise roll the
    # upgrade back if the file were deleted before it next looked at it (older brokers ignore the message)
    _notify_broker_protection(registry, owner)
    return SerialBrokerRegistry.from_path(path)


def _notify_broker_protection(registry: SerialBrokerRegistry, owner: str) -> None:
    try:
        with socket.create_connection((registry.host, int(registry.tcp_port)), timeout=_BROKER_PROBE_TIMEOUT) as sock:
            sock.sendall(
                json.dumps({"type": "hello", "role": "protect"}).encode("utf-8")
                + b"\n"
                + json.dumps({"type": "protect", "owner": owner}).encode("utf-8")
                + b"\n"
            )
            sock.settimeout(_BROKER_PROBE_TIMEOUT)
            sock.recv(4096)  # the hello reply: the broker has read our lines before we close
    except (OSError, ValueError, OverflowError):
        pass


def _launch_observe_serial_window(port: str, *, baudrate: int) -> None:
    if os.name != "nt":
        raise RuntimeError("Auto-launching observe-serial is currently supported only on Windows.")
    project_root = _default_project_root()
    script_path = project_root / "observe-serial.ps1"
    if not script_path.exists():
        raise RuntimeError(f"observe-serial.ps1 was not found under project root: {project_root}")
    env = os.environ.copy()
    env["AUTO_DBG_PROJECT_ROOT"] = str(project_root)
    env["AUTO_DBG_SERIAL_PORT"] = port
    env["AUTO_DBG_SERIAL_BAUDRATE"] = str(baudrate)
    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-NoUi",
        "-SerialPort",
        port,
        "-Baudrate",
        str(baudrate),
        "-Tail",
        "0",
    ]
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(args, cwd=str(project_root), env=env, creationflags=creationflags)


def _default_project_root() -> Path:
    for env_name in ("AUTO_DBG_PROJECT_ROOT", "AUTO_DBG_HOME"):
        raw = os.getenv(env_name)
        if raw:
            return Path(raw).expanduser().resolve()
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "observe-serial.ps1").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    module_path = Path(__file__).resolve()
    for candidate in module_path.parents:
        if (candidate / "observe-serial.ps1").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return cwd


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
    with _acquire_pid_lock(
        lock_path,
        busy_message=f"Serial port {port} is already in use by another autodbg process.",
    ):
        yield


def _serial_lock_dir() -> Path:
    return Path(tempfile.gettempdir()) / "autodbg-serial-locks"


def _serial_lock_path(port: str) -> Path:
    # normalized like the registry: "\\.\COM7" or "/dev/ttyUSB0" used to escape the lock directory
    return _serial_lock_dir() / f"{_windows_safe_name(_normalize_port_name(port))}.lock"


@contextmanager
def _acquire_control_lock(port: str):
    lock_path = _serial_control_lock_path(port)
    with _acquire_pid_lock(
        lock_path,
        busy_message=f"Serial port {port} is already in use by another autodbg control session.",
    ):
        yield


@contextmanager
def _acquire_pid_lock(lock_path: Path, *, busy_message: str):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    for attempt in range(3):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            break
        except FileExistsError as exc:
            if not _remove_stale_lock(lock_path):
                raise SerialPortBusyError(busy_message) from exc
            time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
    else:
        raise SerialPortBusyError(busy_message)

    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            _release_pid_lock(lock_path)


def _serial_control_lock_dir() -> Path:
    return Path(tempfile.gettempdir()) / "autodbg-serial-control-locks"


def _serial_control_lock_path(port: str) -> Path:
    return _serial_control_lock_dir() / f"{_windows_safe_name(_normalize_port_name(port))}.lock"


def serial_trace_log_path(port: str) -> Path:
    return serial_trace_log_dir(port) / "trace.jsonl"


def serial_trace_log_dir(port: str) -> Path:
    path = _serial_trace_dir() / _windows_safe_name(_serial_log_port_dir_name(port))
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_serial_trace_marker(port: str, marker: str) -> SerialTraceEntry:
    return _append_trace_entry(port, "sys", marker)


def serial_broker_registry_path(port: str) -> Path:
    return _serial_broker_dir() / f"{_windows_safe_name(_normalize_port_name(port))}.json"


def write_serial_broker_registry(
    port: str,
    *,
    host: str,
    tcp_port: int,
    baudrate: int,
    owner: str | None = None,
    protected: bool = False,
    pid: int | None = None,
) -> Path:
    registry = SerialBrokerRegistry(
        host=host,
        tcp_port=tcp_port,
        pid=os.getpid() if pid is None else pid,
        serial_port=port,
        baudrate=baudrate,
        owner=owner,
        protected=protected,
    )
    path = serial_broker_registry_path(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(path, json.dumps(registry.to_dict(), ensure_ascii=False) + "\n")
    return path


def load_serial_broker_registry(port: str) -> SerialBrokerRegistry | None:
    path = serial_broker_registry_path(port)
    status, registry = _read_serial_broker_registry(path)
    if status == "invalid":
        _unlink_registry_if_unchanged(path, None)
        return None
    if registry is None:
        # missing, or unreadable right now (another process holding the file): never delete on that basis
        return None
    if _serial_broker_is_gone(registry):
        _unlink_registry_if_unchanged(path, registry)
        return None
    return registry


def remove_serial_broker_registry(port: str, *, owner_pid: int | None = None) -> None:
    """Delete the registry for port; with owner_pid, only when it still belongs to that pid.

    A broker instance that failed to start (the port was already held by another broker) must not remove the
    other broker's registry, so SerialBroker.stop() passes its own pid.
    """
    path = serial_broker_registry_path(port)
    if owner_pid is not None:
        status, registry = _read_serial_broker_registry(path)
        if registry is None or registry.pid != owner_pid:
            return
        _unlink_registry_if_unchanged(path, registry)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def list_serial_broker_registries(*, serial_port: str | None = None) -> list[SerialBrokerRegistry]:
    broker_dir = _serial_broker_dir()
    if not broker_dir.exists():
        return []

    registries: list[SerialBrokerRegistry] = []
    normalized_filter = _normalize_port_name(serial_port) if serial_port else None
    for path in sorted(broker_dir.glob("*.json")):
        status, registry = _read_serial_broker_registry(path)
        if status == "invalid":
            _unlink_registry_if_unchanged(path, None)
            continue
        if registry is None:
            continue
        if _serial_broker_is_gone(registry):
            _cleanup_serial_broker_artifacts(registry.serial_port, expected=registry)
            continue
        if normalized_filter and _normalize_port_name(registry.serial_port) != normalized_filter:
            continue
        registries.append(registry)
    return registries


_REGISTRY_READ_ATTEMPTS = 3
_REGISTRY_READ_RETRY_DELAY = 0.05
_BROKER_PROBE_TIMEOUT = 0.5
_REGISTRY_REPLACE_ATTEMPTS = 20


def _read_serial_broker_registry(path: Path) -> tuple[str, SerialBrokerRegistry | None]:
    """Return ("ok", registry), ("missing", None), ("unreadable", None) or ("invalid", None).

    Retries briefly before giving up: the file may be held by another process for a moment (antivirus, a reader
    on Windows) or be mid-rewrite by an older autodbg that still writes in place, so one failed read or one
    truncated JSON document is not proof that the registry is broken.
    """
    status = "missing"
    for attempt in range(_REGISTRY_READ_ATTEMPTS):
        if attempt:
            time.sleep(_REGISTRY_READ_RETRY_DELAY)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "missing", None
        except OSError:
            status = "unreadable"
            continue
        except ValueError:  # not UTF-8
            status = "invalid"
            continue
        registry = SerialBrokerRegistry.from_text(text)
        if registry is not None:
            return "ok", registry
        status = "invalid"
    return status, None


def _serial_broker_is_gone(registry: SerialBrokerRegistry) -> bool:
    """True only when the broker is definitely gone.

    A dead pid (no such process / exited) is conclusive. When the pid exists but may not be inspected
    (PID_UNKNOWN, e.g. access denied from a sandboxed caller) the broker itself is asked, and only an answer that
    identifies this port's broker counts: another port's broker may have taken over a dead registry's TCP port.
    """
    if not _pid_is_running(registry.pid):
        return True
    if PID_UNKNOWN != _pid_state(registry.pid):
        return False
    return not _probe_serial_broker(registry)


_BROKER_MATCH = "match"  # answered and identified itself as this registry's broker
_BROKER_MISMATCH = "mismatch"  # answered as another port's broker or another pid
_BROKER_BARE = "bare"  # older broker: bare hello without identity
_BROKER_SILENT = "silent"  # nothing answered like a broker


def _broker_identity(registry: SerialBrokerRegistry, *, timeout: float = _BROKER_PROBE_TIMEOUT) -> str:
    reply = _broker_hello(registry.host, registry.tcp_port, timeout=timeout)
    if reply is None:
        return _BROKER_SILENT
    if "serial_port" not in reply and "pid" not in reply:
        return _BROKER_BARE
    if _normalize_port_name(str(reply.get("serial_port", ""))) != _normalize_port_name(registry.serial_port):
        return _BROKER_MISMATCH
    if "pid" in reply and reply.get("pid") != registry.pid:
        return _BROKER_MISMATCH
    return _BROKER_MATCH


def _probe_serial_broker(registry: SerialBrokerRegistry, *, timeout: float = _BROKER_PROBE_TIMEOUT) -> bool:
    """For a pid we may not inspect: is this registry's broker still there?"""
    identity = _broker_identity(registry, timeout=timeout)
    if _BROKER_MATCH == identity:
        return True
    if _BROKER_MISMATCH == identity:
        return False
    # an older broker (bare hello) or no answer at all (e.g. loopback blocked in a sandbox): fall back to the port
    # lock, which a broker holds for its whole life. Better to keep an unreachable entry than to drop a live one.
    return _read_lock_pid(_serial_lock_path(registry.serial_port)) == registry.pid


def _broker_hello(host: str, tcp_port: int, *, timeout: float) -> dict | None:
    """Send the broker handshake and return its hello reply, or None if nothing answers like a broker."""
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((host, int(tcp_port)), timeout=timeout) as sock:
            sock.sendall(json.dumps({"type": "hello", "role": "probe"}).encode("utf-8") + b"\n")
            buffer = b""
            while len(buffer) < 65536:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                chunk = sock.recv(4096)
                if not chunk:
                    return None
                buffer += chunk
                # the broker may broadcast trace lines before the hello reply
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    try:
                        payload = json.loads(line.decode("utf-8", errors="replace"))
                    except (ValueError, RecursionError):
                        continue
                    if isinstance(payload, dict) and payload.get("type") == "hello" and payload.get("ok"):
                        return payload
    except (OSError, ValueError, OverflowError):
        return None
    return None


def _unlink_registry_if_unchanged(path: Path, expected: SerialBrokerRegistry | None) -> None:
    """Delete path unless it now holds a different, valid registry (a new broker may have just registered)."""
    status, current = _read_serial_broker_registry(path)
    if status == "missing":
        return
    if status == "unreadable":
        return
    if expected is None:
        if status != "invalid":
            return
    elif current != expected:
        return
    for _ in range(_REGISTRY_REPLACE_ATTEMPTS):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            # Windows refuses to delete a file a reader has open for a moment; retry briefly
            time.sleep(random.uniform(0.01, 0.05))
        except OSError:
            return
    # still held open: leave it; a dead broker's entry is cleaned up by the next load/list


def _write_text_atomic(path: Path, text: str) -> None:
    """Write a registry so readers never see it half-written: temp file in the same directory, then os.replace."""
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for _ in range(_REGISTRY_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                # Windows refuses to replace a file another process has open; retry briefly
                time.sleep(random.uniform(0.01, 0.05))
        # still blocked: fall back to an in-place write (readers retry on a truncated document)
        path.write_text(text, encoding="utf-8", newline="\n")
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def stop_serial_broker(
    port: str,
    *,
    wait_timeout: float = 2.0,
    allow_protected: bool = False,
) -> SerialBrokerRegistry | None:
    registry = load_serial_broker_registry(port)
    if registry is None:
        _cleanup_serial_broker_artifacts(port)
        return None

    if not _pid_is_running(registry.pid):
        # the broker exited between load() and here
        _cleanup_serial_broker_artifacts(port, expected=registry)
        return registry

    if registry.protected and not allow_protected:
        owner = f" owner={registry.owner}" if registry.owner else ""
        raise SerialBrokerProtectedError(
            f"Refusing to stop protected raw serial broker on {registry.serial_port}"
            f" (pid {registry.pid}{owner}). Use --force only after confirming the human observer can be disconnected."
        )

    if _BROKER_MISMATCH == _broker_identity(registry):
        # the registered pid is alive but its TCP port answers as another broker: the entry is stale (pid reused),
        # so clean it up instead of killing whatever process now owns that pid
        _cleanup_serial_broker_artifacts(port, expected=registry)
        return registry

    _terminate_pid(registry.pid)
    deadline = time.monotonic() + max(wait_timeout, 0.1)
    while time.monotonic() < deadline:
        if not _pid_is_running(registry.pid):
            _cleanup_serial_broker_artifacts(port, expected=registry)
            return registry
        time.sleep(0.05)

    if _pid_is_running(registry.pid):
        raise RuntimeError(f"Timed out while stopping raw serial broker pid {registry.pid} on {port}.")

    _cleanup_serial_broker_artifacts(port, expected=registry)
    return registry


def _cleanup_serial_broker_artifacts(port: str, *, expected: SerialBrokerRegistry | None = None) -> None:
    """Remove the registry only if it is still the dead broker's (expected) or is corrupt, plus stale locks."""
    _unlink_registry_if_unchanged(serial_broker_registry_path(port), expected)
    _remove_stale_lock(_serial_lock_path(port))
    _remove_stale_lock(_serial_control_lock_path(port))


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    # On Windows, SIGTERM is implemented by Python with TerminateProcess.
    # The serial broker is a helper process and forceful termination is intentional here.
    os.kill(pid, signal.SIGTERM)


def _remove_stale_lock(lock_path: Path) -> bool:
    pid = _read_lock_pid(lock_path)
    if pid is not None and _pid_is_running(pid):
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        # still held open (a just-killed process whose handle is not closed yet, or a new owner that just took it):
        # not ours to remove now; the next one to acquire the lock reclaims it if it really is stale
        return False
    return True


def _read_lock_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_is_running(pid: int) -> bool:
    return pid_is_running(pid)


def _pid_state(pid: int) -> str:
    return pid_state(pid)


def _release_pid_lock(lock_path: Path) -> None:
    if _read_lock_pid(lock_path) != os.getpid():
        return
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _serial_trace_dir() -> Path:
    override = os.getenv("AUTO_DBG_SERIAL_LOG_ROOT")
    path = Path(override).expanduser() if override else _default_project_root() / "autodbg" / "serial-log"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _serial_broker_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "autodbg-serial-brokers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_port_name(port: str) -> str:
    return port.lower().replace(":", "_").replace("\\", "_").replace("/", "_")


def _serial_log_port_dir_name(port: str) -> str:
    return port.upper().replace(":", "_").replace("\\", "_").replace("/", "_")


# Windows treats these as device names even with an extension or as a directory: "com7.json" opens the COM7 device,
# "COM7" cannot be a directory, and "nul.json" silently discards whatever is written. On COM1~COM9 that made the
# broker's registry, port lock and trace directory impossible to create.
_WINDOWS_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul", "conin$", "conout$"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
    + ["com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³"]
)
_WINDOWS_NAME_STEM_END = re.compile(r"[.:]")


def _windows_safe_name(name: str) -> str:
    """name, with "_port" inserted after its stem when Windows would read it as a device (com7.json -> com7_port.json).

    Windows decides by the part before the first "." or ":" (trailing spaces ignored), so the suffix goes there.
    Every other name is returned unchanged, so ports that always worked keep their old file names.
    """
    match = _WINDOWS_NAME_STEM_END.search(name)
    cut = match.start() if match else len(name)
    if name[:cut].rstrip(" ").lower() not in _WINDOWS_RESERVED_NAMES:
        return name
    return f"{name[:cut].rstrip(' ')}_port{name[cut:]}"


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
