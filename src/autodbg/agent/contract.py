from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import os
from pathlib import Path
from typing import Any, Callable, Iterator
import json
import sys


AGENT_SCHEMA_VERSION = 1

_PROFILE_ACTIONS = {
    "run",
    "stage-sd",
    "bootstrap-network",
    "device-pull",
    "deploy-verify",
    "observe",
    "exec",
    "fetch-file",
    "fetch-path",
    "collect-evidence",
    "health",
}

_ACTION_ALIASES: dict[str, tuple[str, ...]] = {
    "serial-broker-list": ("serial-broker", "list"),
    "serial-broker-stop": ("serial-broker", "stop"),
    "artifact-server-list": ("artifact-server", "list"),
    "artifact-server-stop": ("artifact-server", "stop"),
}

_CONNECTION_ENV_MAP = {
    "device_password": "AUTO_DBG_DEVICE_PASSWORD",
    "login_prompt": "AUTO_DBG_LOGIN_PROMPT",
    "shell_prompt": "AUTO_DBG_SHELL_PROMPT",
    "host_ip": "AUTO_DBG_HOST_IP",
    "preferred_interfaces": "AUTO_DBG_PREFERRED_INTERFACES",
    "expected_ip": "AUTO_DBG_EXPECTED_IP",
    "pull_base_url": "AUTO_DBG_PULL_BASE_URL",
    "pull_workspace": "AUTO_DBG_PULL_WORKSPACE",
    "wifi_ssid": "AUTO_DBG_WIFI_SSID",
    "wifi_password": "AUTO_DBG_WIFI_PASSWORD",
    "wifi_mode": "AUTO_DBG_WIFI_MODE",
    "network_dir": "AUTO_DBG_NETWORK_DIR",
    "sdcard_drive": "AUTO_DBG_SDCARD_DRIVE",
    "retrieved_root": "AUTO_DBG_RETRIEVED_ROOT",
}

_FIELD_PROMPTS: dict[str, dict[str, Any]] = {
    "action": {
        "target": "request",
        "question": "你希望我执行哪类 auto-debug 动作？例如观察串口、运行启动检查、健康检查、拉取文件、下发文件或查看报告。",
        "example": "run",
    },
    "serial_port": {
        "target": "connection",
        "question": "请提供目标设备串口号，例如 COM24。",
        "example": "COM24",
    },
    "baudrate": {
        "target": "connection",
        "question": "请提供串口波特率；如果不确定，可以先用默认配置。",
        "example": 921600,
    },
    "device_password": {
        "target": "connection",
        "question": "这个动作需要登录设备 shell，请提供设备登录密码；如果设备没有 shell，请不要继续跑 shell 类动作。",
        "example": "your-root-password",
        "sensitive": True,
    },
    "wifi_ssid": {
        "target": "connection",
        "question": "请提供设备要连接的 Wi-Fi SSID。",
        "example": "xiaotudou",
    },
    "wifi_password": {
        "target": "connection",
        "question": "请提供设备要连接的 Wi-Fi 密码。",
        "example": "12345678",
        "sensitive": True,
    },
    "wifi_mode": {
        "target": "connection",
        "question": "请提供 Wi-Fi 认证模式；常见值是 WPA2。",
        "example": "WPA2",
    },
    "host_ip": {
        "target": "connection",
        "question": "请确认 PC 提供 artifact 服务时设备可访问的本机 IP。",
        "example": "192.168.1.2",
    },
    "pull_base_url": {
        "target": "connection",
        "question": "如果已经有 artifact HTTP 服务，请提供设备可访问的 base URL。",
        "example": "http://192.168.1.2:8765",
    },
    "sdcard_drive": {
        "target": "connection",
        "question": "请提供本机 SD 卡盘符，用于 stage-sd 落盘。",
        "example": "E:",
    },
    "shell_command": {
        "target": "options",
        "question": "请提供要在设备 shell 上执行的命令。",
        "example": "ls /mnt/sdcard",
    },
    "remote_path": {
        "target": "options",
        "question": "请提供要从设备拉取的远端路径。",
        "example": "/mnt/sdcard/logprint",
    },
    "source": {
        "target": "options",
        "question": "请提供要下发或复制的本地源文件路径。",
        "example": "payloads\\APP_FT.bin",
    },
    "artifact": {
        "target": "options",
        "question": "请提供本轮要部署或验证的本地产物路径。",
        "example": "payloads\\APP_FT.bin",
    },
    "session_dir": {
        "target": "options",
        "question": "请提供要读取或记录的 session 目录。",
        "example": "artifacts/20260420/091530123-demo",
    },
    "kind": {
        "target": "options",
        "question": "请提供干预类型，例如 ai_patch、human_action 或 config_change。",
        "example": "ai_patch",
    },
    "summary": {
        "target": "options",
        "question": "请提供本次干预的一句话摘要。",
        "example": "Adjusted serial login timeout",
    },
    "port": {
        "target": "options",
        "question": "请提供要操作的 artifact server TCP 端口。",
        "example": 8765,
    },
    "serial_port_or_all": {
        "target": "options",
        "question": "请指定要释放的串口号，或确认是否停止所有 raw serial broker。",
        "example": "serial_port=COM24",
    },
    "session_dir_or_latest": {
        "target": "options",
        "question": "请指定 session_dir，或确认使用 latest 查看最新 session 报告。",
        "example": "latest=true",
    },
}

