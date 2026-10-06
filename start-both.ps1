$ErrorActionPreference = 'Stop'
foreach ($taskRelativeScript in @('portfolio-en\start-portfolio.ps1','recruitment-pilot\start-pilot.ps1')) {
    $taskScriptPath = Join-Path $PSScriptRoot $taskRelativeScript
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + $taskScriptPath + '"')) -WindowStyle Hidden
}
Write-Host 'English portfolio: http://127.0.0.1:8654/'
Write-Host 'Real pilot: http://127.0.0.1:9652/'
Write-Host 'Services run in the background. Initial setup may take a moment.'
foreach ($taskPort in @(8654,9652)) {
    $taskUrl = 'http://127.0.0.1:' + $taskPort + '/'
    $taskReady = $false
    for ($taskAttempt = 0; $taskAttempt -lt 30; $taskAttempt++) {
        try { $null = Invoke-WebRequest -Uri $taskUrl -UseBasicParsing -TimeoutSec 2; $taskReady = $true; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if ($taskReady) { Start-Process $taskUrl } else { Write-Warning ('Not ready yet: ' + $taskUrl + '. Use the individual start.bat for diagnostics.') }
}
