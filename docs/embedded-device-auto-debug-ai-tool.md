# Embedded Device Auto-Debug AI Tool

这份文件不是给人类现场操作员的，而是给 AI Agent 的。

目标：

- 用最少上下文理解当前 `auto-debug` 工具根目录
- 知道这个工具能做什么、什么时候调用什么动作
- 知道必须提供哪些现场参数
- 知道如何判断调用是否成功

## 1. Tool Identity

- Tool name: `embedded-device-auto-debug`
- Project root: current repository root or `AUTO_DBG_PROJECT_ROOT`
- Stable entrypoint: `python -m autodbg agent-call --request -`
- Self-description entrypoint: `python -m autodbg describe-agent-tool --format json`
- MCP tools: `autodbg_describe`, `autodbg_prepare`, `autodbg_action`
- Relative request paths are resolved from `project_root` / `AUTO_DBG_PROJECT_ROOT`, not the caller cwd

## 2. What This Tool Is For

这是一个面向嵌入式设备调试现场的宿主机自动化工具。

它擅长：

- 观察串口
- 建立串口控制
- 登录设备 shell
- 运行健康检查
- 运行启动检查
- 收集证据
- 拉回设备文件
- 下发文件到设备
- 归档 session 产物
- 后台启动 artifact HTTP server
- 列出/停止后台 artifact server
- 从串口日志里提取 success/fatal marker 前后文

它不负责：

- 业务代码编译本身
- 固件完整烧录
- 自动上下电
- 纯视觉主观判断

## 3. Minimum Input

最小必要输入通常只有：

- `serial_port`

按动作不同，可能还需要：

- `device_password`
- `wifi_ssid`
- `wifi_password`
- `wifi_mode`
- `sdcard_drive`

## 4. Primary Actions

### 4.1 Startup Debug

推荐动作链：

1. 如果目标串口缺少观察会话，工具自动拉起 `observe-serial`
2. AI 再用 `watch-serial`
3. `run`
4. `report`

适用：

- 设备启动异常
- 想拿一份完整 session 和 verdict

### 4.2 Health Audit

推荐动作链：

1. 如果目标串口缺少观察会话，工具自动拉起 `observe-serial`
2. AI 再用 `watch-serial`
3. `health`
4. `collect-evidence`
5. `summary`

适用：

- 设备是否 ready
- SD 卡、进程、网络状态排查

### 4.3 Deploy And Verify

推荐动作链：

1. `stage-sd` 或 `device-pull`
2. `run`
3. `fetch-file` 或 `fetch-path`
4. `report`

适用：

- 修改后验证
- 文件/包下发后回归

### 4.4 Test Case Evidence

推荐动作链：

1. 如果目标串口缺少观察会话，工具自动拉起 `observe-serial`
2. `case-begin`
3. 执行目标 case
4. `case-end`
5. 必要时 `case-capture`

适用：

- 测试模式
- 不刷固件
- 需要给 AI 或提测记录提供串口证据贴片

产物：

- `<project_root>\autodbg\serial-log\<COMXX>\cases\<timestamp-case_id>\metadata.json`
- `<project_root>\autodbg\serial-log\<COMXX>\cases\<timestamp-case_id>\trace.jsonl`
- `<project_root>\autodbg\serial-log\<COMXX>\cases\<timestamp-case_id>\trace.txt`
- `<project_root>\autodbg\serial-log\<COMXX>\cases\<timestamp-case_id>\evidence_patch.md`

### 4.5 Debug Mode Rule

- `debug_mode=test`：不要求刷设备固件，不要求 `debug_firmware_method`，重点是 case 级串口日志分片
- `debug_mode=development`：涉及拉包/升级/部署时必须明确 `debug_firmware_method` 和 `firmware_build_time`
- 开发模式的日志边界按嵌入式上电周期处理：第一次上电到下次重新上电为一个周期

## 5. Serial Ownership Rule

这是关键规则：

- 物理 COM 口默认由 `observe-serial` 人工观察窗口持有
- 如果目标串口还没有 `observe-serial` 会话，AI 串口动作应先唤醒系统默认终端打开 `observe-serial`
- AI 自己随后优先使用不带 `raw_live` 的 `watch-serial` 跟随共享 trace
- `observe-serial` 的受保护 raw-live broker 是物理串口的单一拥有者
- 其他 `autodbg` 命令应该复用 broker，而不是重新直接抢串口
- 串口 trace 默认保存到 `<project_root>\autodbg\serial-log\<COMXX>\trace.jsonl`

