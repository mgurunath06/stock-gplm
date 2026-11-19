<#
.SYNOPSIS
  Optional (M7.3): register weekday Scheduled Tasks for the daily operation cycle.
    GPLM pre-open    08:45  readiness report (calendar, live universe, risk inputs, controls, open alerts)
    GPLM intraday    every 15 minutes 09:30-15:30: 15m opportunity scan + alert advance/notify
    GPLM post-close  19:45  evening research (gplm daily) then the 1d opportunity scan + alerts
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File windows\schedule-ops.ps1 -Stocks RELIANCE,HDFCBANK -Risk C:\gplm\risk.json
.NOTES
  Research decision support only; no order is ever placed. Remove with:
  Unregister-ScheduledTask -TaskName "GPLM pre-open","GPLM intraday","GPLM post-close"
#>
param([string]$Stocks = "RELIANCE,HDFCBANK,ICICIBANK", [string]$Risk = "", [switch]$NoIntraday)
$Repo = Split-Path -Parent $PSScriptRoot
$cmd = Join-Path $Repo "windows\gplm.cmd"
$log = Join-Path $Repo "gplm-ops.log"
$riskArg = if ($Risk) { "--risk `"$Risk`"" } else { "" }
$days = @("Monday","Tuesday","Wednesday","Thursday","Friday")

$pre = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$cmd`" ops pre-open --quiet >> `"$log`" 2>&1"
Register-ScheduledTask -TaskName "GPLM pre-open" -Action $pre -Force `
  -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 08:45) -Description "Stock GPLM pre-open readiness"

$post = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ("/c `"$cmd`" daily --stocks $Stocks --quiet >> `"$log`" 2>&1 " +
  "& `"$cmd`" ops post-close --stocks $Stocks $riskArg --quiet >> `"$log`" 2>&1")
Register-ScheduledTask -TaskName "GPLM post-close" -Action $post -Force `
  -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 19:45) -Description "Stock GPLM post-close research + opportunities"

if (-not $NoIntraday) {
  $intra = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$cmd`" ops intraday --stocks $Stocks $riskArg --quiet >> `"$log`" 2>&1"
  $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 09:30
  $rep = New-ScheduledTaskTrigger -Once -At 09:30 -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 6)
  $t.Repetition = $rep.Repetition
  Register-ScheduledTask -TaskName "GPLM intraday" -Action $intra -Trigger $t -Force -Description "Stock GPLM intraday scan (15m)"
}
Write-Host "Registered GPLM pre-open / post-close$(if (-not $NoIntraday) {' / intraday'}). Log: $log"
Write-Host "Intraday scans need live minute data; until a provider is connected (roadmap M8) they report stale data and suppress alerts."
