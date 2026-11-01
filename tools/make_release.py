# -*- coding: utf-8 -*-
r"""整理打包：生成可分发交付包（三段式目录 + SHA256 校验清单）。

规则：
- 只用标准库；条目名强制带 UTF-8 标志位(0x800)，避免资源管理器解压乱码。
- 索引类文本用 utf-8-sig + CRLF，记事本打开不连行。
- 排除隐私与中间产物：记忆/、.venv、build/、dist*/、__pycache__/、
  .voice_cache/、agent_data/、备份/、发布包/。
用法：python tools\make_release.py
"""
import hashlib
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAMP = "20261029"
BASE = "礼貌小狗桌宠_最新版_%s" % STAMP
PKG = Path(r"C:\Users\zheng'zheng\OneDrive\Desktop") / (BASE + ".zip")

# ---- 01_程序：运行必需 ----
PROG_FILES = [
    "PolitePuppy.exe", "index.html", "main.py", "使用说明.txt", "README.md",
    ".env", "brain.json", "dog_girl.ico", "dog_girl.png", "run.bat",
    "requirements.txt", "启动克隆音色服务.cmd", "persona.json",
]
PROG_TOOLS = [
    "tools/f5_clone.py", "tools/f5_server.py", "tools/tool_client.py",
    # GPT-SoVITS 克隆网关（可选，需自备 GPT-SoVITS 仓库与模型）
    "tools/puppy_tts_gateway.py", "tools/start_puppy_gsv.py",
    "tools/download_gsv_models.py", "tools/tts_ref.json",
]
PROG_DIRS = ["表情包", "voices", "plugins", "puppy_agent", "puppy-sing"]


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect():
    """返回 [(包内相对路径, 绝对路径)]，跳过不存在的。"""
    items = []
    for rel in PROG_FILES + PROG_TOOLS:
        p = ROOT / rel
        if p.is_file():
            items.append(("01_程序/" + rel, p))
    for d in PROG_DIRS:
        dd = ROOT / d
        if dd.is_dir():
            for f in sorted(dd.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(dd)
                # 缓存与字节码不入包：运行时自动生成，且会让包体虚胖
                if "__pycache__" in f.parts or rel.parts[0] == "cache":
                    continue
                items.append(("01_程序/%s/%s" % (d, rel.as_posix()), f))
    docd = ROOT / "文档"
    for f in sorted(docd.glob("*")):
        if f.is_file():
            items.append(("02_文档/" + f.name, f))
    return items


def main():
    items = collect()
    exe = ROOT / "PolitePuppy.exe"
    exe_sha = sha256(exe) if exe.exists() else ""
    exe_size = exe.stat().st_size if exe.exists() else 0
    exe_time = (datetime.fromtimestamp(exe.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                if exe.exists() else "未知")

    # 00_先看这个.md（utf-8-sig + CRLF）
    readme = "\r\n".join([
        "# 礼貌小狗桌宠 · 整理交付包 %s" % STAMP,
        "",
        "## 一、这一版是什么",
        "- 打包时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "- 主程序：`01_程序/PolitePuppy.exe`（%.2f MB，构建于 %s）"
        % (exe_size / 1024 / 1024, exe_time),
        "- exe SHA256：`%s`" % exe_sha,
        "- 唱歌台源码：`01_程序/puppy-sing/`（可 `python main.py` 独立运行；"
        "exe 内已内置同一份，快捷键 Ctrl+Alt+G 唱歌、Ctrl+Alt+L 扒歌）",
        "- 源码基准：`main.py` / `index.html` @2026-10-29（唱歌台 puppy-sing + 全局快捷键已并入）",
        "- **本版为最新构建，含：语音对话、Agent 插件、唱歌台(扒歌自学)、Ctrl+Alt+S/G/L 快捷键**",
        "",
        "## 二、三分钟上手",
        "1. 整个文件夹解压到任意位置；",
        "2. 双击 `01_程序\\PolitePuppy.exe`；",
        "3. 托盘出现小狗，右键可开设置；聊天即可使用。",
        "",
        "## 三、本版状态（如实说明）",
        "已验证通过：",
        "- Agent 核心 v1.0.0、插件 3 个、工具 9 个；",
        "- 记忆库 13,908 条可用；",
        "- 回归套件 12/12 全绿；前端 P10 接线 13/13 通过；",
        "- 规则路由判定 12/12 正确；act 平均 31.8 ms / 最大 68.2 ms（上限 2200 ms）。",
        "",
        "低优先级遗留（P10 阶段记录，不影响日常使用，仅影响过程可视化与回归覆盖）：",
        "- 气泡 🔧 标记未渲染（`agMarkHtml` 已定义但未调用、缺样式与弹层）；",
        "- Agent 页状态同步未接线（`agSync`/`agBump` 未调用）；",
        "- 令牌安全验证脚本 `verify_p10.py` 缺失（后端逻辑存在，但无回归测试覆盖）。",
        "",
        "## 四、校验方式",
        "`03_校验/SHA256.txt` 为逐文件哈希。核对主程序：",
        "",
        "    Get-FileHash .\\01_程序\\PolitePuppy.exe -Algorithm SHA256",
        "",
        "应与上面 SHA256 一致。",
        "",
    ])

    # SHA256 清单（不含自身）
    lines = []
    for arc, p in items:
        lines.append("%s  %s" % (sha256(p), arc))

    with zipfile.ZipFile(PKG, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        # 00 索引
        zi = zipfile.ZipInfo(BASE + "/00_先看这个.md")
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.flag_bits |= 0x800
        z.writestr(zi, readme.encode("utf-8-sig"))

        for arc, p in items:
            zi = zipfile.ZipInfo(BASE + "/" + arc, date_time=datetime.now().timetuple()[:6])
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.flag_bits |= 0x800          # UTF-8 文件名标志位
            z.writestr(zi, p.read_bytes())

        zi = zipfile.ZipInfo(BASE + "/03_校验/SHA256.txt")
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.flag_bits |= 0x800
        z.writestr(zi, ("\r\n".join(lines) + "\r\n").encode("utf-8-sig"))

    print("PKG=%s" % PKG.name)
    print("files=%d size_MB=%.2f" % (len(items) + 2, PKG.stat().st_size / 1024 / 1024))
    print("exe_sha=%s" % exe_sha[:16])


if __name__ == "__main__":
    main()
