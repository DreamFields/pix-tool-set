<#
.SYNOPSIS
    Force-reinstall pix-tool-set by killing running server processes first.

.DESCRIPTION
    This script force-kills pix_tool_set processes and Python processes that are
    running pix_tool_set entry points, then reinstalls the package in editable
    mode with dev dependencies. During pip install, it keeps a background killer
    job running to prevent MCP clients from immediately restarting the server and
    locking pix_tool_set.exe again.

.EXAMPLE
    .\reinstall.ps1
#>

$ErrorActionPreference = "Stop"

$maxWaitSeconds = 15

function Test-IsPixToolSetProcess {
    param($Process)

    $commandLine = [string]$Process.CommandLine
    return (
        $Process.Name -ieq "pix_tool_set.exe" -or
        $Process.Name -ieq "pix-tool-set.exe" -or
        $Process.Name -ieq "pix-tool-set-cli.exe" -or
        (
            ($Process.Name -ieq "python.exe" -or $Process.Name -ieq "pythonw.exe" -or $Process.Name -ieq "py.exe") -and
            $commandLine -and
            (
                $commandLine -match "(^|\s)-m\s+pix_tool_set\.mcp_server(\s|$)" -or
                $commandLine -match "(^|\s)-m\s+pix_tool_set\.cli(\s|$)" -or
                $commandLine -match "mcp_server:main" -or
                $commandLine -match "pix_tool_set\.mcp_server"
            )
        )
    )
}

function Get-PixToolSetProcesses {
    Get-CimInstance Win32_Process |
        Where-Object { $_.ProcessId -ne $PID -and (Test-IsPixToolSetProcess $_) }
}

function Stop-PixToolSetProcesses {
    $targets = @(Get-PixToolSetProcesses)
    if (-not $targets) {
        Write-Host "[INFO] No pix-tool-set processes found." -ForegroundColor Gray
        return
    }

    foreach ($target in $targets) {
        Write-Host "[INFO] Force killing PID $($target.ProcessId): $($target.Name) $($target.CommandLine)" -ForegroundColor Yellow
        & taskkill.exe /PID $target.ProcessId /F /T | Out-Host
    }
}

function Test-FileLocked {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $false
    }

    try {
        $stream = [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
        $stream.Close()
        return $false
    } catch {
        return $true
    }
}

$scriptsDir = (& python -c "import sysconfig; print(sysconfig.get_path('scripts'))").Trim()
if (-not $scriptsDir) {
    throw "Failed to resolve Python scripts directory."
}

$exeNames = @("pix_tool_set.exe", "pix-tool-set.exe", "pix-tool-set-cli.exe")
$exePaths = $exeNames | ForEach-Object { Join-Path $scriptsDir $_ }

Write-Host "[INFO] Python scripts directory: $scriptsDir" -ForegroundColor Cyan

# --- Step 1: Force kill existing server processes ---
Stop-PixToolSetProcesses
Start-Sleep -Milliseconds 500
Stop-PixToolSetProcesses

# --- Step 2: Wait for executable handles to be released ---
for ($second = 1; $second -le $maxWaitSeconds; $second++) {
    $lockedPaths = @($exePaths | Where-Object { Test-FileLocked $_ })
    if (-not $lockedPaths) {
        break
    }

    Write-Host "[INFO] Waiting for locked executables to release ($second/$maxWaitSeconds): $($lockedPaths -join ', ')" -ForegroundColor DarkGray
    Stop-PixToolSetProcesses
    Start-Sleep -Seconds 1
}

$lockedPaths = @($exePaths | Where-Object { Test-FileLocked $_ })
if ($lockedPaths) {
    Write-Host "[ERROR] Executable file is still locked after force kill:" -ForegroundColor Red
    $lockedPaths | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    Write-Host "[HINT] Close the MCP client or editor that keeps auto-restarting pix-tool-set, then retry." -ForegroundColor Yellow
    exit 1
}

# --- Step 3: Remove stale pip .deleteme files if any ---
Get-ChildItem -Path $scriptsDir -Filter "pix*tool*set*.deleteme" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "[INFO] Removing stale file: $($_.FullName)" -ForegroundColor DarkGray
    Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
}

# --- Step 4: Keep killing auto-restarted server processes while pip installs ---
$killerJob = Start-Job -ScriptBlock {
    function Test-IsPixToolSetProcessInJob {
        param($Process)

        $commandLine = [string]$Process.CommandLine
        return (
            $Process.Name -ieq "pix_tool_set.exe" -or
            $Process.Name -ieq "pix-tool-set.exe" -or
            $Process.Name -ieq "pix-tool-set-cli.exe" -or
            (
                ($Process.Name -ieq "python.exe" -or $Process.Name -ieq "pythonw.exe" -or $Process.Name -ieq "py.exe") -and
                $commandLine -and
                (
                    $commandLine -match "(^|\s)-m\s+pix_tool_set\.mcp_server(\s|$)" -or
                    $commandLine -match "(^|\s)-m\s+pix_tool_set\.cli(\s|$)" -or
                    $commandLine -match "mcp_server:main" -or
                    $commandLine -match "pix_tool_set\.mcp_server"
                )
            )
        )
    }

    while ($true) {
        Get-CimInstance Win32_Process |
            Where-Object { $_.ProcessId -ne $PID -and (Test-IsPixToolSetProcessInJob $_) } |
            ForEach-Object {
                & taskkill.exe /PID $_.ProcessId /F /T | Out-Null
            }

        Start-Sleep -Milliseconds 300
    }
}

try {
    Write-Host "[INFO] Running: python -m pip install -e .[dev]" -ForegroundColor Cyan
    python -m pip install -e ".[dev]"
    $exitCode = $LASTEXITCODE
} finally {
    Stop-Job $killerJob -ErrorAction SilentlyContinue
    Remove-Job $killerJob -Force -ErrorAction SilentlyContinue
}

if ($exitCode -eq 0) {
    Write-Host "[OK] Reinstallation completed successfully." -ForegroundColor Green
} else {
    Write-Host "[ERROR] Reinstallation failed with exit code $exitCode." -ForegroundColor Red
    exit $exitCode
}
