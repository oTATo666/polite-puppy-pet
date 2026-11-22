# -*- coding: utf-8 -*-
"""AG-5：评测黄金集指标门（unittest 入口）。

指标门：
  · 规则用例（read/keyfile/list/stats/search/chat）100% 通过；
  · 模型用例 100% 通过（行为断言：回落链最终输出必须符合预期）；
  · 有效性门：任何输出 tool ∈ 已知工具集或 ""。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from runner import evaluate  # noqa: E402


class AgentEvalGate(unittest.TestCase):
    def test_rule_cases_100pct(self):
        res = evaluate()
        stats = res["by_cat"]
        for cat in ("read", "keyfile", "list", "stats", "search", "chat"):
            st = stats.get(cat)
            self.assertIsNotNone(st, "缺类别 %s" % cat)
            self.assertEqual(st["passed"], st["total"],
                             "%s 未全过：%d/%d\n失败：%s"
                             % (cat, st["passed"], st["total"],
                                [f for f in res["failures"]
                                 if f["cat"] == cat][:5]))

    def test_model_cases_all_pass(self):
        res = evaluate()
        st = res["by_cat"].get("model")
        self.assertIsNotNone(st)
        fails = [f for f in res["failures"] if f["cat"] == "model"]
        self.assertEqual(st["passed"], st["total"],
                         "model 用例未全过：%d/%d\n失败：%s"
                         % (st["passed"], st["total"], fails[:5]))

    def test_total_at_least_150(self):
        res = evaluate()
        self.assertGreaterEqual(res["total"], 150,
                                "黄金集不足 150 条：%d" % res["total"])


if __name__ == "__main__":
    unittest.main()
