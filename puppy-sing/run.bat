@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 优先使用桌宠项目的虚拟环境（已含 F5TTS 等），否则回退到系统 python
if exist "..\.venv\Scripts\python.exe" (
    set "PY=..\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

REM 缺少 numpy 时自动安装依赖
%PY% -c "import numpy" 2>nul || %PY% -m pip install -r requirements.txt

echo 正在启动 礼貌小狗·唱歌 ...
%PY% main.py
pause
