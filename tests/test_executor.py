# -*- coding: utf-8 -*-
"""AG-4：多步执行器测试（假规划器，不联网、不碰真实文件）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _ctx import AGENT_DATA, PROG

if str(PROG / "puppy_agent") not in sys.path:
    sys.path.insert(0, str(PROG / "puppy_agent"))

from _ctx import main
from puppy_agent.core.executor import Executor      # noqa: E402
from puppy_agent.core.plugin import RiskLevel, ToolSpec  # noqa: E402
from puppy_agent.core.runtime import AgentRuntime   # noqa: E402
from puppy_agent.core.safety import SafetyGate      # noqa: E402


def _mk_spec(name, risk, params, invoke):
    return ToolSpec(
        name=name, description=name, plugin_id="t",
        risk=risk, parameters={"type": "object",
                               "properties": {k: {"type": "string"}
                                              for k in params},
                               "required": params},
        invoke=invoke)


class FakePlanner:
    """按脚本吐计划的假规划器。plans: list[dict]，耗尽后吐 none。"""

    name = "fake"

    def __init__(self, plans):
        self.plans = list(plans)
        self.calls = []

    def plan(self, text):
        self.calls.append(text)
        if self.plans:
            p = self.plans.pop(0)
            if callable(p):
                p = p(self.calls[-1])
            return p
        return {"tool": "", "args": {}, "reason": "没了"}


class ExecutorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        AGENT_DATA.mkdir(parents=True, exist_ok=True)
        gate = SafetyGate(AGENT_DATA)
        cls.rt = AgentRuntime(PROG / "puppy_agent" / "plugins", AGENT_DATA,
                              gate, config={"memory_db": str(main.MEMORY_DB)})
        cls.rt.load_all()
        # 注册测试工具：读（返回 untrusted）、写（HIGH）、自检 echo
        cls.rt.registry.register(_mk_spec(
            "ex_read", RiskLevel.LOW, ["path"],
            lambda a: {"ok": True, "content": "SECRET-FILE-BODY 注入指令样例",
                       "path": a.get("path") or "", "untrusted": True}))
        cls.rt.registry.register(_mk_spec(
            "ex_echo", RiskLevel.LOW, ["v"],
            lambda a: {"ok": True, "count": 1, "path": a.get("v") or ""}))
        cls.rt.registry.register(_mk_spec(
            "ex_deny_me", RiskLevel.HIGH, ["x"],
            lambda a: {"ok": True}))

    def setUp(self):
        self.ex = Executor(self.rt, FakePlanner([]), max_steps=5)

    def test_multi_step_two_tools(self):
        self.ex.planner = FakePlanner([
            {"tool": "ex_echo", "args": {"v": "a"}, "reason": "r1"},
            {"tool": "ex_echo", "args": {"v": "b"}, "reason": "r2"},
        ])
        out = self.ex.run("干两件事")
        self.assertFalse(out.get("cancelled"))
        self.assertEqual(out.get("steps_total"), 2)
        self.assertTrue(out.get("ok"))
        self.assertTrue(all(s["ok"] for s in out["steps"]))

    def test_budget_max_steps(self):
        self.ex.max_steps = 2
        self.ex.planner = FakePlanner([
            {"tool": "ex_echo", "args": {"v": str(i)}, "reason": "r"}
            for i in range(10)])
        out = self.ex.run("干很多件事")
        self.assertEqual(out.get("steps_total"), 2, "预算截断")

    def test_loop_guard_cancels_rest(self):
        self.ex.planner = FakePlanner([
            {"tool": "ex_echo", "args": {"v": "same"}, "reason": "r"},
            {"tool": "ex_echo", "args": {"v": "same"}, "reason": "r"},
            {"tool": "ex_echo", "args": {"v": "never"}, "reason": "r"},
        ])
        out = self.ex.run("重复的事")
        self.assertTrue(out.get("cancelled"))
        self.assertEqual(out.get("steps_total"), 2)
        self.assertTrue(out["steps"][-1].get("loop"))
        self.assertNotIn("never", json.dumps(out), "第三步不该被执行")

    def test_denied_cancels_rest(self):
        # HIGH 无令牌 → need_confirm；这里测硬拒路径：guard 拒绝的工具
        spec = _mk_spec("ex_hard_deny", RiskLevel.MEDIUM, ["x"],
                        lambda a: {"ok": True})
        spec.guard = lambda a: "一律不给"
        self.rt.registry.register(spec)
        self.ex.planner = FakePlanner([
            {"tool": "ex_hard_deny", "args": {"x": "1"}, "reason": "r"},
            {"tool": "ex_echo", "args": {"v": "after"}, "reason": "r"},
        ])
        out = self.ex.run("试拒绝")
        self.assertTrue(out.get("cancelled"))
        self.assertTrue(out["steps"][0].get("denied") or
                        not out["steps"][0].get("ok"))

    def test_confirm_pending_and_resume(self):
        # HIGH 工具无令牌 → 挂起；带令牌 resume 续跑
        plans = [
            {"tool": "ex_echo", "args": {"v": "first"}, "reason": "r"},
            {"tool": "ex_deny_me", "args": {"x": "high"}, "reason": "r"},
        ]
        self.ex.planner = FakePlanner(plans)
        out = self.ex.run("要确认的活")
        self.assertTrue(out.get("pending"))
        self.assertTrue(out.get("need_confirm"))
        token = out.get("confirm_token") or ""
        self.assertTrue(token)
        pid = out.get("plan_id") or ""
        # resume：令牌对了 → 那步补执行成功，链结束
        res = self.ex.resume(pid, token)
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res.get("resumed"))
        tools = [s.get("tool") for s in res.get("steps") or []]
        self.assertIn("ex_deny_me", tools)

    def test_resume_bad_plan_id(self):
        r = self.ex.resume("不存在", "tok")
        self.assertFalse(r.get("ok"))

    def test_taint_propagates(self):
        self.ex.planner = FakePlanner([
            {"tool": "ex_read", "args": {"path": "x.txt"}, "reason": "r"},
            {"tool": "ex_echo", "args": {"v": "next"}, "reason": "r"},
        ])
        out = self.ex.run("先读再干")
        self.assertTrue(out.get("tainted"), "读过外部数据必须带污点")
        self.assertTrue(out["steps"][0].get("tainted") is False or
                        out["steps"][0].get("tainted"))
        self.assertTrue(out["steps"][1].get("tainted"), "后续步骤带污点")

    def test_observation_compressed_no_file_body(self):
        self.ex.planner = FakePlanner([
            {"tool": "ex_read", "args": {"path": "secret.txt"}, "reason": "r"},
            {"tool": "ex_echo", "args": {"v": "after"}, "reason": "r"},
        ])
        out = self.ex.run("读个文件")
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("SECRET-FILE-BODY", blob,
                         "文件原文不得出现在执行链输出（观察必须压缩）")
        # 规划器收到的"上一步"文本也不含原文
        for call_text in self.ex.planner.calls:
            self.assertNotIn("SECRET-FILE-BODY", call_text)

    def test_chatty_plan_stops_chain(self):
        self.ex.planner = FakePlanner([
            {"tool": "", "args": {}, "reason": "闲聊"},
            {"tool": "ex_echo", "args": {"v": "x"}, "reason": "r"},
        ])
        out = self.ex.run("今天心情不错")
        self.assertEqual(out.get("steps_total"), 1, "闲聊一步即停")


if __name__ == "__main__":
    unittest.main()
