# -*- coding: utf-8 -*-
"""AG-7：日常助手插件测试（8 工具）。

方案§7 风险等级：time_now / calc / note_list / remind_list 是 LOW；
note_add / remind_set / remind_cancel 是 MEDIUM；app_open 是 HIGH。
MEDIUM/HIGH 走安全层确认令牌，测试用 _call_confirmed 走完整流程。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

from _ctx import AGENT_DATA, PROG, main

if str(PROG / "puppy_agent") not in sys.path:
    sys.path.insert(0, str(PROG / "puppy_agent"))

from puppy_agent.core.runtime import AgentRuntime   # noqa: E402
from puppy_agent.core.safety import SafetyGate      # noqa: E402


class AssistantPluginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        AGENT_DATA.mkdir(parents=True, exist_ok=True)
        gate = SafetyGate(AGENT_DATA)
        cls.rt = AgentRuntime(PROG / "puppy_agent" / "plugins", AGENT_DATA,
                              gate, config={"memory_db": str(main.MEMORY_DB)})
        cls.rt.load_all()
        names = [s.name for s in cls.rt.registry.list()]
        for t in ("time_now", "calc", "note_add", "note_list",
                  "remind_set", "remind_list", "remind_cancel", "app_open"):
            # setUpClass 是类方法，cls.assertIn 未绑定调用会错位，用 if 抛
            if t not in names:
                raise AssertionError("助手工具没注册：%s" % t)

    def setUp(self):
        # 测试间隔离：每个用例从空提醒表开始（上限测试会故意写满 20 条）
        from puppy_agent.plugins.assistant import _save_json
        _save_json(AGENT_DATA / "reminders.json", [])

    def _call_confirmed(self, tool, args):
        """MEDIUM/HIGH 工具：先拿确认令牌，再带令牌执行。

        方案§7：note_add / remind_set / remind_cancel 是 MEDIUM，
        安全层会先要一次确认——这是设计，不是失败。
        """
        first = self.rt.call(tool, args)
        if first.get("need_confirm"):
            token = str(first.get("confirm_token") or "")
            self.assertTrue(token, "MEDIUM 工具应给确认令牌")
            return self.rt.call(tool, args, token)
        return first

    def test_risk_levels_match_plan(self):
        """方案§7 表格：风险等级逐项对齐。"""
        want = {"time_now": "low", "calc": "low", "note_list": "low",
                "remind_list": "low", "note_add": "medium",
                "remind_set": "medium", "remind_cancel": "medium",
                "app_open": "high"}
        for name, level in want.items():
            spec = self.rt.registry.get(name)
            self.assertIsNotNone(spec, name)
            self.assertEqual(spec.risk.value, level, name)

    def test_medium_tool_needs_confirm(self):
        """MEDIUM 无令牌 → need_confirm（安全层语义不变）。"""
        r = self.rt.call("note_add", {"text": "要确认的便签"})
        self.assertTrue(r.get("need_confirm"), r)
        self.assertTrue(r.get("confirm_token"))

    # ---- time_now ----
    def test_time_now(self):
        r = self.rt.call("time_now", {})
        self.assertTrue(r.get("ok"))
        self.assertIn("iso", r)
        self.assertIn("week", r)

    # ---- calc：白名单 + 禁 eval ----
    def test_calc_basic(self):
        for expr, want in (("1+2*3", 7), ("(1+2)*3", 9),
                           ("2**10", 1024), ("10/4", 2.5),
                           ("-5+3", -2), ("7%3", 1), ("10//3", 3)):
            r = self.rt.call("calc", {"expr": expr})
            self.assertTrue(r.get("ok"), (expr, r))
            self.assertEqual(r.get("value"), want, expr)

    def test_calc_rejects_eval_injection(self):
        """经典注入面：__import__/open/属性访问/名字全部拒绝。"""
        bad = [
            "__import__('os').system('calc')",
            "open('a.txt').read()",
            "__class__",
            "1 .__class__.__mro__",
            "().__class__.__bases__[0].__subclasses__()",
            "eval('1+1')",
            "x", "1;2", "lambda: 1",
        ]
        for expr in bad:
            r = self.rt.call("calc", {"expr": expr})
            self.assertFalse(r.get("ok"), "注入面没拦住：%s" % expr)

    def test_calc_division_by_zero(self):
        r = self.rt.call("calc", {"expr": "1/0"})
        self.assertFalse(r.get("ok"))
        self.assertIn("零", r.get("message") or "")

    # ---- note ----
    def test_note_add_and_list(self):
        r = self._call_confirmed("note_add", {"text": "AG7 合成便签"})
        self.assertTrue(r.get("ok"), r)
        lst = self.rt.call("note_list", {"limit": 5})
        self.assertTrue(lst.get("ok"))
        texts = [it.get("text") for it in lst.get("items") or []]
        self.assertIn("AG7 合成便签", texts)

    def test_note_add_empty(self):
        r = self._call_confirmed("note_add", {"text": "  "})
        self.assertFalse(r.get("ok"))

    # ---- remind：上限 + 静默顺延 ----
    def test_remind_set_and_list(self):
        r = self._call_confirmed("remind_set",
                                 {"text": "AG7 合成提醒", "when": "23:59"})
        self.assertTrue(r.get("ok"), r)
        lst = self.rt.call("remind_list", {})
        self.assertTrue(lst.get("ok"))
        texts = [it.get("text") for it in lst.get("items") or []]
        self.assertIn("AG7 合成提醒", texts)

    def test_remind_set_needs_valid_when(self):
        r = self._call_confirmed("remind_set", {"text": "x", "when": "马上"})
        self.assertFalse(r.get("ok"))
        r2 = self._call_confirmed("remind_set", {"text": "x"})
        self.assertFalse(r2.get("ok"))

    def test_remind_limit_20(self):
        # 直接写满 20 条（绕过 HH:MM 的去重耦合），再设 → 拒绝
        from puppy_agent.plugins.assistant import _save_json
        rem_file = AGENT_DATA / "reminders.json"
        items = [{"id": "r%d" % i, "text": "占位%d" % i,
                  "ts": int(time.time() * 1000) + i * 60000,
                  "original": "10:00", "done": False}
                 for i in range(20)]
        _save_json(rem_file, items)
        r = self._call_confirmed("remind_set", {"text": "第21条", "when": "23:58"})
        self.assertFalse(r.get("ok"), "上限 20 条必须拦住第 21 条")
        self.assertIn("上限", r.get("message") or "")

    def test_remind_cancel(self):
        r = self._call_confirmed("remind_set",
                                 {"text": "要取消的", "when": "23:57"})
        self.assertTrue(r.get("ok"), r)
        rid = r.get("id")
        c = self._call_confirmed("remind_cancel", {"id": rid})
        self.assertTrue(c.get("ok"), c)
        lst = self.rt.call("remind_list", {})
        texts = [it.get("text") for it in lst.get("items") or []]
        self.assertNotIn("要取消的", texts)

    # ---- app_open：白名单空 = 全拒 ----
    def test_app_open_empty_whitelist_denies_all(self):
        from puppy_agent.plugins.assistant import _save_json
        wl = AGENT_DATA / "app_whitelist.json"
        _save_json(wl, [])                       # 空白名单
        r = self.rt.call("app_open", {"app": "notepad"})
        self.assertFalse(r.get("ok"))
        self.assertTrue(r.get("denied"), "空白名单必须是硬拒（denied）")

    def test_app_open_not_in_whitelist(self):
        from puppy_agent.plugins.assistant import _save_json
        wl = AGENT_DATA / "app_whitelist.json"
        _save_json(wl, ["C:\\Windows\\System32\\notepad.exe"])
        r = self.rt.call("app_open", {"app": "cmd"})
        self.assertFalse(r.get("ok"))
        self.assertTrue(r.get("denied"))

    def test_app_open_high_risk_needs_confirm(self):
        from puppy_agent.plugins.assistant import _save_json
        wl = AGENT_DATA / "app_whitelist.json"
        _save_json(wl, ["C:\\Windows\\System32\\notepad.exe"])
        # HIGH 风险 → 无令牌先要确认（安全层语义不变）
        r = self.rt.call("app_open", {"app": "notepad"})
        self.assertTrue(r.get("need_confirm"), r)

    # ---- plugin_lint 兼容 ----
    def test_assistant_plugin_has_manifest_and_permissions(self):
        """AG-1 的 lint 要求：manifest + permissions 字段可读。"""
        import importlib
        mod = importlib.import_module("puppy_agent.plugins.assistant")
        inst = mod.AssistantPlugin()
        self.assertEqual(inst.manifest.id, "assistant")
        self.assertIsInstance(inst.manifest.permissions, list)
        for spec in inst.tools():
            self.assertIn(spec.risk.value, ("low", "medium", "high"))


if __name__ == "__main__":
    unittest.main()
