from __future__ import annotations

import json
from pathlib import Path
from typing import Any


HOME_PLUGIN_NAME = "embedded-device-auto-debug-mcp"


def install_home_plugin(
    *,
    project_root: Path,
    home_root: Path,
    plugin_name: str = HOME_PLUGIN_NAME,
) -> dict[str, Path]:
    project_root = Path(project_root).absolute()
    home_root = Path(home_root).absolute()

    plugin_dir = home_root / "plugins" / plugin_name
    codex_plugin_dir = plugin_dir / ".codex-plugin"
    scripts_dir = plugin_dir / "scripts"
    marketplace_path = home_root / ".agents" / "plugins" / "marketplace.json"

    codex_plugin_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)

    launcher_path = scripts_dir / "launch-autodbg-mcp.ps1"
    plugin_json_path = codex_plugin_dir / "plugin.json"
    mcp_json_path = plugin_dir / ".mcp.json"
    readme_path = plugin_dir / "README.md"
    server_script_path = scripts_dir / "autodbg_mcp_server.py"

    _write_text(plugin_json_path, _render_json(_build_plugin_manifest(plugin_name=plugin_name)))
    _write_text(mcp_json_path, _render_json(_build_mcp_config(launcher_path=launcher_path, project_root=project_root)))
    _write_text(readme_path, _render_home_readme(project_root=project_root, plugin_dir=plugin_dir, marketplace_path=marketplace_path))
    _write_text(server_script_path, _render_server_wrapper())
    _write_text(launcher_path, _render_launcher_script())

    marketplace = _load_marketplace(marketplace_path, home_root=home_root)
    _upsert_marketplace_plugin(marketplace, plugin_name=plugin_name)
    _write_text(marketplace_path, _render_json(marketplace))

    return {
        "home_root": home_root,
        "plugin_dir": plugin_dir,
        "plugin_manifest": plugin_json_path,
        "mcp_config": mcp_json_path,
        "launcher_script": launcher_path,
        "server_script": server_script_path,
        "readme": readme_path,
        "marketplace": marketplace_path,
    }


def _build_plugin_manifest(*, plugin_name: str) -> dict[str, Any]:
    return {
        "name": plugin_name,
        "version": "0.1.0",
        "description": "Global MCP wrapper for the embedded auto-debug tool project.",
        "author": {"name": "Codex"},
        "license": "MIT",
        "keywords": ["embedded", "serial", "debug", "mcp", "autodbg"],
        "mcpServers": "./.mcp.json",
        "interface": {
            "displayName": "Embedded Auto-Debug",
            "shortDescription": "Global MCP tools for embedded serial observation, control, and evidence.",
            "longDescription": (
                "Expose the independent embedded auto-debug project as a home-local MCP plugin "
                "so any workspace can install the same embedded device debugging tool surface."
            ),
            "developerName": "Codex",
            "category": "Coding",
            "capabilities": ["Interactive", "Read", "Write"],
            "defaultPrompt": [
                "Inspect the available serial ports for my embedded device.",
                "Start a startup debug flow on COM19 and summarize the verdict.",
                "Collect health evidence from the current embedded target.",
            ],
            "brandColor": "#2563EB",
        },
    }


def _build_mcp_config(*, launcher_path: Path, project_root: Path) -> dict[str, Any]:
    return {
        "mcpServers": {
            "embedded-device-auto-debug": {
                "command": "powershell.exe",
                "args": [
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(launcher_path),
                ],
                "env": {
                    "AUTO_DBG_PROJECT_ROOT": str(project_root),
                    "AUTO_DBG_MCP_LOG_DIR": str(project_root / ".autodbg"),
                    "PYTHONUTF8": "1",
                },
            }
        }
    }


def _render_home_readme(*, project_root: Path, plugin_dir: Path, marketplace_path: Path) -> str:
    return "\n".join(
        [
            "# Embedded Device Auto-Debug Home Plugin",
            "",
            "这是一个 home-local 全局 MCP plugin。",
            "",
            "默认行为：",
            "",
            "- 从 `AUTO_DBG_PROJECT_ROOT` 读取独立工具项目根目录",
            f"- 当前默认值：`{project_root}`",
            "- MCP 启动日志写入 `AUTO_DBG_MCP_LOG_DIR`，默认是 `${AUTO_DBG_PROJECT_ROOT}\\.autodbg`",
            "- 再从 `${AUTO_DBG_PROJECT_ROOT}\\.venv\\Scripts\\python.exe` 启动 MCP server",
            "",
            "当前入口：",
            "",
            f"- 插件目录：`{plugin_dir}`",
            f"- 用户级 marketplace：`{marketplace_path}`",
            "",
            "如果项目根目录迁移，只需要更新 `AUTO_DBG_PROJECT_ROOT` 或重新执行安装脚本。",
            "",
        ]
    )


