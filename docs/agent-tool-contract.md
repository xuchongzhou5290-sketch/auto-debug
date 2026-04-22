# Agent Tool Contract

目标：

- 让任意 AI Agent 通过一个稳定入口调用 `auto-debug`
- 调用时只传必要连接参数和任务参数
- 工具自己完成 CLI 翻译、session 管理、产物回收、结构化结果输出

当前入口命令：

```powershell
cd X:\Auto-Debug
.\.venv\Scripts\python -m autodbg agent-call --request request.json --pretty
```

也可以直接走标准输入：

```powershell
Get-Content .\docs\examples\agent-call-run.json | .\.venv\Scripts\python -m autodbg agent-call --request - --pretty
```

AI 想先拿自描述清单时，可以直接调用：

```powershell
.\.venv\Scripts\python -m autodbg describe-agent-tool --format json
```

## 1. 请求格式

请求体是一个 JSON 对象。

### 顶层字段

- `schema_version`
  - 当前固定为 `1`
  - 可省略；省略时按 `1` 处理
- `action`
  - 要调用的动作
- `connection`
  - 现场连接参数
- `profiles`
  - profile 与路径覆盖项
- `loop`
  - 可选的跨轮调试上下文
- `options`
  - 动作本身的参数
- `response`
  - 返回内容控制项

### `action`

当前支持：

- `run`
- `observe`
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
- `watch-serial`
- `serial-broker-list`
- `serial-broker-stop`
- `report`
- `summary`
- `resume`
- `record-intervention`
- `show-mvp`
- `ports`

## 2. `connection` 字段

用于传现场必要连接参数。

支持字段：

- `serial_port`
- `baudrate`
- `device_password`
- `login_prompt`
- `shell_prompt`
- `host_ip`
- `preferred_interfaces`
- `expected_ip`
- `pull_base_url`
- `pull_workspace`
- `wifi_ssid`
- `wifi_password`
- `wifi_mode`
- `network_dir`
- `sdcard_drive`
- `retrieved_root`

说明：

- `device_password` 不会出现在返回的 `argv` 里
- `wifi_password` 不会写入 summary 的 profile 快照
- 大部分 `connection` 字段会映射成运行时环境变量覆盖
- `serial_port` / `baudrate` 会优先翻译成目标命令的显式 CLI 参数

## 3. `profiles` 字段

用于覆盖默认 profile 集和产物根目录。

支持字段：

- `device`
- `model`
- `task`
- `transport`
- `profiles_defaults`
- `artifacts_root`
- `settings`

默认情况下，不传这些字段时会走：

```powershell
X:\Auto-Debug\profiles\defaults.toml
```

路径规则：

- `profiles` 里的路径字段如果传相对路径，会按 `project_root` 解析
- 直接 CLI 调用时，这个 `project_root` 就是仓库根目录 `X:\Auto-Debug`
- 通过 MCP / 已安装插件调用时，这个 `project_root` 来自 `AUTO_DBG_PROJECT_ROOT`
- 也就是说，像 `profiles/devices/av130n-lab.toml` 这类值不会再按调用方当前工作目录解析

### `loop` 字段

用于多轮自动调试时，把上一轮 session 和本轮目标稳定传给工具。

支持字段：

- `goal_id`
- `goal`
- `prev_session`
- `iteration`
- `max_iterations`
- `attempt_note`

说明：

- `prev_session` 是上一轮 session 目录；可用相对路径，仍按 `project_root` 解析
- 创建新 session 的动作会把 `loop` 写入 `summary.json`
- 失败或未完成时，`summary.result.carry_forward_request` 会给出下一轮可复用的结构化请求骨架

## 4. `options` 字段

字段名直接对应 CLI 参数的 `dest` 名称，使用 snake_case。

示例：

- `observe_seconds` -> `--observe-seconds`
- `skip_evidence` -> `--skip-evidence`
- `shell_command` -> `--shell-command`
- `remote_path` -> `--remote-path`
- `skip_defaults` -> `--skip-defaults`

列表类型会自动展开成重复参数。

示例：

```json
{
  "shell_command": [
    "uname -a",
    "mount"
  ]
}
```

会被翻译成重复的 `--shell-command`。

路径规则：

- 常见路径型参数如 `source`、`root`、`output`、`session_dir`、`file`、`prev_session`，如果传相对路径，也会按 `project_root` 解析
- 对 `record-intervention` 来说，`session_dir` 与 `file` 同样遵守这条规则

