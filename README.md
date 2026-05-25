# auto-debug

`auto-debug` 是一个面向嵌入式调试现场的宿主机自动化工具。

当前重点不是“写业务代码”，而是把这段重复劳动收成稳定命令链：

- 建 session
- 看串口
- 登 shell
- 跑检查
- 收证据
- 拉文件
- 汇总结论

## 当前范围

- 单设备
- 单默认 profile 组
- 传输自动选择优先级：SD 卡 HTTP helper、设备 downloader、串口 base64+tar bundle
- session 化产物归档
- 以项目根目录相对路径运行，不依赖旧工作区路径

## 最小必填项

要把工具跑起来，原则上只需要这些现场信息：

- 工具根目录：本地部署后默认是 `%LOCALAPPDATA%\Programs\auto-debug`
- 串口：例如 `COM19`
- 设备密码：如果串口登录需要密码
- Wi-Fi 信息：只有在要跑 `bootstrap-network --mode wlan_script` 或 `device-pull --mode wlan_script` 时才需要
- SD 卡盘符：只有在要跑 `stage-sd` 时才需要

不再要求每次显式传四个 profile 路径。

## 默认 Profile 机制

默认 profile 入口定义在：

```powershell
<repo-root>\profiles\defaults.toml
```

默认情况下，`run / observe / exec / health / collect-evidence / device-pull / stage-sd` 这类命令会自动从这里解析：

- device profile
- model profile
- task profile
- transport profile

只有在你想临时切换 profile 时，才需要显式传：

```powershell
--device ...
--model ...
--task ...
--transport ...
```

## 配置优先级

同一项配置的优先级如下：

1. CLI 参数
2. 环境变量
3. `config\user-settings.toml`
4. profile 默认值

这意味着新会话里，AI 只要知道必要输入，直接设置环境变量就能跑，不一定非得先改文件。

## 常用环境变量

- `AUTO_DBG_SERIAL_PORT`
- `AUTO_DBG_SERIAL_BAUDRATE`
- `AUTO_DBG_DEVICE_PASSWORD`
- `AUTO_DBG_WIFI_SSID`
- `AUTO_DBG_WIFI_PASSWORD`
- `AUTO_DBG_WIFI_MODE`
- `AUTO_DBG_NETWORK_DIR`
- `AUTO_DBG_HOST_IP`
- `AUTO_DBG_PULL_BASE_URL`
- `AUTO_DBG_PULL_WORKSPACE`
- `AUTO_DBG_PREFERRED_INTERFACES`
- `AUTO_DBG_SDCARD_DRIVE`
- `AUTO_DBG_RETRIEVED_ROOT`

本地配置不要直接改进仓库，建议先从模板复制：

```powershell
Copy-Item .\config\user-settings.example.toml .\config\user-settings.toml
```

示例：

```powershell
$env:AUTO_DBG_SERIAL_PORT = "COM19"
$env:AUTO_DBG_DEVICE_PASSWORD = "your-root-password"
$env:AUTO_DBG_WIFI_SSID = "xiaotudou"
$env:AUTO_DBG_WIFI_PASSWORD = "12345678"
$env:AUTO_DBG_WIFI_MODE = "WPA2"
```

`AUTO_DBG_PREFERRED_INTERFACES` 使用逗号分隔，例如：

```powershell
$env:AUTO_DBG_PREFERRED_INTERFACES = "eth0,wlan0,usb0"
```

`AUTO_DBG_RETRIEVED_ROOT` 支持相对路径；如果没配，默认回收到项目下的 `retrieved\`。

## 快速开始

## 源码运行说明

仓库根目录下的 `autodbg\` 只是源码 checkout 的开发期入口 shim，用来让 `python -m autodbg ...` 在未安装包时也能转到 `src\autodbg`。正式打包仍以 `pyproject.toml` 中的 `package-dir = {"" = "src"}` 为准。

### 1. 先做一次本地部署

如果你手上是源码仓，先执行：

```powershell
cd <repo-root>
.\install-local-tool.ps1
```

前置条件：

- Windows PowerShell
- Python 3.11+，可用 `python`、`py -3.11`，或通过 `-PythonExe` 显式指定

如果安装目录下已有正在运行的 `auto-debug` / MCP 进程，脚本现在会先提示是否强制关闭后再继续。
如果你要在自动化场景里直接强制处理占用，可以显式加：

```powershell
.\install-local-tool.ps1 -ForceCloseInUseProcesses
```

已经部署过 MCP 后，更新本地安装版时建议显式指定源码路径和安装路径，避免旧环境变量把 home-local MCP 刷到错误位置：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA "Programs\auto-debug"
.\install-local-tool.ps1 -ProjectRoot (Resolve-Path .).Path -InstallRoot $installRoot -ForceCloseInUseProcesses
```

