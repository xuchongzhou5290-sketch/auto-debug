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

$env:AUTO_DBG_PROJECT_ROOT = $projectRoot
& $pythonExe -u $serverScript
exit $LASTEXITCODE