_REQUIRED_ACTION_OPTIONS: dict[str, list[str]] = {
    "exec": ["shell_command"],
    "fetch-file": ["remote_path"],
    "fetch-path": ["remote_path"],
    "stage-sd": ["source"],
    "summary": ["session_dir"],
    "resume": ["session_dir"],
    "record-intervention": ["session_dir", "kind", "summary"],
    "artifact-server-stop": ["port"],
}

_ONE_OF_ACTION_OPTIONS: dict[str, list[dict[str, Any]]] = {
    "report": [{"id": "session_dir_or_latest", "fields": ["session_dir", "latest"]}],
    "serial-broker-stop": [{"id": "serial_port_or_all", "fields": ["serial_port", "all"]}],
}

_DEFAULT_ACTION_SUGGESTIONS = [
    {"action": "watch-serial", "when": "用户想实时看串口或确认设备是否在刷日志"},
    {"action": "run", "when": "用户想跑一轮启动观察、检查和结构化 verdict"},
    {"action": "health", "when": "用户想审计设备进程、SD、网络等健康状态"},
    {"action": "deploy-verify", "when": "用户想闭环执行构建、部署、重启、串口观察和目标验证"},
    {"action": "fetch-file", "when": "用户想从设备拉取单个文件"},
    {"action": "device-pull", "when": "用户想让设备从 PC artifact server 拉取文件"},
    {"action": "report", "when": "用户想查看已有 session 报告"},
]

