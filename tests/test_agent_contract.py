# -*- coding: utf-8 -*-
"""Agent 安全层冻结契约（第四轮任务书 §4）。

固定 safety.py / plugin.py / runtime.py 的**现有语义**，第四轮所有任务
（AG-1 ~ AG-9、N-22 ~ N-26）结束时本文件必须原样通过。

允许的演进：ToolSpec 追加带默认值的新字段；审计记录追加新键。
禁止的演进：改判定逻辑（确认/拒绝/快照/风险分级的行为）、删旧字段、
改返回结构里的旧键。风险等级不许降——只许升。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from _ctx import PROG

import sys
if str(PROG) not in sys.path:
    sys.path.insert(0, str(PROG))

from puppy_agent.core.plugin import (CORE_VERSION, Plugin, PluginManifest,
                                     RiskLevel, ToolSpec, core_version_ok,
                                     max_risk, version_tuple)
from puppy_agent.core.runtime import AgentRuntime, Registry, RuntimeContext
from puppy_agent.core.safety import SNAPSHOT_MAX_FILES, SafetyGate, TOKEN_TTL


def _mk_spec(name="t_low", risk=RiskLevel.LOW, invoke=None, **kw):
    return ToolSpec(
        name=name, description="测试工具 %s" % name, risk=risk,
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]},
        invoke=invoke or (lambda args: {"ok": True, "echo": args.get("path")}),
        **kw)


class RiskLevelContract(unittest.TestCase):
    """风险分级语义：三级、顺序、取最大——不许改。"""

    def test_three_levels_and_values(self):
        self.assertEqual({lv.value for lv in RiskLevel},
                         {"low", "medium", "high"})

    def test_max_risk_takes_highest(self):
        self.assertIs(max_risk(RiskLevel.LOW, RiskLevel.HIGH), RiskLevel.HIGH)
        self.assertIs(max_risk(RiskLevel.MEDIUM, RiskLevel.LOW), RiskLevel.MEDIUM)
        self.assertIs(max_risk(RiskLevel.LOW), RiskLevel.LOW)

    def test_versions_compatible(self):
        self.assertEqual(version_tuple("1.2.3"), (1, 2, 3))
        self.assertTrue(core_version_ok("1.0.0", "1.1.0"))
        self.assertFalse(core_version_ok("1.2.0", "1.1.0"))
        # 当前核心至少满足 1.0.0（AG-1 会把核心升到 1.1.0，此处不断言未来值）
        self.assertTrue(core_version_ok("1.0.0"))


class ConfirmTokenContract(unittest.TestCase):
    """确认令牌：TTL、绑定工具、绑定参数、一次性。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="contract_"))
        self.gate = SafetyGate(self.tmp)

    def test_ttl_constant_unchanged(self):
        self.assertEqual(TOKEN_TTL, 300)

    def test_low_risk_executes_without_confirm(self):
        v = self.gate.check(_mk_spec(), {"path": "a.txt"})
        self.assertTrue(v["ok"])
        self.assertEqual(v["action"], "execute")

    def test_medium_requires_confirm_with_token(self):
        spec = _mk_spec("t_med", RiskLevel.MEDIUM)
        v = self.gate.check(spec, {"path": "a.txt"})
        self.assertFalse(v["ok"])
        self.assertEqual(v["action"], "confirm")
        tok = v["confirm_token"]
        # 同参数 + 同工具 → 放行
        v2 = self.gate.check(spec, {"path": "a.txt"}, confirm_token=tok)
        self.assertTrue(v2["ok"]) and self.assertEqual(v2["action"], "execute")

    def test_token_bound_to_args(self):
        spec = _mk_spec("t_med", RiskLevel.MEDIUM)
        tok = self.gate.check(spec, {"path": "a.txt"})["confirm_token"]
        v = self.gate.check(spec, {"path": "b.txt"}, confirm_token=tok)
        self.assertFalse(v["ok"])
        self.assertEqual(v["action"], "deny")

    def test_token_bound_to_tool(self):
        tok = self.gate.check(_mk_spec("t_med", RiskLevel.MEDIUM),
                              {"path": "a.txt"})["confirm_token"]
        v = self.gate.check(_mk_spec("t_other", RiskLevel.MEDIUM),
                            {"path": "a.txt"}, confirm_token=tok)
        self.assertFalse(v["ok"])
        self.assertEqual(v["action"], "deny")

    def test_token_single_use(self):
        spec = _mk_spec("t_med", RiskLevel.MEDIUM)
        tok = self.gate.check(spec, {"path": "a.txt"})["confirm_token"]
        self.assertTrue(self.gate.check(spec, {"path": "a.txt"},
                                        confirm_token=tok)["ok"])
        v = self.gate.check(spec, {"path": "a.txt"}, confirm_token=tok)
        self.assertFalse(v["ok"])    # 用过即焚

    def test_granted_tool_skips_confirm(self):
        spec = _mk_spec("t_med", RiskLevel.MEDIUM)
        self.gate.grant("t_med")
        v = self.gate.check(spec, {"path": "a.txt"})
        self.assertTrue(v["ok"]) and self.assertTrue(v.get("granted"))
        self.gate.revoke("t_med")
        v2 = self.gate.check(spec, {"path": "a.txt"})
        self.assertEqual(v2["action"], "confirm")

    def test_disabled_bypasses(self):
        self.gate.enabled = False
        v = self.gate.check(_mk_spec("t_med", RiskLevel.MEDIUM), {"path": "x"})
        self.assertTrue(v["ok"]) and self.assertTrue(v.get("bypass"))
        self.gate.enabled = True

    def test_guard_hard_deny_no_confirm(self):
        spec = _mk_spec("t_guard", RiskLevel.MEDIUM,
                        guard=lambda a: "硬拒绝原因")
        v = self.gate.check(spec, {"path": "a.txt"})
        self.assertFalse(v["ok"])
        self.assertEqual(v["action"], "deny")
        self.assertNotIn("confirm_token", v)

    def test_snapshot_field_makes_high_risk_snapshot(self):
        """runtime.call：HIGH + snapshot=True → 执行前先快照；快照可还原。"""
        rt = AgentRuntime(self.tmp / "plugins", self.tmp)
        src = self.tmp / "victim.txt"
        src.write_text("keep", encoding="utf-8")

        def _del(args):
            src.unlink()
            return {"ok": True}

        rt.registry.register(_mk_spec("t_del", RiskLevel.HIGH, invoke=_del,
                                      snapshot=True))
        # HIGH 风险：先走确认流程拿令牌，再带令牌执行
        v = rt.call("t_del", {"path": str(src)})
        self.assertEqual(v.get("need_confirm"), True, v)
        v = rt.call("t_del", {"path": str(src)},
                    confirm_token=v.get("confirm_token"))
        self.assertTrue(v.get("ok"), v)
        self.assertTrue(v.get("snapshot_id"))
        # 回收站里留了原文件（N-27 修复：单文件快照必须能还原）
        snaps = rt.safety.snapshot_list()
        self.assertEqual(len(snaps), 1)
        back = Path(snaps[0]["snapshot"]) / "victim.txt"
        self.assertEqual(back.read_text(encoding="utf-8"), "keep")
        # 还原后文件回来
        self.assertTrue(rt.safety.restore(snaps[0]["snapshot_id"]).get("ok"))
        self.assertEqual(src.read_text(encoding="utf-8"), "keep")

    def test_snapshot_too_big_refused(self):
        self.assertEqual(SNAPSHOT_MAX_FILES, 2000)
        big = self.tmp / "big"
        big.mkdir()
        for i in range(SNAPSHOT_MAX_FILES + 2):
            (big / ("f%04d" % i)).write_text("x", encoding="utf-8")
        snap = self.gate.snapshot(str(big))
        self.assertFalse(snap.get("ok"))
        self.assertIn("上限", snap.get("message", ""))


