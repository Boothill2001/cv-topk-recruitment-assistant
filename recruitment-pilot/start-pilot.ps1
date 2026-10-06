$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
try {
    $taskPilotRunning = Invoke-RestMethod -Uri 'http://127.0.0.1:9652/api/status' -TimeoutSec 2
} catch { $taskPilotRunning = $null }
if ($taskPilotRunning.prompt_version -like 'pilot-*') {
    Write-Host 'Pilot đã chạy: mở http://127.0.0.1:9652 trong browser.'
    return
}
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Không tạo được Python venv.' }
    & $taskPython -m pip install -r requirements-lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Không cài được dependencies.' }
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend\dist\index.html'))) {
    Push-Location frontend
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Không cài được frontend.' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build lỗi.' }
    Pop-Location
}
& $taskPython -c 'import sqlalchemy, psycopg, alembic, python_multipart, orjson'
if ($LASTEXITCODE -ne 0) {
    & $taskPython -m pip install -r requirements-lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Không cập nhật được dependencies.' }
}
if (-not $env:DATABASE_URL) { & (Join-Path $PSScriptRoot 'setup-postgres.ps1') }
Write-Host 'Recruitment pilot: http://127.0.0.1:9652'
Write-Host 'Giữ terminal này chạy. Ctrl+C để dừng. Google auto-sync chỉ chạy sau OAuth.'
& $taskPython -m uvicorn backend.api:app --host 127.0.0.1 --port 9652 --no-access-log
