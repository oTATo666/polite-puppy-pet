# -*- coding: utf-8 -*-
r"""
打包自检：PyInstaller 打完包之后跑一次，确认"运行期才 import 的东西"真的进包了。

为什么需要
    tools\ 下的模块和 memory_organize.py 都是**运行期才 import** 的，
    PyInstaller 的静态分析扫不到；不显式写进 spec 的 hiddenimports / datas，
    打出来的 exe 会缺模块，表现为"某个功能悄悄失效"——语音变成哑巴、
    记忆抽不出事实、插件加载不了，而且不报错。

用法
    :: 只查源码树是否齐全（打包前就可以跑）
       python scripts\pack_check.py

    :: 打完包再查一次：额外校验 exe 存在、大小合理、版本串对得上
       python scripts\pack_check.py --exe PolitePuppy.exe

退出码：0 = 通过；1 = 有缺项。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import private_words   # N-21：私人词表共享模块（同目录）

PROG = Path(__file__).resolve().parent.parent

# 运行期才 import 的模块：PyInstaller 静态分析看不见，必须显式带上
RUNTIME_MODULES = [
    "memory_organize.py",
    "local_brain.py",
    "puppy_agent_boot.py",
    "tools/memory_extract.py",
    "tools/voice_identity.py",
    "tools/voice_profile.py",
    "tools/f5_server.py",
    "tools/f5_clone.py",
    "tools/tool_client.py",
]

# 运行必需的静态资源
ASSETS = [
    "index.html",
    "dog_girl.png",
    "brain.json",
    "persona.json",
]

# 整目录（打包时要 --add-data 进去的）
DIRS = [
    "puppy_agent",
    "puppy_agent/core",
    "puppy_agent/plugins",
    "表情包",
    "voices",
]

# 不该进包的东西（进了就是泄露）
MUST_NOT_SHIP = [
    ".env",
    "记忆/memory.db",
    "memory_diary.txt",
    "private_names.local.txt",   # N-5：私人词表（昵称等），只留在本机
]


def _lines_of(path: Path) -> list:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def check(missing_only: bool = False) -> list:
    """返回 [(ok, 说明)]。"""
    rows = []

    for rel in RUNTIME_MODULES:
        p = PROG / rel
        rows.append((p.is_file(), "运行期模块 %s" % rel))

    for rel in ASSETS:
        p = PROG / rel
        rows.append((p.is_file(), "静态资源 %s" % rel))

    for rel in DIRS:
        p = PROG / rel
        rows.append((p.is_dir() and any(p.iterdir()), "目录 %s" % rel))

    # APP_VERSION 能不能读出来（发布清单靠它，别再手写）
    ver = ""
    for line in _lines_of(PROG / "main.py"):
        m = re.match(r'^APP_VERSION\s*=\s*"([^"]+)"', line)
        if m:
            ver = m.group(1)
            break
    rows.append((bool(ver), "main.py 里的 APP_VERSION（%s）" % (ver or "读不到")))

    # python_agent 的包结构：__init__.py 缺了动态加载会失败
    for rel in ("puppy_agent/__init__.py", "puppy_agent/core/__init__.py",
                "puppy_agent/plugins/__init__.py"):
        rows.append(((PROG / rel).is_file(), "包标记 %s" % rel))

    # 不该随包出去的东西：源码树里存在没关系，但措辞要清楚——这是开发树
    # 的提示，不是发布树检查（发布树的检查在 --tree 模式，直接 FAIL）。
    for rel in MUST_NOT_SHIP:
        p = PROG / rel
        if p.exists():
            rows.append((True, "开发树提示（此文件不进发布包）：%s 存在于源码目录，"
                               "打包/组装时不得带入" % rel))

    return rows


def check_tree(tree: Path, is_release: bool = True) -> list:
    """N-22：--tree 模式——对**发布目录**检查不该出现的文件。

    开发树（01_程序）里有 .env、词表、记忆库是正常的，但发布树里出现
    任何一项就是 FAIL（组装脚本漏拦 / 有人手动塞进去）。
    is_release=False 时用于开发树自查：存在只提示不改判定。
    """
    rows = []
    for rel in MUST_NOT_SHIP:
        p = tree / rel
        if p.exists():
            if is_release:
                rows.append((False, "发布树不该出现：%s" % rel))
            else:
                rows.append((True, "开发树提示（不进发布包）：%s" % rel))
    # 黑名单后缀 / 目录在发布树里同样不许有
    deny_dirs = {"记忆", "backup", "我的声音", ".voice_cache", "agent_data",
                 "__pycache__", ".git"}
    deny_suffixes = {".db", ".db-wal", ".db-shm", ".pyc", ".pyo", ".log"}
    for p in tree.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(tree).as_posix()
        top = rel.split("/")[0]
        if top in deny_dirs or p.name in ("memory_diary.txt", "settings.json",
                                          "private_names.local.txt", ".env"):
            rows.append((False if is_release else True,
                         "%s不该出现：%s" % ("发布树" if is_release else "开发树提示（不进发布包）",
                                            rel)))
            break   # 报一条就够，别刷屏
        if p.suffix.lower() in deny_suffixes and rel != "01_程序/PolitePuppy.exe":
            rows.append((False if is_release else True,
                         "%s不该出现：%s" % ("发布树" if is_release else "开发树提示（不进发布包）",
                                            rel)))
            break
    if is_release and not any(not r[0] for r in rows):
        rows.append((True, "发布树检查：无 .env / 记忆库 / 词表 / 黑名单文件"))
    return rows


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_exe(exe: Path) -> list:
    rows = []
    rows.append((exe.is_file(), "exe 存在：%s" % exe.name))
    if not exe.is_file():
        return rows
    size = exe.stat().st_size
    rows.append((size > 5 * 1024 * 1024,
                 "exe 大小 %.1f MB（<5 MB 基本是没带 WebView2/依赖）= " % (size / 1048576.0)))
    # N-4：exe 比源码旧 → 提示"可能不是最新构建"（警告，不算 FAIL）
    try:
        src_newest = max((PROG / n).stat().st_mtime for n in ("main.py", "index.html"))
        if exe.stat().st_mtime < src_newest:
            rows.append((True,
                         "警告：exe 的修改时间早于 main.py/index.html，"
                         "可能不是最新构建"))
    except OSError:
        pass

    # 版本串不能靠"在 exe 里搜字符串"：onefile 的 pyc 是 zlib 压缩的，
    # 搜不到不等于旧包（v1 构建就踩过这个坑）。改读构建时写下的 build_info.json，
    # 比对版本号和 main.py / index.html 的哈希——这才能证明 exe 是这份源码打的。
    info_file = exe.parent / "build_info.json"
    if not info_file.is_file():
        # N-4：以前这里是"跳过（PASS）"，等于自检白跑——随便一个旧 exe
        # 都能过这一关。没有 build_info.json 就无法证明 exe 是这份源码打的，
        # --exe 模式下直接 FAIL，退出码 1。
        rows.append((False,
                     "缺 build_info.json：无法证明 exe 与源码一致"
                     "（构建后跑 scripts\\write_build_info.py 生成）"))
        return rows
    try:
        info = json.loads(info_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        rows.append((False, "build_info.json 读不了：%s" % exc))
        return rows

    ver = ""
    for line in _lines_of(PROG / "main.py"):
        m = re.match(r'^APP_VERSION\s*=\s*"([^"]+)"', line)
        if m:
            ver = m.group(1)
            break
    rows.append((bool(ver) and info.get("version") == ver,
                 "构建信息版本 %s / 源码 %s" % (info.get("version"), ver or "读不到")))
    for name in ("main.py", "index.html"):
        want = (info.get("source") or {}).get(name, "")
        cur = _sha256(PROG / name)
        rows.append((want == cur, "%s 与构建时一致" % name))
    rows.append((info.get("exe_sha256", "").lower() == _sha256(exe).lower(),
                 "exe 哈希与 build_info 一致"))

    # ---- N-22：--exe 三项新检查 ----
    # 1) sticker_manifest.json 必须在 exe 旁边（N-23：缺了 frozen 版会关闭表情）
    manifest_file = exe.parent / "sticker_manifest.json"
    rows.append((manifest_file.is_file(),
                 "sticker_manifest.json %s（缺了发布版表情全部改签名）"
                 % ("存在" if manifest_file.is_file() else "缺失")))
    # 2) build_info.built_at 不早于源码修改时间——拿旧 build_info 配新源码一票否决
    built_at = str(info.get("built_at") or "")
    try:
        import time as _time
        bt = _time.mktime(_time.strptime(built_at, "%Y-%m-%d %H:%M:%S"))
        src_mtime = max((PROG / n).stat().st_mtime for n in ("main.py", "index.html"))
        rows.append((bt >= src_mtime - 1,   # 1 秒容差
                     "build_info.built_at（%s）不早于源码修改时间" % built_at))
    except (ValueError, OSError):
        rows.append((False, "build_info.built_at 解析失败（%r）" % built_at))
    # 3) MANIFEST.json 里登记的 exe 哈希 == build_info.exe_sha256
    #    （组装后 MANIFEST 登记的是包内 exe，两者不一致 = 组装拿了错文件）
    for mf in (exe.parent / "03_校验" / "MANIFEST.json",
               exe.parent.parent / "03_校验" / "MANIFEST.json"):
        if mf.is_file():
            try:
                man = json.loads(mf.read_text(encoding="utf-8"))
                entries = {e["path"]: e["sha256"] for e in man.get("files", [])}
                rel_exe = "01_程序/%s" % exe.name
                want = entries.get(rel_exe) or entries.get(exe.name)
                rows.append((bool(want) and want.lower() ==
                             str(info.get("exe_sha256", "")).lower(),
                             "MANIFEST 里的 exe 哈希与 build_info 一致"))
            except (OSError, ValueError) as exc:
                rows.append((False, "MANIFEST.json 读不了：%s" % exc))
            break
    return rows


def _release_wordlist_rows() -> list:
    """N-21：--release 时词表必须已配置（抽出来共用）。"""
    if private_words.private_names(PROG):
        return [(True, "私人词表已配置（%d 个词，内容不回显）"
                % len(private_words.private_names(PROG)))]
    return [(False,
             "私人词表为空：发布前必须配置（环境变量 "
             "PUPPY_PRIVATE_NAMES 或 01_程序\\private_names.local.txt）")]


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包前后自检")
    ap.add_argument("--exe", default="", help="已打好的 exe 路径（可选）")
    ap.add_argument("--release", action="store_true",
                    help="发布模式：私人词表为空也判 FAIL（发布检查不允许"
                         "「没配词表」蒙混过关；开发时不用加这个参数）")
    ap.add_argument("--tree", default="", help="N-22：检查一个发布目录（解压后的"
                                                "包内容），不该出现的文件存在即 FAIL")
    args = ap.parse_args(argv)

    rows = []
    if args.tree:
        # --tree：发布树检查（不再重复开发树检查，两棵树分开报告）
        tree = Path(args.tree).resolve()
        if not tree.is_dir():
            print("目录不存在：%s" % tree, file=sys.stderr)
            return 2
        rows = check_tree(tree, is_release=True)
        if args.exe:
            rows += check_exe(Path(args.exe))
        if args.release:
            rows += _release_wordlist_rows()
    else:
        rows = check()
        if args.exe:
            rows += check_exe(Path(args.exe))
        if args.release:
            rows += _release_wordlist_rows()

    bad = [r for r in rows if not r[0]]
    print("=" * 62)
    print("礼貌小狗 · 打包自检")
    print("=" * 62)
    for ok, label in rows:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
    print("-" * 62)
    print("共 %d 项，通过 %d，缺 %d" % (len(rows), len(rows) - len(bad), len(bad)))
    if bad:
        print("缺项：")
        for _, label in bad:
            print("  - " + label)
        print("\n打包时记得：")
        print('  pyinstaller --add-data "puppy_agent;puppy_agent" '
              '--add-data "表情包;表情包" --add-data "voices;voices" '
              "--hidden-import memory_organize --hidden-import local_brain main.py")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
