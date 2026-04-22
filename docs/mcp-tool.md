# Auto-Debug MCP Tool

## 1. 目标

- 把 `auto-debug` 封成一个可被 AI Agent 直接调用的 MCP Tool
- 上层 Agent 不再自己拼 CLI 参数
- 下层继续复用 `agent-call` 和 `describe-agent-tool`

## 2. 当前暴露的 MCP tools

### `autodbg_describe`

- 返回 `auto-debug` 的自描述 manifest
- 适合 Agent 在首次接管时先做能力发现

### `autodbg_action`

- 输入就是 `agent-call` 的结构化 JSON 请求
- 支持动作：
  - `run`
  - `observe`
  - `watch-serial`
  - `exec`
  - `health`
  - `collect-evidence`
  - `fetch-file`
  - `fetch-path`
  - `bootstrap-network`
  - `serve-artifacts`
  - `device-pull`
  - `stage-sd`
  - `storage`
  - `serial-broker-list`
  - `serial-broker-stop`
  - `report`
  - `summary`
  - `resume`
  - `record-intervention`
  - `show-mvp`
  - `ports`

## 3. repo-local 插件路径

- 插件目录：`X:\Auto-Debug\plugins\embedded-device-auto-debug-mcp`
- MCP 配置：`X:\Auto-Debug\plugins\embedded-device-auto-debug-mcp\.mcp.json`
- Marketplace：`X:\Auto-Debug\.agents\plugins\marketplace.json`

## 4. 本地部署工具

当前已经补了一个“本地安装版”：

- 默认安装目录：`%LOCALAPPDATA%\Programs\auto-debug`
- 安装脚本：`X:\Auto-Debug\install-local-tool.ps1`
- 直接调用入口：
  - `autodbg`
  - `observe-serial`
  - `install-home-plugin`

它会自动：

1. 复制独立运行所需内容到本地安装目录
2. 写入用户环境变量：
   - `AUTO_DBG_HOME`
   - `AUTO_DBG_PROJECT_ROOT`