如果只刷新已部署的 MCP 插件，也要显式指定安装版根目录：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA "Programs\auto-debug"
& (Join-Path $installRoot "install-home-plugin.ps1") -ProjectRoot $installRoot -HomeRoot $HOME
```

它会自动：

- 复制独立运行所需内容到 `%LOCALAPPDATA%\Programs\auto-debug`
- 创建安装版独立运行时：`%LOCALAPPDATA%\Programs\auto-debug\.venv`
- 安装 `auto-debug[full]` 依赖
- 写入用户环境变量：
  - `AUTO_DBG_HOME`
  - `AUTO_DBG_PROJECT_ROOT`
- 把 `%LOCALAPPDATA%\Programs\auto-debug\bin` 加到用户 `Path`
- 刷新 home-local MCP plugin，让 Codex 和别的 Agent 直接接到本地安装版

执行完后，打开一个新的 PowerShell 窗口。

### 2. 提供最小现场参数

```powershell
$env:AUTO_DBG_SERIAL_PORT = "COM19"
$env:AUTO_DBG_DEVICE_PASSWORD = "your-root-password"
```

如果你更习惯改文件，也可以编辑：

```powershell
$env:AUTO_DBG_HOME\config\user-settings.toml
```

### 3. 直接调用

```powershell
autodbg show-mvp
autodbg ports
autodbg run
observe-serial
```

如果你暂时还在源码模式里运行，也可以继续用：

```powershell
cd <repo-root>
.\.venv\Scripts\python -m autodbg show-mvp
.\.venv\Scripts\python -m autodbg ports
.\.venv\Scripts\python -m autodbg run
```

`run` 现在会自动：

- 建立 session
- 观察启动串口
- 跑 baseline checks
- 执行默认 evidence commands
- 拉回默认 evidence 小文件
- 写出 `summary.json / report.md / events.jsonl / logs / retrieved`

如果只想保留轻量启动检查：

```powershell
.\.venv\Scripts\python -m autodbg run --skip-evidence
```

## Agent 调用入口

如果目标是让别的 AI Agent 调用这个工具，不建议让它直接拼 CLI 细节。

统一入口已经补成：

```powershell
.\.venv\Scripts\python -m autodbg agent-call --request request.json --pretty
```

或者：

```powershell
Get-Content .\docs\examples\agent-call-run.json | .\.venv\Scripts\python -m autodbg agent-call --request - --pretty
```

这个入口会：

- 接收结构化 JSON 请求
- 自动翻译成现有 CLI 调用
- 注入必要连接参数
- 返回结构化 JSON 响应

补充规则：

- 请求里的相对路径会按 `project_root` 解析；走 MCP / 已安装插件时，这个根目录就是 `AUTO_DBG_PROJECT_ROOT`
- 如果要做自动多轮调试，使用顶层 `loop` 字段传 `prev_session / iteration / goal`
- 每轮代码或配置修改后，可以用 `record-intervention` 追加结构化干预记录
- 串口主入口现在默认 broker-first：AI 执行 `run / exec / health / collect-evidence / device-pull / observe` 时会优先启动或复用 raw-live broker，再通过 broker 控制串口
- 如果 AI 已经在后台执行串口动作，用户可以再开 `observe-serial` 或 `watch-serial --follow` 连接同一个 broker，而不是直接抢物理 COM 口

详细约定见：

- `docs\agent-tool-contract.md`
- `docs\embedded-device-auto-debug-ai-tool.md`
- `docs\examples\agent-call-run.json`
- `docs\examples\agent-call-run-loop.json`
- `docs\examples\agent-call-record-intervention.json`

如果要让新的 AI 会话先快速理解这个工具，再决定怎么调用，推荐先执行：

```powershell
.\.venv\Scripts\python -m autodbg describe-agent-tool --format json
```

MCP Agent 场景下，真正执行前应先走参数引导：

```json
{
  "tool": "autodbg_prepare",
  "arguments": {
    "action": "run"
  }
}
```

如果返回 `missing_required`，Agent 应先问用户这些参数；`ready=true` 后再调用 `autodbg_action`。

如果要把它直接挂成 MCP Tool，再让别的 Agent 走工具调用而不是自己拼命令，直接看：

- `docs\mcp-tool.md`
- `plugins\embedded-device-auto-debug-mcp\.codex-plugin\plugin.json`
- `.agents\plugins\marketplace.json`

如果你要的是“任何工作区都能直接装”的全局插件，当前也已经有 home-local 版本：

- `%USERPROFILE%\plugins\embedded-device-auto-debug-mcp`
- `%USERPROFILE%\.agents\plugins\marketplace.json`

现在也补了自动安装/升级入口。最推荐的入口已经不是直接刷 home plugin，而是先做本地部署：

```powershell
cd <repo-root>
.\install-local-tool.ps1
```

如果你只想刷新 home plugin，也可以：

```powershell
install-home-plugin
```

如果独立安装目录或 home 目录需要显式指定：

```powershell
.\install-local-tool.ps1 -ProjectRoot D:\Auto-Debug -InstallRoot C:\Tools\auto-debug
.\install-home-plugin.ps1 -ProjectRoot C:\Tools\auto-debug -HomeRoot C:\Users\YourName
```

## 常用命令

### 快捷引导

刚接触工具时，先跑快捷引导，不需要先记 action 名称：

```powershell
.\.venv\Scripts\python -m autodbg quickstart --goal "观察设备启动日志"
.\.venv\Scripts\python -m autodbg quickstart --goal "升级后验证" --serial-port COM19 --device-password-known --artifact .\payloads\APP.bin
.\.venv\Scripts\python -m autodbg quickstart --goal "拉取新包" --serial-port COM19 --device-password-known --artifact .\payloads\APP.bin
.\.venv\Scripts\python -m autodbg quickstart --goal "拉取新包" --debug-firmware-method sd_http_helper --serial-port COM19 --device-password-known --artifact .\payloads\APP.bin --helper-cc arm-linux-gnueabihf-gcc --sdcard-drive E:
.\.venv\Scripts\python -m autodbg quickstart --goal "拉取新包" --debug-firmware-method firmware_command --serial-port COM19 --device-password-known --artifact .\payloads\APP.bin
```

MCP Agent 可以直接调用：

```json
{
  "schema_version": 1,
  "action": "quickstart",
  "options": {
    "goal": "健康检查"
  }
}
```

`quickstart` 会返回检测到的串口、最多 3 个待询问问题，以及下一步建议的 MCP request。AI 拿到 `next_requests` 后应先对选中的请求调用 `autodbg_prepare`，确认参数齐全后再执行 `autodbg_action`。

当目标是“拉取/下发新包”时，`quickstart` 会强制先选择 `debug_firmware_method`：

- `firmware_command`：调试固件已集成拉取新包指令或 downloader，直接走固件/下载器拉取路径，不构建 SD helper
- `sd_http_helper`：设备端没有可靠拉取指令时，先 `build-sd-http-helper -> stage-sd`，把 Linux 可执行文件放入 SD 卡，再 `device-pull`

### 串口观察

```powershell
.\observe-serial.ps1
.\observe-serial.ps1 -SerialPort COM20
.\observe-serial.ps1 -SerialPort COM19 -Baudrate 115200 -Tail 80
.\.venv\Scripts\python -m autodbg observe --seconds 10
.\.venv\Scripts\python -m autodbg observe --live --follow
.\.venv\Scripts\python -m autodbg observe --live --follow --focus VQE --focus mmc
.\.venv\Scripts\python -m autodbg observe --live --follow --poke-newline
```

推荐长期调试时先敲一次：

```powershell
.\observe-serial.ps1
```

它会：

- 在命令行里显示一个类窗口选择界面，先选当前串口和常用波特率
- 启动或复用 `COM19` 的 raw-live broker
- 将 `observe-serial` 启动的新 broker 标记为人工观察会话，普通 `serial-broker stop` 不会直接踢掉这个窗口
- 打开持续观察窗口
- 让后续 `autodbg run / exec / health / collect-evidence` 自动复用同一个 broker
- `watch-serial` 会优先直连 broker 的实时 trace 推流，不再只靠轮询 trace 文件
- 在观察窗口里可以直接输入 shell 命令，按回车发送到串口
- 空回车会先尝试重连串口，再发送一个换行探针，并直接显示连接/发送结果
- `Ctrl+L` 会按当前 device profile 走一遍自动登录流程
- 如果该设备密码不对，窗口会提示你重新输入密码并自动重试登录
- 观察窗口现在是全屏 TUI：顶部标题，中间日志区，底部状态行和输入行固定显示
- `Ctrl+P` 可以暂停/恢复屏幕刷新；暂停期间新串口数据继续缓存，恢复后一次性回到最新输出
- `PgUp / PgDn` 可以在 TUI 里翻历史日志，不再依赖终端自身滚动条
- 超出窗口宽度的串口行会自动换行显示，不再静默截断
- 登录中、登录失败、重试密码这些状态都会收在 TUI 的状态行里，不再混进串口日志
- 像 `Sent newline probe` 这类短状态会显示几秒，然后自动回到常驻操作提示
- 默认从 live edge 开始，不会先重放旧 trace

选择方式：

- `Up/Down`：移动
- `Enter`：确认
- `Esc` / `q`：取消
- 进入 TUI 时会清当前可见区域，背景色保持和原终端一致

这一步是“每次调试会话开始前做一次”，不是每条调试命令前都做一次。

如果场景是“AI 在后台跑工具，人工也要同步看串口”，顺序固定为：

1. 人工先开 `observe-serial`
2. AI 再调 `watch-serial` 或 `run / exec / health`
3. 人工窗口还开着时，不要主动 `serial-broker stop`
4. 如果确实要强制释放人工观察 broker，先确认窗口可以断开，再执行 `serial-broker stop --serial-port COM19 --force`

如果你想先回看历史，再显式加：

```powershell
.\observe-serial.ps1 -Tail 40
```

如果你是脚本/自动化调用，不想进入交互选择，就显式传参数或加：

```powershell
.\observe-serial.ps1 -NoUi -SerialPort COM19 -Baudrate 115200
```

### 串口共享查看

```powershell
.\.venv\Scripts\python -m autodbg watch-serial --serial-port COM19 --tail 40 --follow
.\.venv\Scripts\python -m autodbg watch-serial --serial-port COM19 --tail 0 --follow --raw-live --baudrate 115200
.\.venv\Scripts\python -m autodbg serial-broker list
.\.venv\Scripts\python -m autodbg serial-broker stop --serial-port COM19
.\.venv\Scripts\python -m autodbg serial-broker stop --serial-port COM19 --force
```

### 串口控制与取证

```powershell
.\.venv\Scripts\python -m autodbg exec --shell-command "ls /mnt/sdcard"
.\.venv\Scripts\python -m autodbg health
.\.venv\Scripts\python -m autodbg collect-evidence
.\.venv\Scripts\python -m autodbg fetch-file --remote-path /etc/wlanname
.\.venv\Scripts\python -m autodbg fetch-path --remote-path /mnt/sdcard/autodbg
```

### 网络拉起与设备拉取

```powershell
.\.venv\Scripts\python -m autodbg bootstrap-network --mode wlan_script
.\.venv\Scripts\python -m autodbg serve-artifacts --port 8765 --workspace /mnt/sdcard/autodbg
.\.venv\Scripts\python -m autodbg artifact-server list
.\.venv\Scripts\python -m autodbg artifact-server stop --port 8765
.\.venv\Scripts\python -m autodbg device-pull --mode lan_ready --transfer-mode auto
.\.venv\Scripts\python -m autodbg device-pull --mode lan_ready --transfer-mode sd_http_helper
.\.venv\Scripts\python -m autodbg device-pull --mode offline --transfer-mode serial_bundle
```

`serve-artifacts` 默认后台启动并返回 `pid / base_url / health_url / log`，避免阻塞 MCP 调用链；如果需要旧式前台阻塞模式，显式加 `--foreground`。

当设备端没有 `curl / wget / busybox wget`，但可以从 SD 卡执行临时程序时，可以使用内置 C 源码构建一个最小 HTTP 拉取器：

```powershell
.\.venv\Scripts\python -m autodbg build-sd-http-helper --cc <target-gcc> --output .\artifacts\autodbg-http-pull
.\.venv\Scripts\python -m autodbg stage-sd --source .\artifacts\autodbg-http-pull --target-subdir autodbg --dest-name autodbg-http-pull
.\.venv\Scripts\python -m autodbg device-pull --mode lan_ready --transfer-mode auto --sd-http-helper-path /mnt/sdcard/autodbg/autodbg-http-pull
```

`auto` 的选择顺序是：SD 卡 HTTP helper、设备自带 downloader、串口 base64+tar bundle。探测 SD helper 时要求目标文件存在且可执行；如果 auto 已选择 SD helper 但运行失败，会在设备存在 `curl / wget / busybox wget` 时自动回退到普通 HTTP。`device-pull` 为拉取临时启动的 PC 端 HTTP 服务会在设备拉取命令返回后立即关闭，后续 list / post-pull / reboot / observe 阶段不会继续暴露该服务。强制走 SD helper 时传 `--transfer-mode sd_http_helper`。该 helper 只依赖 libc/POSIX socket，只支持普通 HTTP，不支持 HTTPS。目标 toolchain 不支持静态链接时给 `build-sd-http-helper` 加 `--no-static`。

### SD 卡落盘

```powershell
.\.venv\Scripts\python -m autodbg storage
.\.venv\Scripts\python -m autodbg stage-sd --source .\artifacts\deploy-probe.txt --target-subdir debug\autodbg
```

### 结果查看

```powershell
.\.venv\Scripts\python -m autodbg report --latest
.\.venv\Scripts\python -m autodbg summary --session-dir .\artifacts\20260417\...
.\.venv\Scripts\python -m autodbg resume --session-dir .\artifacts\20260417\...
.\.venv\Scripts\python -m autodbg agent-call --request .\docs\examples\agent-call-run.json --pretty
```

## 显式切换 Profile

如果后面 `profiles\` 下不止一套设备配置，可以继续显式指定：

```powershell
.\.venv\Scripts\python -m autodbg run `
  --device .\profiles\devices\other-device.toml `
  --model .\profiles\models\other-model.toml `
  --task .\profiles\tasks\startup-check.toml `
  --transport .\profiles\transports\network-serial-fallback.toml
