from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, BinaryIO

from autodbg import __version__
from autodbg.agent import (
    AGENT_SCHEMA_VERSION,
    AgentCallError,
    build_agent_error_response,
    build_agent_intake_plan,
    build_agent_tool_manifest,
    execute_agent_request,
)
from autodbg.cli.main import _build_parser, _dispatch_command


SERVER_NAME = "embedded-device-auto-debug-mcp"
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
MAX_LSP_CONTENT_LENGTH = 10 * 1024 * 1024


def _build_agent_request_schema(
    *,
    action_names: list[str],
    require_action: bool,
    include_goal: bool = False,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "schema_version": {
            "type": "integer",
            "enum": [AGENT_SCHEMA_VERSION],
            "default": AGENT_SCHEMA_VERSION,
        },
        "action": {
            "type": "string",
            "enum": action_names,
            "description": "Structured auto-debug action name.",
        },
        "connection": {
            "type": "object",
            "description": "现场连接参数；敏感字段只在运行时环境里使用，不写入 argv。",
            "properties": {
                "serial_port": {"type": "string"},
                "baudrate": {"type": "integer"},
                "device_password": {"type": "string"},
                "login_prompt": {"type": "string"},
                "shell_prompt": {"type": "string"},
                "host_ip": {"type": "string"},
                "preferred_interfaces": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                },
                "expected_ip": {"type": "string"},
                "pull_base_url": {"type": "string"},
                "pull_workspace": {"type": "string"},
                "wifi_ssid": {"type": "string"},
                "wifi_password": {"type": "string"},
                "wifi_mode": {"type": "string"},
                "network_dir": {"type": "string"},
                "sdcard_drive": {"type": "string"},
                "retrieved_root": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "profiles": {
            "type": "object",
            "description": "Profile 和默认路径覆盖项。",
            "properties": {
                "device": {"type": "string"},
                "model": {"type": "string"},
                "task": {"type": "string"},
                "transport": {"type": "string"},
                "profiles_defaults": {"type": "string"},
                "artifacts_root": {"type": "string"},
                "settings": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "options": {
            "type": "object",
            "description": "CLI 参数的 snake_case 形式；不同 action 的字段不同。",
            "additionalProperties": True,
        },
        "loop": {
            "type": "object",
            "description": "跨轮调试上下文；用于把上一轮 session 和当前轮次稳定传入。",
            "properties": {
                "goal_id": {"type": "string"},
                "goal": {"type": "string"},
                "prev_session": {"type": "string"},
                "iteration": {"type": "integer"},
                "max_iterations": {"type": "integer"},
                "attempt_note": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "response": {
            "type": "object",
            "description": "返回内容控制项。",
            "properties": {
                "include_summary": {"type": "boolean"},
                "include_stdout": {"type": "boolean"},
                "include_stderr": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "request": {
            "type": "object",
            "description": "可选兼容包装；如果提供，server 会把它当作完整请求体。",
            "additionalProperties": True,
        },
    }
    if include_goal:
        properties["goal"] = {
            "type": "string",
            "description": "Natural-language debug goal; used only to infer a likely action when action is omitted.",
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["action"] if require_action else [],
        "additionalProperties": False,
    }


def build_mcp_tools(*, project_root: Path) -> list[dict[str, Any]]:
    manifest = build_agent_tool_manifest(project_root=project_root)
    action_names = [item["name"] for item in manifest["actions"]]
    return [
        {
            "name": "autodbg_describe",
            "description": (
                "Return the embedded auto-debug tool manifest, including supported actions, "
                "required inputs, operating rules, and recommended workflows."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "autodbg_prepare",
            "description": (
                "Plan an embedded auto-debug request before execution. "
                "Use this to identify missing required parameters and generate concise questions for the user."
            ),
            "inputSchema": _build_agent_request_schema(
                action_names=action_names,
                require_action=False,
                include_goal=True,
            ),
        },
        {
            "name": "autodbg_action",
            "description": (
                "Execute one structured embedded auto-debug request. "
                f"Supported actions: {', '.join(action_names)}."
            ),
            "inputSchema": _build_agent_request_schema(action_names=action_names, require_action=True),
        },
    ]


def call_mcp_tool(name: str, arguments: dict[str, Any] | None, *, project_root: Path) -> dict[str, Any]:
    if name == "autodbg_describe":
        manifest = build_agent_tool_manifest(project_root=project_root)
        return {
            "content": [{"type": "text", "text": json.dumps(manifest, indent=2, ensure_ascii=False)}],
            "structuredContent": manifest,
        }
    if name == "autodbg_prepare":
        payload = arguments or {}
        nested_request = payload.get("request")
        if isinstance(nested_request, dict):
            request = dict(nested_request)
        else:
            request = dict(payload)
        request.setdefault("schema_version", AGENT_SCHEMA_VERSION)
        try:
            response = build_agent_intake_plan(request, project_root=project_root)
        except (AgentCallError, json.JSONDecodeError, ValueError) as exc:
            response = build_agent_error_response(project_root=project_root, error=exc)
        return {
            "content": [{"type": "text", "text": json.dumps(response, indent=2, ensure_ascii=False)}],
            "structuredContent": response,
            "isError": bool(response.get("error")),
        }
    if name == "autodbg_action":
        payload = arguments or {}
        nested_request = payload.get("request")
        if isinstance(nested_request, dict):
            request = dict(nested_request)
        else:
            request = dict(payload)
        request.setdefault("schema_version", AGENT_SCHEMA_VERSION)
        try:
            response = execute_agent_request(
                request,
                project_root=project_root,
                parser_builder=_build_parser,
                dispatcher=_dispatch_command,
            )
        except (AgentCallError, json.JSONDecodeError, ValueError) as exc:
            response = build_agent_error_response(project_root=project_root, error=exc)
        return {
            "content": [{"type": "text", "text": json.dumps(response, indent=2, ensure_ascii=False)}],
            "structuredContent": response,
            "isError": not response.get("ok", False),
        }
    raise ValueError(f"Unsupported MCP tool: {name}")


def dispatch_mcp_request(message: dict[str, Any], *, project_root: Path) -> dict[str, Any] | None:
    method = message.get("method")
    if not isinstance(method, str):
        return _jsonrpc_error(message.get("id"), code=-32600, text="Invalid Request")

    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        if request_id is None:
            return None
        protocol_version = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": __version__,
                },
                "instructions": (
                    "Use autodbg_describe first when an agent needs discovery. "
                    "Use autodbg_prepare before autodbg_action when required parameters are uncertain, "
                    "then ask the user for missing_required fields. "
                    "Use autodbg_action for actual serial observation, control, evidence, and transport flows."
                ),
            },
        )

    if method in {"notifications/initialized", "initialized", "$/cancelRequest", "exit"}:
        return None

    if method == "ping":
        return _jsonrpc_result(request_id, {})
    if method == "shutdown":
        return _jsonrpc_result(request_id, {})
    if method == "tools/list":
        return _jsonrpc_result(request_id, {"tools": build_mcp_tools(project_root=project_root)})
    if method == "tools/call":
        try:
            tool_name = str(params["name"])
            arguments = params.get("arguments")
            if arguments is not None and not isinstance(arguments, dict):
                raise ValueError("tools/call arguments must be a JSON object.")
            result = call_mcp_tool(tool_name, arguments, project_root=project_root)
        except KeyError:
            return _jsonrpc_error(request_id, code=-32602, text="tools/call is missing name.")
        except ValueError as exc:
            return _jsonrpc_error(request_id, code=-32602, text=str(exc))
        return _jsonrpc_result(request_id, result)
    if method == "resources/list":
        return _jsonrpc_result(request_id, {"resources": []})
    if method == "prompts/list":
        return _jsonrpc_result(request_id, {"prompts": []})

    return _jsonrpc_error(request_id, code=-32601, text=f"Method not found: {method}")


def serve_stdio(*, project_root: Path, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    input_stream = stdin if stdin is not None else sys.stdin.buffer
    output_stream = stdout if stdout is not None else sys.stdout.buffer
    while True:
        try:
            message = _read_mcp_message(input_stream)
        except json.JSONDecodeError as exc:
            _write_mcp_message(output_stream, _jsonrpc_error(None, code=-32700, text=str(exc)))
            continue
        if message is None:
            return 0
        response = dispatch_mcp_request(message, project_root=project_root)
        if response is not None:
            _write_mcp_message(output_stream, response)


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, *, code: int, text: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": text,
        },
    }


def _read_mcp_message(stream: BinaryIO) -> dict[str, Any] | None:
    """读取 MCP stdio transport 的一帧消息。

    MCP 规范（stdio transport）采用 **newline-delimited JSON (NDJSON)**：
    每条 JSON-RPC 消息占一行，以 ``\\n`` 终止，不允许消息体内嵌换行。

    历史上这里曾使用 LSP 风格的 ``Content-Length`` 头 + 裸字节体作为框架，
    这和 MCP 规范不兼容 —— Claude Code 等严格按规范发送 NDJSON 的客户端
    会因为服务端读不到 ``Content-Length`` 头而无限等待直至 30 秒超时。

    为了兼容少数会发送 LSP 头的调用方（比如历史的 Codex 入口），此处保留了
    自动探测：第一条非空输入如果以 ``Content-Length`` 开头就走旧的 header
    路径，否则按 NDJSON 解析。
    """
    while True:
        line = stream.readline()
        if not line:
            return None  # EOF
        text = line.decode("utf-8", errors="replace")
        stripped = text.strip()
        if not stripped:
            continue  # 跳过心跳/空行

        # 兼容旧的 LSP 风格 framing：Content-Length 头 + 空行 + 裸 body。
        if stripped.lower().startswith("content-length"):
            headers = {}
            _, _, first_value = stripped.partition(":")
            headers["content-length"] = first_value.strip()
            while True:
                nxt = stream.readline()
                if not nxt or nxt in {b"\r\n", b"\n"}:
                    break
                k, _, v = nxt.decode("utf-8").partition(":")
                headers[k.strip().lower()] = v.strip()
            length_text = headers.get("content-length")
            if not length_text:
                raise json.JSONDecodeError("Missing Content-Length header.", "", 0)
            try:
                length = int(length_text)
            except ValueError as exc:
                raise json.JSONDecodeError("Invalid Content-Length header.", "", 0) from exc
            if length <= 0 or length > MAX_LSP_CONTENT_LENGTH:
                raise json.JSONDecodeError("Invalid Content-Length header.", "", 0)
            payload = stream.read(length)
            return json.loads(payload.decode("utf-8"))

        # 默认：newline-delimited JSON。
        return json.loads(stripped)


def _write_mcp_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    """按 MCP 规范以 newline-delimited JSON 输出一条消息。"""
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stream.write(data)
    stream.write(b"\n")
    stream.flush()