对 AI 的实际含义：

- 如果用户要求“我也想同步看串口”，确认串口后直接让工具自动拉起或复用 `observe-serial`
- AI 侧优先使用不带 `raw_live` 的 `watch-serial`
- 不要在用户还在看串口时调用 `serial-broker-stop`
- `observe-serial` 启动的 broker 会标记为人工观察会话；`serial-broker-stop` 默认拒绝停止，只有用户确认允许断开时才传 `force=true`
- 如果用户要求“观察串口不要被调试打断”，先确保 `observe-serial` 已启动，再做后续控制动作

## 6. Response Rule

调用 `agent-call` 时：

- 外层 shell 退出码不代表业务动作是否成功
- 必须检查 JSON 里的：
  - `ok`
  - `exit_code`
  - `error`

如果动作产生 session，还应进一步读取：

- `session_dir`
- `summary_path`
- `report_path`
- `summary`

## 7. Recommended AI Workflow

如果 AI 第一次接管这个工具，建议：

1. 调 `describe-agent-tool --format json`
2. 确认用户提供了哪些连接参数
3. MCP 场景先调 `autodbg_prepare`
4. 如果 `missing_required` 非空，先向用户提问，不要直接猜参数
5. 先判断用户目标属于：
   - test case evidence
   - development deploy/package pull
   - startup debug
   - health audit
   - deploy and verify
   - file retrieval
   - serial observation
6. 组织 `agent-call` JSON 请求
7. 只根据 JSON 响应里的 `ok / exit_code / summary / error` 判断结果

`autodbg_prepare` 是 Agent 的参数引导入口：

- 输入可以是 `action`，也可以先只给 `goal`
- 输出 `missing_required / recommended / user_questions / suggested_request`
- 一次最多问用户 3 个缺失必填项
- 不要猜串口、密码、远端路径、本地源文件、session 目录或 stop 目标

如果目标是自动多轮调试，再额外遵守：

1. 每轮结束后优先读取 `summary.result`
2. 串口判断优先读取 `summary.run_results.observation.marker_verdict / marker_windows`
3. 如果 `decision != success`，优先复用 `summary.result.carry_forward_request`
4. 本轮改了代码、配置或现场条件后，用 `record-intervention` 写回结构化干预
5. 下一轮请求在顶层 `loop` 里显式带上 `prev_session / iteration / goal`

## 8. Safe Defaults

- 默认 profile 入口：`<project_root>\profiles\defaults.toml`
- 默认 settings：`<project_root>\config\user-settings.toml`
- 默认 session root：`<project_root>\artifacts`
- 默认 fetched files root：`<project_root>\retrieved`

## 9. Typical AI Requests

### 9.1 Run Startup Check

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

### 9.2 Observe Shared Serial

```json
{
  "schema_version": 1,
  "action": "watch-serial",
  "connection": {
    "serial_port": "COM19"
  },
  "options": {
    "follow": true,
    "tail": 0
  }
}
```

这个请求会跟随共享 trace；如果该串口还没有 `observe-serial` 会话，工具会先唤醒系统默认终端打开：

```powershell
observe-serial
```

### 9.3 Fetch Device File

```json
{
  "schema_version": 1,
  "action": "fetch-file",
  "connection": {
    "serial_port": "COM19",
    "device_password": "your-root-password"
  },
  "options": {
    "remote_path": "/etc/wlanname"
  }
}
```

### 9.4 Continue Next Iteration

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

### 9.5 Record Intervention

```json
{
  "schema_version": 1,
  "action": "record-intervention",
  "options": {
    "session_dir": "artifacts/20260420/091530123-av130n-lab-startup_check-abcd",
    "kind": "ai_patch",
    "summary": "Adjust serial login timing",
    "file": [
      "src/autodbg/cli/main.py"
    ],
    "expected_effect": "The next run should establish shell access."
  }
}
```

## 10. Source Of Truth

对 AI 来说，优先级应该是：

1. `describe-agent-tool --format json`
2. `docs/mcp-tool.md`
3. `docs/agent-tool-contract.md`
4. 这份文件
5. `README.md`
