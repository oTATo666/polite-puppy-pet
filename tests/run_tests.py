# -*- coding: utf-8 -*-
r"""
一键冒烟测试（不需要 pytest，只用标准库 unittest）。

用法
    cd 01_程序
    python tests\run_tests.py            :: 跑全部
    python tests\run_tests.py -v         :: 带每条用例名
    python tests\run_tests.py http       :: 只跑某一个文件（按名字过滤）

覆盖范围（对应《改进执行方案》P1-4）
    1. /api/health 分层自检            —— P1-2
    2. 记忆读写 / 软删 / 事实层复活     —— P1-1
    3. Agent 路由、沙盒、敏感路径、越界确认 —— P0-2
    4. 敏感路径 404、跨站 403、接口可用  —— P0-2 / P1-5

测试全程只写临时目录，不碰真实的 记忆\memory.db 和 .env。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbosity = 1
    if "-v" in argv:
        argv.remove("-v")
        verbosity = 2
    pattern = "test_*.py"
    if argv:
        pattern = "test_*%s*.py" % argv[0]

    suite = unittest.defaultTestLoader.discover(
        str(HERE), pattern=pattern, top_level_dir=str(HERE))
    if not suite.countTestCases():
        print("没找到任何用例（pattern=%s）" % pattern)
        return 2

    print("=" * 64)
    print("礼貌小狗 · 冒烟测试（%d 条用例，数据目录为临时目录）"
          % suite.countTestCases())
    print("=" * 64)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    print("=" * 64)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("结果：%d 条，通过 %d，失败/错误 %d，跳过 %d"
          % (total, total - bad - len(result.skipped), bad, len(result.skipped)))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
