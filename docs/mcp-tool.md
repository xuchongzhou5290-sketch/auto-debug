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

## 6. 调用建议

1. 先调 `autodbg_describe`
2. 根据返回的 `actions / required_inputs / operating_rules` 组织请求
3. 再调 `autodbg_action`
4. 以返回里的 `ok + exit_code + summary` 作为结果判断

## 7. 当前未实现 / 未闭环

### 7.1 设备与现场控制

- 还没有自动上下电、继电器、电源时序控制
- 还没有统一的烧录/刷机主链路
- 还没有多设备并发编排和任务队列

### 7.2 串口与终端能力

- 当前 TUI 仍然是面向调试 shell 的行式交互，不是完整 VT100 终端模拟器
- raw-live broker 还没有空闲超时自动释放
- 第三方串口工具还不能像 `autodbg` 一样无缝复用 broker

### 7.3 文件传输与网络

- 局域网传输当前主打 `host serve + device pull`，还没有做成完整双向同步
- 还没有断点续传、增量同步、多设备批量分发
- 还没有零配置局域网发现和自动拓扑识别

### 7.4 构建与验证闭环

- 还没有把代码编译/打包收成 `auto-debug` 的核心主链路
- 还没有“修改源码 -> 自动构建 -> 自动下发 -> 自动回归”的完整一站式流水线
- 视觉类结果、PTZ 效果、灯板状态这类主观验证仍然需要人工确认

### 7.5 MCP 层边界

- 现在虽然已经有本地安装版和 home-local 全局插件，但它仍然依赖 `.venv`
- 还没有做成完全自带运行时、零 Python 前置条件的独立发布包