_ACTION_METADATA: dict[str, dict[str, Any]] = {
    "run": {
        "category": "session",
        "summary": "Observe startup, run baseline checks, collect default evidence, and return a machine verdict.",
        "required_connection": ["serial_port"],
        "recommended_connection": ["device_password"],
        "common_options": ["observe_seconds", "skip_evidence", "evidence_timeout"],
        "creates_session": True,
    },
    "observe": {
        "category": "serial",
        "summary": "Capture serial output into a session with optional live streaming and marker focus.",
        "required_connection": ["serial_port"],
        "recommended_connection": [],
        "common_options": ["seconds", "live", "follow", "markers_only", "focus", "poke_newline"],
        "creates_session": True,
    },
    "watch-serial": {
        "category": "serial",
        "summary": "Follow the shared serial trace or broker-backed raw-live TUI without reopening the physical COM port.",
        "required_connection": ["serial_port"],
        "recommended_connection": [],
        "common_options": ["tail", "follow", "show_system", "raw_live", "baudrate", "stdin_probe", "stdin_shell"],
        "creates_session": False,
    },
    "exec": {
        "category": "control",
        "summary": "Login over serial and execute one shell command.",
        "required_connection": ["serial_port", "device_password"],
        "recommended_connection": [],
        "common_options": ["shell_command", "timeout"],
        "creates_session": True,
    },
    "health": {
        "category": "control",
        "summary": "Run the health probe bundle and summarize storage/process/network readiness.",
        "required_connection": ["serial_port", "device_password"],
        "recommended_connection": [],
        "common_options": ["timeout", "skip_sd_write_probe"],
        "creates_session": True,
    },
    "collect-evidence": {
        "category": "evidence",
        "summary": "Run evidence commands, fetch selected files, and archive the results into a session.",
        "required_connection": ["serial_port", "device_password"],
        "recommended_connection": [],
        "common_options": ["shell_command", "remote_file", "skip_defaults", "timeout"],
        "creates_session": True,
    },
    "fetch-file": {
        "category": "evidence",
        "summary": "Fetch one device-side file over the serial shell.",
        "required_connection": ["serial_port", "device_password"],
        "recommended_connection": [],
        "common_options": ["remote_path", "output", "timeout"],
        "creates_session": True,
    },
    "fetch-path": {
        "category": "evidence",
        "summary": "Fetch a device-side file or directory; directories are returned as tar streams.",
        "required_connection": ["serial_port", "device_password"],
        "recommended_connection": [],
        "common_options": ["remote_path", "output", "timeout"],
        "creates_session": True,
    },
    "bootstrap-network": {
        "category": "transport",
        "summary": "Bring networking up on the device before LAN-based pull or debugging.",
        "required_connection": ["serial_port", "device_password"],
        "recommended_connection": ["wifi_ssid", "wifi_password", "wifi_mode", "network_dir"],
        "common_options": ["mode", "bootstrap_command", "check_command", "timeout"],
        "creates_session": True,
    },
    "serve-artifacts": {
        "category": "transport",
        "summary": "Expose a local payload directory over HTTP for device pull; defaults to a non-blocking background server.",
        "required_connection": [],
        "recommended_connection": ["host_ip", "pull_base_url"],
        "common_options": [
            "root",
            "bind",
            "port",
            "base_url",
            "duration_seconds",
            "foreground",
            "startup_timeout",
            "health_name",
            "no_auto_port",
            "workspace",
        ],
        "creates_session": False,
    },
    "device-pull": {
        "category": "transport",
        "summary": "Serve local artifacts and trigger a device-side pull via LAN or serial bundle fallback.",
        "required_connection": ["serial_port", "device_password"],
        "recommended_connection": ["wifi_ssid", "wifi_password", "wifi_mode", "host_ip", "pull_base_url"],
        "common_options": [
            "mode",
            "transfer_mode",
            "workspace",
            "port",
            "bind",
            "list_command",
            "post_pull_command",
            "reboot_command",
            "post_observe_seconds",
            "validation_command",
            "expect_marker",
            "reject_marker",
            "expected_version",
            "timeout",
        ],
        "creates_session": True,
    },
    "deploy-verify": {
        "category": "workflow",
        "summary": "Run one closed debug loop: optional host build, device deploy/apply commands, restart, serial observation, and target-specific validation.",
        "required_connection": ["serial_port", "device_password"],
        "recommended_connection": ["host_ip", "pull_base_url"],
        "common_options": [
            "build_command",
            "artifact",
            "post_pull_command",
            "reboot_command",
            "observe_seconds",
            "validation_command",
            "expect_marker",
            "reject_marker",
            "expected_version",
            "git_commit",
            "changed_file",
            "expected_effect",
            "timeout",
        ],
        "creates_session": True,
    },
    "stage-sd": {
        "category": "transport",
        "summary": "Copy a local file onto the configured SD card drive for manual or device-side pickup.",
        "required_connection": ["sdcard_drive"],
        "recommended_connection": [],
        "common_options": ["source", "target_subdir", "dest_name", "no_verify", "allow_non_removable"],
        "creates_session": True,
    },
    "storage": {
        "category": "transport",
        "summary": "Inspect host drive letters and removable-drive candidates for SD staging.",
        "required_connection": [],
        "recommended_connection": ["sdcard_drive"],
        "common_options": [],
        "creates_session": False,
    },
    "artifact-server-list": {
        "category": "transport",
        "summary": "List registered background artifact servers.",
        "required_connection": [],
        "recommended_connection": [],
        "common_options": ["port"],
        "creates_session": False,
    },
    "artifact-server-stop": {
        "category": "transport",
        "summary": "Stop a background artifact server started by serve-artifacts.",
        "required_connection": [],
        "recommended_connection": [],
        "common_options": ["port"],
        "creates_session": False,
    },
    "serial-broker-list": {
        "category": "serial",
        "summary": "List active raw serial brokers.",
        "required_connection": [],
        "recommended_connection": ["serial_port"],
        "common_options": ["serial_port"],
        "creates_session": False,
    },
    "serial-broker-stop": {
        "category": "serial",
        "summary": "Stop one or more raw serial brokers and release the physical COM port.",
        "required_connection": [],
        "recommended_connection": ["serial_port"],
        "common_options": ["serial_port", "all"],
        "creates_session": False,
    },
    "report": {
        "category": "session",
        "summary": "Print report.md for a session or the latest session.",
        "required_connection": [],
        "recommended_connection": [],
        "common_options": ["session_dir", "latest", "artifacts_root"],
        "creates_session": False,
    },
    "summary": {
        "category": "session",
        "summary": "Print summary.json for a session.",
        "required_connection": [],
        "recommended_connection": [],
        "common_options": ["session_dir"],
        "creates_session": False,
    },
    "resume": {
        "category": "session",
        "summary": "Alias for reading a stored session summary.",
        "required_connection": [],
        "recommended_connection": [],
        "common_options": ["session_dir"],
        "creates_session": False,
    },
    "record-intervention": {
        "category": "session",
        "summary": "Append a structured intervention record onto an existing session for the next debug iteration.",
        "required_connection": [],
        "recommended_connection": [],
        "common_options": [
            "session_dir",
            "kind",
            "summary",
            "details",
            "file",
            "git_commit",
            "expected_effect",
            "related_session",
            "metadata_json",
        ],
        "creates_session": False,
    },
    "show-mvp": {
        "category": "meta",
        "summary": "Print the MVP workflow entry point and reference document.",
        "required_connection": [],
        "recommended_connection": [],
        "common_options": [],
        "creates_session": False,
    },
    "ports": {
        "category": "meta",
        "summary": "List host serial ports using pyserial.",
        "required_connection": [],
        "recommended_connection": [],
        "common_options": [],
        "creates_session": False,
    },
}


class AgentCallError(ValueError):
    """Raised when an agent request cannot be translated into a valid invocation."""


@dataclass(slots=True)
class AgentInvocation:
    action: str
    argv: list[str]
    env_updates: dict[str, str]
    artifacts_root: Path | None
    response_options: dict[str, Any]


def load_agent_request(request: str | Path | None) -> dict[str, Any]:
    if request is None or str(request) == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(request).read_text(encoding="utf-8")
    if not raw.strip():
        raise AgentCallError("Agent request JSON is empty.")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise AgentCallError("Agent request must be a JSON object.")
    schema_version = payload.get("schema_version", AGENT_SCHEMA_VERSION)
    if schema_version != AGENT_SCHEMA_VERSION:
        raise AgentCallError(
            f"Unsupported schema_version={schema_version}; expected {AGENT_SCHEMA_VERSION}."
        )
    return payload


