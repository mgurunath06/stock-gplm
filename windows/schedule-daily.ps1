<#
.SYNOPSIS
  Optional: register a Windows Scheduled Task that runs the evening workflow on weekdays.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File windows\schedule-daily.ps1 -Stocks RELIANCE,HDFCBANK,ICICIBANK -At 19:30
#>
param([string]$Stocks = "RELIANCE,HDFCBANK,ICICIBANK", [string]$At = "19:30", [string]$Device = "auto")
$Repo = Split-Path -Parent $PSScriptRoot
$cmd = Join-Path $Repo "windows\gplm.cmd"
$log = Join-Path $Repo "gplm-daily.log"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$cmd`" daily --stocks $Stocks --device $Device --quiet >> `"$log`" 2>&1"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $At
Register-ScheduledTask -TaskName "GPLM daily" -Action $action -Trigger $trigger -Description "Stock GPLM evening research run" -Force
Write-Host "Registered 'GPLM daily' at $At on weekdays. Log: $log"
