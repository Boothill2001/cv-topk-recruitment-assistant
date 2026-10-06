$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    $taskPortfolioPage = Invoke-WebRequest -Uri 'http://127.0.0.1:8654/' -UseBasicParsing -TimeoutSec 2
    if ($taskPortfolioPage.Content -match 'TalentLens') {
        Write-Host 'English portfolio is already running: http://127.0.0.1:8654/'
        return
    }
} catch {}
if (-not (Test-Path -LiteralPath 'node_modules')) {
    & npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Could not install frontend dependencies.' }
}
Write-Host 'English portfolio: http://127.0.0.1:8654/'
Write-Host 'Keep this window open. Ctrl+C stops the demo.'
& npm.cmd run dev