def build_agent_invocation(request: dict[str, Any], *, project_root: Path) -> AgentInvocation:
    action = str(request.get("action", "")).strip()
    if not action:
        raise AgentCallError("Agent request is missing action.")

    if action == "agent-call":
        raise AgentCallError("Nested agent-call requests are not allowed.")

    action_tokens = list(_ACTION_ALIASES.get(action, (action,)))
    profiles = _ensure_mapping(request.get("profiles"), label="profiles")
    connection = _ensure_mapping(request.get("connection"), label="connection")
    options = _ensure_mapping(request.get("options"), label="options")
    loop = _ensure_mapping(request.get("loop"), label="loop")
    response = _ensure_mapping(request.get("response"), label="response")

    argv = list(action_tokens)
    command_name = action_tokens[0]

    if command_name in _PROFILE_ACTIONS:
        _append_path_option(argv, "--device", profiles.get("device"), project_root=project_root)
        _append_path_option(argv, "--model", profiles.get("model"), project_root=project_root)
        _append_path_option(argv, "--task", profiles.get("task"), project_root=project_root)
        _append_path_option(argv, "--transport", profiles.get("transport"), project_root=project_root)
        _append_path_option(argv, "--profiles-defaults", profiles.get("profiles_defaults"), project_root=project_root)
        _append_path_option(argv, "--artifacts-root", profiles.get("artifacts_root"), project_root=project_root)
        _append_path_option(argv, "--settings", profiles.get("settings"), project_root=project_root)
        _append_option(argv, "--serial-port", connection.get("serial_port"))
        _append_option(argv, "--baudrate", connection.get("baudrate"))
        _append_option(argv, "--goal-id", loop.get("goal_id"))
        _append_option(argv, "--goal", loop.get("goal"))
        _append_path_option(argv, "--prev-session", loop.get("prev_session"), project_root=project_root)
        _append_option(argv, "--iteration", loop.get("iteration"))
        _append_option(argv, "--max-iterations", loop.get("max_iterations"))
        _append_option(argv, "--attempt-note", loop.get("attempt_note"))
    elif command_name == "storage":
        _append_path_option(argv, "--device", profiles.get("device"), project_root=project_root)
        _append_path_option(argv, "--settings", profiles.get("settings"), project_root=project_root)
    elif command_name == "report":
        _append_path_option(argv, "--artifacts-root", profiles.get("artifacts_root"), project_root=project_root)
    elif command_name == "watch-serial":
        _append_option(argv, "--serial-port", options.get("serial_port", connection.get("serial_port")))
        _append_option(argv, "--baudrate", options.get("baudrate", connection.get("baudrate")))
    elif command_name == "serial-broker":
        if len(action_tokens) < 2:
            raise AgentCallError("serial-broker action must include list or stop.")
        broker_action = action_tokens[1]
        if broker_action == "list":
            _append_option(argv, "--serial-port", options.get("serial_port", connection.get("serial_port")))
        elif broker_action == "stop" and not options.get("all"):
            _append_option(argv, "--serial-port", options.get("serial_port", connection.get("serial_port")))

    for key, value in options.items():
        if command_name == "watch-serial" and key in {"serial_port", "baudrate"}:
            continue
        if command_name == "serial-broker" and key == "serial_port":
            continue
        flag = "--" + key.replace("_", "-")
        if key in {"source", "root", "output", "session_dir", "prev_session", "artifact", "changed_file"}:
            _append_path_option(argv, flag, value, project_root=project_root)
            continue
        if key == "file":
            _append_path_option(argv, flag, value, project_root=project_root)
            continue
        _append_option(argv, flag, value)

    env_updates = _build_agent_environment(connection)
    artifacts_root = _agent_artifacts_root(command_name, profiles, project_root)
    return AgentInvocation(
        action=action,
        argv=argv,
        env_updates=env_updates,
        artifacts_root=artifacts_root,
        response_options=response,
    )


def build_agent_response(
    invocation: AgentInvocation,
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    wrapper_error: dict[str, str] | None,
    project_root: Path,
    session_dir: Path | None = None,
) -> dict[str, Any]:
    if session_dir is None and invocation.action in {"report", "summary", "resume", "record-intervention"}:
        session_dir = _resolve_session_dir(
            action=invocation.action,
            argv=invocation.argv,
            artifacts_root=invocation.artifacts_root,
        )
    summary_path = session_dir / "summary.json" if session_dir is not None else None
    report_path = session_dir / "report.md" if session_dir is not None else None
    manifest_path = session_dir / "artifacts_manifest.json" if session_dir is not None else None

    include_summary = invocation.response_options.get("include_summary", True)
    include_stdout = invocation.response_options.get("include_stdout", True)
    include_stderr = invocation.response_options.get("include_stderr", True)

    summary = None
    if include_summary and summary_path is not None and summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))

    ok = wrapper_error is None and exit_code == 0
    error = wrapper_error
    if error is None and not ok:
        error = {
            "type": "CommandFailed",
            "message": _last_non_empty_line(stderr) or _last_non_empty_line(stdout) or f"Command exited with {exit_code}.",
        }

    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "ok": ok,
        "action": invocation.action,
        "argv": invocation.argv,
        "exit_code": exit_code,
        "project_root": str(project_root),
        "artifacts_root": str(invocation.artifacts_root) if invocation.artifacts_root is not None else None,
        "session_dir": str(session_dir) if session_dir is not None else None,
        "summary_path": str(summary_path) if summary_path is not None and summary_path.is_file() else None,
        "report_path": str(report_path) if report_path is not None and report_path.is_file() else None,
        "artifacts_manifest_path": str(manifest_path) if manifest_path is not None and manifest_path.is_file() else None,
        "summary": summary,
        "stdout": stdout if include_stdout else None,
        "stderr": stderr if include_stderr else None,
        "error": error,
    }


