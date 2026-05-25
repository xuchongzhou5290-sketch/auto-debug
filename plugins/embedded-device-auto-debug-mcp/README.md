# Embedded Device Auto-Debug MCP Plugin

这个插件把当前 `auto-debug` 仓库或本地安装版的 `agent-call` / `describe-agent-tool` 封成 MCP tools：

- `autodbg_describe`
- `autodbg_prepare`
- `autodbg_action`

当前 `.mcp.json` 不写死仓库盘符，启动链路是：

- `powershell.exe`
- `scripts\launch-autodbg-mcp.ps1`
- `${AUTO_DBG_PROJECT_ROOT}\.venv\Scripts\python.exe`
- `scripts\autodbg_mcp_server.py`

MCP 启动日志目录：

1. `launch-autodbg-mcp.ps1 -LogDir <path>`
2. `AUTO_DBG_MCP_LOG_DIR`
3. `${AUTO_DBG_PROJECT_ROOT}\.autodbg`

相对日志路径会按 `AUTO_DBG_PROJECT_ROOT` 解析。launcher 只把 stderr 写入日志文件，避免污染 MCP stdio。

项目根解析顺序：

1. `AUTO_DBG_PROJECT_ROOT`
2. `AUTO_DBG_HOME`
3. 从插件脚本位置向上回推到仓库根目录

推荐先执行仓库根目录下的 `.\install-local-tool.ps1`，让脚本生成独立安装版和 home-local MCP 插件。

已经部署过 MCP 后，更新时需要显式指定路径，避免旧环境变量把插件刷新到错误目录：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA "Programs\auto-debug"
.\install-local-tool.ps1 -ProjectRoot (Resolve-Path .).Path -InstallRoot $installRoot -ForceCloseInUseProcesses
& (Join-Path $installRoot "install-home-plugin.ps1") -ProjectRoot $installRoot -HomeRoot $HOME
```

详细说明见：

- `docs\mcp-tool.md`
