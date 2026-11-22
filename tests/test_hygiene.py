# -*- coding: utf-8 -*-
"""冒烟测试 7：工程卫生（阶段 C）。

锁三件容易悄悄退化的事：
  1. 裸 `except Exception: pass` 不许再变多（基线 33，上限 40）；
  2. main.py / index.html 的换行不许被编辑器改成混合（项目约定纯 CRLF）；
  3. 源码与文档里不许再出现本机绝对路径 / 用户名 / 私人昵称（N-5）。

N-5 补充：私人词不再写死在测试里——从环境变量 PUPPY_PRIVATE_NAMES 或
本地配置 private_names.local.txt（不入库）读取，并自动派生 8.3 短名
（前 6 字符 + ~1，如用户名 someone -> SOMEON~1，Windows TEMP 路径
用的就是这种形式，上一轮证据日志里泄漏过的正是它）。
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

from _ctx import main  # noqa: F401

PROG = Path(main.__file__).resolve().parent

BARE_LIMIT = 40
SKIP_DIRS = {"记忆", "voices", ".voice_cache", "agent_data", "__pycache__",
             ".git", "node_modules", "build", "dist"}
TEXT_EXT = {".py", ".html", ".js", ".css", ".json", ".txt", ".md", ".cmd",
            ".bat", ".sql", ".ps1", ".spec", ".example"}
# 词表本身必然含敏感词，不扫自己（N-21：词表逻辑统一在 scripts/private_words.py）
_SCRIPTS = PROG / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import private_words  # noqa: E402

PRIVATE_LIST_FILE = private_words.LIST_FILE


def _private_names() -> list:
    """N-21：私人词表统一走 scripts/private_words.py（测试/发布门同源）。"""
    return private_words.private_names(PROG)


def _text_files():
    for p in PROG.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
            continue
        if p.name == PRIVATE_LIST_FILE:
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(PROG).parts):
            continue
        yield p


class HygieneTest(unittest.TestCase):
    def test_bare_except_not_growing(self):
        lines = (PROG / "main.py").read_text(encoding="utf-8").splitlines()
        bare = 0
        for i, line in enumerate(lines):
            if re.match(r"^\s*except Exception\s*:\s*$", line):
                for nxt in lines[i + 1:i + 4]:
                    if nxt.strip():
                        if nxt.strip() == "pass":
                            bare += 1
                        break
        self.assertLessEqual(bare, BARE_LIMIT,
                             "裸 except + pass 变多了：%d（上限 %d）" % (bare, BARE_LIMIT))

    def test_main_files_are_pure_crlf(self):
        for name in ("main.py", "index.html"):
            data = (PROG / name).read_bytes()
            crlf = data.count(b"\r\n")
            lf_only = data.count(b"\n") - crlf
            self.assertEqual(lf_only, 0, "%s 混入了 %d 个裸 LF" % (name, lf_only))
            self.assertGreater(crlf, 1000, "%s 换行数异常" % name)

    def test_no_local_absolute_paths(self):
        # 动态拼，避免这条测试把自己算成命中
        abs_user = "C:" + "\\" + "Users"
        privates = _private_names()
        if not privates:
            print("[hygiene] 未配置私人词表（环境变量 PUPPY_PRIVATE_NAMES 或 "
                  "01_程序/private_names.local.txt），本轮只查绝对路径与 8.3 短名")
        # N-24/D1-D3：个人版数据文件（语料/人设/日记）本来就含真实称呼，
        # 属正常；这条测试守的是"源码与文档不出现"，发布包的 0 命中由
        # package_release 的扫描 + --release 门保证。
        data_files = {"brain.json", "persona.json", "memory_diary.txt"}
        hits, notes = [], []
        for p in _text_files():
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            low = text.lower()
            if abs_user.lower() in low:
                hits.append(str(p.relative_to(PROG)) + "（本机绝对路径）")
            for name in privates:
                if name.lower() in low:
                    # 失败消息里打码，避免测试输出本身把词打出来
                    tag = "%s（私人词 %s***）" % (p.relative_to(PROG), name[:2])
                    (notes if p.name in data_files else hits).append(tag)
        if notes:
            print("[hygiene] 个人版数据文件含私人词（正常，不外发）：%r" % notes)
        self.assertEqual(hits, [], "出现本机路径/私人词：%r" % hits)

    def test_private_list_stays_out_of_pack(self):
        """N-5：私人词表不许出现在打包清单（MUST_NOT_SHIP）之外——
        它要么不存在，要么被 pack_check 明确列为"别打进包"。"""
        pc = PROG / "scripts" / "pack_check.py"
        text = pc.read_text(encoding="utf-8")
        self.assertIn(PRIVATE_LIST_FILE, text,
                      "pack_check 的 MUST_NOT_SHIP 应包含 private_names.local.txt")


if __name__ == "__main__":
    unittest.main()
