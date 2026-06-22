from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from autodbg.control.controller import LoginResult
from autodbg.serial.broker import SerialBroker, serial_open_recovery_hints
from autodbg.serial.observer import MarkerHit
from autodbg.serial.runtime import (
    SerialBrokerProtectedError,
    SerialBrokerRegistry,
    SerialTraceEntry,
    SerialTraceStreamClient,
    list_serial_broker_registries,
    load_serial_broker_registry,
    open_serial_port,
    serial_trace_log_path,
    stop_serial_broker,
)
from autodbg.state.machine import StateSnapshot


def _cli_main_module():
    from autodbg.cli import main as cli_main

    return cli_main


def _read_trace_entries(trace_path: Path) -> list[SerialTraceEntry]:
    if not trace_path.exists():
        return []
    entries: list[SerialTraceEntry] = []
    for raw_line in trace_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entries.append(SerialTraceEntry.from_json_line(raw_line))
        except Exception:
            continue
    return entries


def _resolve_watch_start_index(*, last_count: int, entry_count: int, tail: int, replay_existing: bool) -> int:
    if replay_existing:
        if tail <= 0:
            return 0
        return max(entry_count - tail, 0)
    return min(last_count, entry_count)


def _print_trace_entries(entries: list[SerialTraceEntry], *, show_system: bool) -> None:
    for entry in entries:
        _print_watch_trace_entry(entry, show_system=show_system)


def _format_trace_entry(entry: SerialTraceEntry) -> str:
    stamp = entry.timestamp.split("T")[-1]
    direction = entry.direction.upper()
    payload = entry.payload if entry.payload else "(empty line)"
    return f"[{direction} {stamp}] {payload}"


def _print_watch_trace_entry(entry: SerialTraceEntry, *, show_system: bool) -> bool:
    if entry.direction == "sys":
        if entry.payload.startswith("SERIAL_CONNECTED"):
            baudrate = entry.payload.partition("baudrate=")[2] or "unknown"
            print(f"[DONE] {entry.port} connected successfully @ {baudrate}", flush=True)
            return True
        if entry.payload.startswith("SERIAL_UNAVAILABLE"):
            error_summary = entry.payload.partition("error=")[2] or "serial link unavailable"
            print(f"[ERROR] {entry.port} is unavailable: {error_summary}", flush=True)
            print(f"[TODO] Press Enter to retry reconnecting {entry.port}.", flush=True)
            return True
        if entry.payload.startswith("SERIAL_OPEN_FAILED"):
            error_summary = entry.payload.partition("error=")[2] or "serial link unavailable"
            print(f"[ERROR] Failed to open {entry.port}: {error_summary}", flush=True)
            for hint in serial_open_recovery_hints(entry.port, error_summary):
                print(f"[TODO] {hint}", flush=True)
            return True
        if not show_system:
            return False
    print(_format_trace_entry(entry), flush=True)
    return True


class _WatchStdinShellState:
    def __init__(self) -> None:
        self.buffer = ""
        self.prompt_visible = False
        self.status_message = _default_watch_status()
        self.status_expires_at: float | None = None
        self.recent_lines: list[str] = []
        self.scroll_offset = 0
        self.paused = False
        self.paused_new_lines = 0
        self.pause_log_end_index: int | None = None
        self.screen_dirty = True


_WATCH_TUI_SCROLLBACK_LIMIT = 2000
_WATCH_TUI_TRANSIENT_STATUS_SECONDS = 4.0


def _default_watch_status() -> str:
    return "Enter=send/probe | Ctrl+L=login | Ctrl+P=pause | PgUp/PgDn=history | Ctrl+C=exit"


def _set_watch_status_message(
    state: _WatchStdinShellState,
    message: str,
    *,
    transient_seconds: float | None = None,
) -> None:
    state.status_message = message or _default_watch_status()
    state.status_expires_at = (
        time.monotonic() + max(transient_seconds, 0.1)
        if transient_seconds is not None
        else None
    )
    state.screen_dirty = True


def _refresh_watch_status_message(
    state: _WatchStdinShellState,
    *,
    now: float | None = None,
) -> bool:
    if state.status_expires_at is None:
        return False
    current = time.monotonic() if now is None else now
    if current < state.status_expires_at:
        return False
    state.status_expires_at = None
    default_message = _default_watch_status()
    if state.status_message != default_message:
        state.status_message = default_message
        state.screen_dirty = True
        return True
    return False


def _watch_terminal_columns() -> int:
    try:
        return max(os.get_terminal_size().columns, 40)
    except OSError:
        return 120


def _watch_terminal_lines() -> int:
    try:
        return max(os.get_terminal_size().lines, 8)
    except OSError:
        return 30