class AuditContract(unittest.TestCase):
    """审计：JSONL、固定字段集、extra 只追加不覆盖。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit_"))
        self.gate = SafetyGate(self.tmp)

    def test_audit_record_shape(self):
        self.gate.audit("t", {"path": "a" * 300}, "low", True, "d", 12,
                        extra={"plan_id": "p1"})
        rec = self.gate.audit_tail(1)[0]
        for key in ("ts", "time", "tool", "args", "risk", "ok", "detail", "ms"):
            self.assertIn(key, rec)
        self.assertEqual(rec["tool"], "t")
        self.assertEqual(rec["risk"], "low")
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["ms"], 12)
        self.assertEqual(rec["plan_id"], "p1")
        # 长参数截断到 200 字符 + 省略号
        self.assertTrue(rec["args"]["path"].endswith("…"))

    def test_runtime_writes_audit_on_validate_fail(self):
        rt = AgentRuntime(self.tmp / "plugins", self.tmp)
        rt.registry.register(_mk_spec("t_need_path", RiskLevel.LOW))
        rt.call("t_need_path", {})
        rec = rt.safety.audit_tail(1)[0]
        self.assertFalse(rec["ok"])
        self.assertIn("缺少必填参数", rec["detail"])

    def test_audit_tail_order_newest_last(self):
        self.gate.audit("a", {}, "low", True)
        self.gate.audit("b", {}, "low", True)
        tail = self.gate.audit_tail(10)
        self.assertEqual([r["tool"] for r in tail], ["b", "a"])


class ToolSpecContract(unittest.TestCase):
    """ToolSpec 契约：schema 字段、validate 规则、新字段必须带默认值。"""

    def test_schema_fields_fixed(self):
        s = _mk_spec("t", RiskLevel.MEDIUM).schema()
        for key in ("name", "description", "risk", "parameters", "plugin_id",
                    "destructive", "snapshot", "tags"):
            self.assertIn(key, s)
        self.assertEqual(s["risk"], "medium")

    def test_validate_required_and_types(self):
        spec = _mk_spec()
        self.assertTrue(spec.validate({}))
        errs = spec.validate({"path": 123})
        self.assertTrue(any("字符串" in e for e in errs))
        self.assertEqual(spec.validate({"path": "ok"}), [])

    def test_new_fields_have_defaults(self):
        """SDK v2 的新字段必须带默认值——旧插件零改动可加载。"""
        spec = ToolSpec(name="minimal", description="d")
        for attr in ("plugin_id", "destructive", "snapshot", "tags",
                     "guard", "risk_fn", "permissions"):
            self.assertEqual(getattr(spec, attr, None) in (None, "", [], False),
                             True, attr)

    def test_risk_fn_dynamic(self):
        spec = _mk_spec("t_dyn", RiskLevel.LOW,
                        risk_fn=lambda a: RiskLevel.MEDIUM)
        gate = SafetyGate(Path(tempfile.mkdtemp(prefix="rf_")))
        v = gate.check(spec, {"path": "x"})
        self.assertEqual(v["action"], "confirm")
        spec2 = _mk_spec("t_dyn2", RiskLevel.LOW,
                         risk_fn=lambda a: "bad-value")
        self.assertIs(gate.effective_risk(spec2, {}), RiskLevel.LOW)   # 异常回落静态值


class RuntimeContract(unittest.TestCase):
    """注册表与加载语义。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rt_"))
        self.rt = AgentRuntime(self.tmp / "plugins", self.tmp)

    def test_duplicate_name_raises(self):
        self.rt.registry.register(_mk_spec("dup"))
        with self.assertRaises(ValueError):
            self.rt.registry.register(_mk_spec("dup"))

    def test_unregister_plugin_removes_tools(self):
        spec = _mk_spec("mine")
        spec.plugin_id = "p1"
        self.rt.registry.register(spec)
        self.rt.registry._plugins["p1"] = Plugin()
        n = self.rt.registry.unregister_plugin("p1")
        self.assertEqual(n, 1)
        self.assertIsNone(self.rt.registry.get("mine"))

    def test_unknown_tool_message(self):
        res = self.rt.call("no_such_tool", {})
        self.assertFalse(res["ok"])
        self.assertIn("没有这个工具", res["message"])

    def test_invoke_exception_caught(self):
        def boom(args):
            raise RuntimeError("炸了")
        self.rt.registry.register(_mk_spec("boom", invoke=boom))
        res = self.rt.call("boom", {"path": "x"})
        self.assertFalse(res["ok"])
        self.assertIn("执行出错", res["message"])

    def test_context_shape(self):
        """RuntimeContext 保留 data_dir/safety/persona/config/shared 五件。"""
        ctx = self.rt.ctx
        for attr in ("data_dir", "safety", "persona", "config", "shared"):
            self.assertTrue(hasattr(ctx, attr))
        self.assertEqual(ctx.data_dir, self.tmp)


if __name__ == "__main__":
    unittest.main()
