@echo off
rem 启动本地 F5-TTS 克隆音色服务（后台常驻）
rem 桌宠启动时会自动拉起它；也可手动双击本脚本单独启动/排障。
chcp 65001 >nul
cd /d "C:\Users\zheng'zheng\OneDrive\Desktop\礼貌小狗桌宠"

set PY=C:\Users\zheng'zheng\WorkBuddy\voice-clone-env\Scripts\python.exe

if not exist "%PY%" (
  echo [错误] 找不到克隆音色环境：%PY%
  echo 请先恢复 voice-clone-env，或改用系统 python 装 f5_tts。
  pause
  exit /b 1
)

echo 正在启动 F5-TTS 克隆音色服务（端口 9880）...
echo 关闭本窗口会停止服务；桌宠会自动调用 http://127.0.0.1:9880
"%PY%" tools\f5_server.py --port 9880
pause