def build_agent_error_response(*, project_root: Path, error: Exception) -> dict[str, Any]:
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "ok": False,
        "action": None,
        "argv": None,
        "exit_code": 1,
        "project_root": str(project_root),
        "artifacts_root": None,
        "session_dir": None,
        "summary_path": None,
        "report_path": None,
        "artifacts_manifest_path": None,
        "summary": None,
        "stdout": None,
        "stderr": None,
        "error": {"type": type(error).__name__, "message": str(error)},
    }


def execute_agent_request(
    request: dict[str, Any],
    *,
    project_root: Path,
    parser_builder: Callable[[], Any],
    dispatcher: Callable[[Any], int],
) -> dict[str, Any]:
    invocation = build_agent_invocation(request, project_root=project_root)
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    wrapper_error: dict[str, str] | None = None
    exit_code = 1
    before_sessions = list_session_dirs(invocation.artifacts_root)
    with temporary_agent_environment(invocation.env_updates):
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            try:
                parser = parser_builder()
                nested_args = parser.parse_args(invocation.argv)
                exit_code = dispatcher(nested_args)
            except SystemExit as exc:
                exit_code = int(exc.code) if isinstance(exc.code, int) else 1
            except Exception as exc:  # pragma: no cover - guarded by structured response
                wrapper_error = {"type": type(exc).__name__, "message": str(exc)}
                exit_code = 1
    after_sessions = list_session_dirs(invocation.artifacts_root)
    new_sessions = after_sessions - before_sessions
    detected_session_dir = None
    if new_sessions:
        detected_session_dir = max(
            new_sessions,
            key=lambda path: path.relative_to(invocation.artifacts_root).as_posix(),
        )
    return build_agent_response(
        invocation,
        exit_code=exit_code,
        stdout=stdout_buffer.getvalue(),
        stderr=stderr_buffer.getvalue(),
        wrapper_error=wrapper_error,
        project_root=project_root,
        session_dir=detected_session_dir,
    )


@contextmanager
def temporary_agent_environment(overrides: dict[str, str]) -> Iterator[None]:
    marker = object()
    previous: dict[str, object] = {}
    for key, value in overrides.items():
        previous[key] = os.environ.get(key, marker)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is marker:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)


def list_session_dirs(artifacts_root: Path | None) -> set[Path]:
    if artifacts_root is None or not artifacts_root.exists():
        return set()
    return {
        session_dir
        for day_dir in artifacts_root.iterdir()
        if day_dir.is_dir()
        for session_dir in day_dir.iterdir()
        if session_dir.is_dir()
    }


def _build_agent_environment(connection: dict[str, Any]) -> dict[str, str]:
    env_updates: dict[str, str] = {}
    for key, env_name in _CONNECTION_ENV_MAP.items():
        value = connection.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            text = ",".join(str(item) for item in value)
        else:
            text = str(value)
        env_updates[env_name] = text
    return env_updates


def _agent_artifacts_root(command_name: str, profiles: dict[str, Any], project_root: Path) -> Path | None:
    if command_name not in _PROFILE_ACTIONS and command_name != "report":
        return None
    raw = profiles.get("artifacts_root")
    if raw is None:
        return project_root / "artifacts"
    return _resolve_local_path(raw, project_root=project_root)


def _append_path_option(argv: list[str], flag: str, value: Any, *, project_root: Path) -> None:
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            _append_path_option(argv, flag, item, project_root=project_root)
        return
    argv.extend([flag, str(_resolve_local_path(value, project_root=project_root))])


