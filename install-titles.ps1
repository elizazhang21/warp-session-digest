# Install a Windows Scheduled Task that keeps Warp session titles = one-line summaries (runs titles.py every 30s),
# and swap Warp's official Claude Code plugin for the fork in claude-plugin/.
# Optional: $env:DIGEST_LANG="Japanese"; .\install-titles.ps1
# Uninstall: .\uninstall-titles.ps1
$ErrorActionPreference = "Stop"

$Repo = $PSScriptRoot
$TaskName = "WarpSessionDigestTitles"
$Lang = if ($env:DIGEST_LANG) { $env:DIGEST_LANG } else { "English" }
$CacheDir = Join-Path $env:USERPROFILE ".cache\warp-session-digest"
$LogPath = Join-Path $CacheDir "titles.log"
$PidFile = Join-Path $CacheDir "titles.pid"
$TitlesPy = Join-Path $Repo "titles.py"

New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

# Resolve pythonw.exe (windowless) with fallback to python.exe
$PyExe = & python3 -c "import os, sys; p = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'); print(p if os.path.exists(p) else sys.executable)"
if (-not $PyExe -or -not (Test-Path $PyExe)) {
    throw "Python 3.9+ not found in PATH."
}

# Stop any previously running titles.py daemon
if (Test-Path $PidFile) {
    $OldPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($OldPid -match '^\d+$') {
        Stop-Process -Id ([int]$OldPid) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$Args = "`"$TitlesPy`" --loop 30 --lang `"$Lang`" --log `"$LogPath`""
$Action = New-ScheduledTaskAction -Execute $PyExe -Argument $Args -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Updates Warp terminal session list titles with AI session summaries (warp-session-digest)" -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

$ClaudeCmd = Get-Command claude -ErrorAction SilentlyContinue
if ($ClaudeCmd) {
    try { & claude plugin marketplace add $Repo 2>&1 | Out-Null } catch {}
    try { & claude plugin marketplace update warp-session-digest 2>&1 | Out-Null } catch {}
    & claude plugin install warp-digest@warp-session-digest
    try { & claude plugin disable warp@claude-code-warp 2>&1 | Out-Null } catch {}
    Write-Host "Claude Code plugin: warp-digest enabled, warp@claude-code-warp disabled (applies to new sessions; /reload-plugins in open ones)"
}

Write-Host "installed Scheduled Task '$TaskName' (log: $LogPath)"
