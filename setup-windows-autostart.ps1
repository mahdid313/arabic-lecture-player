# Run this once in PowerShell as Administrator on Windows
# It creates a Task Scheduler task that starts the WSL downloader service at logon

$taskName = "ArabicLectureDownloader"
$wslExe   = "$env:SystemRoot\System32\wsl.exe"

# Remove any old version of the task
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action   = New-ScheduledTaskAction -Execute $wslExe -Argument "-d Ubuntu --exec sudo systemctl start arabic-downloader"
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable $true

Register-ScheduledTask `
    -TaskName $taskName `
    -Action   $action `
    -Trigger  $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force

Write-Host ""
Write-Host "Task '$taskName' registered. The downloader will start automatically on next logon."
Write-Host "To start it now without rebooting, run:"
Write-Host "  wsl -d Ubuntu --exec sudo systemctl start arabic-downloader"
Write-Host ""
Write-Host "To check status: wsl -d Ubuntu --exec sudo systemctl status arabic-downloader"
