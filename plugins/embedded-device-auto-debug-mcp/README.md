# Embedded Device Auto-Debug MCP Plugin

这个插件把 `X:\Auto-Debug` 的 `agent-call` / `describe-agent-tool` 封成两个 MCP tools：

- `autodbg_describe`
- `autodbg_action`

当前 `.mcp.json` 直接指向：

- `X:\Auto-Debug\.venv\Scripts\python.exe`
- `X:\Auto-Debug\plugins\embedded-device-auto-debug-mcp\scripts\autodbg_mcp_server.py`

所以它是针对当前独立项目根目录 `X:\Auto-Debug` 生成的本地插件。

详细说明见：

- `X:\Auto-Debug\docs\mcp-tool.md`