def _append_option(argv: list[str], flag: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        if value:
            argv.append(flag)
        return
    if isinstance(value, list):
        for item in value:
            _append_option(argv, flag, item)
        return
    argv.extend([flag, str(value)])


def _resolve_session_dir(action: str, argv: list[str], artifacts_root: Path | None) -> Path | None:
    if action in {"summary", "resume", "record-intervention"}:
        session_dir = _extract_flag_value(argv, "--session-dir")
        return Path(session_dir).absolute() if session_dir else None
    if action == "report":
        session_dir = _extract_flag_value(argv, "--session-dir")
        if session_dir:
            return Path(session_dir).absolute()
        if "--latest" in argv and artifacts_root is not None and artifacts_root.exists():
            return max(
                (
                    session_dir
                    for day_dir in artifacts_root.iterdir()
                    if day_dir.is_dir()
                    for session_dir in day_dir.iterdir()
                    if session_dir.is_dir()
                ),
                default=None,
                key=lambda path: path.relative_to(artifacts_root).as_posix(),
            )
        return None
    if artifacts_root is None or not artifacts_root.exists():
        return None
    session_dirs = [
        session_dir
        for day_dir in artifacts_root.iterdir()
        if day_dir.is_dir()
        for session_dir in day_dir.iterdir()
        if session_dir.is_dir()
    ]
    if not session_dirs:
        return None
    return max(session_dirs, key=lambda path: path.relative_to(artifacts_root).as_posix())


def _extract_flag_value(argv: list[str], flag: str) -> str | None:
    for index, token in enumerate(argv[:-1]):
        if token == flag:
            return argv[index + 1]
    return None


def _last_non_empty_line(text: str) -> str | None:
    for line in reversed(text.splitlines()):
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return None


def _ensure_mapping(raw: Any, *, label: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentCallError(f"Agent request field {label} must be a JSON object.")
    return raw


def _resolve_local_path(value: Any, *, project_root: Path) -> Path:
    candidate = Path(str(value))
    if candidate.is_absolute():
        return candidate
    return project_root / candidate


def build_agent_intake_plan(request: dict[str, Any] | None, *, project_root: Path) -> dict[str, Any]:
    payload = request or {}
    if not isinstance(payload, dict):
        raise AgentCallError("Agent intake request must be a JSON object.")

    connection = _ensure_mapping(payload.get("connection"), label="connection")
    options = _ensure_mapping(payload.get("options"), label="options")
    profiles = _ensure_mapping(payload.get("profiles"), label="profiles")
    loop = _ensure_mapping(payload.get("loop"), label="loop")

    explicit_action = str(payload.get("action", "")).strip()
    inferred_action = None if explicit_action else _infer_action_from_goal(str(payload.get("goal") or loop.get("goal") or ""))
    action = explicit_action or inferred_action
    action = _canonical_action_name(action) if action else ""

    missing_required: list[dict[str, Any]] = []
    recommended: list[dict[str, Any]] = []
    supplied: list[dict[str, Any]] = []

    if not action:
        missing_required.append(_build_intake_prompt("action", required=True, reason="No action was provided or inferred from the goal."))
    elif action not in _ACTION_METADATA:
        raise AgentCallError(f"Unsupported action for intake: {action}.")
    else:
        metadata = _ACTION_METADATA[action]
        for field in metadata.get("required_connection", []):
            source = _connection_value_source(field, connection)
            if source is None:
                missing_required.append(
                    _build_intake_prompt(
                        field,
                        required=True,
                        reason=f"{action} requires connection.{field}.",
                    )
                )
            else:
                supplied.append({"target": "connection", "field": field, "source": source})

        for field in _REQUIRED_ACTION_OPTIONS.get(action, []):
            if _is_present(options.get(field)):
                supplied.append({"target": "options", "field": field, "source": "request"})
            else:
                missing_required.append(
                    _build_intake_prompt(
                        field,
                        required=True,
                        reason=f"{action} requires options.{field}.",
                    )
                )

        for rule in _ONE_OF_ACTION_OPTIONS.get(action, []):
            fields = [str(item) for item in rule["fields"]]
            if any(_is_present(options.get(field)) for field in fields):
                supplied.append({"target": "options", "field": "|".join(fields), "source": "request"})
            else:
                missing_required.append(
                    _build_intake_prompt(
                        str(rule["id"]),
                        required=True,
                        reason=f"{action} requires one of: {', '.join(fields)}.",
                    )
                )

        for field in metadata.get("recommended_connection", []):
            if _connection_value_source(field, connection) is None:
                recommended.append(
                    _build_intake_prompt(
                        field,
                        required=False,
                        reason=f"{field} is recommended for {action}, but not always required.",
                    )
                )

    ready = bool(action) and not missing_required
    user_questions = [item["question"] for item in missing_required[:3]]
    if not user_questions and recommended:
        user_questions = [item["question"] for item in recommended[:2]]

    suggested_request = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "action": action or None,
        "connection": dict(connection),
        "profiles": dict(profiles),
        "options": dict(options),
    }
    if loop:
        suggested_request["loop"] = dict(loop)
    suggested_request = {key: value for key, value in suggested_request.items() if value not in ({}, None)}

    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "ok": True,
        "tool": "autodbg_prepare",
        "project_root": str(project_root),
        "action": action or None,
        "inferred_action": inferred_action,
        "ready": ready,
        "should_ask_user": bool(missing_required),
        "missing_required": missing_required,
        "recommended": recommended,
        "supplied": supplied,
        "user_questions": user_questions,
        "action_suggestions": [] if action else list(_DEFAULT_ACTION_SUGGESTIONS),
        "suggested_request": suggested_request,
        "agent_instructions": [
            "Call autodbg_prepare before autodbg_action when the action or required field values are uncertain.",
            "Ask the user for missing_required fields before calling autodbg_action; do not guess serial_port, passwords, destructive stop targets, or session_dir.",
            "Ask at most three concise questions at a time, and reuse supplied/environment values instead of asking again.",
            "Treat recommended fields as optional: ask only when they materially affect the selected workflow.",
        ],
    }


