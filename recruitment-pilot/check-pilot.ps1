$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Mở start-pilot.cmd để cài đặt lần đầu trước.' }
Write-Host '1/3: Kiểm tra dịch vụ và dữ liệu. Không gọi AI.'
& $taskPython -m backend.maintenance
if ($LASTEXITCODE -ne 0) { throw 'Dịch vụ hoặc database chưa sẵn sàng. Đọc kết quả phía trên.' }
Write-Host '2/3: Kiểm thử các luồng và chống lỗi cũ.'
& $taskPython -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Kiểm thử thất bại. Chưa bàn giao bản cập nhật.' }
Write-Host '3/3: Kiểm tra và build giao diện.'
& npm.cmd --prefix frontend run build
if ($LASTEXITCODE -ne 0) { throw 'Build thất bại. Chưa bàn giao bản cập nhật.' }
Write-Host 'Kiểm tra đạt. Tiếp tục thao tác browser theo MAINTENANCE.md trước bàn giao.'
