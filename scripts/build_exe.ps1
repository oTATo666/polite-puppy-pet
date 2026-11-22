# 一键打包：用 Python 3.10 跑 PyInstaller，然后写 build_info、跑自检。
# 用法（在 01_程序 目录下）：powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
# 说明：不指定 -Python 时，依次找 .venv、py -3.10、当前 python。
param([string]$Python = "")

$ErrorActionPreference = "Stop"
$prog = Split-Path -Parent $PSScriptRoot
Set-Location $prog

if (-not $Python) {
    foreach ($c in @("$prog\.venv\Scripts\python.exe", "$prog\voice-clone-env\Scripts\python.exe")) {
        if (Test-Path $c) { $Python = $c; break }
    }
}
if (-not $Python) {
    $v = & py -3.10 -c "import sys; print(sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0) { $Python = "py -3.10" } else { $Python = "python" }
}

"使用解释器：$Python"
& $Python -c "import sys; print(sys.version)"

& $Python -m PyInstaller --noconfirm --clean --distpath ".\dist" --workpath ".\build" "scripts\PolitePuppy.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败：$LASTEXITCODE" }

& $Python "scripts\write_build_info.py" --exe ".\dist\PolitePuppy.exe" --out ".\dist\build_info.json"
if ($LASTEXITCODE -ne 0) { throw "写 build_info 失败：$LASTEXITCODE" }

& $Python "scripts\pack_check.py" --exe ".\dist\PolitePuppy.exe"
if ($LASTEXITCODE -ne 0) { throw "打包自检未通过" }

"`n下一步：把 dist\PolitePuppy.exe 和 dist\build_info.json 放回 01_程序，再跑"
"  python scripts\package_release.py --with-exe --out ..\发布_v1"
