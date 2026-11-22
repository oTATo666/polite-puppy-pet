# -*- coding: utf-8 -*-
"""私人词表的共享小模块（N-21）。

谁来用
    · tests/test_hygiene.py   —— 源码树扫描（开发时的护栏）
    · scripts/pack_check.py   —— --release 发布门（发布时词表为空直接 FAIL）
    · scripts/package_release.py —— 发布门 + 组装/扫描
    · scripts/simulate_acceptance.py —— 输出打码

词表来源（可叠加，顺序无关）
    1. 环境变量 PUPPY_PRIVATE_NAMES：逗号分隔；
    2. <prog_dir>/private_names.local.txt：一行一个，# 开头是注释。
       该文件在 .gitignore 里，不进版本库，pack_check / package_release
       都会拦它进发布包。

每个词额外派生 8.3 短名（前 6 字符 + "~1"）一起查——Windows 对超过 8
字符的名字就是这么缩的（someone -> SOMEON~1），路径和日志里出现的
多半是短名形式。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_NAME = "PUPPY_PRIVATE_NAMES"
LIST_FILE = "private_names.local.txt"


def private_names(prog_dir) -> list:
    """读私人词表（env + 本地文件），并派生 8.3 短名。词表为空返回 []。"""
    names = []
    raw = os.environ.get(ENV_NAME, "")
    names += [x.strip() for x in raw.split(",") if x.strip()]
    cfg = Path(prog_dir) / LIST_FILE
    if cfg.is_file():
        try:
            for line in cfg.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    names.append(line)
        except OSError:
            pass
    out, seen = [], set()
    for n in names:
        for v in (n, n[:6] + "~1"):
            k = v.lower()
            if v and k not in seen:
                seen.add(k)
                out.append(v)
    return out


def redact_text(text: str) -> str:
    """把输出里的本机痕迹打码：TEMP 路径 -> %TEMP%，用户名/短名 -> %USERPROFILE%。

    注意 TEMP 打码必须先于用户名打码——TEMP 路径里含用户名（含 8.3 短名），
    先换占位符就不会留下短名残渣（上一轮证据日志踩过的坑）。
    用户名换完后再把残留的「盘符:\\Users\\ 占位符」前缀一并吃掉——发布扫描
    的「绝对用户路径」标准连双反斜杠写法都会命中，光换名字不够。
    """
    t = re.sub(r"[Cc]:\\Users\\[^\\\r\n\"']+\\AppData\\Local\\Temp",
               "%TEMP%", text)
    user = Path.home().name
    short = user[:6] + "~1"
    for w in (user, short):
        if w:
            t = re.sub(re.escape(w), "%USERPROFILE%", t, flags=re.IGNORECASE)
    t = re.sub(r"[Cc]:\\Users\\\r?\n%USERPROFILE%", "%USERPROFILE%", t)
    t = re.sub(r"[Cc]:\\Users\\%USERPROFILE%", "%USERPROFILE%", t)
    return t
