# 兼容壳：真正的打包脚本在 scripts\build_exe.ps1
#
# 1.1 起，spec / 自检 / build_info 都收进了 scripts\，这一份只做转调，
# 免得老习惯 `.\build.ps1` 或旧文档里的命令直接报「找不到文件」。
#
# 用法：powershell -ExecutionPolicy Bypass -File .\build.ps1
#       powershell -ExecutionPolicy Bypass -File .\build.ps1 -Python C:\Python310\python.exe

param([string]$Python = "")

$ErrorActionPreference = "Stop"
$target = Join-Path $PSScriptRoot "scripts\build_exe.ps1"

if (-not (Test-Path $target)) {
    Write-Host "找不到 $target（scripts\ 目录缺失？）" -ForegroundColor Red
    exit 1
}

Write-Host "build.ps1 已迁移，转调 scripts\build_exe.ps1 ..." -ForegroundColor Cyan
if ($Python) {
    & $target -Python $Python
} else {
    & $target
}
exit $LASTEXITCODE
