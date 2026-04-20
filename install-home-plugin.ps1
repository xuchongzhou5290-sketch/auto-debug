param(
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$HomeRoot = $HOME,
    [string]$PluginName = "embedded-device-auto-debug-mcp"
)

$ErrorActionPreference = "Stop"

$pythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python entrypoint does not exist: $pythonExe"
}

& $pythonExe -m autodbg install-home-plugin --project-root $ProjectRoot --home-root $HomeRoot --plugin-name $PluginName
exit $LASTEXITCODE
