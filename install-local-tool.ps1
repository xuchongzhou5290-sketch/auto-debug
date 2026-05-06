param(
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\auto-debug"),
    [string]$PythonExe,
    [switch]$SkipVenv,
    [switch]$SkipLocalSettings,
    [switch]$SkipHomePlugin,
    [switch]$ForceCloseInUseProcesses,
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"

function Get-AutodbgFullPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    return [System.IO.Path]::GetFullPath($Path)
}

function Get-AutodbgPythonArgs {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$PythonCommand
    )

    if ($PythonCommand.Count -le 1) {
        return @()
    }
    return @($PythonCommand[1..($PythonCommand.Count - 1)])
}

function Invoke-AutodbgPython {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$PythonCommand,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $allArgs = @()
    $allArgs += Get-AutodbgPythonArgs -PythonCommand $PythonCommand
    $allArgs += $Arguments
    & $PythonCommand[0] @allArgs
}

function Test-AutodbgPythonCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$PythonCommand
    )

    try {
        Invoke-AutodbgPython -PythonCommand $PythonCommand -Arguments @(
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        ) *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Resolve-AutodbgBootstrapPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPath,
        [string]$RequestedPython
    )

    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($RequestedPython)) {
        $candidates += ,@($RequestedPython)
    }

    $sourcePython = Join-Path $RootPath ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $sourcePython) {
        $candidates += ,@($sourcePython)
    }

    $candidates += ,@("py", "-3.11")
    $candidates += ,@("py", "-3")
    $candidates += ,@("python")

    foreach ($candidate in $candidates) {
        if (Test-AutodbgPythonCommand -PythonCommand $candidate) {
            return [string[]]$candidate
        }
    }

    throw "Python 3.11+ was not found. Install Python 3.11+ or rerun with -PythonExe <path-to-python.exe>."
}

function Format-AutodbgPythonCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$PythonCommand
    )

    return ($PythonCommand -join " ")
}

function Remove-AutodbgPathInsideRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPath,
        [Parameter(Mandatory = $true)]
        [string]$TargetPath
    )

    $rootFull = (Get-AutodbgFullPath -Path $RootPath).TrimEnd('\')
    $targetFull = (Get-AutodbgFullPath -Path $TargetPath).TrimEnd('\')
    $rootPrefix = "$rootFull\"

    if (-not $targetFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside install root: $targetFull"
    }

    if (Test-Path -LiteralPath $targetFull) {
        Remove-Item -LiteralPath $targetFull -Recurse -Force
    }
}

function Get-AutodbgInstallProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPath
    )

    $normalizedRoot = (Get-AutodbgFullPath -Path $RootPath).TrimEnd('\')
    $rootPrefix = "$normalizedRoot\"

    $matched = Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and (
            ($_.ExecutablePath -and (Get-AutodbgFullPath -Path $_.ExecutablePath).StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) -or
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

if ($SkipVenv) {
    throw "-SkipVenv is no longer supported for local deployment because the generated wrappers and MCP launcher require an installed Python runtime."
}

$ProjectRoot = Get-AutodbgFullPath -Path $ProjectRoot
$InstallRoot = Get-AutodbgFullPath -Path $InstallRoot
if ($ProjectRoot.TrimEnd('\').Equals($InstallRoot.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "InstallRoot must be different from ProjectRoot because deployment recreates the install .venv."
}
$bootstrapPython = Resolve-AutodbgBootstrapPython -RootPath $ProjectRoot -RequestedPython $PythonExe

Write-Output "[ oo... ] 2/5 steps"
Write-Output "[DONE] Project root: $ProjectRoot"
Write-Output "[DONE] Install root: $InstallRoot"
Write-Output "[DONE] Bootstrap Python: $(Format-AutodbgPythonCommand -PythonCommand $bootstrapPython)"

Confirm-AutodbgInstallProcessesClosed -RootPath $InstallRoot -ForceClose:$ForceCloseInUseProcesses

$existingPythonPath = $env:PYTHONPATH
$projectPythonPath = "$ProjectRoot;$ProjectRoot\src"
if ([string]::IsNullOrWhiteSpace($existingPythonPath)) {
    $env:PYTHONPATH = $projectPythonPath
} else {
    $env:PYTHONPATH = "$projectPythonPath;$existingPythonPath"
}

$installArgs = @(
    "-m", "autodbg",
    "install-local-tool",
    "--project-root", $ProjectRoot,
    "--install-root", $InstallRoot,
    "--skip-venv"
)
if ($SkipLocalSettings) {
    $installArgs += "--skip-local-settings"
}

Invoke-AutodbgPython -PythonCommand $bootstrapPython -Arguments $installArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$installVenv = Join-Path $InstallRoot ".venv"
$installPython = Join-Path $installVenv "Scripts\python.exe"
Write-Output "[ ooo.. ] 3/5 steps"
Write-Output "[ACTIVE] Creating isolated install runtime: $installVenv"
Remove-AutodbgPathInsideRoot -RootPath $InstallRoot -TargetPath $installVenv
Invoke-AutodbgPython -PythonCommand $bootstrapPython -Arguments @("-m", "venv", $installVenv)
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
if (-not (Test-Path -LiteralPath $installPython)) {
    throw "Installed Python runtime was not created: $installPython"
}

if (-not $SkipDependencyInstall) {
    Write-Output "[ACTIVE] Installing auto-debug package and runtime dependencies"
    & $installPython -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    $packageSpec = "{0}[full]" -f $InstallRoot
    & $installPython -m pip install --upgrade --editable $packageSpec
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} else {
    Write-Output "[TODO] Dependency installation skipped; serial/network commands may fail until dependencies are installed."
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
