# Uninstall the Windows Scheduled Task and restore the official Warp Claude Code plugin.
$ErrorActionPreference = "Continue"

$TaskName = "WarpSessionDigestTitles"
$PidFile = Join-Path $env:USERPROFILE ".cache\warp-session-digest\titles.pid"

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

if (Test-Path $PidFile) {
    $OldPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($OldPid -match '^\d+$') {
        Stop-Process -Id ([int]$OldPid) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

if (Get-Command claude -ErrorAction SilentlyContinue) {
    & claude plugin uninstall warp-digest@warp-session-digest
    & claude plugin enable warp@claude-code-warp 2>$null
}

Write-Host "uninstalled Scheduled Task '$TaskName'"