```

也可以直接修改：

```powershell
<repo-root>\profiles\defaults.toml
```

## 路径依赖收口说明

这次独立项目后，已经做了这些收口：

- 默认运行目录统一按项目根解析
- `config\user-settings.toml` 的 `retrieved_root` 改成相对路径
- profile 默认入口改成 `profiles\defaults.toml`
- manifest 不再暴露宿主机绝对根路径
- `pull-probe` 静态脚本不再写死旧工作区地址

仍然属于“现场必要输入”的内容有：

- 串口名
- 设备密码
- Wi-Fi 信息
- SD 卡盘符
- 目标文件路径或设备侧远程路径

这些不是路径耦合问题，而是现场本身必须提供的信息。

## 目录说明

- `src\autodbg\`：主代码
- `config\user-settings.toml`：本机常用覆盖项
- `profiles\defaults.toml`：默认 profile 入口
- `profiles\`：设备 / 机型 / 任务 / 传输模板
- `docs\serial-live-tip.md`：实时串口观测手册
- `docs\mvp-workflow.md`：MVP 工作流说明
- `payloads\pull-probe\`：默认拉取探针样例
- `artifacts\`：session 产物
- `retrieved\`：从设备回收的文件

## 依赖

`install-local-tool.ps1` 会在安装目录内自动创建 `.venv` 并安装 `auto-debug[full]`。如果你在源码模式手工维护开发环境，建议安装：

- `pyserial`
- `requests`
- `pydantic`
- `typer`
- `rich`
