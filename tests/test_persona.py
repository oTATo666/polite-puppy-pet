# -*- coding: utf-8 -*-
"""冒烟测试 6：persona 回归集（阶段 C 的 Wave 2 项）。

目的
    任何语料 / 起稿 / 打分改动之后，先跑这一组，防止"越改越不像她"。
    这里锁的是**硬规则**，不是审美：
      - 长句不能用单字回；
      - 不许出现解释腔（因为 / 所以 / 建议 / 首先 / 总之）；
      - 六轮内不许重复同一句；
      - 句长不超过角色的 MAX_LEN。

话题回声率
    10 条样例里，回复与他这句话有 2 字重叠的数量。
    当前实测是 2/10 —— 这是**现状基线**，不是目标值：语料里对"面试""下雨"
    这类话题本来就没有对应句子，靠打分权重补不出来（P1-6 遗留项）。
    这里断言 >= 2，只用于"别再往下掉"的回归保护。
"""

from __future__ import annotations

import random
import unittest

from _ctx import HAS_BRAIN, SKIP_NO_BRAIN, main  # noqa: F401  （只为了让 sys.path / 日志先就绪）

import local_brain

PROMPTS = [
    "我今天下班路上突然想起来你说的那件事了",
    "楼下那家小卖部好像关门了",
    "今天好累啊",
    "我明天要去面试，有点紧张",
    "你还记得我上次说的那只猫吗",
    "外面下雨了",
    "我睡不着",
    "今天发工资了",
    "我有点想你了",
    "周末要不要一起去公园",
]

BANNED = ("因为", "所以", "建议", "首先", "总之", "其实你可以")

# 固定种子：同一个仓库状态下结果可复现，语料一变就会显形。
SEED = 20261120


def _bigrams(text: str) -> set:
    return {text[i:i + 2] for i in range(len(text) - 1)}


@unittest.skipUnless(HAS_BRAIN, SKIP_NO_BRAIN)
class PersonaRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = local_brain.Brain()
        cls.b._rng = random.Random(SEED)

    def test_ten_turn_persona_rules(self):
        history: list[dict] = []
        replies: list[str] = []
        echoed = 0

        for prompt in PROMPTS:
            out = self.b.reply(prompt, history)
            self.assertTrue(out.get("ok"), out)
            reply = str(out.get("reply") or "")

            self.assertGreaterEqual(len(reply), 2,
                                    "长句不能单字回复：%r -> %r" % (prompt, reply))
            self.assertLessEqual(len(reply), local_brain.Brain.MAX_LEN,
                                 "超长回复：%r" % reply)
            for word in BANNED:
                self.assertNotIn(word, reply, "解释腔：%r" % reply)

            recent = [m["text"] for m in history[-6:] if m.get("role") == "assistant"]
            self.assertNotIn(reply, recent, "六轮内重复：%r" % reply)

            if _bigrams(prompt) & _bigrams(reply):
                echoed += 1

            replies.append(reply)
            history += [{"role": "user", "text": prompt},
                        {"role": "assistant", "text": reply}]

        self.assertGreaterEqual(echoed, 2,
                                "话题回声率下降（当前基线 2/10）：%r" % replies)

    def test_no_empty_reply_on_short_ping(self):
        out = self.b.reply("在吗", [])
        self.assertTrue(out.get("ok"))
        self.assertTrue(str(out.get("reply") or "").strip())


if __name__ == "__main__":
    unittest.main()
