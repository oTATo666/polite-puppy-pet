# -*- coding: utf-8 -*-
"""冒烟测试 5：本地大脑的措辞约束（P1-6）。"""

from __future__ import annotations

import unittest

from _ctx import HAS_BRAIN, SKIP_NO_BRAIN, main  # noqa: F401  （只为了让 sys.path / 日志先就绪）

import local_brain


@unittest.skipUnless(HAS_BRAIN, SKIP_NO_BRAIN)
class BrainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = local_brain.Brain()
        cls.st = local_brain.status()

    def test_corpus_loaded(self):
        self.assertTrue(self.st.get("ok"), self.st)
        self.assertGreater(self.st.get("sentences") or 0, 100)

    def test_no_single_char_for_long_question(self):
        """他打了一整句，就不能只回一个"嗯"。"""
        long_text = "我今天下班路上突然想起来你说的那件事了"
        for _ in range(20):
            out = self.b.reply(long_text, [])
            self.assertTrue(out.get("ok"), out)
            self.assertGreaterEqual(len(out["reply"]), 2,
                                    "长句不能用单字回复：%r" % out["reply"])

    def test_short_question_may_be_short(self):
        out = self.b.reply("在吗", [])
        self.assertTrue(out.get("reply"))

    def test_thought_explains_decision(self):
        # 中性句（不带明显意图）才会走打分分支；"好累"会直接走共情早退
        out = self.b.reply("楼下那家小卖部好像关门了", [])
        joined = " ".join(out.get("thought") or [])
        self.assertIn("打分", joined)

    def test_gate_rejects_explaining_draft(self):
        plan = self.b.deliberate("今天好累啊", [])
        bad = self.b.enforce("因为你太累了，所以我建议你早点睡", plan)
        self.assertFalse(bad.get("ok"))
        self.assertTrue(bad.get("reason"))

    def test_gate_accepts_short_draft(self):
        plan = self.b.deliberate("今天好累啊", [])
        ok = self.b.enforce("咋了", plan)
        self.assertTrue(ok.get("ok"), ok)
        self.assertEqual(ok.get("reply"), "咋了")


if __name__ == "__main__":
    unittest.main()
