# -*- coding: utf-8 -*-
"""AG-6：状态与审计字段测试。

三条线：
  1. /api/agent/state——AG-6 新字段齐全（phase/plan_id/step/steps_total/
     tool/mood_hint/planner），**旧字段一个不少且类型不变**；
  2. /api/health——layers.agent 出现，旧四层（brain/ollama/cloud/tts）不动；
  3. 审计——runtime.call(meta=…) 把 plan_id/step/planner/model/tainted/
     latency 并入审计行（旧字段不变）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from _ctx import AGENT_DATA, PROG, main

if str(PROG / "puppy_agent") not in sys.path:
    sys.path.insert(0, str(PROG / "puppy_agent"))

from puppy_agent.core.plugin import RiskLevel, ToolSpec  # noqa: E402
from puppy_agent.core.runtime import AgentRuntime        # noqa: E402
from puppy_agent.core.safety import SafetyGate           # noqa: E402


class AgentStateFieldsTest(unittest.TestCase):
    """旧字段不变 + 新字段齐全。"""

    OLD_FIELDS = {"ok", "ready", "booted", "error", "version",
                  "tools", "plugins", "pending_confirm"}
    NEW_FIELDS = {"phase", "plan_id", "step", "steps_total", "tool",
                  "mood_hint", "planner"}

    def test_state_has_all_fields(self):
        st = main._agent_state()
        self.assertEqual(set(st.keys()), self.OLD_FIELDS | self.NEW_FIELDS |
                         {"audit_lines"} if st.get("ready")
                         else self.OLD_FIELDS | self.NEW_FIELDS,
                         "字段集合变了：%s" % sorted(st.keys()))

    def test_state_old_field_types(self):
        st = main._agent_state()
        self.assertIsInstance(st["ok"], bool)
        self.assertIsInstance(st["version"], int)
        self.assertIsInstance(st["tools"], int)
        self.assertIsInstance(st["plugins"], int)
        self.assertIsInstance(st["pending_confirm"], int)
        self.assertIsInstance(st["error"], str)

    def test_state_new_field_defaults(self):
        st = main._agent_state()
        self.assertEqual(st["phase"], "idle")
        self.assertEqual(st["plan_id"], "")
        self.assertEqual(st["step"], 0)
        self.assertEqual(st["steps_total"], 0)
        self.assertEqual(st["tool"], "")
        self.assertIsInstance(st["mood_hint"], str)
        self.assertIn(st["planner"], {"off", "rule", "local", "cloud"})

    def test_phase_update_reflects_in_state(self):
        # 方案§9 状态机：idle → thinking → acting → waiting_confirm → done/error
        main._agent_set_phase("acting", "pid1", 2, 5, "fs_read")
        try:
            st = main._agent_state()
            self.assertEqual(st["phase"], "acting")
            self.assertEqual(st["plan_id"], "pid1")
            self.assertEqual(st["step"], 2)
            self.assertEqual(st["steps_total"], 5)
            self.assertEqual(st["tool"], "fs_read")
            # mood_hint 是枚举（方案§9）：working | puzzled | proud | sorry
            self.assertEqual(st["mood_hint"], "working")
        finally:
            main._agent_set_phase("idle")   # 还原，防影响别的测试

    def test_resume_requires_executor(self):
        # executor 没装配时 → 明确报错而不是 500
        saved = main.AGENT_STATE.get("executor")
        main.AGENT_STATE["executor"] = None
        try:
            r = main._agent_resume({"plan_id": "x", "confirm_token": "y"})
            self.assertFalse(r.get("ok"))
            self.assertIn("执行器", r.get("message") or "")
        finally:
            main.AGENT_STATE["executor"] = saved


class HealthAgentLayerTest(unittest.TestCase):
    """/api/health：layers.agent 出现且旧四层不动。"""

    def test_health_layers(self):
        h = main._health()
        layers = h.get("layers") or {}
        self.assertIn("agent", layers, "AG-6：health 必须带 agent 层")
        for old in ("brain", "ollama", "cloud", "tts"):
            self.assertIn(old, layers, "旧层不能丢：%s" % old)
        agent = layers["agent"]
        for key in ("ok", "booted", "planner", "tools", "plugins",
                    "pending_confirm", "reason"):
            self.assertIn(key, agent)
        self.assertIn(agent.get("planner"),
                      {"off", "rule", "local", "cloud"})
        # 兼容旧顶层字段还在
        for key in ("provider", "baseUrl", "model", "models", "hasKey",
                    "version", "pid", "frozen"):
            self.assertIn(key, h)


class AuditMetaTest(unittest.TestCase):
    """runtime.call(meta=…) → 审计行带执行链上下文。"""

    @classmethod
    def setUpClass(cls):
        AGENT_DATA.mkdir(parents=True, exist_ok=True)
        gate = SafetyGate(AGENT_DATA)
        cls.rt = AgentRuntime(PROG / "puppy_agent" / "plugins", AGENT_DATA,
                              gate, config={"memory_db": str(main.MEMORY_DB)})
        cls.rt.load_all()
        cls.rt.registry.register(ToolSpec(
            name="ag6_probe", description="探针", plugin_id="t",
            risk=RiskLevel.LOW,
            parameters={"type": "object", "properties": {"v": {"type": "string"}}},
            invoke=lambda a: {"ok": True, "message": "done"}))

    def test_audit_meta_fields(self):
        meta = {"plan_id": "pl123", "step": 2, "steps_total": 4,
                "planner": "model", "model": "fake-7b", "tainted": True,
                "latency_ms": 123}
        self.rt.call("ag6_probe", {"v": "x"}, meta=meta)
        tail = self.rt.safety.audit_tail(1)
        self.assertTrue(tail)
        rec = tail[0]
        # 旧字段不变
        for old in ("ts", "time", "tool", "args", "risk", "ok", "detail", "ms"):
            self.assertIn(old, rec, "审计旧字段丢了：%s" % old)
        self.assertEqual(rec.get("tool"), "ag6_probe")
        self.assertTrue(rec.get("ok"))
        # 新字段并入
        for key in ("plan_id", "step", "steps_total", "planner", "model",
                    "tainted"):
            self.assertEqual(rec.get(key), meta[key], "审计缺字段 %s" % key)
        self.assertEqual(rec.get("latency_ms"), 123)

    def test_audit_without_meta_still_works(self):
        """不传 meta（旧调用方式）→ 审计照样写，无 meta 字段也无异常。"""
        self.rt.call("ag6_probe", {"v": "y"})
        tail = self.rt.safety.audit_tail(1)
        self.assertTrue(tail)
        rec = tail[0]
        self.assertEqual(rec.get("tool"), "ag6_probe")
        self.assertNotIn("plan_id", rec, "不带 meta 就不该出现链字段")


if __name__ == "__main__":
    unittest.main()
