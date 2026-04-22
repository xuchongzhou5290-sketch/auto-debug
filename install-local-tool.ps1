param(
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\auto-debug"),
    [switch]$SkipVenv,
    [switch]$SkipLocalSettings,
    [switch]$SkipHomePlugin,
    [switch]$ForceCloseInUseProcesses
)

$ErrorActionPreference = "Stop"

function Get-AutodbgInstallProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPath
    )

    $normalizedRoot = [System.IO.Path]::GetFullPath($RootPath).TrimEnd('\')
    $rootPrefix = "$normalizedRoot\"

    $matched = Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and (
            ($_.ExecutablePath -and [System.IO.Path]::GetFullPath($_.ExecutablePath).StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) -or
            ($_.CommandLine -and $_.CommandLine.IndexOf($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
        )
    }

    return @($matched | Sort-Object ProcessId -Unique)
}

function Stop-AutodbgInstallProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [array]$Processes
    )

    foreach ($process in $Processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    }

    $remainingIds = @($Processes | ForEach-Object { $_.ProcessId })
    if ($remainingIds.Count -gt 0) {
        Wait-Process -Id $remainingIds -Timeout 5 -ErrorAction SilentlyContinue
    }
}

function Confirm-AutodbgInstallProcessesClosed {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPath,
        [switch]$ForceClose
    )

    $processes = @(Get-AutodbgInstallProcesses -RootPath $RootPath)
    if ($processes.Count -eq 0) {
        return
    }

    Write-Output "[ oo... ] 2/5 steps"
    Write-Output "[ACTIVE] Detected running processes under the install root:"
    foreach ($process in $processes | Select-Object -First 8) {
        $summary = if ($process.ExecutablePath) { $process.ExecutablePath } else { $process.CommandLine }
        Write-Output ("[TODO] PID {0} {1} :: {2}" -f $process.ProcessId, $process.Name, $summary)
    }
    if ($processes.Count -gt 8) {
        Write-Output "[TODO] Additional matching processes: $($processes.Count - 8)"
    }

    if ($ForceClose) {
        Write-Output "[ACTIVE] Force closing install-root processes before reinstall"
        Stop-AutodbgInstallProcesses -Processes $processes
        return
    }

    try {
        $choices = @(
            (New-Object System.Management.Automation.Host.ChoiceDescription "&Yes", "Force close the running auto-debug processes and continue."),
            (New-Object System.Management.Automation.Host.ChoiceDescription "&No", "Stop installation and close them manually.")
        )
        $choice = $Host.UI.PromptForChoice(
            "auto-debug install",
            "Detected $($processes.Count) running process(es) under $RootPath. Force close them before reinstall?",
            $choices,
            1
        )
    } catch {
        throw "Detected running processes under $RootPath. Close them manually or rerun with -ForceCloseInUseProcesses."
    }

    if ($choice -ne 0) {
        throw "Installation cancelled because files under $RootPath are still in use."
    }

    Write-Output "[ACTIVE] Force closing install-root processes before reinstall"
    Stop-AutodbgInstallProcesses -Processes $processes
}

$pythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python entrypoint does not exist: $pythonExe"
}

Confirm-AutodbgInstallProcessesClosed -RootPath $InstallRoot -ForceClose:$ForceCloseInUseProcesses

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
