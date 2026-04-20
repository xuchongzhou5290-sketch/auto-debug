param(
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\auto-debug"),
    [switch]$SkipVenv,
    [switch]$SkipLocalSettings,
    [switch]$SkipHomePlugin
)

$ErrorActionPreference = "Stop"

$pythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python entrypoint does not exist: $pythonExe"
}

$existingPythonPath = $env:PYTHONPATH
$projectPythonPath = "$ProjectRoot;$ProjectRoot\src"
if ([string]::IsNullOrWhiteSpace($existingPythonPath)) {
    $env:PYTHONPATH = $projectPythonPath
} else {
    $env:PYTHONPATH = "$projectPythonPath;$existingPythonPath"
}

$args = @(
    "-m", "autodbg",
    "install-local-tool",
    "--project-root", $ProjectRoot,
    "--install-root", $InstallRoot
)
if ($SkipVenv) {
    $args += "--skip-venv"
}
if ($SkipLocalSettings) {
    $args += "--skip-local-settings"
}

& $pythonExe @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

[Environment]::SetEnvironmentVariable("AUTO_DBG_HOME", $InstallRoot, "User")
[Environment]::SetEnvironmentVariable("AUTO_DBG_PROJECT_ROOT", $InstallRoot, "User")

$binDir = Join-Path $InstallRoot "bin"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathItems = @()
if (-not [string]::IsNullOrWhiteSpace($userPath)) {
    $pathItems = $userPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
}
if ($pathItems -notcontains $binDir) {
    $updatedPath = @($pathItems + $binDir) -join ';'
    [Environment]::SetEnvironmentVariable("Path", $updatedPath, "User")
}

if (-not $SkipHomePlugin) {
    $installedPluginScript = Join-Path $InstallRoot "install-home-plugin.ps1"
    if (-not (Test-Path -LiteralPath $installedPluginScript)) {
        throw "Installed home plugin script does not exist: $installedPluginScript"
    }
    & $installedPluginScript -ProjectRoot $InstallRoot -HomeRoot $HOME
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Write-Output "[ oooo. ] 4/5 steps"
Write-Output "[DONE] User environment updated: AUTO_DBG_HOME / AUTO_DBG_PROJECT_ROOT"
Write-Output "[DONE] User PATH updated: $binDir"
if (-not $SkipHomePlugin) {
    Write-Output "[DONE] Home plugin refreshed from installed local tool"
}
Write-Output "[TODO] Open a new shell before calling autodbg directly."
