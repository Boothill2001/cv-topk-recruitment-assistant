$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Chưa cài pilot. Mở start-pilot.cmd trước.' }
& $taskPython -m backend.maintenance --require-idle
if ($LASTEXITCODE -ne 0) { throw 'Chờ tác vụ hoàn tất và kiểm tra dịch vụ trước khi sao lưu để cập nhật.' }
& $taskPython -m backend.backup --verify
if ($LASTEXITCODE -ne 0) { throw 'Sao lưu hoặc thử khôi phục chưa đạt. Chưa cập nhật database.' }
Write-Host 'Đã sao lưu và thử khôi phục trong database riêng. Production không bị ghi đè.'
Write-Host 'Để chuyển máy, cần giữ thêm uploads, source snapshot và cấu hình/token cục bộ theo MAINTENANCE.md.'
