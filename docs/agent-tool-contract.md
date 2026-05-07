# Agent Tool Contract

目标：

- 让任意 AI Agent 通过一个稳定入口调用 `auto-debug`
- 调用时只传必要连接参数和任务参数
- 工具自己完成 CLI 翻译、session 管理、产物回收、结构化结果输出

当前入口命令：

```powershell
cd <repo-root>
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
- `artifact-server-list`
- `artifact-server-stop`
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
<repo-root>\profiles\defaults.toml
```

路径规则：

- `profiles` 里的路径字段如果传相对路径，会按 `project_root` 解析
- 直接 CLI 调用时，这个 `project_root` 就是当前仓库根目录
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

### 人工 + AI 串口协同规则

如果用户自己也要实时看串口，不要默认让 AI 单独占着串口跑。

- AI 应先明确引导用户在独立终端打开 `observe-serial` 或源码模式下的 `.\observe-serial.ps1`
- 人工观察窗口启动后，AI 侧优先使用 `watch-serial` 跟随共享 trace，或直接执行会复用 broker 的 `run / exec / health / collect-evidence`
- 如果已经有人工观察窗口，AI 不应再默认加 `raw_live=true` 去重新抢物理串口
- 只在需要启动或接管共享 broker 时才使用 `watch-serial --raw-live`
- 只要用户还在看串口，AI 就不应主动执行 `serial-broker-stop`

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

`serve-artifacts` 的默认行为是后台启动 HTTP server，并在 stdout 返回：

- `pid`
- `Base URL`
- `Health URL`
- `Log`
- 停止命令 `autodbg artifact-server stop --port <port>`

如果上层确实需要阻塞式前台服务，传：

```json
{
  "foreground": true
}
```

如果端口被占用，默认会自动选择后续可用端口；需要严格失败时传：

```json
{
  "no_auto_port": true
}
```

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
    "transfer_mode": "auto",
    "sd_http_helper_path": "/mnt/sdcard/autodbg/autodbg-http-pull"
  }
}
```

`device-pull` 的 `transfer_mode=auto` 会按 `sd_http_helper -> http -> serial_bundle` 选择通道：

- `sd_http_helper`：设备没有 downloader，但 SD 卡内已放置 `autodbg-http-pull` 可执行文件
- `http`：设备已有 `curl / wget / busybox wget`
- `serial_bundle`：网络不可用或只能通过串口传 base64+tar

SD helper 的 C 源码在 `src/autodbg/assets/autodbg_http_pull.c`，AI 作为 MCP 使用时可以先调用 `build-sd-http-helper` 交叉编译：

```json
{
  "schema_version": 1,
  "action": "build-sd-http-helper",
  "options": {
    "cc": "arm-linux-gnueabihf-gcc",
    "output": "artifacts/autodbg-http-pull"
  }
}
```

编译成功后再通过 `stage-sd` 放入 SD 卡。若目标 toolchain 不支持静态链接，传 `options.static=false`。

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
- `run` 的 `summary.run_results.observation` 会包含 `marker_windows` 和 `marker_verdict`，用于读取串口 success/fatal marker 的前后文切片

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

如果当前场景是“AI 在后台调试，用户也要直接看串口”，推荐顺序固定为：

1. 先让用户执行 `observe-serial`
2. AI 再调 `watch-serial` 或直接调 `run / exec / health`
3. 结束前不要擅自停 broker，除非用户明确表示不再需要观察窗口

## 9. MCP 封装

当前已经补了一层 repo-local MCP plugin：

- 插件目录：`<repo-root>\plugins\embedded-device-auto-debug-mcp`
- MCP 配置：`<repo-root>\plugins\embedded-device-auto-debug-mcp\.mcp.json`
- Marketplace：`<repo-root>\.agents\plugins\marketplace.json`

当前暴露三个 MCP tools：

- `autodbg_describe`
- `autodbg_prepare`
- `autodbg_action`

建议 MCP 上层调用顺序：

1. 新手或目标不明确时，先调 `quickstart`
2. 再调 `autodbg_describe`
3. 调 `autodbg_prepare` 检查参数是否齐全
4. 如果返回 `missing_required`，先向用户询问这些字段
5. `ready=true` 后再调 `autodbg_action`
6. 仍然以 `ok / exit_code / summary / error` 为主判断结果

`quickstart` 是正式快捷引导 action，适合用户只说“我要开始调试设备”这类模糊目标时使用。它会返回 `questions / detected_ports / next_requests`，AI 应先问完 `questions`，再对选定的 `next_requests` 调 `autodbg_prepare`。

`autodbg_prepare` 不会接触设备，只返回参数引导：

- `ready`
- `missing_required`
- `recommended`
- `user_questions`
- `suggested_request`

Agent 规则：

- 不要猜 `serial_port / device_password / remote_path / source / session_dir / stop target`
- 一次最多向用户问 3 个问题
- `recommended` 字段不是阻塞项，只有会影响当前 workflow 时再问
