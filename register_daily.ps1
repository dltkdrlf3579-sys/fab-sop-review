param([string]$At = "08:00", [string]$PythonExe = (Get-Command python -ErrorAction Stop).Source)
$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "run_daily.py"
$configPath = Join-Path $PSScriptRoot "config.ini"
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument ('-X utf8 "{0}" --config "{1}"' -f $scriptPath, $configPath) -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, "HH:mm", [Globalization.CultureInfo]::InvariantCulture))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "FAB SOP Daily Review" -Action $action -Trigger $trigger -Settings $settings -Description "FAB SOP registered-date review" -ErrorAction Stop
