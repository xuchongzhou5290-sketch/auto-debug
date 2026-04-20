# Embedded Device Auto-Debug AI Tool

这份文件不是给人类现场操作员的，而是给 AI Agent 的。

目标：

- 用最少上下文理解 `X:\Auto-Debug`
- 知道这个工具能做什么、什么时候调用什么动作
- 知道必须提供哪些现场参数
- 知道如何判断调用是否成功

## 1. Tool Identity

- Tool name: `embedded-device-auto-debug`
- Project root: `X:\Auto-Debug`
- Stable entrypoint: `python -m autodbg agent-call --request -`
- Self-description entrypoint: `python -m autodbg describe-agent-tool --format json`
- MCP tools: `autodbg_describe`, `autodbg_action`

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

1. `watch-serial`
2. `run`
3. `report`

适用：

- 设备启动异常
- 想拿一份完整 session 和 verdict

### 4.2 Health Audit

推荐动作链：

1. `watch-serial`
2. `health`
3. `collect-evidence`
4. `summary`

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

## 5. Serial Ownership Rule

这是关键规则：

- 如果需要一个长期不断流的观察窗口，应优先使用 `watch-serial` 或 `observe-serial.ps1`
- `raw-live broker` 是物理串口的单一拥有者
- 其他 `autodbg` 命令应该复用 broker，而不是重新直接抢串口

对 AI 的实际含义：

- 如果用户要求“观察串口不要被调试打断”，先启动 broker，再做后续控制动作

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
3. 先判断用户目标属于：
   - startup debug
   - health audit
   - deploy and verify
   - file retrieval
   - serial observation
4. 组织 `agent-call` JSON 请求
5. 只根据 JSON 响应里的 `ok / exit_code / summary / error` 判断结果

## 8. Safe Defaults

- 默认 profile 入口：`X:\Auto-Debug\profiles\defaults.toml`
- 默认 settings：`X:\Auto-Debug\config\user-settings.toml`
- 默认 session root：`X:\Auto-Debug\artifacts`
- 默认 fetched files root：`X:\Auto-Debug\retrieved`

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
    "tail": 0,
    "raw_live": true,
    "stdin_shell": true
  }
}
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

## 10. Source Of Truth

对 AI 来说，优先级应该是：

1. `describe-agent-tool --format json`
2. `docs/mcp-tool.md`
3. `docs/agent-tool-contract.md`
4. 这份文件
5. `README.md`
