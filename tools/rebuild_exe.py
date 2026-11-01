# -*- coding: utf-8 -*-
r"""
礼貌小狗 · 重新打包 exe

改了 main.py 之后必须重打，否则 exe 里跑的还是旧代码（index.html / tools\ 不用重打）。
命令与 build.ps1 一致，只是输出到 dist_new\，不动正在用的 dist\PolitePuppy.exe，
打完自己确认没问题再手动换。

用法：
    python tools\rebuild_exe.py            # 输出到 dist_new\PolitePuppy.exe
    python tools\rebuild_exe.py --into dist # 直接覆盖 dist\
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PY = VENV_PY if os.path.isfile(VENV_PY) else "py"

DIST = "dist_new"
if "--into" in sys.argv:
    i = sys.argv.index("--into")
    if i + 1 < len(sys.argv):
        DIST = sys.argv[i + 1]

ARGS = [
    PY, "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "PolitePuppy",
    "--icon", "dog_girl.ico",
    "--add-data", "index.html;.",
    "--add-data", "dog_girl.png;.",
    "--add-data", "memory_diary.txt;.",
    # 人格画像：exe 旁边那份优先，这只是兜底
    "--add-data", "persona.json;.",
    # Agent 插件包：以磁盘文件形式带进去，改插件 .py 重启就生效，不用重打包
    "--add-data", "puppy_agent;puppy_agent",
    # 唱歌台（puppy-sing）：解包到可写目录由 core.py 处理，这里只带文件进去
    "--add-data", "puppy-sing;puppy-sing",
    # 引导模块是动态 import，显式点名保证被打进 PYZ
    "--hidden-import", "puppy_agent_boot",
    "--collect-all", "pythonnet",
    "--collect-all", "webview",
    "--collect-all", "pptx", "--collect-all", "lxml",
    "--hidden-import", "clr",
    "--hidden-import", "pptx",
    "--hidden-import", "lxml._elementpath",
    "--hidden-import", "webview.platforms.winforms",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "pystray._win32", "--hidden-import", "pystray._util.win32",
    "--exclude-module", "PyQt5", "--exclude-module", "PyQt6",
    "--exclude-module", "PySide2", "--exclude-module", "PySide6",
    "--distpath", DIST,
    "main.py",
]


def main():
    print("工作目录：%s" % ROOT)
    print("解释器：%s" % PY)
    print("输出目录：%s" % DIST)
    print("命令：%s\n" % " ".join(ARGS))
    t0 = time.time()
    proc = subprocess.Popen(ARGS, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    for line in proc.stdout:
        print(line.rstrip(), flush=True)
    proc.wait()
    cost = int(time.time() - t0)
    out = os.path.join(ROOT, DIST, "PolitePuppy.exe")
    print("\n耗时 %d 秒，退出码 %d" % (cost, proc.returncode))
    if os.path.isfile(out):
        print("产物：%s（%.2f MB）" % (out, os.path.getsize(out) / 1024.0 / 1024.0))
    else:
        print("!! 没找到产物：%s" % out)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
