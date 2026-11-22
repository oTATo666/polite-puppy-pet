@echo off
rem 启动本地 F5-TTS 克隆音色服务（后台常驻）
rem 桌宠启动时会自动拉起它；也可手动双击本脚本单独启动/排障。
chcp 65001 >nul
rem %~dp0 = 本脚本所在目录（带尾反斜杠）。原来这里写死了作者本机的绝对路径，
rem 换目录就启动不了，也把用户名泄在脚本里。
cd /d "%~dp0"

rem 克隆环境用哪个 python：优先环境变量 F5_PYTHON，其次本目录下的 .venv / voice-clone-env
set "PY=%F5_PYTHON%"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0voice-clone-env\Scripts\python.exe" set "PY=%~dp0voice-clone-env\Scripts\python.exe"

if not defined PY (
  echo [错误] 没找到克隆音色环境的 python。
  echo   设一个环境变量 F5_PYTHON 指向该环境的 python.exe，或者
  echo   把环境放在本目录下的 .venv\ 或 voice-clone-env\。
  pause
  exit /b 1
)
if not exist "%PY%" (
  echo [错误] 找不到克隆音色环境：%PY%
  pause
  exit /b 1
)

echo 正在启动 F5-TTS 克隆音色服务（端口 9880）...
echo 关闭本窗口会停止服务；桌宠会自动调用 http://127.0.0.1:9880
"%PY%" "%~dp0tools\f5_server.py" --port 9880
pause
