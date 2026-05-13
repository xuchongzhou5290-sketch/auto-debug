param(
    [string]$SerialPort,
    [int]$Baudrate,
    [int]$Tail = 0,
    [switch]$NoUi
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python runtime not found: $python"
}

$existingPythonPath = $env:PYTHONPATH
$projectPythonPath = "$projectRoot;$projectRoot\src"
if ([string]::IsNullOrWhiteSpace($existingPythonPath)) {
    $env:PYTHONPATH = $projectPythonPath
} else {
    $env:PYTHONPATH = "$projectPythonPath;$existingPythonPath"
}

function Get-ObserveSerialDefaults {
    $settingsPath = Join-Path $projectRoot "config\user-settings.toml"
    $defaults = [ordered]@{
        SerialPort = $null
        Baudrate   = $null
    }

    if ($env:AUTO_DBG_SERIAL_PORT) {
        $defaults.SerialPort = $env:AUTO_DBG_SERIAL_PORT
    }
    if ($env:AUTO_DBG_SERIAL_BAUDRATE -match '^\d+$') {
        $defaults.Baudrate = [int]$env:AUTO_DBG_SERIAL_BAUDRATE
    }

    if (-not (Test-Path -LiteralPath $settingsPath)) {
        return [pscustomobject]$defaults
    }

    $inSerialSection = $false
    foreach ($rawLine in Get-Content -Path $settingsPath) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        if ($line -match '^\[(.+)\]$') {
            $inSerialSection = ($Matches[1] -eq "serial")
            continue
        }
        if (-not $inSerialSection) {
            continue
        }
        if (-not $defaults.SerialPort -and $line -match '^port\s*=\s*"([^"]+)"') {
            $defaults.SerialPort = $Matches[1]
            continue
        }
        if (-not $defaults.Baudrate -and $line -match '^baudrate\s*=\s*(\d+)') {
            $defaults.Baudrate = [int]$Matches[1]
        }
    }

    return [pscustomobject]$defaults
}

function Get-ObserveSerialPortChoices {
    $descriptions = @{}
    try {
        Get-CimInstance Win32_PnPEntity -ErrorAction Stop |
            Where-Object { $_.Name -match '\((COM\d+)\)' } |
            ForEach-Object {
                $descriptions[$Matches[1].ToUpperInvariant()] = $_.Name
            }
    }
    catch {
    }

    $portNames = @()
    try {
        $portNames = [System.IO.Ports.SerialPort]::GetPortNames()
    }
    catch {
    }

    $sortedNames = $portNames |
        Sort-Object {
            if ($_ -match '^COM(\d+)$') {
                [int]$Matches[1]
            }
            else {
                [int]::MaxValue
            }
        }

    $choices = @()
    foreach ($portName in $sortedNames) {
        $normalized = $portName.ToUpperInvariant()
        $description = $descriptions[$normalized]
        $label = if ($description) { "$portName - $description" } else { $portName }
        $choices += [pscustomobject]@{
            Port  = $portName
            Label = $label
        }
    }

    if (-not $choices -and $descriptions.Count -gt 0) {
        foreach ($portName in ($descriptions.Keys | Sort-Object)) {
            $choices += [pscustomobject]@{
                Port  = $portName
                Label = "$portName - $($descriptions[$portName])"
            }
        }
    }

    return $choices
}

function Write-ObserveDialogLine {
    param(
        [int]$Left,
        [int]$Top,
        [int]$Width,
        [string]$Text = "",
        [ConsoleColor]$Foreground = [ConsoleColor]::Gray,
        [ConsoleColor]$Background = [ConsoleColor]::DarkBlue
    )

    if ($Width -le 0) {
        return
    }

    $renderText = if ($Text.Length -gt $Width) { $Text.Substring(0, $Width) } else { $Text.PadRight($Width) }
    $originalForeground = [Console]::ForegroundColor
    $originalBackground = [Console]::BackgroundColor
    try {
        [Console]::SetCursorPosition($Left, $Top)
        [Console]::ForegroundColor = $Foreground
        [Console]::BackgroundColor = $Background
        [Console]::Write($renderText)
    }
    finally {
        [Console]::ForegroundColor = $originalForeground
        [Console]::BackgroundColor = $originalBackground
    }
}

