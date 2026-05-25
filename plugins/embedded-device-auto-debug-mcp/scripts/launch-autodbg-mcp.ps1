param(
    [string]$LogDir
)

$ErrorActionPreference = "Stop"

$projectRoot = $env:AUTO_DBG_PROJECT_ROOT
if ([string]::IsNullOrWhiteSpace($projectRoot)) {
    $projectRoot = $env:AUTO_DBG_HOME
}
if ([string]::IsNullOrWhiteSpace($projectRoot)) {
    $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..\..")).Path
}

$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$serverScript = Join-Path $PSScriptRoot "autodbg_mcp_server.py"

if (-not (Test-Path -LiteralPath $projectRoot)) {
    throw "AUTO_DBG_PROJECT_ROOT does not exist: $projectRoot"
}
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python entrypoint does not exist: $pythonExe. Run install-local-tool.ps1 first."
}
if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "MCP server script does not exist: $serverScript"
}

if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = $env:AUTO_DBG_MCP_LOG_DIR
}
if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $projectRoot ".autodbg"
}
elseif (-not [System.IO.Path]::IsPathRooted($LogDir)) {
    $LogDir = Join-Path $projectRoot $LogDir
}
$LogDir = [System.IO.Path]::GetFullPath($LogDir)
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stderrLog = Join-Path $LogDir ("mcp-stderr-{0}-{1}.log" -f $timestamp, $PID)
$launcherLog = Join-Path $LogDir "mcp-launcher.log"
Add-Content -LiteralPath $launcherLog -Encoding UTF8 -Value (
    "{0} project_root={1} log_dir={2} server={3} stderr={4}" -f
    (Get-Date).ToString("o"), $projectRoot, $LogDir, $serverScript, $stderrLog
)

$env:AUTO_DBG_PROJECT_ROOT = $projectRoot
$env:AUTO_DBG_MCP_LOG_DIR = $LogDir
& $pythonExe -u $serverScript 2>> $stderrLog
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Add-Content -LiteralPath $launcherLog -Encoding UTF8 -Value (
        "{0} exit_code={1} stderr={2}" -f (Get-Date).ToString("o"), $exitCode, $stderrLog
    )
}
exit $exitCode
