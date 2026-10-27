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

from autodbg.utils.process import PID_UNKNOWN, pid_is_running, pid_process_name, pid_state


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
def open_serial_port(
    port: str,
    baudrate: int,
    timeout: float = 0.2,
    *,
    broker_first: bool = True,
    control_wait: float | None = None,
):
    """control_wait: seconds to wait for another control session on this port (default _CONTROL_LOCK_WAIT_SECONDS)."""
    lock_wait = _CONTROL_LOCK_WAIT_SECONDS if control_wait is None else control_wait
    broker_registry = load_serial_broker_registry(port)
    if broker_registry is not None:
        with _acquire_control_lock(port, wait_timeout=lock_wait):
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
        with _acquire_control_lock(port, wait_timeout=lock_wait):
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


_OBSERVE_LAUNCH_WAIT_SECONDS = 60.0
# a launch marker older than this no longer stops another launch, even if its window is still open
_OBSERVE_LAUNCH_MARKER_MAX_AGE = 300.0
_OBSERVE_LAUNCH_CLAIM_GRACE = 10.0
_OBSERVE_LAUNCH_POLL_SECONDS = 0.2
_OBSERVE_LAUNCH_LOCK_TIMEOUT = 10.0
_OBSERVE_STATUS_MAX_AGE = 24 * 3600.0
# set for a window launched by ensure_observe_serial_broker: where that window reports why it could not start
OBSERVE_STATUS_FILE_ENV = "AUTO_DBG_OBSERVE_STATUS_FILE"


