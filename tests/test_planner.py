# -*- coding: utf-8 -*-
"""AG-2：规划器接口对照测试。

规则本体从 main.py::_agent_plan 原样搬进 puppy_agent.core.planner.RulePlanner，
这里的职责是**锁死两边一致**：

  · 原有用例（迁移前就存在的行为）逐条对照——main._agent_plan 与
    RulePlanner().plan 必须返回完全相同的 dict；
  · 新增 40 条合成用例覆盖全部四条规则分支 + 边界（空/超长/标点/引号路径/
    密钥无扩展名/记忆搜索词清洗），同样两边对照；
  · 接口契约：plan() 返回 {tool, args, reason} 三键；闲聊/空文本 tool=""。

对"新前端 + 旧后端"式漂移的防线：以后任何人改规划逻辑，只能改 planner.py，
main 侧壳子不动；两边若出现分叉，本文件立刻红。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from _ctx import AGENT_DATA, PROG, main

if str(PROG / "puppy_agent") not in sys.path:
    sys.path.insert(0, str(PROG / "puppy_agent"))

from puppy_agent.core.planner import RulePlanner  # noqa: E402


# 原有用例：迁移前 main._agent_plan 就这么答（行为基线）
LEGACY_CASES = [
    # (输入, 期望 {"tool", "args"} )
    ("", ""),
    ("帮我看看 notes.txt 写了啥", "fs_read"),
    ("读一下 D:\\样例\\Desktop\\报告.md", "fs_read"),
    # 含空格路径：迁移前正则就不支持（[^\s…]+? 遇空格断开）——保持原样，
    # 支持含空格路径属于新能力，须在 planner.py 显式改 + 本表同步改基线
    ("读一下 D:\\样例\\Desktop\\a b\\报告.md", ""),
    ("打开 config.json", "fs_read"),
    ("看看 index.html 里那个按钮", "fs_read"),
    ("读 D:\\o'brien\\notes.txt", "fs_read"),          # 单引号路径
    ("读一下 .ssh\\config", "fs_read"),                        # 密钥无扩展名
    ("看看 D:\\x\\.env", "fs_read"),
    ("翻翻 id_rsa", ""),                                       # 无动词触发词 → 闲聊
    ("列一下 D 盘的文件夹", "fs_list"),
    ("目录里有什么", "fs_list"),
    ("有哪些文件", "fs_list"),
    ("列目录", "fs_list"),
    ("记忆库里有多少条", "mem_stats"),
    ("记忆统计", "mem_stats"),
    ("你记了多少", "mem_stats"),
    ("你还记得我叫什么吗", "mem_search"),
    ("帮我回忆一下上周说过的话", "mem_search"),
    ("记忆里有没有他的生日", "mem_search"),
    ("搜一下记忆", "mem_search"),
    ("今天天气怎么样", ""),                                    # 闲聊
    ("你觉得我人怎么样", ""),                                  # 闲聊
    ("x" * 201, ""),                                           # 超长
    ("读一下没有扩展名的文件", ""),                            # 动词但无文件名
]

# 新增 40 条：覆盖四分支 + 边界（两边对照，不写死期望值，
# 因为断言的对象就是"两边一致"本身；仅在注释里标注覆盖点）
EXTRA_CASES = [
    # --- fs_read 分支（动词 + 扩展名） ---
    "读读 todo.txt",
    "打开 C:\\a\\b\\c.py",
    "看一下 readme.MD",                       # 大小写
    "瞅瞅 data.csv 里的数字",
    "翻翻 logs\\app.log",
    "读一下 main.py",
    "看看 settings.ini",
    "打开脚本 run.bat",
    "帮我看看 table.sql",
    "读 notes.md 顺便总结一下",
    # --- fs_read 密钥/配置分支（无常规扩展名） ---
    "读一下 D:\\样例\\.ssh\\config",
    "打开 .ssh/config",
    "看看 known_hosts",
    "读 D:\\样例\\AppData\\Local\\Temp\\credentials",
    "打开 hosts",
    "读一下 .env",
    # --- fs_list 分支 ---
    "列出 D:\\projects",
    "文件夹里有什么",
    "看看目录",
    "目录下有什么",
    "列个清单文件夹",
    "有什么文件在 C:\\",
    # --- mem_stats 分支 ---
    "记忆库有多少条了",
    "存了多少条",
    "记住多少东西了",
    "问下记忆库状态",
    # --- mem_search 分支（清洗边界） ---
    "你还记得吗我说过的那个电影",
    "回忆一下昨天",
    "回想一下他的名字",
    "查一下记忆里关于猫的内容",
    "搜一下记忆 面试",
    "记忆库里有没有存过快递地址",
    "你记不记得我说过想学吉他",
    "还记得吗那天吃火锅",
    # --- 闲聊 / 不触发 ---
    "帮我写一首诗",
    "把文件删了吧",                          # 危险动词但无规则 → 闲聊（保守）
    "读一下！",                              # 动词后无文件名
    "列一下要点",                            # "列一下" 但无路径 → fs_list 兜底 "."
    "帮我回忆一下你之前说过的关于工作的事",
]


class PlannerContractTest(unittest.TestCase):
    """AG-2：接口契约 + 两边逐条对照。"""

    @classmethod
    def setUpClass(cls):
        AGENT_DATA.mkdir(parents=True, exist_ok=True)
        cls.rp = RulePlanner()
        cls.cases = [(t, tool) for t, tool in LEGACY_CASES] + \
                    [(t, None) for t in EXTRA_CASES]

    def _both(self, text: str):
        a = main._agent_plan(text)
        b = self.rp.plan(text)
        return a, b

    def test_identical_on_legacy_cases(self):
        """原有用例：main 壳与 RulePlanner 返回值完全一致，且 tool 符合基线。"""
        for text, want in LEGACY_CASES:
            a, b = self._both(text)
            self.assertEqual(a, b, "两边不一致：%r\nmain=%r\nplanner=%r"
                             % (text, a, b))
            self.assertEqual((a.get("tool") or ""), want,
                             "基线漂移：%r → %r（期望 %r）" % (text, a.get("tool"), want))

    def test_identical_on_extra_cases(self):
        """新增 40 条：两边逐条完全一致。"""
        for text in EXTRA_CASES:
            a, b = self._both(text)
            self.assertEqual(a, b, "两边不一致：%r\nmain=%r\nplanner=%r"
                             % (text, a, b))

    def test_return_shape(self):
        """返回三键齐全；闲聊/空文本 tool 为空串、args 为空 dict。"""
        for text in ("", "今天天气怎么样", "帮我看看 notes.txt",
                     "你还记得我叫什么吗", "记忆统计", "列目录"):
            r = self.rp.plan(text)
            self.assertEqual(set(r.keys()), {"tool", "args", "reason"}, (text, r))
            self.assertIsInstance(r["args"], dict)
            self.assertIsInstance(r["reason"], str) and None
            if text in ("", "今天天气怎么样"):
                self.assertEqual(r["tool"], "")

    def test_extra_cases_cover_all_branches(self):
        """EXTRA_CASES 确实覆盖了全部四个工具分支（防用例空转）。"""
        got = set()
        for text in EXTRA_CASES:
            got.add(self.rp.plan(text).get("tool") or "")
        self.assertTrue({"fs_read", "fs_list", "mem_stats", "mem_search", ""}
                        <= got, got)

    def test_rule_planner_never_calls_model(self):
        """RulePlanner 是纯函数式规则：不引用任何模型路由（冒烟：名字与依赖）。"""
        import inspect
        src = inspect.getsource(type(self.rp))
        self.assertNotIn("ModelRouter", src)
        self.assertNotIn("urlopen", src)
        self.assertNotIn("requests", src)


if __name__ == "__main__":
    unittest.main()
