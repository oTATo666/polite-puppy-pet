# -*- mode: python ; coding: utf-8 -*-
"""礼貌小狗 · 打包配置（Python 3.10 / PyInstaller 6）

用法（在 01_程序 目录下）：
    python -m PyInstaller --noconfirm --clean scripts\\PolitePuppy.spec
    python scripts\\write_build_info.py --exe dist\\PolitePuppy.exe
    python scripts\\pack_check.py --exe dist\\PolitePuppy.exe

约定：
- 只打"程序本体 + 包内资源"；.env、记忆库、日记、声纹一律不进包。
- puppy_agent 再以数据目录拷一份：插件是运行期按路径加载的，只有 pyc 不够。
- tools/ 与 表情包/ 不打进来：程序按 exe 旁边那份找（见 main.py 的 tools_dir）。
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

BASE = os.path.dirname(SPECPATH)

datas = [
    (os.path.join(BASE, "index.html"), "."),
    (os.path.join(BASE, "dog_girl.png"), "."),
    (os.path.join(BASE, "brain.json"), "."),
    (os.path.join(BASE, "persona.json"), "."),
    (os.path.join(BASE, "puppy_agent"), "puppy_agent"),
]
binaries = []
hiddenimports = [
    "clr",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "pystray._win32",
    "pystray._util.win32",
    "memory_organize",
    "local_brain",
    "puppy_agent_boot",
    "puppy_core",
    "pptx",
]

datas += collect_data_files("webview")
binaries += collect_dynamic_libs("webview")

_net = collect_all("pythonnet")
datas += _net[0]
binaries += _net[1]
hiddenimports += _net[2]

a = Analysis(
    [os.path.join(BASE, "main.py")],
    pathex=[BASE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PolitePuppy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(BASE, "dog_girl.ico"),
)