def _canonical_action_name(action: str) -> str:
    cleaned = str(action).strip()
    if not cleaned:
        return ""
    if cleaned in _ACTION_METADATA:
        return cleaned
    for alias, tokens in _ACTION_ALIASES.items():
        if cleaned == " ".join(tokens) or cleaned == "-".join(tokens):
            return alias
    return cleaned


def _infer_action_from_goal(goal: str) -> str | None:
    text = goal.lower()
    if not text:
        return None
    if any(token in text for token in ["端口", "ports", "com口", "串口列表"]):
        return "ports"
    if any(token in text for token in ["观察串口", "看串口", "串口日志", "serial log", "watch serial"]):
        return "watch-serial"
    if any(token in text for token in ["健康", "health", "状态检查", "sd卡", "进程"]):
        return "health"
    if any(token in text for token in ["拉取文件", "取文件", "fetch file", "remote file"]):
        return "fetch-file"
    if any(token in text for token in ["闭环", "升级验证", "构建部署", "deploy verify", "deploy-verify", "build deploy", "刷机验证"]):
        return "deploy-verify"
    if any(token in text for token in ["下发", "拉包", "device pull", "lanupg", "artifact"]):
        return "device-pull"
    if any(token in text for token in ["报告", "report"]):
        return "report"
    if any(token in text for token in ["启动", "调试", "debug", "run"]):
        return "run"
    return None


def _connection_value_source(field: str, connection: dict[str, Any]) -> str | None:
    if _is_present(connection.get(field)):
        return "request"
    env_name = _CONNECTION_ENV_MAP.get(field)
    if env_name and _is_present(os.environ.get(env_name)):
        return f"env:{env_name}"
    return None


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if value is False:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, (list, tuple, set, dict)) and not value:
        return False
    return True


def _build_intake_prompt(field: str, *, required: bool, reason: str) -> dict[str, Any]:
    guidance = _FIELD_PROMPTS.get(
        field,
        {
            "target": "options",
            "question": f"请提供 {field}。",
            "example": "",
        },
    )
    return {
        "field": field,
        "target": guidance.get("target", "options"),
        "required": required,
        "sensitive": bool(guidance.get("sensitive", False)),
        "question": str(guidance.get("question", f"请提供 {field}。")),
        "example": guidance.get("example"),
        "reason": reason,
    }


def build_agent_tool_manifest(*, project_root: Path) -> dict[str, Any]:
    defaults_path = project_root / "profiles" / "defaults.toml"
    settings_path = project_root / "config" / "user-settings.toml"
    all_actions = sorted({*_ACTION_METADATA.keys()})
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "tool": {
            "name": "embedded-device-auto-debug",
            "cli_module": "autodbg",
            "project_root": str(project_root),
            "entrypoint": "python -m autodbg agent-call --request -",
            "describe_entrypoint": "python -m autodbg describe-agent-tool --format json",
            "purpose": "Host-side automation tool for embedded device serial observation, control, evidence collection, file transfer, and session reporting.",
        },
        "discovery": {
            "defaults_path": str(defaults_path),
            "settings_path": str(settings_path),
            "recommended_first_actions": ["ports", "watch-serial", "run"],
        },
        "serial_collaboration": {
            "goal": "Keep the human operator and the AI on the same serial session without fighting over the physical COM port.",
            "human_entrypoints": ["observe-serial", ".\\observe-serial.ps1"],
            "ai_entrypoint": "watch-serial",
            "shared_owner": "raw-live broker",
            "rules": [
                "If the user also needs live serial visibility, first guide them to open observe-serial in a separate terminal before long-running debug.",
                "When a human-facing observe window or broker-backed watcher is already running, prefer watch-serial without raw_live so the AI follows the shared trace instead of taking over the COM port again.",
                "Use raw_live only when intentionally starting or attaching to the shared broker for both human and AI observers.",
                "Do not stop serial-broker or close the broker-backed watcher until the human no longer needs the shared serial view.",
            ],
        },
        "required_inputs": {
            "minimum": ["serial_port"],
            "conditional": {
                "device_password": ["run", "exec", "health", "collect-evidence", "fetch-file", "fetch-path", "bootstrap-network", "device-pull", "deploy-verify"],
                "wifi_ssid,wifi_password,wifi_mode": ["bootstrap-network", "device-pull when mode is wlan_script"],
                "sdcard_drive": ["stage-sd"],
            },
        },
        "request_contract": {
            "top_level_fields": ["schema_version", "action", "connection", "profiles", "options", "loop", "response"],
            "loop_fields": ["goal_id", "goal", "prev_session", "iteration", "max_iterations", "attempt_note"],
        },
        "intake_protocol": {
            "mcp_tool": "autodbg_prepare",
            "purpose": "Validate the intended action and return missing required parameters plus user-facing questions before running autodbg_action.",
            "when_to_call": [
                "Before autodbg_action if the action is uncertain.",
                "Before autodbg_action if required connection/options may be missing.",
                "Before destructive or session-specific actions such as serial-broker-stop, artifact-server-stop, report, summary, resume, or record-intervention.",
            ],
            "question_policy": [
                "Ask missing_required first and do not guess serial_port, passwords, stop targets, source paths, remote paths, or session_dir.",
                "Ask at most three concise questions per user turn.",
                "Do not ask for recommended fields unless they materially affect the workflow.",
            ],
        },
        "connection_fields": [
            {
                "name": key,
                "environment_variable": env_name,
                "sensitive": key in {"device_password", "wifi_password"},
            }
            for key, env_name in _CONNECTION_ENV_MAP.items()
        ],
        "actions": [
            {
                "name": action,
                **_ACTION_METADATA[action],
            }
            for action in all_actions
        ],
        "response_contract": {
            "shell_exit_code_rule": "The wrapper process returns shell exit code 0 whenever it can print a JSON response; inspect ok and exit_code for the real result.",
            "top_level_fields": [
                "schema_version",
                "ok",
                "action",
                "argv",
                "exit_code",
                "project_root",
                "artifacts_root",
                "session_dir",
                "summary_path",
                "report_path",
                "artifacts_manifest_path",
                "summary",
                "stdout",
                "stderr",
                "error",
            ],
        },
        "operating_rules": [
            "If the user also needs to watch serial live, first guide them to open observe-serial in a separate terminal, then let the AI reuse that broker-backed view.",
            "Prefer watch-serial without raw_live when a human-facing observe window already owns the shared broker.",
            "Raw-live broker mode is the single owner of the physical COM port; other autodbg commands should reuse the broker instead of opening the port directly.",
            "Do not stop serial-broker while the operator still needs the shared serial view.",
            "Use ok plus exit_code from the JSON response as the source of truth, not the outer shell exit code.",
            "Most control and evidence actions create a session directory under artifacts and may also write retrieved files under retrieved.",
            "When a transient status appears in the watch TUI, it automatically falls back to the default operation hints after a few seconds.",
        ],
        "recommended_workflows": [
            {
                "name": "startup_debug",
                "steps": ["watch-serial", "run", "report"],
            },
            {
                "name": "health_audit",
                "steps": ["watch-serial", "health", "collect-evidence", "summary"],
            },
            {
                "name": "deploy_and_verify",
                "steps": ["stage-sd or device-pull", "run", "fetch-file or fetch-path", "report"],
            },
            {
                "name": "human_ai_shared_serial",
                "steps": ["human: observe-serial", "ai: watch-serial", "ai: run or exec", "report"],
            },
        ],
    }