def _watch_console_viewport(
    writer=None,
) -> tuple[object | None, tuple[int, int] | None, int | None, int | None, int | None, int | None]:
    if writer not in {None, sys.stdout} or os.name != "nt":
        return None, None, None, None, None, None
    try:
        import ctypes
    except ImportError:
        return None, None, None, None, None, None

    class _Coord(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class _SmallRect(ctypes.Structure):
        _fields_ = [
            ("Left", ctypes.c_short),
            ("Top", ctypes.c_short),
            ("Right", ctypes.c_short),
            ("Bottom", ctypes.c_short),
        ]

    class _ConsoleScreenBufferInfo(ctypes.Structure):
        _fields_ = [
            ("dwSize", _Coord),
            ("dwCursorPosition", _Coord),
            ("wAttributes", ctypes.c_ushort),
            ("srWindow", _SmallRect),
            ("dwMaximumWindowSize", _Coord),
        ]

    handle = ctypes.windll.kernel32.GetStdHandle(-11)
    if handle in (0, -1):
        return None, None, None, None, None, None
    info = _ConsoleScreenBufferInfo()
    if not ctypes.windll.kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        return None, None, None, None, None, None
    return (
        handle,
        (int(info.dwCursorPosition.X), int(info.dwCursorPosition.Y)),
        int(info.srWindow.Left),
        int(info.srWindow.Top),
        int(info.srWindow.Right),
        int(info.srWindow.Bottom),
    )


def _watch_console_move_cursor(handle, x: int, y: int) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
    except ImportError:
        return False

    class _Coord(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    kernel32 = ctypes.windll.kernel32
    try:
        kernel32.SetConsoleCursorPosition.argtypes = [ctypes.c_void_p, _Coord]
        kernel32.SetConsoleCursorPosition.restype = ctypes.c_bool
    except Exception:
        pass
    return bool(kernel32.SetConsoleCursorPosition(handle, _Coord(x, y)))


def _watch_console_write_text(handle, x: int, y: int, text: str) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
    except ImportError:
        return False

    class _Coord(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    written = ctypes.c_ulong(0)
    kernel32 = ctypes.windll.kernel32
    try:
        kernel32.WriteConsoleOutputCharacterW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            _Coord,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.WriteConsoleOutputCharacterW.restype = ctypes.c_bool
    except Exception:
        pass
    return bool(kernel32.WriteConsoleOutputCharacterW(handle, text, len(text), _Coord(x, y), ctypes.byref(written)))


def _watch_console_set_cursor_visible(visible: bool, *, writer=None) -> bool:
    handle, _cursor, _left_col, _top_row, _right_col, _bottom_row = _watch_console_viewport(writer)
    if handle is None or os.name != "nt":
        return False
    try:
        import ctypes
    except ImportError:
        return False

    class _ConsoleCursorInfo(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong), ("bVisible", ctypes.c_bool)]

    info = _ConsoleCursorInfo()
    kernel32 = ctypes.windll.kernel32
    if not kernel32.GetConsoleCursorInfo(handle, ctypes.byref(info)):
        return False
    info.bVisible = bool(visible)
    return bool(kernel32.SetConsoleCursorInfo(handle, ctypes.byref(info)))


def _watch_tui_geometry(writer=None) -> tuple[object | None, int | None, int | None, int | None, int | None]:
    handle, _cursor, left_col, top_row, right_col, bottom_row = _watch_console_viewport(writer)
    if handle is None or left_col is None or top_row is None or right_col is None or bottom_row is None:
        return None, None, None, None, None
    viewport_width = max((right_col - left_col) + 1, 40)
    viewport_height = max((bottom_row - top_row) + 1, 8)
    return handle, left_col, top_row, viewport_width, viewport_height


def _fit_watch_tui_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    cleaned = text.replace("\r", " ").replace("\n", " ")
    if len(cleaned) <= width:
        return cleaned.ljust(width)
    if width <= 3:
        return cleaned[:width]
    return cleaned[: width - 3] + "..."


def _build_watch_tui_header(serial_port: str, baudrate: int | None, width: int) -> str:
    baudrate_text = f" @ {baudrate}" if baudrate else ""
    header = (
        f"[AUTO-DEBUG SERIAL TUI] {serial_port}{baudrate_text} | "
        "Ctrl+P=pause | Ctrl+L=login | PgUp/PgDn=history | Ctrl+C=exit"
    )
    if len(header) <= width:
        return header
    compact_baudrate = f" @{baudrate}" if baudrate else ""
    compact_header = (
        f"[AUTO-DEBUG] {serial_port}{compact_baudrate} | "
        "Ctrl+P=pause | ^L=login | PgUp/PgDn"
    )
    if len(compact_header) <= width:
        return compact_header
    short_header = f"{serial_port}{compact_baudrate} | Ctrl+P=pause | ^L=login"
    if len(short_header) <= width:
        return short_header
    return f"{serial_port} | Ctrl+P=pause"


def _wrap_watch_tui_line(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    cleaned = text.replace("\r", " ").replace("\n", " ")
    if not cleaned:
        return [""]
    return [cleaned[index : index + width] for index in range(0, len(cleaned), width)]


def _watch_log_line_count(state: _WatchStdinShellState) -> int:
    if state.paused and state.pause_log_end_index is not None:
        return max(min(state.pause_log_end_index, len(state.recent_lines)), 0)
    return len(state.recent_lines)


def _watch_pause_status_message(state: _WatchStdinShellState, base_status: str) -> str:
    if not state.paused:
        return base_status
    paused_text = "PAUSED"
    if state.paused_new_lines:
        paused_text += f" +{state.paused_new_lines} line(s)"
    default_status = _default_watch_status()
    if base_status and base_status != default_status:
        return f"{paused_text} | {base_status}"
    return f"{paused_text} | Ctrl+P=resume | PgUp/PgDn=history | Ctrl+C=exit"


def _toggle_watch_pause(state: _WatchStdinShellState) -> str:
    if state.paused:
        released_lines = state.paused_new_lines
        state.paused = False
        state.paused_new_lines = 0
        state.pause_log_end_index = None
        state.scroll_offset = 0
        state.screen_dirty = True
        if released_lines:
            return f"Live output resumed; {released_lines} buffered line(s) are visible."
        return "Live output resumed."
    state.paused = True
    state.paused_new_lines = 0
    state.pause_log_end_index = len(state.recent_lines)
    state.screen_dirty = True
    return "Live output paused; incoming lines are still captured."


def _append_watch_recent_line(state: _WatchStdinShellState, line: str) -> None:
    state.recent_lines.append(line)
    if state.paused:
        state.paused_new_lines += 1
    elif state.scroll_offset > 0:
        state.scroll_offset += 1
    overflow = len(state.recent_lines) - _WATCH_TUI_SCROLLBACK_LIMIT
    if overflow > 0:
        del state.recent_lines[:overflow]
        if state.pause_log_end_index is not None:
            state.pause_log_end_index = max(state.pause_log_end_index - overflow, 0)
        state.scroll_offset = max(state.scroll_offset - overflow, 0)


def _watch_tui_log_rows(height: int) -> int:
    return max(max(height, 8) - 4, 1)


def _clamp_watch_scroll_offset(state: _WatchStdinShellState, *, log_rows: int) -> int:
    max_offset = max(_watch_log_line_count(state) - log_rows, 0)
    if state.scroll_offset > max_offset:
        state.scroll_offset = max_offset
    elif state.scroll_offset < 0:
        state.scroll_offset = 0
    return state.scroll_offset


def _page_watch_history(
    state: _WatchStdinShellState,
    *,
    height: int,
    direction: str,
) -> bool:
    log_rows = _watch_tui_log_rows(height)
    page_step = max((log_rows * 3 + 3) // 4, 1)
    current_offset = _clamp_watch_scroll_offset(state, log_rows=log_rows)
    max_offset = max(_watch_log_line_count(state) - log_rows, 0)
    if direction == "page_up":
        next_offset = min(current_offset + page_step, max_offset)
    elif direction == "page_down":
        next_offset = max(current_offset - page_step, 0)
    elif direction == "home":
        next_offset = max_offset
    elif direction == "end":
        next_offset = 0
    else:
        return False
    if next_offset == current_offset:
        return False
    state.scroll_offset = next_offset
    state.screen_dirty = True
    return True


def _build_watch_tui_rows(
    state: _WatchStdinShellState,
    *,
    serial_port: str,
    width: int,
    height: int,
    baudrate: int | None = None,
    input_label: str | None = None,
    input_value: str | None = None,
    status_message: str | None = None,
) -> tuple[list[str], int, int]:
    safe_width = max(width, 40)
    safe_height = max(height, 8)
    effective_status = status_message if status_message is not None else state.status_message
    if not effective_status:
        effective_status = _default_watch_status()
    effective_status = _watch_pause_status_message(state, effective_status)
    effective_input_label = input_label or f"[INPUT {serial_port}]"
    effective_input_value = state.buffer if input_value is None else input_value
    header = _build_watch_tui_header(serial_port, baudrate, safe_width)
    separator = "-" * safe_width
    log_rows = _watch_tui_log_rows(safe_height)
    scroll_offset = _clamp_watch_scroll_offset(state, log_rows=log_rows)
    line_count = _watch_log_line_count(state)
    end_index = line_count - scroll_offset if scroll_offset > 0 else line_count
    end_index = max(min(end_index, line_count), 0)
    visible_lines: list[str] = []
    for line in reversed(state.recent_lines[:end_index]):
        wrapped = _wrap_watch_tui_line(line, safe_width)
        visible_lines[0:0] = wrapped
        if len(visible_lines) >= log_rows:
            visible_lines = visible_lines[-log_rows:]
            break
    history_prefix = f"[history +{scroll_offset}] " if scroll_offset > 0 else ""
    rows = [_fit_watch_tui_text(header, safe_width)]
    for index in range(log_rows):
        line = visible_lines[index] if index < len(visible_lines) else ""
        rows.append(_fit_watch_tui_text(line, safe_width))
    rows.append(separator)
    rows.append(_fit_watch_tui_text(f"Status: {history_prefix}{effective_status}", safe_width))
    input_prefix = f"{effective_input_label} "
    input_body_width = max(safe_width - len(input_prefix), 0)
    visible_input = effective_input_value[-input_body_width:] if input_body_width > 0 else ""
    rows.append(_fit_watch_tui_text(f"{input_prefix}{visible_input}", safe_width))
    cursor_col = min(len(input_prefix) + len(visible_input), max(safe_width - 1, 0))
    cursor_row = len(rows) - 1
    return rows, cursor_col, cursor_row


def _clear_watch_stdin_shell_prompt(*, writer=None) -> None:
    if writer is None:
        writer = sys.stdout
    handle, left_col, top_row, width, height = _watch_tui_geometry(writer)
    if handle is None or left_col is None or top_row is None or width is None or height is None:
        return
    blank = " " * width
    for row in range(height):
        _watch_console_write_text(handle, left_col, top_row + row, blank)
    _watch_console_set_cursor_visible(True, writer=writer)
    _watch_console_move_cursor(handle, left_col, top_row + height - 1)
    writer.flush()


def _render_watch_shell_screen(
    state: _WatchStdinShellState,
    *,
    serial_port: str,
    baudrate: int | None = None,
    input_label: str | None = None,
    input_value: str | None = None,
    status_message: str | None = None,
    writer=None,
) -> None:
    if writer is None:
        writer = sys.stdout
    handle, left_col, top_row, width, height = _watch_tui_geometry(writer)
    if handle is None or left_col is None or top_row is None or width is None or height is None:
        return
    rows, cursor_col, cursor_row = _build_watch_tui_rows(
        state,
        serial_port=serial_port,
        width=width,
        height=height,
        baudrate=baudrate,
        input_label=input_label,
        input_value=input_value,
        status_message=status_message,
    )
    writer.flush()
    _watch_console_set_cursor_visible(False, writer=writer)
    for index, row in enumerate(rows):
        _watch_console_write_text(handle, left_col, top_row + index, row)
    _watch_console_move_cursor(handle, left_col + cursor_col, top_row + cursor_row)
    writer.flush()
    state.screen_dirty = False


def _set_watch_stdin_shell_status(
    state: _WatchStdinShellState,
    *,
    serial_port: str,
    baudrate: int | None = None,
    message: str,
    transient_seconds: float | None = None,
    writer=None,
) -> None:
    _set_watch_status_message(state, message, transient_seconds=transient_seconds)
    if state.prompt_visible:
        _render_watch_shell_screen(
            state,
            serial_port=serial_port,
            baudrate=baudrate,
            writer=writer,
        )


def _run_watch_auto_login(
    args: argparse.Namespace,
    *,
    serial_port: str,
    state: _WatchStdinShellState | None = None,
    writer=None,
    status_writer=None,
) -> tuple[bool, str]:
    if status_writer is None:
        status_writer = lambda _message: None
    status_writer(f"Logging in on {serial_port}...")
    try:
        cli_main = _cli_main_module()
        profiles = cli_main._load_profiles_from_args(args)
        controller = cli_main.DeviceController(profiles.device, profiles.model, profiles.transport)
        login_result = controller.login(timeout=20.0)
    except cli_main.ProfileResolutionError as exc:
        return False, f"Failed to resolve the login profile for {serial_port}: {exc}"
    except Exception as exc:
        return False, f"Auto login failed on {serial_port}: {exc}"

    if login_result.success:
        status_writer(f"Auto login reached the root shell on {serial_port}")
        return True, f"Auto login reached the root shell on {serial_port}"
    if login_result.login_prompt_seen and not login_result.shell_prompt_seen:
        return _retry_watch_auto_login_with_password(
            controller,
            serial_port=serial_port,
            state=state,
            writer=writer,
            status_writer=status_writer,
        )
    return False, f"Auto login did not reach the shell on {serial_port}"


def _retry_watch_auto_login_with_password(
    controller: DeviceController,
    *,
    serial_port: str,
    state: _WatchStdinShellState | None = None,
    writer=None,
    status_writer=None,
) -> tuple[bool, str]:
    credentials = controller.device_profile.credentials
    username = credentials.username if credentials is not None else "root"
    password_env = credentials.password_env if credentials is not None and credentials.password_env else "AUTO_DBG_DEVICE_PASSWORD"
    original_present = password_env in os.environ
    original_value = os.environ.get(password_env)
    if status_writer is None:
        status_writer = lambda _message: None

    status_writer(f"Login failed; enter the password for {username} on {serial_port}. Enter=confirm Esc=cancel")
    for attempt in range(1, 4):
        if attempt > 1:
            status_writer(f"Retry password attempt {attempt}/3 for {username} on {serial_port}. Enter=confirm Esc=cancel")
        password = _cli_main_module()._prompt_watch_password(
            serial_port=serial_port,
            username=username,
            password_env=password_env,
            state=state,
            writer=writer,
            status_message=f"Password attempt {attempt}/3 | Enter=confirm Esc=cancel",
        )
        if password is None:
            _restore_watch_password_env(password_env, original_present=original_present, original_value=original_value)
            return False, f"Auto login canceled on {serial_port}; password was not updated."
        retry_result = controller.login(timeout=20.0)
        if retry_result.success:
            status_writer(f"Auto login reached the root shell on {serial_port}")
            return True, f"Auto login reached the root shell on {serial_port}"
        if not retry_result.login_prompt_seen:
            _restore_watch_password_env(password_env, original_present=original_present, original_value=original_value)
            return False, f"Auto login retry on {serial_port} did not reach the login prompt or shell."

    _restore_watch_password_env(password_env, original_present=original_present, original_value=original_value)
    return False, f"Auto login failed after 3 password attempts on {serial_port}."


def _prompt_watch_password(
    *,
    serial_port: str,
    username: str,
    password_env: str,
    state: _WatchStdinShellState | None = None,
    writer=None,
    status_message: str = "",
) -> str | None:
    if writer is None:
        writer = sys.stdout
    if os.name != "nt":
        try:
            password = getpass.getpass("")
        except (EOFError, KeyboardInterrupt):
            return None
        if not password:
            return None
        os.environ[password_env] = password
        return password
    try:
        import msvcrt  # type: ignore
    except ImportError:
        return None

    password = ""
    label = f"[PASSWORD {serial_port} {username}]"
    while True:
        if state is not None:
            _render_watch_shell_screen(
                state,
                serial_port=serial_port,
                input_label=label,
                input_value="*" * len(password),
                status_message=status_message,
                writer=writer,
            )
        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            if msvcrt.kbhit():
                msvcrt.getwch()
            continue
        if char == "\x03":
            raise KeyboardInterrupt
        if char == "\x1b":
            return None
        if char in ("\r", "\n"):
            if not password:
                return None
            os.environ[password_env] = password
            return password
        if char in ("\b", "\x7f"):
            if password:
                password = password[:-1]
            continue
        if char.isprintable():
            password += char


def _restore_watch_password_env(password_env: str, *, original_present: bool, original_value: str | None) -> None:
    if original_present and original_value is not None:
        os.environ[password_env] = original_value
        return
    os.environ.pop(password_env, None)


def _watch_probe_key_pressed() -> bool:
    if os.name != "nt":
        return False
    try:
        import msvcrt  # type: ignore
    except ImportError:
        return False
    pressed = False
    while msvcrt.kbhit():
        char = msvcrt.getwch()
        if char in ("\r", "\n"):
            pressed = True
    return pressed


def _send_watch_newline_probe(serial_port: str, baudrate: int) -> tuple[bool, str]:
    return _send_watch_serial_text(serial_port, baudrate, "\n")


def _send_watch_serial_text(serial_port: str, baudrate: int, text: str) -> tuple[bool, str]:
    payload = text.encode("utf-8")
    display = text.rstrip("\r\n")
    try:
        with open_serial_port(serial_port, baudrate, timeout=0.2) as handle:
            handle.write(payload)
            handle.flush()
        if not display:
            return True, f"Sent newline probe to {serial_port}"
        if len(display) > 80:
            display = display[:77] + "..."
        return True, f"Sent command to {serial_port}: {display}"
    except Exception as exc:
        if not display:
            return False, f"Failed to send newline probe to {serial_port}: {exc}"
        return False, f"Failed to send command to {serial_port}: {exc}"


def _consume_watch_stdin_probe(
    *,
    serial_port: str,
    baudrate: int,
    enabled: bool,
    key_reader=None,
    probe_sender=None,
) -> None:
    if not enabled:
        return
    if key_reader is None:
        key_reader = _watch_probe_key_pressed
    if probe_sender is None:
        probe_sender = _send_watch_newline_probe
    if not key_reader():
        return
    ok, message = probe_sender(serial_port, baudrate)
    prefix = "[DONE]" if ok else "[ERROR]"
    print(f"{prefix} {message}", flush=True)


def _watch_shell_read_chars() -> list[str]:
    if os.name != "nt":
        return []
    try:
        import msvcrt  # type: ignore
    except ImportError:
        return []
    chars: list[str] = []
    while msvcrt.kbhit():
        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            # The second scan-code byte can arrive a fraction later than the
            # prefix. Read it directly so PgUp/PgDn/Home/End don't leak as I/Q/G/O.
            extended = msvcrt.getwch()
            if extended == "I":
                chars.append("__PAGE_UP__")
            elif extended == "Q":
                chars.append("__PAGE_DOWN__")
            elif extended == "G":
                chars.append("__SCROLL_TOP__")
            elif extended == "O":
                chars.append("__SCROLL_BOTTOM__")
            elif extended == "<":
                chars.append("__TOGGLE_PAUSE__")
            continue
        if char == "\x0c":
            chars.append("__AUTO_LOGIN__")
            continue
        if char == "\x10":
            chars.append("__TOGGLE_PAUSE__")
            continue
        chars.append(char)
    return chars


def _watch_shell_login_action(
    args: argparse.Namespace,
    state: _WatchStdinShellState,
    *,
    serial_port: str,
    baudrate: int,
    writer=None,
) -> tuple[bool, str]:
    return _run_watch_auto_login(
        args,
        serial_port=serial_port,
        state=state,
        writer=writer,
        status_writer=lambda message: _set_watch_stdin_shell_status(
            state,
            serial_port=serial_port,
            baudrate=baudrate,
            message=message,
            transient_seconds=None,
            writer=writer,
        ),
    )


def _consume_watch_stdin_shell(
    *,
    serial_port: str,
    baudrate: int,
    enabled: bool,
    state: _WatchStdinShellState,
    char_reader=None,
    text_sender=None,
    login_action=None,
    writer=None,
    printer=None,
) -> None:
    if not enabled:
        return
    if char_reader is None:
        char_reader = _watch_shell_read_chars
    if text_sender is None:
        text_sender = _send_watch_serial_text
    if login_action is None:
        login_action = lambda: (False, "Auto login is unavailable in this watch session.")
    if writer is None:
        writer = sys.stdout
    if printer is None:
        printer = print
    if not state.prompt_visible:
        _render_watch_shell_screen(state, serial_port=serial_port, baudrate=baudrate, writer=writer)
        state.prompt_visible = True
    chars = char_reader()
    if not chars:
        return
    _handle, _left_col, _top_row, _width, viewport_height = _watch_tui_geometry(writer)
    page_height = viewport_height if viewport_height is not None else _watch_terminal_lines()
    for char in chars:
        if char == "\x03":
            raise KeyboardInterrupt
        if char == "__TOGGLE_PAUSE__":
            message = _toggle_watch_pause(state)
            _set_watch_status_message(
                state,
                message,
                transient_seconds=None if state.paused else _WATCH_TUI_TRANSIENT_STATUS_SECONDS,
            )
            _render_watch_shell_screen(state, serial_port=serial_port, baudrate=baudrate, writer=writer)
            state.prompt_visible = True
            continue
        if char in {"__PAGE_UP__", "__PAGE_DOWN__", "__SCROLL_TOP__", "__SCROLL_BOTTOM__"}:
            direction_map = {
                "__PAGE_UP__": "page_up",
                "__PAGE_DOWN__": "page_down",
                "__SCROLL_TOP__": "home",
                "__SCROLL_BOTTOM__": "end",
            }
            if _page_watch_history(state, height=page_height, direction=direction_map[char]):
                _render_watch_shell_screen(state, serial_port=serial_port, baudrate=baudrate, writer=writer)
                state.prompt_visible = True
            continue
        if char == "__AUTO_LOGIN__":
            _set_watch_status_message(state, f"Logging in on {serial_port}...")
            _render_watch_shell_screen(state, serial_port=serial_port, baudrate=baudrate, writer=writer)
            ok, message = login_action()
            _set_watch_status_message(
                state,
                message,
                transient_seconds=_WATCH_TUI_TRANSIENT_STATUS_SECONDS,
            )
            _render_watch_shell_screen(state, serial_port=serial_port, baudrate=baudrate, writer=writer)
            state.prompt_visible = True
            continue
        if char in ("\r", "\n"):
            payload = state.buffer + "\n"
            state.buffer = ""
            ok, message = text_sender(serial_port, baudrate, payload)
            _set_watch_status_message(
                state,
                message,
                transient_seconds=_WATCH_TUI_TRANSIENT_STATUS_SECONDS,
            )
            _render_watch_shell_screen(state, serial_port=serial_port, baudrate=baudrate, writer=writer)
            state.prompt_visible = True
            continue
        if char in ("\b", "\x7f"):
            if state.buffer:
                state.buffer = state.buffer[:-1]
                state.screen_dirty = True
                _render_watch_shell_screen(state, serial_port=serial_port, baudrate=baudrate, writer=writer)
            continue
        if char.isprintable():
            state.buffer += char
            state.screen_dirty = True
            _render_watch_shell_screen(state, serial_port=serial_port, baudrate=baudrate, writer=writer)


def _apply_watch_trace_entry_to_tui_state(
    entry: SerialTraceEntry,
    *,
    show_system: bool,
    state: _WatchStdinShellState,
) -> None:
    if entry.direction == "sys":
        if entry.payload.startswith("SERIAL_CONNECTED"):
            baudrate = entry.payload.partition("baudrate=")[2] or "unknown"
            _set_watch_status_message(
                state,
                f"{entry.port} connected successfully @ {baudrate}",
                transient_seconds=_WATCH_TUI_TRANSIENT_STATUS_SECONDS,
            )
            return
        if entry.payload.startswith("SERIAL_UNAVAILABLE"):
            error_summary = entry.payload.partition("error=")[2] or "serial link unavailable"
            _set_watch_status_message(
                state,
                f"{entry.port} unavailable: {error_summary} | Enter=retry | Ctrl+L=login",
                transient_seconds=_WATCH_TUI_TRANSIENT_STATUS_SECONDS,
            )
            return
        if not show_system:
            if not state.paused:
                state.screen_dirty = True
            return
    _append_watch_recent_line(state, _format_trace_entry(entry))
    if not state.paused:
        state.screen_dirty = True


def _emit_watch_trace_entry(
    entry: SerialTraceEntry,
    *,
    show_system: bool,
    stdin_shell_state: _WatchStdinShellState | None,
    serial_port: str,
    baudrate: int,
) -> None:
    if stdin_shell_state is not None:
        _apply_watch_trace_entry_to_tui_state(entry, show_system=show_system, state=stdin_shell_state)
        if stdin_shell_state.screen_dirty:
            _render_watch_shell_screen(stdin_shell_state, serial_port=serial_port, baudrate=baudrate)
            stdin_shell_state.prompt_visible = True
        return
    printed = _print_watch_trace_entry(entry, show_system=show_system)


def _follow_trace_via_broker(
    args: argparse.Namespace,
    registry,
    *,
    serial_port: str,
    baudrate: int,
    show_system: bool,
    stdin_probe: bool,
    stdin_shell: bool,
    initial_entries: list[SerialTraceEntry] | None = None,
) -> None:
    stream = SerialTraceStreamClient(
        broker_registry=registry,
        serial_port=serial_port,
        timeout=0.1 if (stdin_probe or stdin_shell) else 0.5,
    )
    stdin_shell_state = _WatchStdinShellState() if stdin_shell else None
    try:
        if stdin_shell_state is not None:
            for entry in initial_entries or []:
                _apply_watch_trace_entry_to_tui_state(entry, show_system=show_system, state=stdin_shell_state)
            _render_watch_shell_screen(stdin_shell_state, serial_port=serial_port, baudrate=baudrate)
            stdin_shell_state.prompt_visible = True
        while True:
            if stdin_shell_state is not None and _refresh_watch_status_message(stdin_shell_state):
                _render_watch_shell_screen(stdin_shell_state, serial_port=serial_port, baudrate=baudrate)
                stdin_shell_state.prompt_visible = True
            _consume_watch_stdin_shell(
                serial_port=serial_port,
                baudrate=baudrate,
                enabled=stdin_shell,
                state=stdin_shell_state or _WatchStdinShellState(),
                login_action=lambda: _watch_shell_login_action(
                    args,
                    stdin_shell_state or _WatchStdinShellState(),
                    serial_port=serial_port,
                    baudrate=baudrate,
                ),
            )
            _consume_watch_stdin_probe(
                serial_port=serial_port,
                baudrate=baudrate,
                enabled=stdin_probe and not stdin_shell,
            )
            entry = stream.read_entry(timeout=0.1 if (stdin_probe or stdin_shell) else 0.5)
            if entry is None:
                continue
            _emit_watch_trace_entry(
                entry,
                show_system=show_system,
                stdin_shell_state=stdin_shell_state,
                serial_port=serial_port,
                baudrate=baudrate,
            )
    finally:
        if stdin_shell_state is not None and stdin_shell_state.prompt_visible:
            _clear_watch_stdin_shell_prompt()
        stream.close()


def _command_watch_serial(args: argparse.Namespace) -> int:
    cli_main = _cli_main_module()
    serial_port, baudrate = cli_main._resolve_serial_connection_from_settings(
        args,
        serial_port=getattr(args, "serial_port", None),
        baudrate=getattr(args, "baudrate", None),
    )
    assert serial_port is not None
    trace_path = cli_main.serial_trace_log_path(serial_port)
    initial_entries = cli_main._read_trace_entries(trace_path)
    broker: SerialBroker | None = None
    existing_broker = cli_main.load_serial_broker_registry(serial_port)
    print("[ o.... ] 1/5 steps")
    print(f"[ACTIVE] Watching shared serial trace for {serial_port}")
    print(f"[TODO] Trace log: {trace_path}")
    if args.stdin_shell and not args.follow:
        print("[ERROR] --stdin-shell requires --follow.")
        print("[TODO] Retry with watch-serial --follow --stdin-shell.")
        return 1
    if args.raw_live:
        if existing_broker is None:
            protect_human_session = bool(getattr(args, "protect_human_session", False))
            broker_kwargs: dict[str, Any] = {
                "serial_port": serial_port,
                "baudrate": baudrate,
            }
            if protect_human_session:
                broker_kwargs.update({"owner": "human-observe", "protected": True})
            broker = cli_main.SerialBroker(**broker_kwargs)
            try:
                broker.start()
            except Exception as exc:
                try:
                    broker.stop()
                except Exception:
                    pass
                broker = None
                print("[ xx... ] 2/5 steps")
                print(f"[ERROR] Failed to open raw serial broker on {serial_port}.")
                print(f"[TODO] {exc}")
                for hint in serial_open_recovery_hints(serial_port, exc):
                    print(f"[TODO] {hint}")
                return 1
            existing_broker = cli_main.load_serial_broker_registry(serial_port)
            print(f"[DONE] Raw serial broker started on {serial_port} @ {baudrate}")
            if protect_human_session:
                print("[DONE] Human observation guard enabled; serial-broker stop now requires --force.")
            print(f"[DONE] {serial_port} connected successfully @ {baudrate}")
        else:
            if bool(getattr(args, "protect_human_session", False)) and not cli_main.is_observe_serial_broker(existing_broker):
                protected_broker = cli_main.protect_serial_broker_registry(serial_port)
                if protected_broker is not None:
                    existing_broker = protected_broker
                    print("[DONE] Existing raw serial broker upgraded to a protected observe-serial session.")
            print(
                f"[DONE] Attached to existing raw serial broker on {serial_port} "
                f"via {existing_broker.host}:{existing_broker.tcp_port}"
            )
            if cli_main.is_observe_serial_broker(existing_broker):
                print("[DONE] Human observation guard enabled; serial-broker stop now requires --force.")
        if args.follow:
            if cli_main.is_observe_serial_broker(existing_broker):
                print("[TODO] observe-serial owns the physical COM port; AI clients should attach with watch-serial without --raw-live.")
            else:
                print("[TODO] Raw live follow keeps the physical serial port open until this watcher exits.")
                print(
                    f"[TODO] Release it from another shell with: "
                    f"autodbg serial-broker stop --serial-port {serial_port}"
                )
    elif args.follow and not cli_main.is_observe_serial_broker(existing_broker):
        try:
            existing_broker = cli_main.ensure_observe_serial_broker(serial_port, baudrate=baudrate)
        except Exception as exc:
            print("[ERROR] Failed to open observe-serial for shared serial observation.")
            print(f"[TODO] {exc}")
            return 1
        print(f"[DONE] observe-serial is active for {serial_port}; AI will attach without taking the COM port.")
    if not trace_path.exists():
        print("[TODO] No shared trace file exists yet. observe-serial will create it after the first serial event.")
    elif args.follow and args.tail == 0:
        print("[TODO] Starting from the live edge; existing trace lines are hidden.")
    elif args.tail > 0 and initial_entries:
        print(f"[TODO] Replaying the latest {min(args.tail, len(initial_entries))} trace line(s) first.")
    if args.follow and args.stdin_shell:
        print(f"[TODO] Interactive serial shell enabled on {serial_port}; TUI keeps status and operation hints inside the screen.")
    elif args.follow and args.stdin_probe:
        print(f"[TODO] Press Enter in this window to send a newline probe to {serial_port}.")
    try:
        replay_existing = args.tail > 0
        last_count = len(initial_entries) if args.follow and args.tail == 0 else 0
        seed_entries: list[SerialTraceEntry] = []
        if initial_entries:
            start_index = _resolve_watch_start_index(
                last_count=last_count,
                entry_count=len(initial_entries),
                tail=args.tail,
                replay_existing=replay_existing,
            )
            if args.follow and args.stdin_shell:
                seed_entries = initial_entries[start_index:]
            else:
                _print_trace_entries(initial_entries[start_index:], show_system=args.show_system)
            last_count = len(initial_entries)
            replay_existing = False
        if not args.follow:
            return 0
        live_registry = cli_main.load_serial_broker_registry(serial_port)
        if args.stdin_shell and live_registry is None:
            print("[ERROR] --stdin-shell requires a live broker.")
            print("[TODO] Open observe-serial for this port, then retry with watch-serial --follow --stdin-shell.")
            return 1
        if live_registry is not None:
            print(f"[DONE] Streaming live broker events from {live_registry.host}:{live_registry.tcp_port}")
            cli_main._follow_trace_via_broker(
                args,
                live_registry,
                serial_port=serial_port,
                baudrate=baudrate,
                show_system=args.show_system,
                stdin_probe=args.stdin_probe,
                stdin_shell=args.stdin_shell,
                initial_entries=seed_entries,
            )
            return 0
        while True:
            entries = cli_main._read_trace_entries(trace_path)
            start_index = _resolve_watch_start_index(
                last_count=last_count,
                entry_count=len(entries),
                tail=args.tail,
                replay_existing=replay_existing,
            )
            _print_trace_entries(entries[start_index:], show_system=args.show_system)
            last_count = len(entries)
            replay_existing = False
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("[DONE] Shared serial trace stopped by user")
    finally:
        if broker is not None:
            broker.stop()
    return 0


def _command_serial_broker(args: argparse.Namespace) -> int:
    cli_main = _cli_main_module()
    if args.broker_command == "list":
        registries = cli_main.list_serial_broker_registries(serial_port=args.serial_port)
        print("[ o.... ] 1/5 steps")
        print("[ACTIVE] Inspecting local raw serial brokers")
        if args.serial_port:
            print(f"[TODO] Filter: {args.serial_port}")
        if not registries:
            print("[TODO] No active raw serial brokers found.")
            return 0
        print("[ oooo. ] 4/5 steps")
        print(f"[DONE] Active raw serial brokers: {len(registries)}")
        for registry in registries:
            guard = " protected" if registry.protected else ""
            owner = f" owner={registry.owner}" if registry.owner else ""
            print(
                f"[DONE] {registry.serial_port}: pid={registry.pid} "
                f"tcp={registry.host}:{registry.tcp_port} baudrate={registry.baudrate}{guard}{owner}"
            )
        return 0

    if args.broker_command == "stop":
        if args.all:
            target_ports = [registry.serial_port for registry in cli_main.list_serial_broker_registries()]
        else:
            target_ports = [args.serial_port]
        target_ports = list(dict.fromkeys(target_ports))
        print("[ o.... ] 1/5 steps")
        print("[ACTIVE] Stopping raw serial brokers")
        if not target_ports:
            print("[TODO] No active raw serial brokers found.")
            return 0
        stopped = 0
        protected_skipped = 0
        for port in target_ports:
            try:
                registry = cli_main.stop_serial_broker(port, allow_protected=bool(getattr(args, "force", False)))
            except SerialBrokerProtectedError as exc:
                protected_skipped += 1
                print("[ERROR] Protected human observation broker was not stopped.")
                print(f"[TODO] {exc}")
                print("[TODO] Close the observe-serial window yourself, or rerun with --force after confirming no one is watching.")
                continue
            except Exception as exc:
                print("[ oxx.. ] 2/5 steps")
                print(f"[ERROR] Failed to stop raw serial broker on {port}: {exc}")
                print("[TODO] Retry once or kill the watcher process manually if it is already wedged.")
                return 1
            if registry is None:
                print(f"[TODO] No active raw serial broker found for {port}.")
                continue
            stopped += 1
            print(f"[DONE] Stopped raw serial broker on {port} (pid {registry.pid})")
        print("[ oooo. ] 4/5 steps")
        print(f"[DONE] Raw serial brokers stopped: {stopped}/{len(target_ports)}")
        if protected_skipped:
            print(f"[TODO] Protected human observation brokers skipped: {protected_skipped}")
            return 1
        return 0

    raise ValueError(f"Unsupported serial broker subcommand: {args.broker_command}")
