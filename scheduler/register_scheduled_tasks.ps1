# Registers the pipeline orchestrator to run every weekday at 8:00 AM.
#
# Must be run from an ELEVATED PowerShell window (Right-click PowerShell ->
# "Run as Administrator") -- registering a Scheduled Task needs admin
# rights that a normal terminal session doesn't have on this machine.
#
# Run once: right-click this file -> "Run with PowerShell" won't be enough
# if it's not elevated; safest is to open an admin PowerShell window and run:
#   & "Q:\Supern4thers\W0RK\Coresix EU\AI Automation Test\nats-linkedin-automation\scheduler\register_scheduled_tasks.ps1"
#
# This machine's timezone is already UTC+8 (Singapore Standard Time), the
# same offset as Philippine Time with no daylight saving -- so 8:00 AM
# local time IS 8:00 AM PHT, no conversion needed.

$workDir = "Q:\Supern4thers\W0RK\Coresix EU\AI Automation Test\nats-linkedin-automation"
$pythonPath = "$workDir\.venv\Scripts\python.exe"

$action = New-ScheduledTaskAction -Execute $pythonPath -Argument "scheduler\run_pipeline.py" -WorkingDirectory $workDir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 8:00AM
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries

Register-ScheduledTask -TaskName "NatsLinkedInAutomation-Pipeline" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Runs the nats-linkedin-automation pipeline (scraper->scorer->drafter->classifier->poster) every weekday morning." `
    -Force

Write-Output "Registered. Verify with: Get-ScheduledTask -TaskName 'NatsLinkedInAutomation-Pipeline'"
Write-Output ""
Write-Output "IMPORTANT: this task requires NATS and the approval bot to already be running"
Write-Output "(they auto-start at your next Windows login via the Startup folder). If you are"
Write-Output "not logged in at 8:00 AM, those services won't be running and the pipeline run"
Write-Output "will fail at the scraper stage (can't reach NATS)."
Write-Output ""
Write-Output "To stop this from running automatically later, either:"
Write-Output "  Unregister-ScheduledTask -TaskName 'NatsLinkedInAutomation-Pipeline' -Confirm:`$false"
Write-Output "or just ask Claude to stop it."
