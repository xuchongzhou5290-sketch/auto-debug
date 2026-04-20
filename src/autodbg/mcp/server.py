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
    build_agent_tool_manifest,
    execute_agent_request,
)
from autodbg.cli.main import _build_parser, _dispatch_command


SERVER_NAME = "embedded-device-auto-debug-mcp"
DEFAULT_PROTOCOL_VERSION = "2025-03-26"


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
            "name": "autodbg_action",
            "description": (
                "Execute one structured embedded auto-debug request. "
                f"Supported actions: {', '.join(action_names)}."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
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
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    ]


def call_mcp_tool(name: str, arguments: dict[str, Any] | None, *, project_root: Path) -> dict[str, Any]:
    if name == "autodbg_describe":
        manifest = build_agent_tool_manifest(project_root=project_root)
        return {
            "content": [{"type": "text", "text": json.dumps(manifest, indent=2, ensure_ascii=False)}],
            "structuredContent": manifest,
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
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None if not headers else {}
        if line in {b"\r\n", b"\n"}:
            break
        key, _, value = line.decode("utf-8").partition(":")
        headers[key.strip().lower()] = value.strip()
    length_text = headers.get("content-length")
    if not length_text:
        raise json.JSONDecodeError("Missing Content-Length header.", "", 0)
    payload = stream.read(int(length_text))
    return json.loads(payload.decode("utf-8"))


def _write_mcp_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(data)
    stream.flush()
