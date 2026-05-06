# Code Review: auto-debug

- Date: 2026-05-06
- Scope: Full source tree (`src/autodbg/`)
- Reviewer: Claude Code

---

## Project Overview

Embedded device auto-debug scaffold. Provides serial observation, device control, evidence collection, firmware deployment, and MCP Server interface for AI agent integration.

**Tech stack**: Python 3.11+, pyserial, argparse, MCP (NDJSON stdio transport)

**Architecture**: `models` -> `profiles` -> `config` -> `control/serial/deploy` -> `workflows` -> `cli/mcp/agent`

---

## Strengths

| # | Area | Detail |
|---|------|--------|
| 1 | Architecture | Clean layered design with clear separation of concerns |
| 2 | Serial concurrency | File lock + broker pattern for multi-process COM port sharing; stale lock auto-cleanup |
| 3 | Windows compat | Correct use of Win32 ctypes API (OpenProcess, GetDriveTypeW, GetVolumeInformationW) |
| 4 | Observability | Serial trace JSONL, session artifacts, summary/report generation form a complete audit trail |
| 5 | MCP protocol | NDJSON + backward-compatible LSP framing detection |
| 6 | Agent intake | `autodbg_prepare` validates parameter completeness before execution and generates user-facing prompts |

---

## Issues

### P1 — Critical

#### 1. `cli/main.py` is excessively large (64k+ tokens)

- **File**: `src/autodbg/cli/main.py`
- **Problem**: A single file contains the implementation of every subcommand (run, observe, exec, health, fetch-file, device-pull, stage-sd, bootstrap-network, watch-serial, serve-artifacts, report, etc.). This makes the file extremely difficult to navigate, review, and maintain.
- **Suggestion**: Extract each subcommand into its own module under `cli/`, e.g.:
  ```
  cli/
    __init__.py
    main.py          # parser construction + dispatch only
    cmd_run.py
    cmd_observe.py
    cmd_exec.py
    cmd_health.py
    cmd_watch.py
    cmd_deploy.py
    cmd_transport.py
    cmd_session.py
  ```
  `main.py` should only build the `argparse` parser tree and dispatch to the appropriate handler.

---

### P2 — Medium

#### 2. `_sha256_file` duplicated across modules

- **Files**:
  - `src/autodbg/deploy/deployer.py:92`
  - `src/autodbg/host/artifact_server.py:403`
- **Problem**: Identical function defined in two places. Any future change (e.g., switching to `hashlib.file_digest` in Python 3.11+) must be applied twice.
- **Suggestion**: Extract to a shared utility, e.g. `utils/hash.py`.

#### 3. `_pid_is_running` duplicated with inconsistent logic

- **Files**:
  - `src/autodbg/serial/runtime.py:462` — uses `OpenProcess(0x1000)`, treats non-zero handle as alive
  - `src/autodbg/host/artifact_server.py:357` — additionally checks `GetExitCodeProcess` for exit code 259 (`STILL_ACTIVE`)
- **Problem**: Two implementations with subtly different behavior. The `runtime.py` version can report a terminated process as alive if its handle is still valid but the process has exited.
- **Suggestion**: Unify into a single implementation. Prefer the `artifact_server.py` version (checking exit code 259) as the canonical one, and place it in a shared utility.

#### 4. Race condition in serial port file lock

- **File**: `src/autodbg/serial/runtime.py:261-283`
- **Problem**: `_acquire_port_lock` uses `O_CREAT | O_EXCL` for lock creation and recursively retries after cleaning a stale lock. If two processes simultaneously detect and remove the same stale lock, both will succeed in creating the lock file, defeating the mutual exclusion.
- **Suggestion**:
  - On Windows, prefer a named mutex (`CreateMutexW`) for robust cross-process locking.
  - Alternatively, add a small random delay before retry, or use `msvcrt.locking` / `fcntl.flock` for atomic lock acquisition.

---

### P3 — Low Risk

#### 5. `_terminate_pid` uses `SIGTERM` on Windows