function Clear-ObserveViewport {
    param(
        [int]$Left,
        [int]$Top,
        [int]$Width,
        [int]$Height,
        [ConsoleColor]$Background,
        [ConsoleColor]$Foreground
    )

    if ($Width -le 0 -or $Height -le 0) {
        return
    }

    for ($row = 0; $row -lt $Height; $row++) {
        Write-ObserveDialogLine `
            -Left $Left `
            -Top ($Top + $row) `
            -Width $Width `
            -Background $Background `
            -Foreground $Foreground
    }
}

function Show-ObserveListDialog {
    param(
        [string]$Title,
        [string]$Prompt,
        [object[]]$Items,
        [int]$DefaultIndex = 1
    )

    if (-not $Items -or $Items.Count -eq 0) {
        return $null
    }

    $labels = @($Items | ForEach-Object { [string]$_.Label })
    $windowWidth = [Console]::WindowWidth
    $windowHeight = [Console]::WindowHeight
    $windowLeft = [Console]::WindowLeft
    $windowTop = [Console]::WindowTop
    $maxLabelWidth = ($labels | Measure-Object -Maximum Length).Maximum
    $listWidth = [Math]::Min([Math]::Max($maxLabelWidth + 4, 36), [Math]::Max(36, $windowWidth - 10))
    $visibleRows = [Math]::Min([Math]::Max(5, [Math]::Min($Items.Count, 10)), [Math]::Max(5, $windowHeight - 10))
    $dialogWidth = [Math]::Min($listWidth + 4, [Math]::Max(40, $windowWidth - 4))
    $dialogHeight = [Math]::Min($visibleRows + 7, [Math]::Max(12, $windowHeight - 2))
    $left = $windowLeft + [Math]::Max([int](($windowWidth - $dialogWidth) / 2), 0)
    $top = $windowTop + [Math]::Max([int](($windowHeight - $dialogHeight) / 2), 0)
    $listLeft = $left + 2
    $listTop = $top + 3
    $listWidth = $dialogWidth - 4
    $selectedIndex = [Math]::Max([Math]::Min($DefaultIndex - 1, $Items.Count - 1), 0)
    $scrollOffset = [Math]::Max([Math]::Min($selectedIndex - [int]($visibleRows / 2), [Math]::Max($Items.Count - $visibleRows, 0)), 0)
    $originalForeground = [Console]::ForegroundColor
    $originalBackground = [Console]::BackgroundColor
    $borderForeground = if ($originalForeground -eq $originalBackground) { [ConsoleColor]::Gray } else { $originalForeground }
    $accentForeground = if ($originalForeground -eq [ConsoleColor]::Yellow) { [ConsoleColor]::White } else { [ConsoleColor]::Yellow }
    $bodyForeground = if ($originalForeground -eq $originalBackground) { [ConsoleColor]::Gray } else { $originalForeground }
    $selectedBackground = [ConsoleColor]::DarkGray
    $selectedForeground = if ($bodyForeground -eq [ConsoleColor]::Black) { [ConsoleColor]::White } else { $bodyForeground }
    $cursorVisible = $true

    try {
        try {
            $cursorVisible = [Console]::CursorVisible
            [Console]::CursorVisible = $false
        }
        catch {
        }

        while ($true) {
            Clear-ObserveViewport `
                -Left $windowLeft `
                -Top $windowTop `
                -Width $windowWidth `
                -Height $windowHeight `
                -Background $originalBackground `
                -Foreground $bodyForeground

            for ($row = 0; $row -lt $dialogHeight; $row++) {
                Write-ObserveDialogLine -Left $left -Top ($top + $row) -Width $dialogWidth -Background $originalBackground -Foreground $bodyForeground
            }

            Write-ObserveDialogLine -Left $left -Top $top -Width $dialogWidth -Text ("+" + ("-" * ($dialogWidth - 2)) + "+") -Foreground $borderForeground -Background $originalBackground
            for ($row = 1; $row -lt ($dialogHeight - 1); $row++) {
                Write-ObserveDialogLine -Left $left -Top ($top + $row) -Width 1 -Text "|" -Foreground $borderForeground -Background $originalBackground
                Write-ObserveDialogLine -Left ($left + $dialogWidth - 1) -Top ($top + $row) -Width 1 -Text "|" -Foreground $borderForeground -Background $originalBackground
            }
            Write-ObserveDialogLine -Left $left -Top ($top + $dialogHeight - 1) -Width $dialogWidth -Text ("+" + ("-" * ($dialogWidth - 2)) + "+") -Foreground $borderForeground -Background $originalBackground

            $titleText = " $Title "
            $titleLeft = $left + [Math]::Max([int](($dialogWidth - $titleText.Length) / 2), 1)
            Write-ObserveDialogLine -Left $titleLeft -Top $top -Width $titleText.Length -Text $titleText -Foreground $accentForeground -Background $originalBackground
            Write-ObserveDialogLine -Left ($left + 2) -Top ($top + 1) -Width ($dialogWidth - 4) -Text $Prompt -Foreground $bodyForeground -Background $originalBackground

            for ($row = 0; $row -lt $visibleRows; $row++) {
                $itemIndex = $scrollOffset + $row
                $itemText = if ($itemIndex -lt $Items.Count) { "  " + [string]$Items[$itemIndex].Label } else { "" }
                if ($itemIndex -eq $selectedIndex) {
                    Write-ObserveDialogLine -Left $listLeft -Top ($listTop + $row) -Width $listWidth -Text $itemText -Foreground $selectedForeground -Background $selectedBackground
                }
                else {
                    Write-ObserveDialogLine -Left $listLeft -Top ($listTop + $row) -Width $listWidth -Text $itemText -Foreground $bodyForeground -Background $originalBackground
                }
            }

            $footerTop = $top + $dialogHeight - 2
            Write-ObserveDialogLine -Left ($left + 2) -Top $footerTop -Width ($dialogWidth - 4) -Text "< Enter confirm >    < Esc cancel >    < Up/Down move >" -Foreground $borderForeground -Background $originalBackground

            $key = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
            switch ($key.VirtualKeyCode) {
                38 {
                    if ($selectedIndex -gt 0) {
                        $selectedIndex--
                        if ($selectedIndex -lt $scrollOffset) {
                            $scrollOffset = $selectedIndex
                        }
                    }
                    continue
                }
                40 {
                    if ($selectedIndex -lt ($Items.Count - 1)) {
                        $selectedIndex++
                        if ($selectedIndex -ge ($scrollOffset + $visibleRows)) {
                            $scrollOffset = $selectedIndex - $visibleRows + 1
                        }
                    }
                    continue
                }
                13 {
                    return $selectedIndex + 1
                }
                27 {
                    return $null
                }
                default {
                    if ($key.Character -in @('k', 'K') -and $selectedIndex -gt 0) {
                        $selectedIndex--
                        if ($selectedIndex -lt $scrollOffset) {
                            $scrollOffset = $selectedIndex
                        }
                    }
                    elseif ($key.Character -in @('j', 'J') -and $selectedIndex -lt ($Items.Count - 1)) {
                        $selectedIndex++
                        if ($selectedIndex -ge ($scrollOffset + $visibleRows)) {
                            $scrollOffset = $selectedIndex - $visibleRows + 1
                        }
                    }
                    elseif ($key.Character -in @('q', 'Q')) {
                        return $null
                    }
                }
            }
        }
    }
    finally {
        try {
            Clear-ObserveViewport `
                -Left $windowLeft `
                -Top $windowTop `
                -Width $windowWidth `
                -Height $windowHeight `
                -Background $originalBackground `
                -Foreground $bodyForeground
            [Console]::ForegroundColor = $originalForeground
            [Console]::BackgroundColor = $originalBackground
            [Console]::CursorVisible = $cursorVisible
            [Console]::SetCursorPosition($windowLeft, $windowTop)
        }
        catch {
        }
    }
}

function Select-ObserveSerialFromConsole {
    param(
        [string]$InitialPort,
        [int]$InitialBaudrate
    )

    $portChoices = Get-ObserveSerialPortChoices
    if (-not $portChoices) {
        Write-Host "[ERROR] No serial ports were detected. Check the cable/driver, or pass -SerialPort directly."
        return [pscustomobject]@{
            Canceled = $true
        }
    }

    $baudChoices = @(
        [pscustomobject]@{ Label = "115200"; Value = 115200 },
        [pscustomobject]@{ Label = "9600"; Value = 9600 },
        [pscustomobject]@{ Label = "57600"; Value = 57600 },
        [pscustomobject]@{ Label = "38400"; Value = 38400 },
        [pscustomobject]@{ Label = "19200"; Value = 19200 },
        [pscustomobject]@{ Label = "230400"; Value = 230400 },
        [pscustomobject]@{ Label = "460800"; Value = 460800 },
        [pscustomobject]@{ Label = "921600"; Value = 921600 }
    )

    $defaultPortIndex = 1
    for ($i = 0; $i -lt $portChoices.Count; $i++) {
        if ($portChoices[$i].Port -eq $InitialPort) {
            $defaultPortIndex = $i + 1
            break
        }
    }

    $selectedPortIndex = Show-ObserveListDialog `
        -Title "Serial Port" `
        -Prompt "Please Select:" `
        -Items $portChoices `
        -DefaultIndex $defaultPortIndex
    if ($null -eq $selectedPortIndex) {
        return [pscustomobject]@{
            Canceled = $true
        }
    }

    $defaultBaudrate = if ($InitialBaudrate) { [int]$InitialBaudrate } else { 115200 }
    $defaultBaudIndex = 1
    for ($i = 0; $i -lt $baudChoices.Count; $i++) {
        if ($baudChoices[$i].Value -eq $defaultBaudrate) {
            $defaultBaudIndex = $i + 1
            break
        }
    }

    $selectedBaudIndex = Show-ObserveListDialog `
        -Title "Baudrate" `
        -Prompt "Please Select:" `
        -Items $baudChoices `
        -DefaultIndex $defaultBaudIndex
    if ($null -eq $selectedBaudIndex) {
        return [pscustomobject]@{
            Canceled = $true
        }
    }

    $selectedPort = $portChoices[$selectedPortIndex - 1].Port
    $selectedBaudrate = $baudChoices[$selectedBaudIndex - 1].Value

    Write-Host "[DONE] Selected serial port: $selectedPort"
    Write-Host "[DONE] Selected baudrate: $selectedBaudrate"

    return [pscustomobject]@{
        Canceled   = $false
        SerialPort = $selectedPort
        Baudrate   = $selectedBaudrate
    }
}

function Show-ObserveSerialPicker {
    param(
        [string]$InitialPort,
        [int]$InitialBaudrate
    )

    if ([Console]::IsInputRedirected) {
        Write-Host "[ERROR] Interactive selection requires a terminal. Pass -NoUi with explicit parameters for automation."
        return [pscustomobject]@{
            Canceled = $true
        }
    }

    try {
        return Select-ObserveSerialFromConsole -InitialPort $InitialPort -InitialBaudrate $InitialBaudrate
    }
    catch {
        Write-Host "[ERROR] Failed to start interactive selection: $($_.Exception.Message)"
        return [pscustomobject]@{
            Canceled = $true
        }
    }
}

$defaults = Get-ObserveSerialDefaults
if (-not $PSBoundParameters.ContainsKey("SerialPort") -and $defaults.SerialPort) {
    $SerialPort = $defaults.SerialPort
}
if (-not $PSBoundParameters.ContainsKey("Baudrate") -and $defaults.Baudrate) {
    $Baudrate = $defaults.Baudrate
}
if (-not $Baudrate) {
    $Baudrate = 115200
}

$showPicker = (-not $NoUi) -and (-not $PSBoundParameters.ContainsKey("SerialPort")) -and (-not $PSBoundParameters.ContainsKey("Baudrate"))
if ($showPicker) {
    $selection = Show-ObserveSerialPicker -InitialPort $SerialPort -InitialBaudrate $Baudrate
    if ($selection -and $selection.Canceled) {
        Write-Host "[TODO] Serial observe canceled by user."
        exit 0
    }
    if ($selection -and -not $selection.Canceled) {
        $SerialPort = $selection.SerialPort
        $Baudrate = $selection.Baudrate
    }
}

if (-not $SerialPort) {
    throw "No serial port selected. Pass -SerialPort, set AUTO_DBG_SERIAL_PORT, or choose one from the picker."
}

$arguments = @(
    "-m", "autodbg",
    "watch-serial",
    "--raw-live",
    "--follow",
    "--stdin-shell",
    "--protect-human-session",
    "--tail", "$Tail",
    "--serial-port", "$SerialPort",
    "--baudrate", "$Baudrate"
)

Push-Location $projectRoot
try {
    & $python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