## 5. `response` 字段

可选控制项：

- `include_summary`
  - 默认 `true`
- `include_stdout`
  - 默认 `true`
- `include_stderr`
  - 默认 `true`

## 6. 最小示例

### 6.1 直接跑 `run`

```json
{
  "schema_version": 1,
  "action": "run",
  "connection": {
    "serial_port": "COM19",
    "device_password": "your-root-password"
  }
}
```

### 6.2 跑 `exec`

```json
{
  "schema_version": 1,
  "action": "exec",
  "connection": {
    "serial_port": "COM19",
    "device_password": "your-root-password"
  },
  "options": {
    "shell_command": "ls /mnt/sdcard",
    "timeout": 20.0
  }
}
```

### 6.3 跑 `device-pull`

```json
{
  "schema_version": 1,
  "action": "device-pull",
  "connection": {
    "serial_port": "COM19",
    "device_password": "your-root-password",
    "wifi_ssid": "xiaotudou",
    "wifi_password": "12345678",
    "wifi_mode": "WPA2"
  },
  "options": {
    "mode": "lan_ready",
    "transfer_mode": "auto"
  }
}
```

### 6.4 多轮继续跑 `run`

```json
{
  "schema_version": 1,
  "action": "run",
  "connection": {
    "serial_port": "COM19",
    "device_password": "your-root-password"
  },
  "loop": {
    "goal_id": "startup-fix-001",
    "goal": "Reach app_ready without panic",
    "prev_session": "artifacts/20260420/091530123-av130n-lab-startup_check-abcd",
    "iteration": 2,
    "max_iterations": 8,
    "attempt_note": "retry after login timing fix"
  }
}
```

### 6.5 记录本轮干预

```json
{
  "schema_version": 1,
  "action": "record-intervention",
  "options": {
    "session_dir": "artifacts/20260420/091530123-av130n-lab-startup_check-abcd",
    "kind": "ai_patch",
    "summary": "Adjust serial login timing",
    "details": "Increase the observe window and retry after the login helper patch.",
    "file": [
      "src/autodbg/cli/main.py"
    ],
    "git_commit": "abc1234",
    "expected_effect": "The next iteration should reach the root shell and complete run."
  }
}
```

## 7. 响应格式

响应体总是 JSON。

### 顶层字段

- `schema_version`
- `ok`
- `action`
- `argv`
- `exit_code`
- `project_root`
- `artifacts_root`
- `session_dir`
- `summary_path`
- `report_path`
- `artifacts_manifest_path`
- `summary`
- `stdout`
- `stderr`
- `error`

说明：

- `ok` 表示目标动作是否成功
- `exit_code` 是目标动作自己的退出码
- `agent-call` 本身在能成功返回 JSON 时固定返回 shell exit code `0`
- 也就是说，Agent 应该看 `ok` 和 `exit_code`，不是看 `agent-call` 的进程退出码
- 对会创建 session 的动作，`summary` 里还会带上稳定的 `loop`、`result`、`interventions` 三块
- 上层 AI 做多轮编排时，应优先读取 `summary.result.decision / failure_stage / next_actions / carry_forward_request`

### 失败响应示例

```json
{
  "schema_version": 1,
  "ok": false,
  "action": "run",
  "exit_code": 1,
  "error": {
    "type": "CommandFailed",
    "message": "[ERROR] Login prompt reached but shell access was not established."
  }
}
```

## 8. 当前建议

如果后面要把它进一步接成 MCP tool / Codex plugin / 别的 Agent wrapper，建议都统一调用：

```powershell
python -m autodbg agent-call --request -
```

这样上层只要负责：

- 组织 JSON 请求
- 读取 JSON 响应

下层 CLI 细节、session 目录、profile 默认值、运行时环境覆盖，都继续由 `auto-debug` 自己维护。

## 9. MCP 封装

当前已经补了一层 repo-local MCP plugin：

- 插件目录：`X:\Auto-Debug\plugins\embedded-device-auto-debug-mcp`
- MCP 配置：`X:\Auto-Debug\plugins\embedded-device-auto-debug-mcp\.mcp.json`
- Marketplace：`X:\Auto-Debug\.agents\plugins\marketplace.json`

当前暴露两个 MCP tools：

- `autodbg_describe`
- `autodbg_action`

建议 MCP 上层调用顺序：

1. 先调 `autodbg_describe`
2. 再调 `autodbg_action`
3. 仍然以 `ok / exit_code / summary / error` 为主判断结果
