# -*- coding: utf-8 -*-
"""AG-5：Agent 规划评测（黄金集 runner）。

跑 tests/agent_eval/cases.json 的合成黄金用例（方案§8 六类）：
  · 规则类（read/keyfile/list/stats/search/chat）——RulePlanner 精确匹配；
  · model——ModelPlanner + 假模型脚本，断言最终 tool/args；
  · inject——假模型输出夹注入标记，必须整条丢弃（tool 必须为空）；
  · overreach——允许被提议，但执行必须被安全层拒绝或需要确认
    （真 runtime 实调，denied / need_confirm / ok=False 都算通过）；
  · multistep——executor + 假规划器脚本，验证步骤顺序。

指标门（gate）：全部类别 100% 通过（确定性行为，一票否决）；
有效性门：任何输出 tool ∈ 已知工具集或 ""。

tests/agent_eval/test_eval.py 与 scripts/agent_eval.py 共用本模块。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROG = HERE.parent.parent          # 01_程序
CASES = HERE / "cases.json"

if str(PROG / "puppy_agent") not in sys.path:
    sys.path.insert(0, str(PROG / "puppy_agent"))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(PROG) not in sys.path:
    sys.path.insert(0, str(PROG))

from puppy_agent.core.model_planner import ModelPlanner   # noqa: E402
from puppy_agent.core.planner import RulePlanner          # noqa: E402

KNOWN_TOOLS = {"fs_read", "fs_list", "mem_search", "mem_stats", "fs_write"}

#: 模型用例的注册表视图（假注册表，LOW/MEDIUM/HIGH 各有代表）
REGISTRY_VIEW = {
    "tools": [
        {"name": "fs_read", "description": "读文本文件", "risk": "MEDIUM"},
        {"name": "fs_list", "description": "列目录", "risk": "LOW"},
        {"name": "mem_search", "description": "搜记忆", "risk": "LOW"},
        {"name": "mem_stats", "description": "记忆统计", "risk": "LOW"},
        {"name": "fs_write", "description": "写文件", "risk": "HIGH"},
    ],
    "known": lambda t: t in KNOWN_TOOLS,
}


def load_cases() -> list:
    return json.loads(CASES.read_text(encoding="utf-8"))


def make_model_planner(script):
    from fake_model import make_router
    router, fm = make_router(script)
    mp = ModelPlanner(router, REGISTRY_VIEW, rule=RulePlanner(),
                      cloud_consent_fn=lambda: True)
    return mp, fm


def _runtime():
    """懒构建真 runtime（临时数据目录，由 tests/_ctx 设置）。"""
    try:
        import _ctx  # noqa: F401  （import 即把数据目录指到临时目录）
        from _ctx import AGENT_DATA
        from puppy_agent.core.runtime import AgentRuntime
        from puppy_agent.core.safety import SafetyGate
        from puppy_agent.core.personality import DEFAULT_PERSONA
    except Exception:
        return None
    AGENT_DATA.mkdir(parents=True, exist_ok=True)
    rt = AgentRuntime(PROG / "puppy_agent" / "plugins", AGENT_DATA,
                      SafetyGate(AGENT_DATA), persona=dict(DEFAULT_PERSONA))
    rt.load_all()
    return rt


_RT = [None, False]


def get_runtime():
    if not _RT[1]:
        _RT[0] = _runtime()
        _RT[1] = True
    return _RT[0]


def run_case(case, rule=None):
    """跑一条规划类用例（规则/model/inject）。返回 {"ok", "got", "why"}。"""
    text = case.get("text") or ""
    want_tool = case.get("expect_tool") or ""
    want_keys = case.get("expect_args_keys")
    want_args = case.get("expect_args")
    cat = case.get("cat")

    if cat in ("model", "inject"):
        mp, _ = make_model_planner(case.get("script") or [])
        got = mp.plan(text)
        why = ""
        if (got.get("tool") or "") != want_tool:
            why = "tool=%r 期望 %r（planner=%s, fallback=%s）" % (
                got.get("tool"), want_tool, got.get("planner"),
                mp.last_fallback)
        elif want_args is not None and (got.get("args") or {}) != want_args:
            why = "args=%r 期望 %r" % (got.get("args"), want_args)
        return {"ok": not why, "got": got, "why": why}

    rp = rule or RulePlanner()
    got = rp.plan(text)
    why = ""
    if (got.get("tool") or "") != want_tool:
        why = "tool=%r 期望 %r（reason=%s）" % (
            got.get("tool"), want_tool, got.get("reason"))
    elif want_keys is not None and set(got.get("args") or {}) != set(want_keys):
        why = "args 键 %r 期望 %r" % (sorted(got.get("args") or {}),
                                      sorted(want_keys))
    return {"ok": not why, "got": got, "why": why}


def run_overreach(case):
    """越权用例（方案§8）：允许被提议，但**执行必须被安全层拦下**。

    不等规则路由（有些敏感路径规则压根不认，比如 *.db / cookies /
    含空格的名字），直接按"模型提议了 (tool, args)"去调 rt.call——
    验的是安全层这道真正的门，而不是路由认不认路。
    """
    rt = get_runtime()
    want_tool = str(case.get("expect_tool") or "")
    want_args = case.get("expect_args") or {}
    if rt is None:
        return {"ok": False, "got": {}, "why": "runtime 不可用"}
    if not want_tool:
        return {"ok": False, "got": {}, "why": "用例缺 expect_tool"}
    routed = RulePlanner().plan(case.get("text") or "")
    res = rt.call(want_tool, dict(want_args))
    if not isinstance(res, dict):
        return {"ok": False, "got": {}, "why": "工具返回格式不对"}
    blocked = bool(res.get("denied")) or bool(res.get("need_confirm")) \
        or not res.get("ok")
    why = "" if blocked else "安全层没拦：危险路径直接执行成功了"
    got = {"tool": want_tool, "args": want_args,
           "routed_tool": str(routed.get("tool") or ""),
           "enforced": {"denied": bool(res.get("denied")),
                        "need_confirm": bool(res.get("need_confirm")),
                        "ok": bool(res.get("ok")),
                        "msg": str(res.get("message") or "")[:80]}}
    return {"ok": blocked, "got": got, "why": why}


def run_multistep(case):
    """多步用例：executor + 假规划器脚本，验证步骤顺序与终止。"""
    rt = get_runtime()
    if rt is None:
        return {"ok": False, "got": {}, "why": "runtime 不可用"}
    script = (case.get("script") or [{}])[0]
    pairs = script.get("steps") or []
    from puppy_agent.core.executor import Executor

    class _ScriptPlanner:
        name = "script"

        def __init__(self, steps):
            self.steps = [{"tool": t, "args": a, "reason": "脚本"}
                          for t, a in steps]

        def plan(self, text):
            if self.steps:
                return self.steps.pop(0)
            return {"tool": "", "args": {}, "reason": "结束"}

    ex = Executor(rt, _ScriptPlanner(pairs), max_steps=5)
    out = ex.run(case.get("text") or "")
    got_tools = [s.get("tool") for s in out.get("steps") or []
                 if s.get("tool")]
    want_tools = [t for t, _ in pairs]
    ok = got_tools[:len(want_tools)] == want_tools[:len(got_tools)] and \
        bool(got_tools)
    why = "" if ok else "步骤顺序 %r 期望 %r" % (got_tools, want_tools)
    return {"ok": ok,
            "got": {"tools": got_tools,
                    "steps_total": out.get("steps_total"),
                    "cancelled": out.get("cancelled")},
            "why": why}


def evaluate(rule_only: bool = False) -> dict:
    """跑全量。返回汇总 {"total", "passed", "failed", "by_cat", "failures"}。"""
    cases = load_cases()
    rule = RulePlanner()
    by_cat: dict = {}
    failures = []
    for case in cases:
        cat = case.get("cat")
        if cat == "overreach":
            res = run_overreach(case)
        elif cat == "multistep":
            res = run_multistep(case)
        else:
            res = run_case(case, rule=rule)
        tool = (res["got"].get("tool") or "") if isinstance(res["got"], dict) else ""
        if tool and tool not in KNOWN_TOOLS:
            res = {"ok": False, "got": res["got"],
                   "why": "无效工具 %r（有效性门）" % tool}
        st = by_cat.setdefault(cat, {"total": 0, "passed": 0})
        st["total"] += 1
        if res["ok"]:
            st["passed"] += 1
        else:
            failures.append({"id": case.get("id"), "cat": cat,
                             "text": (case.get("text") or "")[:40],
                             "why": res["why"]})
    total = sum(s["total"] for s in by_cat.values())
    passed = sum(s["passed"] for s in by_cat.values())
    return {"total": total, "passed": passed, "failed": total - passed,
            "by_cat": by_cat, "failures": failures}
