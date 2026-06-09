# 实时串口观测手册

适用场景：

- 只想看设备串口
- 想边看边跑 `run / exec / health / collect-evidence`
- 想排查“为什么串口被占用”

## 1. 先做一次基础准备

```powershell
cd <repo-root>
$env:AUTO_DBG_SERIAL_PORT = "COM19"
```

如果设备登录需要密码，再补：

```powershell
$env:AUTO_DBG_DEVICE_PASSWORD = "your-root-password"
```

如果你的目标是“长期看串口，而且不受调试命令开关串口影响”，直接先开这一条：

```powershell
.\observe-serial.ps1
```

这条脚本会自动：

- 在命令行里显示一个类窗口选择界面，先选串口和常用波特率
- 从 `config\user-settings.toml` 或 `AUTO_DBG_SERIAL_PORT` 解析串口
- 启动或复用受保护 raw-live broker
- 将新启动的 broker 标记为人工观察会话，普通 `serial-broker stop` 不会直接踢掉这个窗口
- 持续输出共享串口 trace
- 优先直连 broker 的实时 trace 推流，不再只靠 0.3 秒轮询 trace 文件
- 串口 trace 默认保存到 `<project_root>\autodbg\serial-log\<COMXX>\trace.jsonl`
- 在观察窗口里可以直接输入 shell 命令，按回车发送到串口
- 空回车会先尝试重连串口，再发送一个换行探针，并显示连接/发送结果
- `Ctrl+L` 会按当前 device profile 走一遍自动登录流程
- 如果该设备密码不对，窗口会提示你重新输入密码并自动重试登录
- 观察窗口现在是全屏 TUI：顶部标题，中间日志区，底部固定显示状态行和输入行
- `Ctrl+P` 可以暂停/恢复屏幕刷新；暂停期间新串口数据继续缓存，恢复后一次性回到最新输出
- `PgUp / PgDn` 可以在 TUI 里翻历史日志，不再依赖终端滚动条
- 超出窗口宽度的串口行会自动换行显示，不再静默截断
- 热键提示、登录失败和重试密码状态都会收在状态行里，不再额外刷多行提示
- `Sent newline probe` 这类短状态会在空闲几秒后自动切回常驻操作提示
- 默认从 live edge 开始，不会先重放旧 trace

选择方式：

- `Up/Down`：移动
- `Enter`：确认
- `Esc` / `q`：取消
- 进入 TUI 时会清当前可见区域，背景色保持和原终端一致

这一步是“每次调试会话开始前做一次”，不是每次 `run / exec / health` 前都重开一次。

如果场景是“AI 在后台调试，人工也要实时看串口”，这里有一个硬规则：

- 如果目标串口还没有 `observe-serial` 会话，AI 会先唤醒系统默认终端打开 `observe-serial`
- AI 再用 `watch-serial` 跟随共享 trace，或直接执行会复用 broker 的 `run / exec / health / collect-evidence`
- 人工窗口还在看时，不要主动执行 `serial-broker stop`
- 如果确实要释放人工观察 broker，需要确认可以断开后执行 `serial-broker stop --serial-port COM19 --force`

如果你想先回看历史，再显式加：

```powershell
.\observe-serial.ps1 -Tail 40
```

如果你不想进入交互选择，也可以直接传：

```powershell
.\observe-serial.ps1 -NoUi -SerialPort COM19 -Baudrate 115200
```

## 2. 先选模式

- 短时间抓一段串口：
  - `observe --seconds 10`
- 直接实时盯物理串口：
  - `observe --live --follow`
- 边看边控，但只看 `autodbg` 的共享 `TX/RX`：
  - `watch-serial --follow`
- 想看全量原始串口流，同时允许 `autodbg` 共用同一物理串口：
  - `watch-serial --raw-live --follow`
- 怀疑后台 broker 没关：
  - `serial-broker list`
  - `serial-broker stop --serial-port COM19`

## 3. 常用命令

### 3.1 抓 10 秒串口

```powershell
.\.venv\Scripts\python -m autodbg observe --seconds 10
```

### 3.2 实时看物理串口

```powershell
.\.venv\Scripts\python -m autodbg observe --live --follow
```

特点：

- 直接打开物理串口
- 停止方式：`Ctrl+C`
- 适合只想看设备自己刷什么

### 3.3 边看边控，但不额外抢物理串口

