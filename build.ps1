# 打包脚本：走 spec 模式，打完自动自检
#
# ⚠ 不要改回"命令行参数打包"（--add-data ... main.py）。
#   tools/ 下的模块是运行期才 import 的，命令行方式扫不到，必然漏，
#   而且漏了不报错——voice_identity 当初就是这么丢的，用户点到音色才炸。
#   spec 里已经显式列了，新增 tools 模块请同步改 spec 的 datas。
#
# 注意：不要设 $ErrorActionPreference = "Stop"——PyInstaller 的 INFO 走 stderr，
# 配合 2>&1 重定向会被当成终止性错误，脚本第一行就退出。
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

$py = "py"
if (Test-Path ".venv\Scripts\python.exe") { $py = ".venv\Scripts\python.exe" }

Write-Host "`n[1/3] 清理旧产物" -ForegroundColor Cyan
foreach ($d in @("build", "dist")) {
  if (Test-Path $d) { Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host "`n[2/3] PyInstaller 打包（spec 模式）" -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --distpath "dist" --workpath "build" "PolitePuppy_voicefix.spec"
if ($LASTEXITCODE -ne 0) {
  Write-Host "`n构建失败，退出码 $LASTEXITCODE" -ForegroundColor Red
  exit $LASTEXITCODE
}
if (-not (Test-Path "dist\PolitePuppy.exe")) {
  Write-Host "`n构建失败：dist\PolitePuppy.exe 没生成" -ForegroundColor Red
  exit 1
}

Write-Host "`n[3/3] 打包自检（断言关键模块真的在 exe 里）" -ForegroundColor Cyan
& $py "dev\pack_check.py" "dist\PolitePuppy.exe"
if ($LASTEXITCODE -ne 0) {
  Write-Host "`n自检没过，这个 exe 不要发出去。" -ForegroundColor Red
  exit 1
}

$f = Get-Item "dist\PolitePuppy.exe"
$size = [math]::Round($f.Length / 1MB, 2)
$hash = (Get-FileHash "dist\PolitePuppy.exe" -Algorithm SHA256).Hash.ToLower()

Write-Host "`n产物：dist\PolitePuppy.exe  $size MB" -ForegroundColor Green
Write-Host "SHA256: $hash" -ForegroundColor DarkGray
Write-Host "`n下一步（可选，但建议）：" -ForegroundColor DarkGray
Write-Host "  $py dev\smoke_test.py          起实例跑 12 项回归" -ForegroundColor DarkGray
Write-Host "  别忘了把 dist\PolitePuppy.exe 覆盖到交付包的 01_程序\ 下，并重算 SHA256.txt" -ForegroundColor DarkGray