def ensure_observe_serial_broker(
    port: str,
    *,
    baudrate: int,
    wait_timeout: float = _OBSERVE_LAUNCH_WAIT_SECONDS,
) -> SerialBrokerRegistry:
    registry = load_serial_broker_registry(port)
    if is_observe_serial_broker(registry):
        return registry

    # one window per port: a caller that finds another caller's window still starting waits for it, since every
    # extra window used to start from scratch (and a slow start made each retry open one more)
    launch, ours = _claim_observe_launch(port)
    window = None
    if ours:
        try:
            window = _launch_observe_serial_window(port, baudrate=baudrate, status_file=Path(launch["status_file"]))
        except Exception:
            _release_observe_launch(port, launch["token"])
            raise
        launch["window_pid"] = window.pid
        _update_observe_launch(port, launch)
    status_file = Path(launch["status_file"]) if launch.get("status_file") else None

    deadline = time.monotonic() + max(wait_timeout, 0.1)
    last_registry: SerialBrokerRegistry | None = registry
    while True:
        registry = load_serial_broker_registry(port)
        if registry is not None:
            last_registry = registry
            if is_observe_serial_broker(registry):
                if ours:
                    _release_observe_launch(port, launch["token"])
                    _remove_quietly(status_file)
                return registry
        # the window reports through its own status file: python writes why it could not open the port,
        # observe-serial.ps1 writes the exit code before it keeps a failed window open for the user to read
        status = read_observe_launch_status(status_file) if status_file is not None else None
        if status is not None:
            if ours:
                _release_observe_launch(port, launch["token"])
            raise RuntimeError(_format_observe_launch_failure(port, status))
        if ours:
            exit_code = window.poll()
            if exit_code is not None:
                _release_observe_launch(port, launch["token"])
                raise RuntimeError(_format_observe_launch_failure(port, {"exit_code": exit_code}))
        elif _read_observe_launch_marker(port).get("token") != launch.get("token") or not _observe_launch_is_live(launch):
            # its launcher clears the marker as soon as the broker registers: look once more before giving up
            registry = load_serial_broker_registry(port)
            if is_observe_serial_broker(registry):
                return registry
            status = read_observe_launch_status(status_file) if status_file is not None else None
            if status is not None:
                raise RuntimeError(_format_observe_launch_failure(port, status))
            raise RuntimeError(
                f"The observe-serial window another autodbg call opened for {port} closed before opening the port. "
                f"Run observe-serial -SerialPort {port} in a terminal to see why."
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(_OBSERVE_LAUNCH_POLL_SECONDS)

    if last_registry is not None:
        return protect_serial_broker_registry(port) or last_registry
    window_pid = launch.get("window_pid")
    window_text = f" (window pid {window_pid})" if window_pid else ""
    raise RuntimeError(
        f"observe-serial for {port} is still starting after {wait_timeout:.0f} s{window_text}. "
        f"Its window was left open: wait until it shows {port} connected, then retry. "
        f"Do not open another observe-serial for {port}."
    )


def _format_observe_launch_failure(port: str, status: dict) -> str:
    if status.get("error"):
        lines = [f"observe-serial could not open {port}: {status['error']}"]
        hints = status.get("hints")
        if isinstance(hints, list):
            lines.extend(f"Hint: {hint}" for hint in hints)
        return "\n".join(lines)
    return (
        f"The observe-serial window for {port} exited with code {status.get('exit_code', 'unknown')} before opening "
        f"the port. Run observe-serial -SerialPort {port} in a terminal to see why."
    )


def _observe_launch_marker_path(port: str) -> Path:
    # no ".json" suffix: registry listings treat every *.json in this directory as a broker registry
    return _serial_broker_dir() / f"{_windows_safe_name(_normalize_port_name(port))}.launching"


def _claim_observe_launch(port: str) -> tuple[dict, bool]:
    """(launch, True) when this caller is to open the window, else (another caller's live launch, False)."""
    safe_name = _windows_safe_name(_normalize_port_name(port))
    with _observe_launch_lock(port):
        current = _read_observe_launch_marker(port)
        if _observe_launch_is_live(current):
            return current, False
        token = f"{os.getpid()}-{random.getrandbits(48):012x}"
        launch = {
            "token": token,
            "launcher_pid": os.getpid(),
            "window_pid": None,
            "started_at": time.time(),
            "status_file": str(_serial_broker_dir() / f"{safe_name}.launch-{token}.status"),
        }
        _write_text_atomic(_observe_launch_marker_path(port), json.dumps(launch) + "\n")
    _remove_old_observe_status_files(safe_name)
    return launch, True


def _update_observe_launch(port: str, launch: dict) -> None:
    with _observe_launch_lock(port):
        if _read_observe_launch_marker(port).get("token") == launch["token"]:
            _write_text_atomic(_observe_launch_marker_path(port), json.dumps(launch) + "\n")


def _release_observe_launch(port: str, token: str) -> None:
    """Remove the launch marker if it is still this launch's (a newer launch may have replaced a stale one)."""
    try:
        with _observe_launch_lock(port):
            if _read_observe_launch_marker(port).get("token") == token:
                _remove_quietly(_observe_launch_marker_path(port))
    except RuntimeError:
        pass


@contextmanager
def _observe_launch_lock(port: str):
    """Serialize launch decisions for port across processes; the OS drops the lock if its holder dies."""
    path = _serial_broker_dir() / f"{_windows_safe_name(_normalize_port_name(port))}.launch-lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.monotonic() + _OBSERVE_LAUNCH_LOCK_TIMEOUT
        while not _try_lock_fd(fd):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for another autodbg call that is launching observe-serial for {port}.")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock_fd(fd)
    finally:
        os.close(fd)


def _try_lock_fd(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_fd(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _read_observe_launch_marker(port: str) -> dict:
    """The current launch marker for port, or {} when there is none."""
    path = _observe_launch_marker_path(port)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        return payload
    # unreadable content (an older autodbg's claim, or a crash mid-write): only its age is known
    try:
        return {"started_at": path.stat().st_mtime}
    except OSError:
        return {}


def _observe_launch_is_live(marker: dict) -> bool:
    if not marker:
        return False
    try:
        started_at = float(marker.get("started_at") or 0.0)
    except (TypeError, ValueError):
        return False
    age = time.time() - started_at
    if age > _OBSERVE_LAUNCH_MARKER_MAX_AGE:
        return False
    pid = marker.get("window_pid") or marker.get("launcher_pid")
    if pid is None:
        return age <= _OBSERVE_LAUNCH_CLAIM_GRACE
    try:
        return _pid_is_running(int(pid))
    except (TypeError, ValueError):
        return False


def record_observe_start_error(port: str, error: str, hints: list[str]) -> None:
    """Report to the autodbg call that launched this observe-serial window why it could not open the port."""
    raw_path = os.getenv(OBSERVE_STATUS_FILE_ENV)
    if not raw_path:
        return  # opened by hand: nobody is waiting on it
    payload = {
        "serial_port": port,
        "pid": os.getpid(),
        "recorded_at": time.time(),
        "error": error,
        "hints": list(hints),
    }
    path = Path(raw_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(path, json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_observe_launch_status(path: Path) -> dict | None:
    """What a launched observe-serial window reported (an error, or the exit code), or None so far."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None  # being written
    return payload if isinstance(payload, dict) else None


def _remove_old_observe_status_files(safe_name: str) -> None:
    # a window closed by the user after a successful start still writes its exit code; sweep those up now and then
    try:
        candidates = list(_serial_broker_dir().glob(f"{safe_name}.launch-*.status"))
    except OSError:
        return
    now = time.time()
    for path in candidates:
        try:
            if now - path.stat().st_mtime > _OBSERVE_STATUS_MAX_AGE:
                path.unlink()
        except OSError:
            continue


def _remove_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


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


def _launch_observe_serial_window(port: str, *, baudrate: int, status_file: Path) -> subprocess.Popen:
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
    env[OBSERVE_STATUS_FILE_ENV] = str(status_file)
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
    return subprocess.Popen(args, cwd=str(project_root), env=env, creationflags=creationflags)


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
        # the port lock holder (a broker, or direct access) writes nearly every trace line: it alone rotates the trace
        owner_key = _normalize_port_name(port)
        _TRACE_ROTATION_OWNERS.add(owner_key)
        try:
            yield
        finally:
            _TRACE_ROTATION_OWNERS.discard(owner_key)


def _serial_lock_dir() -> Path:
    return Path(tempfile.gettempdir()) / "autodbg-serial-locks"


def _serial_lock_path(port: str) -> Path:
    # normalized like the registry: "\\.\COM7" or "/dev/ttyUSB0" used to escape the lock directory
    return _serial_lock_dir() / f"{_windows_safe_name(_normalize_port_name(port))}.lock"


_CONTROL_LOCK_WAIT_SECONDS = 20.0
_CONTROL_LOCK_RETRY_SECONDS = 0.25


@contextmanager
def _acquire_control_lock(port: str, *, wait_timeout: float = _CONTROL_LOCK_WAIT_SECONDS):
    # two AI commands on one port used to fail at once; wait a while for the other one to finish instead
    lock_path = _serial_control_lock_path(port)
    deadline = time.monotonic() + max(wait_timeout, 0.0)
    while True:
        acquired = False
        try:
            with _acquire_pid_lock(
                lock_path,
                busy_message=f"Serial port {port} is already in use by another autodbg control session.",
            ):
                acquired = True
                yield
            return
        except SerialPortBusyError:
            if acquired or time.monotonic() >= deadline:
                raise
        time.sleep(_CONTROL_LOCK_RETRY_SECONDS)


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
                raise SerialPortBusyError(busy_message + _lock_holder_description(lock_path)) from exc
            time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
    else:
        raise SerialPortBusyError(busy_message + _lock_holder_description(lock_path))

    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            _release_pid_lock(lock_path)


def _lock_holder_description(lock_path: Path) -> str:
    pid = _read_lock_pid(lock_path)
    if pid is None:
        return ""
    name = pid_process_name(pid)
    return f" Held by pid {pid}" + (f" ({name})." if name else ".")


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


def _windows_safe_name(name: str, suffix: str = "_port") -> str:
    """name, with suffix inserted after its stem when Windows would read it as a device (com7.json -> com7_port.json).

    Windows decides by the part before the first "." or ":" (trailing spaces ignored), so the suffix goes there.
    Every other name is returned unchanged, so ports that always worked keep their old file names.
    """
    match = _WINDOWS_NAME_STEM_END.search(name)
    cut = match.start() if match else len(name)
    if name[:cut].rstrip(" ").lower() not in _WINDOWS_RESERVED_NAMES:
        return name
    return f"{name[:cut].rstrip(' ')}{suffix}{name[cut:]}"


_TRACE_MAX_BYTES_DEFAULT = 64 * 1024 * 1024
_TRACE_KEEP_DEFAULT = 30
_TRACE_ROTATION_RETRY_SECONDS = 2.0
_ROTATED_TRACE_RE = re.compile(r"^trace\.(\d{8}-\d{6})(?:-(\d+))?\.jsonl$")
_TRACE_ROTATION_OWNERS: set[str] = set()  # normalized ports whose port lock this process holds
_TRACE_ROTATION_LOCK = threading.Lock()
_TRACE_ROTATION_RETRY_AT: dict[Path, float] = {}


def _append_trace_entry(port: str, direction: str, payload: str) -> SerialTraceEntry:
    entry = SerialTraceEntry(
        timestamp=datetime.now().isoformat(timespec="milliseconds"),
        port=port,
        direction=direction,
        payload=payload,
        pid=os.getpid(),
    )
    log_path = serial_trace_log_path(port)
    if _normalize_port_name(port) in _TRACE_ROTATION_OWNERS:
        # rotate before writing, so trace.jsonl always exists and holds the newest line
        max_bytes, keep = _trace_rotation_limits()
        if max_bytes > 0:
            _rotate_trace_log(log_path, max_bytes=max_bytes, keep=keep)
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
    return entry


def _trace_rotation_limits() -> tuple[int, int]:
    """(rotate once the trace reaches this many bytes, rotated segments to keep); 0 disables either."""
    return (
        _env_int("AUTO_DBG_SERIAL_TRACE_MAX_BYTES", _TRACE_MAX_BYTES_DEFAULT),
        _env_int("AUTO_DBG_SERIAL_TRACE_KEEP", _TRACE_KEEP_DEFAULT),
    )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def _rotate_trace_log(log_path: Path, *, max_bytes: int, keep: int) -> Path | None:
    """Rename trace.jsonl to trace.<YYYYmmdd-HHMMSS>[-n].jsonl so readers never face an ever-growing file."""
    with _TRACE_ROTATION_LOCK:
        if time.monotonic() < _TRACE_ROTATION_RETRY_AT.get(log_path, 0.0):
            return None
        try:
            if log_path.stat().st_size < max_bytes:
                return None
        except OSError:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # above every segment of this second, even pruned ones' successors: reusing a freed name would sort the
        # newest data first and get it pruned next
        sequence = 1 + max(
            (_rotation_sequence(path) for path in serial_trace_rotated_paths(log_path) if serial_trace_rotation_stamp(path) == stamp),
            default=-1,
        )
        target = log_path.with_name(f"trace.{stamp}.jsonl" if sequence == 0 else f"trace.{stamp}-{sequence}.jsonl")
        while target.exists():
            sequence += 1
            target = log_path.with_name(f"trace.{stamp}-{sequence}.jsonl")
        try:
            os.rename(log_path, target)
        except FileNotFoundError:
            return None
        except OSError:
            # Windows refuses to rename a file another process has open (a case marker being appended, a reader):
            # keep appending to it and try again a little later
            _TRACE_ROTATION_RETRY_AT[log_path] = time.monotonic() + _TRACE_ROTATION_RETRY_SECONDS
            return None
        _TRACE_ROTATION_RETRY_AT.pop(log_path, None)
    if keep > 0:
        for stale in serial_trace_rotated_paths(log_path)[:-keep]:
            try:
                stale.unlink()
            except OSError:
                continue
    return target


def serial_trace_rotated_paths(trace_path: Path) -> list[Path]:
    """The rotated segments of trace_path, oldest first."""
    try:
        candidates = list(trace_path.parent.glob("trace.*.jsonl"))
    except OSError:
        return []
    keyed: list[tuple[tuple[str, int], Path]] = []
    for path in candidates:
        match = _ROTATED_TRACE_RE.match(path.name)
        if match:
            keyed.append(((match.group(1), int(match.group(2) or 0)), path))
    return [path for _key, path in sorted(keyed)]


def serial_trace_rotation_stamp(path: Path) -> str | None:
    """The YYYYmmdd-HHMMSS a rotated segment was rotated at, or None for any other file."""
    match = _ROTATED_TRACE_RE.match(path.name)
    return match.group(1) if match else None


def _rotation_sequence(path: Path) -> int:
    match = _ROTATED_TRACE_RE.match(path.name)
    return int(match.group(2) or 0) if match else 0


def serial_trace_stat(path: Path) -> tuple[int, str | None]:
    """(size, identity) from one stat, so both describe the same file; (0, None) when it does not exist.

    The identity survives a rename on the same volume; it is None when the filesystem reports none.
    """
    try:
        stat_result = path.stat()
    except OSError:
        return 0, None
    file_id = f"{stat_result.st_dev}:{stat_result.st_ino}" if stat_result.st_ino else None
    return stat_result.st_size, file_id


def serial_trace_file_id(path: Path) -> str | None:
    return serial_trace_stat(path)[1]


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
