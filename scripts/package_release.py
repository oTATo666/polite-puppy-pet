# -*- coding: utf-8 -*-
r"""
发布脚本：用白名单组装外发包。

为什么必须白名单
    以前的包是「整个目录拷一遍」，结果把 记忆\memory.db（1.4 万条真实对话）、
    记忆\backup\ 下 6 份全量备份、voices\我的声音\ 下 20 个真人声纹 wav、
    .env 明文 Key 一起发了出去。白名单的思路反过来：**只拷明确列出来的东西**，
    没列的一律不出去；拷完再扫一遍，扫到不该有的直接判失败。

用法
    :: 默认输出到 上级目录\发布_<版本>_<日期>\
       python scripts\package_release.py

    :: 指定输出目录
       python scripts\package_release.py --out D:\外发\礼貌小狗

    :: 顺带带上已经打好的 exe（默认不带：改完源码必须重新打包，旧 exe 会盖掉新代码）
       python scripts\package_release.py --with-exe

    :: 只做校验（扫一个已有的包，不生成）
       python scripts\package_release.py --check D:\外发\礼貌小狗

产出
    发布_<版本>_<日期>\
      00_先看这个.md
      01_程序\            程序 + 素材 + .env.example（没有 .env）
      02_文档\            说明文档
      03_校验\SHA256.txt  逐文件哈希
      03_校验\MANIFEST.json  版本号 + 文件清单 + 生成时间（自动生成，不再手写）

退出码：0 = 通过；1 = 校验失败（附具体原因）；2 = 参数/路径错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import private_words   # N-21：私人词表共享模块（同目录）

# 程序目录 = 本脚本的上一级（01_程序）
PROG_DIR = Path(__file__).resolve().parent.parent
PKG_DIR = PROG_DIR.parent

# ---------------------------------------------------------------- 白名单
# 只列文件名（相对 01_程序）。列不到的东西不会被拷贝。
# 注意：exe / build_info.json / sticker_manifest.json 不在这里——它们是
# 构建产物，只能从 dist\ 目录取（N-22），从源码目录取必拿旧文件。
INCLUDE_FILES = [
    ".env.example",
    "README.md",
    "使用说明.txt",
    "main.py",
    "index.html",
    "identity.py",
    "local_brain.py",
    "memory_organize.py",
    "puppy_core.py",
    "puppy_agent_boot.py",
    "persona.json",
    "brain.json",
    "PuppyAgent.config.json",
    "requirements.txt",
    "requirements-voice.txt",
    "run.bat",
    "启动克隆音色服务.cmd",
    "dog_girl.png",
    "dog_girl.ico",
]

# 构建产物（相对 dist 目录）：发布包里 exe 和清单必须是同一时间点的产物
DIST_FILES = [
    "PolitePuppy.exe",
    "build_info.json",
    "sticker_manifest.json",
]

# 整目录白名单（相对 01_程序）
INCLUDE_DIRS = [
    "puppy_agent",
    "tools",
    "scripts",
    "tests",
    "表情包",
    "voices/profiles/小狗",
]

# 单文件白名单里的“目录内的点文件”
INCLUDE_VOICE_INDEX = ["voices/active.json", "voices/voice_id.json"]

# 文档目录整份带走
DOC_DIR_NAME = "02_文档"

# 万一以后有人往白名单里加了危险项，这里再兜一层
DENY_DIR_NAMES = {
    "记忆", "backup", "我的声音", ".voice_cache", "agent_data",
    "__pycache__", ".git", ".idea", ".vscode", "node_modules", "生成",
}
# N-21：private_names.local.txt（私人词表）只留本机；.example 模板不在
# INCLUDE_FILES 白名单里，天然不会进外发包。
DENY_FILE_NAMES = {".env", "memory_diary.txt", "settings.json",
                   "private_names.local.txt"}
DENY_SUFFIXES = {".db", ".db-wal", ".db-shm", ".pyc", ".pyo", ".log", ".tmp"}

# 文本扫描：这些扩展名会被当文本读，检查有没有泄露用户名 / 绝对路径
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".html", ".js", ".css", ".bat",
                 ".cmd", ".sql", ".example", ".cfg", ".ini", ".yml", ".yaml"}


class Fail(Exception):
    pass


# 扫描用的特征串：拆开拼，免得这个脚本扫到自己（它自己也随包发布）
USER_ROOT = "C:" + "\\" + "Users"
USER_ROOT_JSON = "C:" + "\\\\" + "Users"


def app_version() -> str:
    """从 main.py 里读 APP_VERSION（找不到就 unknown），不再手写版本号。"""
    try:
        text = (PROG_DIR / "main.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "unknown"


def _skip(path: Path) -> bool:
    name = path.name
    if name in DENY_FILE_NAMES or name in DENY_DIR_NAMES:
        return True
    if path.suffix.lower() in DENY_SUFFIXES:
        return True
    if name.endswith((".db-wal", ".db-shm")):
        return True
    return False


def _copy_tree(src: Path, dst: Path, log: list,
               skip_names: set | None = None,
               skip_suffixes: set | None = None) -> None:
    """按白名单拷目录，逐项过滤。"""
    skip_names = skip_names or set()
    skip_suffixes = skip_suffixes or set()
    for root, dirs, files in os.walk(src):
        root_p = Path(root)
        dirs[:] = [d for d in dirs if d not in DENY_DIR_NAMES]
        rel_root = root_p.relative_to(src)
        target_root = dst / rel_root
        for d in dirs:
            target_root.joinpath(d).mkdir(parents=True, exist_ok=True)
        for f in files:
            p = root_p / f
            if _skip(p):
                log.append("跳过（黑名单）：%s" % p.relative_to(src.parent))
                continue
            if f in skip_names or p.suffix.lower() in skip_suffixes:
                log.append("跳过（release）：%s" % p.relative_to(src.parent))
                continue
            out = target_root / f
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_dist_artifacts(dist_dir: Path, prog_out: Path) -> list:
    """N-22：从构建产物目录拷 exe + build_info + 表情清单，并当场验证绑定。

    验证三条，任何一条不过直接抛 Fail 中止组装：
      1. sha256(包内 exe) == build_info.exe_sha256 —— exe 和构建信息是一对；
      2. build_info 源码哈希 == 包内源码哈希 —— exe 是这份源码打的；
      3. sticker_manifest.json 存在 —— frozen 版缺清单会关闭表情（N-23）。
    """
    problems: list[str] = []
    for name in DIST_FILES:
        src = dist_dir / name
        if not src.is_file():
            problems.append("构建产物缺失：%s（应在 %s）" % (name, dist_dir))
            continue
        shutil.copy2(src, prog_out / name)

    if problems:
        raise Fail("；".join(problems))

    exe = prog_out / "PolitePuppy.exe"
    info = json.loads((prog_out / "build_info.json").read_text(encoding="utf-8"))
    got = _sha256(exe)
    want = str(info.get("exe_sha256", "")).lower()
    if got != want:
        raise Fail("exe 与 build_info 不是同一次构建：sha256(exe)=%s… != "
                   "build_info.exe_sha256=%s…（N-22 中止组装）"
                   % (got[:16], want[:16]))
    for name in ("main.py", "index.html"):
        info_hash = str((info.get("source") or {}).get(name, "")).lower()
        if info_hash != _sha256(prog_out / name):
            raise Fail("build_info 里的 %s 哈希与包内源码不一致"
                       "（exe 是旧源码打的？N-22 中止组装）" % name)
    if not (prog_out / "sticker_manifest.json").is_file():
        raise Fail("sticker_manifest.json 未随构建产物拷入（N-22 中止组装）")
    return problems


def build(out_dir: Path, with_exe: bool = False,
          dist_dir: Path | None = None, is_release: bool = False,
          tests_count: int = 0) -> dict:
    prog_out = out_dir / "01_程序"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    prog_out.mkdir(parents=True, exist_ok=True)
    log: list[str] = []

    # ---- 单文件 ----
    missing = []
    for name in INCLUDE_FILES:
        src = PROG_DIR / name
        if not src.is_file():
            missing.append(name)
            continue
        dst = prog_out / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # ---- N-24/N-25：--release 时用脱敏版替换人设与语料 ----
    # 个人版的 persona.json / brain.json 含真实名字与真实语料，只留在本机；
    # 外发包必须用 release\ 下的中性人设与脱敏语料。缺文件 = 中止，宁可
    # 不发也不能把未脱敏的东西带出去。
    if is_release:
        rel_persona = PROG_DIR / "release" / "persona.json"
        if not rel_persona.is_file():
            raise Fail("release\\persona.json 不存在：外发包必须有中性人设"
                       "（跑 scripts\\make_release_persona.py 或手工准备）")
        shutil.copy2(rel_persona, prog_out / "persona.json")
        rel_brain = PROG_DIR / "release" / "brain.json"
        if not rel_brain.is_file():
            raise Fail("release\\brain.json 不存在：外发包必须用脱敏语料"
                       "（先跑 scripts\\scrub_brain.py）")
        shutil.copy2(rel_brain, prog_out / "brain.json")

    # ---- 目录 ----
    # N-26：--release 时 voices 不带任何参考音频 / voice_id.json——
    # 声纹是生物特征，未确认授权前绝不外发；克隆功能保留（用户自己提供音频）。
    voice_skip_names = {"voice_id.json"} if is_release else set()
    voice_skip_suffixes = {".wav", ".mp3", ".flac", ".pcm", ".ogg", ".m4a"} \
        if is_release else set()
    for name in INCLUDE_DIRS:
        src = PROG_DIR / name
        if not src.is_dir():
            missing.append(name + "/")
            continue
        if name.startswith("voices"):
            _copy_tree(src, prog_out / name, log,
                       skip_names=voice_skip_names,
                       skip_suffixes=voice_skip_suffixes)
        else:
            _copy_tree(src, prog_out / name, log)

    # ---- 单文件：voices 索引 ----
    for name in INCLUDE_VOICE_INDEX:
        if is_release and name.endswith("voice_id.json"):
            log.append("跳过（release）：voices/voice_id.json（N-26 声纹不外发）")
            continue
        src = PROG_DIR / name
        if src.is_file():
            dst = prog_out / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            missing.append(name)

    # ---- 可选：exe + 构建产物（N-22：只从 dist 取，且当场验证绑定） ----
    if with_exe:
        dist = dist_dir or (PROG_DIR / "dist")
        if not dist.is_dir():
            raise Fail("构建产物目录不存在：%s（先跑 PyInstaller）" % dist)
        _copy_dist_artifacts(dist, prog_out)

    # ---- 文档 ----
    docs_src = PKG_DIR / DOC_DIR_NAME
    if docs_src.is_dir():
        # N-8：审阅过程文档（状态更新 / 任务完成列表 / STATUS）不外发——
        # 那是内部流水，不是给用户的说明。
        (out_dir / DOC_DIR_NAME).mkdir(parents=True, exist_ok=True)
        for p in sorted(docs_src.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(docs_src)
            if rel.name.startswith(("状态更新_", "任务完成列表", "STATUS")):
                log.append("跳过（内部文档）：%s" % rel)
                continue
            dst = out_dir / DOC_DIR_NAME / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)

    # ---- 根目录说明 ----
    head = PKG_DIR / "00_先看这个.md"
    if head.is_file():
        shutil.copy2(head, out_dir / head.name)

    # ---- N-8：回填 README 数字占位符（必须在算 MANIFEST 哈希之前！） ----
    # 占位符：{{TESTS}} / {{FILES}} / {{EXE_SHA16}}。
    # TESTS 由 --tests 传入（通常从证据文件读到的用例数）；FILES = 包内文件数；
    # EXE_SHA16 从 build_info.json 读（与 exe 同一构建产物）。
    readme = out_dir / "01_程序" / "README.md"
    if readme.is_file():
        try:
            txt = readme.read_text(encoding="utf-8")
            n_files = sum(1 for p in out_dir.rglob("*")
                          if p.is_file() and "03_校验" not in p.parts)
            sha16 = ""
            bi = out_dir / "01_程序" / "build_info.json"
            if bi.is_file():
                sha16 = str(json.loads(bi.read_text(encoding="utf-8"))
                            .get("exe_sha256") or "")[:16]
            txt = txt.replace("{{TESTS}}", str(int(tests_count)))
            txt = txt.replace("{{FILES}}", str(n_files))
            txt = txt.replace("{{EXE_SHA16}}", sha16 or "缺 build_info")
            readme.write_text(txt, encoding="utf-8")
            log.append("README 数字回填：测试 %s / 文件 %d / SHA16 %s"
                       % (tests_count, n_files, sha16[:8]))
        except (OSError, ValueError) as exc:
            log.append("README 回填失败：%s" % exc)

    # ---- 校验清单 ----
    ver = app_version()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    check_dir = out_dir / "03_校验"
    check_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(out_dir).as_posix()
        if rel.startswith("03_校验/"):
            continue
        data = p.read_bytes()
        entries.append({"path": rel, "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})

    manifest = {
        "name": "礼貌小狗桌宠",
        "version": ver,
        "generated_at": stamp,
        "generator": "scripts/package_release.py",
        "python": "3.10",
        "file_count": len(entries),
        "total_bytes": sum(e["bytes"] for e in entries),
        "files": entries,
    }
    (check_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (check_dir / "SHA256.txt").write_text(
        "".join("%s  %s\n" % (e["sha256"], e["path"]) for e in entries),
        encoding="utf-8")

    return {"out": str(out_dir), "version": ver, "files": len(entries),
            "bytes": manifest["total_bytes"], "missing": missing, "log": log}


def scan(root: Path) -> list[str]:
    """扫包：返回问题列表，空列表 = 通过。"""
    problems: list[str] = []
    user = Path.home().name
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if p.is_dir():
            if p.name in DENY_DIR_NAMES:
                problems.append("不该出现的目录：%s" % rel)
            continue
        # N-21：黑名单文件（.env / 词表 / settings.json / 日记）通用兜底
        if p.name in DENY_FILE_NAMES:
            problems.append("不该出现的私密文件：%s" % rel)
        if p.suffix.lower() in (".db", ".db-wal", ".db-shm", ".pyc", ".pyo"):
            problems.append("不该出现的文件：%s" % rel)
        if p.name in DENY_DIR_NAMES:
            problems.append("不该出现的文件：%s" % rel)
        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if USER_ROOT in text or USER_ROOT_JSON in text:
            problems.append("出现绝对用户路径：%s" % rel)
        elif user and user in text:
            problems.append("出现本机用户名「%s」：%s" % (user, rel))
    return problems


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="按白名单组装外发包，并做泄露扫描")
    ap.add_argument("--out", default="", help="输出目录（默认 上级目录\发布_<版本>_<日期>）")
    ap.add_argument("--with-exe", action="store_true", help="顺带拷入已打好的 exe")
    ap.add_argument("--dist", default="", help="构建产物目录（默认 01_程序\\dist；"
                                                "N-22：exe/清单只从这里取）")
    ap.add_argument("--check", default="", help="只扫描一个已有的包，不生成")
    ap.add_argument("--tests", type=int, default=0,
                    help="README 占位符 {{TESTS}} 的实测用例数（从证据文件读）")
    ap.add_argument("--release", action="store_true",
                    help="发布模式：私人词表为空直接失败（发布前必须配置词表，"
                         "否则扫描器会安静地放过昵称）")
    args = ap.parse_args(argv)

    # N-21 发布门：词表为空的机器不允许做发布检查/组装——先配置词表再来
    if args.release and not private_words.private_names(PROG_DIR):
        print("私人词表为空：发布前必须配置（环境变量 PUPPY_PRIVATE_NAMES "
              "或 01_程序\\private_names.local.txt）", file=sys.stderr)
        return 1

    if args.check:
        target = Path(args.check).resolve()
        if not target.is_dir():
            print("目录不存在：%s" % target, file=sys.stderr)
            return 2
        problems = scan(target)
        if problems:
            print("扫描未通过，共 %d 处：" % len(problems))
            for x in problems[:60]:
                print("  - " + x)
            return 1
        print("扫描通过：无 .db / 无 .env / 无 我的声音 / 无 __pycache__ / 无本机用户名")
        return 0

    ver = app_version()
    out = Path(args.out).resolve() if args.out else (
        PKG_DIR / ("发布_%s_%s" % (ver, time.strftime("%Y%m%d"))))
    try:
        res = build(out, with_exe=args.with_exe,
                    dist_dir=Path(args.dist).resolve() if args.dist else None,
                    is_release=args.release, tests_count=args.tests)
    except OSError as exc:
        print("组装失败：%s" % exc, file=sys.stderr)
        return 2
    except Fail as exc:
        # N-22：验证不过就中止，且不留半成品目录（防止有人拿残包当发布包）
        shutil.rmtree(out, ignore_errors=True)
        print("组装中止：%s" % exc, file=sys.stderr)
        return 1

    print("已组装：%s" % res["out"])
    print("版本：%s（从 main.py 的 APP_VERSION 自动读取）" % res["version"])
    print("文件：%d 个，共 %.1f MB" % (res["files"], res["bytes"] / 1048576.0))
    if res["missing"]:
        print("注意：白名单里有 %d 项不存在，已跳过：" % len(res["missing"]))
        for m in res["missing"]:
            print("  - " + m)
    if res["log"]:
        print("按黑名单跳过 %d 项" % len(res["log"]))

    problems = scan(out)
    if problems:
        print("\n扫描未通过，共 %d 处：" % len(problems))
        for x in problems[:60]:
            print("  - " + x)
        return 1
    print("扫描通过：无 .db / 无 .env / 无 我的声音 / 无 __pycache__ / 无本机用户名")
    print("清单：%s" % (out / "03_校验" / "MANIFEST.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
