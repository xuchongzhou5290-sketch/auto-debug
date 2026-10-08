from __future__ import annotations

import os

if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
    import ctypes
    from ctypes import wintypes

PID_ALIVE = "alive"
PID_DEAD = "dead"
PID_UNKNOWN = "unknown"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87  # OpenProcess: no process with this pid


def pid_state(pid: int) -> str:
    """PID_DEAD only when the OS says the pid does not exist (or has exited); PID_UNKNOWN when we may not look.

    Access denied (caller in a sandbox / another token or integrity level) or any other API failure is
    PID_UNKNOWN, never PID_DEAD: callers use this to decide whether another process's broker registry or lock
    file is stale and may be deleted.
    """
    if pid <= 0:
        return PID_DEAD
    if os.name == "nt":
        return _windows_pid_state(int(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return PID_DEAD
    except PermissionError:
        # EPERM: the process exists but belongs to someone else
        return PID_ALIVE
    except OSError:
        return PID_UNKNOWN
    return PID_ALIVE


def pid_is_running(pid: int) -> bool:
    """False only when the pid is definitely gone (PID_UNKNOWN counts as running)."""
    return pid_state(pid) != PID_DEAD


def _windows_pid_state(pid: int) -> str:  # pragma: no cover - exercised on Windows hosts
    if pid > 0xFFFFFFFF:
        # not a Windows pid; OpenProcess would silently truncate it to another process's pid
        return PID_DEAD
    handle, error = _open_process(pid)
    if not handle:
        return PID_DEAD if error == _ERROR_INVALID_PARAMETER else PID_UNKNOWN
    try:
        exit_code = _get_exit_code(handle)
        if exit_code is None:
            return PID_UNKNOWN
        return PID_ALIVE if exit_code == _STILL_ACTIVE else PID_DEAD
    finally:
        _kernel32().CloseHandle(handle)


_KERNEL32 = None


def _kernel32():  # pragma: no cover - exercised on Windows hosts
    global _KERNEL32
    if _KERNEL32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32 = kernel32
    return _KERNEL32


def _open_process(pid: int) -> tuple[int | None, int]:  # pragma: no cover - exercised on Windows hosts
    """Return (handle or None, GetLastError) for OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)."""
    handle = _kernel32().OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        return handle, 0
    return None, ctypes.get_last_error()


def _get_exit_code(handle) -> int | None:  # pragma: no cover - exercised on Windows hosts
    exit_code = wintypes.DWORD()
    if not _kernel32().GetExitCodeProcess(handle, ctypes.byref(exit_code)):
        return None
    return int(exit_code.value)