- **File**: `src/autodbg/serial/runtime.py:430-433`
- **Problem**: On Windows, `os.kill(pid, signal.SIGTERM)` is equivalent to `TerminateProcess`, which forcefully kills the process without cleanup (no `atexit` handlers, no flush, no context manager `__exit__`). This may leave serial ports or lock files in an inconsistent state.
- **Suggestion**: If graceful shutdown is desired, use `GenerateConsoleCtrlEvent` with `CTRL_BREAK_EVENT`. If forceful termination is intentional, add a comment documenting this behavior.

#### 6. Inconsistent marker matching strategy in `SerialObserver`

- **File**: `src/autodbg/serial/observer.py:241-283`
- **Problem**:
  - `fatal_markers` and `success_markers`: case-insensitive (`marker.lower() in line.lower()`)
  - `app_ready_markers` and `app_start_markers`: case-sensitive (`marker in line`)
  - This inconsistency is not documented and may cause unexpected classification results.
- **Suggestion**: Either unify to a single matching strategy (preferably case-insensitive), or allow the profile to specify per-marker case sensitivity.

#### 7. Variable redeclaration in `host/storage.py`

- **File**: `src/autodbg/host/storage.py:65-66` and `84-85`
- **Problem**: `total_bytes: int | None = None` and `free_bytes: int | None = None` are declared in both the `if os.name != "nt"` and `else` branches. While functionally correct, some type checkers or linters may emit redefinition warnings.
- **Suggestion**: Declare the variables once before the `if/else` block.

---

### P4 — Defensive / Hardening

#### 8. Missing validation on `Content-Length` in MCP message reader

- **File**: `src/autodbg/mcp/server.py:332-346`
- **Problem**: When falling back to LSP-style framing, the `Content-Length` header value is passed directly to `int()` and then to `stream.read()`. A malicious or malformed value (negative number, extremely large number) could cause unexpected behavior.
- **Suggestion**:
  ```python
  length = int(length_text)
  if length <= 0 or length > 10 * 1024 * 1024:  # 10 MB cap
      raise json.JSONDecodeError("Invalid Content-Length", "", 0)
  ```

#### 9. Duplicate `autodbg/` package at project root

- **Files**:
  - `autodbg/__init__.py`, `autodbg/__main__.py` (project root)
  - `src/autodbg/` (full package)
- **Problem**: `pyproject.toml` sets `package-dir = {"" = "src"}`, so `pip install` only picks up `src/autodbg`. The root-level `autodbg/` may shadow the installed package when running from the project directory, causing import confusion.
- **Suggestion**: Verify the root-level `autodbg/` is still needed. If it exists only for historical reasons, remove it. If it serves as a dev-time shortcut, document this in `README.md`.

#### 10. Runtime dependencies declared as optional only

- **File**: `pyproject.toml:14-23`
- **Problem**: `dependencies = []` with all actual dependencies under `[project.optional-dependencies] full`. A bare `pip install auto-debug` installs no dependencies, and most commands will fail with `ImportError` at runtime.
- **Suggestion**: If this is intentional (to allow partial usage without pyserial), document it clearly. Otherwise, move the core dependencies to `dependencies` and keep only truly optional ones in extras.

---

## Action Items Summary

| Priority | Issue | Effort | Impact |
|----------|-------|--------|--------|
| P1 | Split `cli/main.py` into per-command modules | High | Maintainability |
| P2 | Deduplicate `_sha256_file` | Low | Code hygiene |
| P2 | Unify `_pid_is_running` | Low | Correctness |
| P2 | Fix serial lock race condition | Medium | Reliability |
| P3 | Document `_terminate_pid` behavior on Windows | Low | Clarity |
| P3 | Unify marker matching case sensitivity | Low | Correctness |
| P3 | Fix variable redeclaration in `storage.py` | Low | Code hygiene |
| P4 | Validate `Content-Length` in MCP reader | Low | Security |
| P4 | Remove or document root-level `autodbg/` | Low | Dev experience |
| P4 | Clarify dependency installation | Low | Onboarding |
