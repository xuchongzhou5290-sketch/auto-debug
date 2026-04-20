import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autodbg.mcp.server import build_mcp_tools, call_mcp_tool, dispatch_mcp_request


class McpServerTest(unittest.TestCase):
    def test_build_mcp_tools_contains_describe_and_action(self) -> None:
        tools = build_mcp_tools(project_root=Path("X:/Auto-Debug"))
        tool_names = [item["name"] for item in tools]

        self.assertEqual(tool_names, ["autodbg_describe", "autodbg_action"])
        action_schema = tools[1]["inputSchema"]
        self.assertIn("run", action_schema["properties"]["action"]["enum"])
        self.assertIn("watch-serial", action_schema["properties"]["action"]["enum"])

    def test_call_mcp_tool_describe_returns_manifest(self) -> None:
        result = call_mcp_tool("autodbg_describe", {}, project_root=Path("X:/Auto-Debug"))

        self.assertIn("structuredContent", result)
        self.assertEqual(result["structuredContent"]["tool"]["name"], "embedded-device-auto-debug")

    def test_call_mcp_tool_action_uses_structured_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            fake_response = {
                "schema_version": 1,
                "ok": True,
                "action": "ports",
                "argv": ["ports"],
                "exit_code": 0,
                "project_root": str(project_root),
                "artifacts_root": None,
                "session_dir": None,
                "summary_path": None,
                "report_path": None,
                "artifacts_manifest_path": None,
                "summary": None,
                "stdout": "COM19\n",
                "stderr": "",
                "error": None,
            }

            with patch("autodbg.mcp.server.execute_agent_request", return_value=fake_response) as mocked:
                result = call_mcp_tool(
                    "autodbg_action",
                    {"action": "ports"},
                    project_root=project_root,
                )

        mocked.assert_called_once()
        self.assertFalse(result.get("isError", False))
        self.assertEqual(result["structuredContent"]["action"], "ports")
        self.assertIn("COM19", result["content"][0]["text"])

    def test_dispatch_mcp_request_lists_tools(self) -> None:
        response = dispatch_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            },
            project_root=Path("X:/Auto-Debug"),
        )

        self.assertEqual(response["id"], 2)
        self.assertEqual(response["result"]["tools"][0]["name"], "autodbg_describe")

    def test_dispatch_mcp_request_wraps_unknown_tool_as_invalid_params(self) -> None:
        response = dispatch_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "unknown_tool",
                    "arguments": {},
                },
            },
            project_root=Path("X:/Auto-Debug"),
        )

        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("Unsupported MCP tool", response["error"]["message"])


if __name__ == "__main__":
    unittest.main()
