# -*- coding: utf-8 -*-
r"""AG-5：Agent 规划评测命令行。

用法
    cd 01_程序
    python scripts\agent_eval.py            :: 跑全量并输出汇总
    python scripts\agent_eval.py --json     :: 输出 JSON（给证据文件用）
    python scripts\agent_eval.py --min 150  :: 要求黄金集至少 N 条（默认 150）

指标门：全类别 100% 通过才退出码 0；黄金集不足 150 条退出码 2。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # scripts/
PROG = HERE.parent                               # 01_程序
EVAL_DIR = PROG / "tests" / "agent_eval"

for p in (str(EVAL_DIR), str(PROG), str(PROG / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from runner import evaluate  # noqa: E402


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Agent 规划评测（黄金集）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--min", type=int, default=150, help="黄金集最少条数")
    args = ap.parse_args(argv)

    res = evaluate()
    summary = {
        "total": res["total"],
        "passed": res["passed"],
        "failed": res["failed"],
        "by_cat": {k: {"total": v["total"], "passed": v["passed"]}
                   for k, v in res["by_cat"].items()},
        "failures": res["failures"][:20],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        print("=" * 56)
        print("Agent 规划评测（AG-5 黄金集）")
        print("=" * 56)
        for cat, st in summary["by_cat"].items():
            mark = "OK " if st["passed"] == st["total"] else "FAIL"
            print("  [%s] %-8s %3d/%3d" % (mark, cat, st["passed"], st["total"]))
        print("-" * 56)
        print("合计：%d/%d 通过，失败 %d" % (res["passed"], res["total"],
                                            res["failed"]))
        if res["failures"]:
            print("失败明细（前 20）：")
            for f in res["failures"]:
                print("  %s [%s] %s… → %s" % (f["id"], f["cat"],
                                              f["text"], f["why"]))

    if res["total"] < args.min:
        print("黄金集只有 %d 条（要求 ≥%d）" % (res["total"], args.min),
              file=sys.stderr)
        return 2
    if res["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
