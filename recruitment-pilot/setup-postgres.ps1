$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPgBin = 'C:\Program Files\PostgreSQL\17\bin'
$taskPgData = Join-Path $PSScriptRoot 'runtime\postgres-data'
$taskPgConfig = Join-Path $PSScriptRoot 'runtime\database.json'
if (-not (Test-Path -LiteralPath (Join-Path $taskPgBin 'initdb.exe'))) { throw 'Cần PostgreSQL 17 binaries hoặc DATABASE_URL do khách cung cấp.' }
if (-not (Test-Path -LiteralPath $taskPgData)) {
    New-Item -ItemType Directory -Force -Path runtime | Out-Null
    $taskBytes = New-Object byte[] 32
    $taskRng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $taskRng.GetBytes($taskBytes)
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'runtime\postgres-password.txt'),[Convert]::ToBase64String($taskBytes))
    & (Join-Path $taskPgBin 'initdb.exe') -D $taskPgData -U pilot_admin --encoding=UTF8 --auth-host=scram-sha-256 --auth-local=trust --pwfile=runtime\postgres-password.txt
    if ($LASTEXITCODE -ne 0) { throw 'Không tạo được PostgreSQL riêng.' }
}
& (Join-Path $taskPgBin 'pg_ctl.exe') -D $taskPgData status | Out-Null
if ($LASTEXITCODE -ne 0) {
    & (Join-Path $taskPgBin 'pg_ctl.exe') -D $taskPgData -l runtime\postgres.log -o '-h 127.0.0.1 -p 55432' -w start
    if ($LASTEXITCODE -ne 0) { throw 'Không khởi động được PostgreSQL riêng trên 55432.' }
}
& .venv\Scripts\python.exe -m backend.migrate_storage
if ($LASTEXITCODE -ne 0) { throw 'Migration lỗi; SQLite gốc vẫn được giữ.' }