```powershell
.\.venv\Scripts\python -m autodbg watch-serial --tail 40 --follow
```

特点：

- 只 tail 共享 trace
- 不直接打开物理串口
- 如果当前串口还没有 `observe-serial`，`--follow` 会先唤醒系统默认终端打开 `observe-serial`
- 适合 AI 侧查看，不影响后续人工观察窗口

### 3.4 看全量原始串口流，同时继续控制设备

```powershell
.\observe-serial.ps1
.\.venv\Scripts\python -m autodbg watch-serial --tail 0 --follow
```

这个模式会：

- 由 `observe-serial` 启动或复用本地受保护 broker
- 由 `observe-serial` 持续占用真实 `COM19`
- 让后续 `autodbg` 控制命令通过 broker 复用同一个串口
- 进入全屏 TUI：顶部是标题，中央是实时日志，底部固定显示状态和输入行
- `Ctrl+L` 登录状态、密码重试提示都只会出现在底部状态行，不再把日志刷乱
- `Ctrl+P` 暂停/恢复屏幕刷新，长串口行会按窗口宽度自动换行

注意：

- 这不是“线程泄漏”
- 这是设计上的长期占口模式
- 如果你把 observe 窗口留在后台，后面就会看起来像“串口一直被占用”；这是人工观察窗口在持有串口

### 3.5 测试模式切 case 证据

测试模式不刷固件，默认复用连续主 trace，然后为单个 case 写分片：

```powershell
.\.venv\Scripts\python -m autodbg case-begin --serial-port COM19 --case-id wifi_case_001 --title "WiFi reconnect"
# 执行测试 case
.\.venv\Scripts\python -m autodbg case-end --serial-port COM19 --case-id wifi_case_001 --result fail
.\.venv\Scripts\python -m autodbg case-capture --serial-port COM19 --case-id wifi_case_001 --focus panic --before 20 --after 40
```

输出目录：

```text
<project_root>\autodbg\serial-log\COM19\cases\<timestamp-wifi_case_001>\
```

`case-end` 会生成 `metadata.json / trace.jsonl / trace.txt / evidence_patch.md`，适合直接贴给 AI 或测试记录。

### 3.6 回收遗留 broker

```powershell
.\.venv\Scripts\python -m autodbg serial-broker list
.\.venv\Scripts\python -m autodbg serial-broker stop --serial-port COM19
.\.venv\Scripts\python -m autodbg serial-broker stop --serial-port COM19 --force
```

## 4. 常用变体

只看 marker：

```powershell
.\.venv\Scripts\python -m autodbg observe --live --follow --markers-only
```

按关键词聚焦：

```powershell
.\.venv\Scripts\python -m autodbg observe --live --follow --focus VQE --focus mmc
```

设备太安静，重连后顺手敲一个换行：

```powershell
.\.venv\Scripts\python -m autodbg observe --live --follow --poke-newline
```

### 4.1 自动 marker 判断

`run` 会把串口观察结果写入 `summary.run_results.observation`：

- `marker_verdict`: `success` / `fatal` / `null`
- `marker_windows`: 命中 marker 前后若干行上下文

机型 profile 可以声明：

```toml
[markers]
success = ["fw verify ok", "ready"]
fatal = ["assert", "stack overflow", "hard fault"]
context_lines = 5
```

这样上层 AI 不需要把整段串口日志塞进上下文，直接读取结构化 verdict 和关键切片即可。

## 5. 结论

- `observe`：默认先确保 `observe-serial` 受保护 broker 存在，再复用它观察串口
- `watch-serial`：默认只看共享 trace；`--follow` 时如果缺少 `observe-serial` 会自动唤醒一个终端窗口
- `watch-serial --raw-live`：仅用于 `observe-serial` 或显式人工接管
- `observe-serial.ps1`：推荐的一键长期观察入口
- `serial-broker stop`：释放遗留 raw-live broker；人工观察 broker 默认会拒绝停止，需要 `--force`
- 人工 + AI 协同时：默认由 `observe-serial` 持有串口；缺少时 AI 自动拉起，之后 AI 用 `watch-serial` 或控制命令复用该 broker

如果你已经在当前 shell 里设置了 `AUTO_DBG_SERIAL_PORT`，那么大多数串口主命令都不再需要重复传四个 profile 路径。