def _render_server_wrapper() -> str:
    return "\n".join(
        [
            "from __future__ import annotations",
            "",
            "import os",
            "from pathlib import Path",
            "import sys",
            "",
            "",
            'project_root_text = os.environ.get("AUTO_DBG_PROJECT_ROOT") or os.environ.get("AUTO_DBG_HOME")',
            "if not project_root_text:",
            '    raise SystemExit("AUTO_DBG_PROJECT_ROOT or AUTO_DBG_HOME is not set.")',
            "PROJECT_ROOT = Path(project_root_text).absolute()",
            "",
            "if not PROJECT_ROOT.exists():",
            '    raise SystemExit(f"AUTO_DBG_PROJECT_ROOT does not exist: {PROJECT_ROOT}")',
            "",
            "if str(PROJECT_ROOT) not in sys.path:",
            "    sys.path.insert(0, str(PROJECT_ROOT))",
            "",
            "from autodbg.mcp.server import serve_stdio",
            "",
            "",
            "raise SystemExit(serve_stdio(project_root=PROJECT_ROOT))",
            "",
        ]
    )


def _render_launcher_script() -> str:
    return "\n".join(
        [
            "param(",
            "    [string]$LogDir",
            ")",
            "",
            '$ErrorActionPreference = "Stop"',
            "",
            '$projectRoot = $env:AUTO_DBG_PROJECT_ROOT',
            'if ([string]::IsNullOrWhiteSpace($projectRoot)) {',
            '    $projectRoot = $env:AUTO_DBG_HOME',
            "}",
            'if ([string]::IsNullOrWhiteSpace($projectRoot)) {',
            '    throw "AUTO_DBG_PROJECT_ROOT or AUTO_DBG_HOME is not set."',
            "}",
            "",
            '$pythonExe = Join-Path $projectRoot ".venv\\Scripts\\python.exe"',
            '$serverScript = Join-Path $PSScriptRoot "autodbg_mcp_server.py"',
            "",
            'if (-not (Test-Path -LiteralPath $projectRoot)) {',
            '    throw "AUTO_DBG_PROJECT_ROOT does not exist: $projectRoot"',
            "}",
            'if (-not (Test-Path -LiteralPath $pythonExe)) {',
            '    throw "Python entrypoint does not exist: $pythonExe"',
            "}",
            'if (-not (Test-Path -LiteralPath $serverScript)) {',
            '    throw "MCP server script does not exist: $serverScript"',
            "}",
            "",
            'if ([string]::IsNullOrWhiteSpace($LogDir)) {',
            '    $LogDir = $env:AUTO_DBG_MCP_LOG_DIR',
            "}",
            'if ([string]::IsNullOrWhiteSpace($LogDir)) {',
            '    $LogDir = Join-Path $projectRoot ".autodbg"',
            "}",
            "elseif (-not [System.IO.Path]::IsPathRooted($LogDir)) {",
            "    $LogDir = Join-Path $projectRoot $LogDir",
            "}",
            "$LogDir = [System.IO.Path]::GetFullPath($LogDir)",
            "New-Item -ItemType Directory -Path $LogDir -Force | Out-Null",
            "",
            '$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"',
            '$stderrLog = Join-Path $LogDir ("mcp-stderr-{0}-{1}.log" -f $timestamp, $PID)',
            '$launcherLog = Join-Path $LogDir "mcp-launcher.log"',
            "Add-Content -LiteralPath $launcherLog -Encoding UTF8 -Value (",
            '    "{0} project_root={1} log_dir={2} server={3} stderr={4}" -f',
            '    (Get-Date).ToString("o"), $projectRoot, $LogDir, $serverScript, $stderrLog',
            ")",
            "",
            "$env:AUTO_DBG_PROJECT_ROOT = $projectRoot",
            "$env:AUTO_DBG_MCP_LOG_DIR = $LogDir",
            "& $pythonExe -u $serverScript 2>> $stderrLog",
            "$exitCode = $LASTEXITCODE",
            "if ($exitCode -ne 0) {",
            "    Add-Content -LiteralPath $launcherLog -Encoding UTF8 -Value (",
            '        "{0} exit_code={1} stderr={2}" -f (Get-Date).ToString("o"), $exitCode, $stderrLog',
            "    )",
            "}",
            "exit $exitCode",
            "",
        ]
    )


def _load_marketplace(path: Path, *, home_root: Path) -> dict[str, Any]:
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Marketplace file is not a JSON object: {path}")
        plugins = data.get("plugins")
        if not isinstance(plugins, list):
            data["plugins"] = []
        interface = data.get("interface")
        if not isinstance(interface, dict):
            data["interface"] = {}
        data.setdefault("name", f"{home_root.name.lower()}-local")
        data["interface"].setdefault("displayName", f"{home_root.name} Local Plugins")
        return data
    return {
        "name": f"{home_root.name.lower()}-local",
        "interface": {"displayName": f"{home_root.name} Local Plugins"},
        "plugins": [],
    }


def _upsert_marketplace_plugin(marketplace: dict[str, Any], *, plugin_name: str) -> None:
    entry = {
        "name": plugin_name,
        "source": {
            "source": "local",
            "path": f"./plugins/{plugin_name}",
        },
        "policy": {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        },
        "category": "Coding",
    }
    plugins = marketplace.setdefault("plugins", [])
    for index, existing in enumerate(plugins):
        if isinstance(existing, dict) and existing.get("name") == plugin_name:
            plugins[index] = entry
            return
    plugins.append(entry)


def _render_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