def render_agent_tool_markdown(*, project_root: Path) -> str:
    manifest = build_agent_tool_manifest(project_root=project_root)
    lines: list[str] = []
    tool = manifest["tool"]
    discovery = manifest["discovery"]
    lines.append("# Embedded Device Auto-Debug Tool")
    lines.append("")
    lines.append(f"- Name: `{tool['name']}`")
    lines.append(f"- Entrypoint: `{tool['entrypoint']}`")
    lines.append(f"- Discovery: `{tool['describe_entrypoint']}`")
    lines.append(f"- Project root: `{tool['project_root']}`")
    lines.append("")
    lines.append("## Purpose")
    lines.append("")
    lines.append(f"- {tool['purpose']}")
    lines.append("")
    lines.append("## Minimum Input")
    lines.append("")
    for item in manifest["required_inputs"]["minimum"]:
        lines.append(f"- `{item}`")
    lines.append("")
    lines.append("## Conditional Input")
    lines.append("")
    for field, when in manifest["required_inputs"]["conditional"].items():
        lines.append(f"- `{field}`: {', '.join(when)}")
    lines.append("")
    lines.append("## Action Summary")
    lines.append("")
    for action in manifest["actions"]:
        lines.append(f"- `{action['name']}`: {action['summary']}")
    lines.append("")
    lines.append("## Recommended First Actions")
    lines.append("")
    for action in discovery["recommended_first_actions"]:
        lines.append(f"- `{action}`")
    lines.append("")
    lines.append("## Intake Before Action")
    lines.append("")
    intake = manifest["intake_protocol"]
    lines.append(f"- MCP tool: `{intake['mcp_tool']}`")
    lines.append(f"- Purpose: {intake['purpose']}")
    for rule in intake["question_policy"]:
        lines.append(f"- {rule}")
    lines.append("")
    lines.append("## Human + AI Shared Serial")
    lines.append("")
    collaboration = manifest["serial_collaboration"]
    lines.append(f"- Goal: {collaboration['goal']}")
    lines.append(f"- Human entrypoints: {', '.join(f'`{item}`' for item in collaboration['human_entrypoints'])}")
    lines.append(f"- AI entrypoint: `{collaboration['ai_entrypoint']}`")
    lines.append(f"- Shared owner: `{collaboration['shared_owner']}`")
    for rule in collaboration["rules"]:
        lines.append(f"- {rule}")
    lines.append("")
    lines.append("## Operating Rules")
    lines.append("")
    for rule in manifest["operating_rules"]:
        lines.append(f"- {rule}")
    lines.append("")
    lines.append("## Response Rule")
    lines.append("")
    lines.append(f"- {manifest['response_contract']['shell_exit_code_rule']}")
    return "\n".join(lines) + "\n"