3. 把 `bin\` 加进用户 `Path`
4. 用安装后的本地工具刷新 home-local MCP plugin

这意味着：

- `MCP server` 不再依赖 `X:\Auto-Debug`
- 任何工作区都可以共用同一份本地安装版
- 只要当前用户环境变量生效，`autodbg` 和 `observe-serial` 就能直接调用

## 5. home-local 全局插件

为了让任何工作区都能直接装，当前又补了一层 home-local 插件：

- 插件目录：`C:\Users\xcz5290\plugins\embedded-device-auto-debug-mcp`
- 用户级 marketplace：`C:\Users\xcz5290\.agents\plugins\marketplace.json`

这层插件的启动方式不是写死某个 workspace，而是：

1. 从 `AUTO_DBG_PROJECT_ROOT` 读取独立工具根目录
2. 默认值应当指向本地安装目录，例如 `%LOCALAPPDATA%\Programs\auto-debug`
3. 再从 `${AUTO_DBG_PROJECT_ROOT}\.venv\Scripts\python.exe` 启动 MCP server

这意味着：

- 任意工作区都可以装同一个 home-local 插件
- 独立工具项目如果将来迁移路径，只需要改 `AUTO_DBG_PROJECT_ROOT`
- 但前提仍然是该根目录下要有本地安装版 `auto-debug` 和对应 `.venv`

### 自动安装 / 升级

当前最推荐的安装入口：

```powershell
cd X:\Auto-Debug
.\install-local-tool.ps1
```

它会自动：

1. 安装或覆盖本地工具目录
2. 写入用户环境变量和 `Path`
3. 安装或覆盖 `~\plugins\embedded-device-auto-debug-mcp`
4. 安装或更新 `~\.agents\plugins\marketplace.json`
5. 将 `.mcp.json` 的默认 `AUTO_DBG_PROJECT_ROOT` 指向本地安装目录

如果只想单独刷新 home plugin：

```powershell
install-home-plugin
```

如果独立项目根、安装目录或 home 目录需要显式指定：

```powershell
.\install-local-tool.ps1 -ProjectRoot D:\Auto-Debug -InstallRoot C:\Tools\auto-debug
.\install-home-plugin.ps1 -ProjectRoot C:\Tools\auto-debug -HomeRoot C:\Users\YourName
```

## 6. 接入 Claude Code

前面 3-5 节讲的是 **Codex 体系** 的插件 marketplace 登记（`~/.agents/plugins/marketplace.json`）。**Claude Code 不读这套配置**，需要单独注册一次。

MCP server 本身是通用的 stdio JSON-RPC 服务，Codex 和 Claude Code 都能连，差别只在"配置文件写在哪里"。

### 6.1 两种 scope

| Scope | 配置位置 | 适用场景 |
|---|---|---|
| **项目级** | 工作区根目录的 `.mcp.json` | 只想在某个项目下启用；配置可随仓库分发 |
| **用户级** | `~/.claude.json` 的 `mcpServers` 字段 | 希望任意工作区都能直接调用 |

### 6.2 项目级接入（推荐起步）

在任意工作区根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "autodbg": {
      "command": "powershell.exe",
      "args": [
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File",
        "C:\\Users\\<YourName>\\plugins\\embedded-device-auto-debug-mcp\\scripts\\launch-autodbg-mcp.ps1"
      ],
      "env": {
        "AUTO_DBG_PROJECT_ROOT": "C:\\Users\\<YourName>\\AppData\\Local\\Programs\\auto-debug",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

内容等同于 `~\plugins\embedded-device-auto-debug-mcp\.mcp.json` —— 后者是 `install-local-tool.ps1` 为 Codex 生成的，Claude Code 可直接复用其 JSON。

### 6.3 用户级接入

```powershell
claude mcp add autodbg --scope user -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\<YourName>\plugins\embedded-device-auto-debug-mcp\scripts\launch-autodbg-mcp.ps1"
```

然后编辑 `~/.claude.json`，在刚生成的 `mcpServers.autodbg` 节点下补 `env`（CLI 目前不方便直接带环境变量）：

```json
"env": {
  "AUTO_DBG_PROJECT_ROOT": "C:\\Users\\<YourName>\\AppData\\Local\\Programs\\auto-debug",
  "PYTHONUTF8": "1"
}
```

### 6.4 Windows 前置条件

- Claude Code CLI 在 Windows 需要 git-bash。若 `claude mcp ...` 报错提示缺少 bash，装 git-for-windows，或设置环境变量 `CLAUDE_CODE_GIT_BASH_PATH` 指向 `bash.exe`。
- 不想装 git-bash？走项目级 `.mcp.json` 路径即可，完全不需要跑 CLI。

### 6.5 验证

新开一个 Claude Code 会话后：

```
/mcp
```

应当能看到 `autodbg` 已连接。然后让 Claude 先调 `autodbg_describe` 读取 manifest，再以 `autodbg_action` 执行具体 action（参见下一节）。

### 6.6 版本刷新

MCP server 启动时走的是**本地安装副本**（`%LOCALAPPDATA%\Programs\auto-debug\`）的 `.venv`，不是源码项目 `X:\Auto-Debug\` 的 `.venv`。修改了源码要让 MCP 里生效，需要重刷本地副本：

```powershell
cd X:\Auto-Debug
.\install-local-tool.ps1
```

之后重启 Claude Code 会话（或在 `/mcp` 面板里断开重连 `autodbg`）即可。

## 7. 调用建议

1. 先调 `autodbg_describe`
2. 根据返回的 `actions / required_inputs / operating_rules` 组织请求
3. 再调 `autodbg_action`
4. 以返回里的 `ok + exit_code + summary` 作为结果判断
5. 如果需要多轮继续调试，优先复用 `summary.result.carry_forward_request`

### 7.1 路径解析规则

- 通过 `autodbg_action` 传入的路径字段，如果不是绝对路径，会按 `AUTO_DBG_PROJECT_ROOT` 解析
- 常见字段包括：
  - `profiles.device / model / task / transport / profiles_defaults / artifacts_root / settings`
  - `options.session_dir / source / root / output / file`
  - `loop.prev_session`
- 这意味着上层 MCP 调用方可以稳定传 repo 相对路径，比如 `profiles/devices/av130n-lab.toml`，不会再错误落到当前工作区 cwd

### 7.2 多轮调试建议

- 新开一轮时，使用顶层 `loop` 字段传入：
  - `goal_id`
  - `goal`
  - `prev_session`
  - `iteration`
  - `max_iterations`
  - `attempt_note`
- 每轮结束后，读取 `summary.loop` 和 `summary.result`
- 如果本轮做了代码或配置修改，再调 `record-intervention` 把干预写回目标 session
- 下一轮优先直接使用上一轮 `summary.result.carry_forward_request`

## 8. 当前未实现 / 未闭环

### 8.1 设备与现场控制

- 还没有自动上下电、继电器、电源时序控制
- 还没有统一的烧录/刷机主链路
- 还没有多设备并发编排和任务队列

### 8.2 串口与终端能力

- 当前 TUI 仍然是面向调试 shell 的行式交互，不是完整 VT100 终端模拟器
- raw-live broker 还没有空闲超时自动释放
- 第三方串口工具还不能像 `autodbg` 一样无缝复用 broker

### 8.3 文件传输与网络

- 局域网传输当前主打 `host serve + device pull`，还没有做成完整双向同步
- 还没有断点续传、增量同步、多设备批量分发
- 还没有零配置局域网发现和自动拓扑识别

### 8.4 构建与验证闭环

- 还没有把代码编译/打包收成 `auto-debug` 的核心主链路
- 还没有“修改源码 -> 自动构建 -> 自动下发 -> 自动回归”的完整一站式流水线
- 视觉类结果、PTZ 效果、灯板状态这类主观验证仍然需要人工确认

### 8.5 MCP 层边界

- 现在虽然已经有本地安装版和 home-local 全局插件，但它仍然依赖 `.venv`
- 还没有做成完全自带运行时、零 Python 前置条件的独立发布包
