$ErrorActionPreference = 'Stop'
$taskPilotUrl = 'http://127.0.0.1:9652/'
function Test-PilotReady {
    try {
        $taskStatus = Invoke-RestMethod -Uri ($taskPilotUrl + 'api/status') -TimeoutSec 2
        return $taskStatus.prompt_version -like 'pilot-*'
    } catch { return $false }
}
if (-not (Test-PilotReady)) {
    $taskStartScript = Join-Path $PSScriptRoot 'start-pilot.ps1'
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + $taskStartScript + '"')) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'runtime\launcher-out.log') -RedirectStandardError (Join-Path $PSScriptRoot 'runtime\launcher-error.log')
    $taskDeadline = (Get-Date).AddSeconds(60)
    while (-not (Test-PilotReady)) {
        if ((Get-Date) -gt $taskDeadline) { throw 'Pilot did not start within 60 seconds. See runtime/launcher-error.log, or run start-pilot.cmd for diagnostics.' }
        Start-Sleep -Milliseconds 500
    }
}
Start-Process $taskPilotUrl
Write-Host ('Vietnamese pilot opened: ' + $taskPilotUrl)
