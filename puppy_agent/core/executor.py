# -*- coding: utf-8 -*-
"""礼帽小狗 · 多步执行器（AG-4 / Agent 2.0）
=============================================

把「规划 → 执行 → 观察 → 再规划」收进一个有预算、有防循环、有污点追踪
的循环里。**安全语义不变**：每一步仍走 runtime.call（风险分级 / 确认令牌 /
沙盒 / 审计一个不少），执行器只负责调度。

防线（对应审阅「自主循环的风险」）：
  · 预算：max_steps（默认 5 步）+ 时间预算（默认 30s），超了即停；
  · 防循环：同一步骤指纹（tool + 排序后的 args）第二次出现 → 取消剩余；
  · 拒绝即停：任何一步被安全层硬拒（denied）→ 取消剩余步骤；
  · 确认挂起：某步 need_confirm 且没有令牌 → 计划挂起（可 resume 续跑），
    不丢上下文；
  · 污点追踪：只要读过外部数据（untrusted），后续所有步骤与最终输出都标
    tainted=True——提示词层据此降低对结果的信任；
  · 观察压缩：工具结果只留一句人话进下一步规划，不把原文喂回去
    （文件内容不进提示词——与 AG-3 的请求体最小化同一条线）。

接口：
    Executor(runtime, planner).run(text) -> dict
    Executor.resume(plan_id, confirm_token) -> dict   # 挂起计划续跑
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

DEFAULT_MAX_STEPS = 3      # 方案§6：默认最多 3 步（配置可调到 5）
DEFAULT_BUDGET_MS = 20_000  # 方案§6：总时长 20 秒


def _fingerprint(tool: str, args: dict) -> str:
    try:
        canon = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        canon = str(args)
    return "%s|%s" % (tool, canon)


class Executor:
    """多步执行器。planner 是任何带 plan(text)->dict 的对象。"""

    name = "executor"

    def __init__(self, runtime, planner,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 budget_ms: int = DEFAULT_BUDGET_MS):
        self.rt = runtime
        self.planner = planner
        self.max_steps = max(1, int(max_steps))
        self.budget_ms = int(budget_ms)
        # 挂起中的计划（等确认令牌）：plan_id → 状态
        self.pending: dict[str, dict] = {}

    # ---- 观察压缩 ----------------------------------------------------------
    @staticmethod
    def compress(tool: str, res: dict) -> str:
        """把工具结果压成一句人话。原文不进下一步（防注入扩散）。"""
        if not isinstance(res, dict):
            return "工具返回格式不对"
        if not res.get("ok"):
            return "失败了：%s" % (res.get("message") or "原因不明")
        # 只留事实：ok / 计数 / 名称。content 一律不回带。
        bits = []
        if res.get("count") is not None:
            bits.append("%s 项" % res.get("count"))
        if res.get("path"):
            p = str(res["path"])
            bits.append(p.split("\\")[-1].split("/")[-1])
        if res.get("stats") and isinstance(res["stats"], dict):
            bits.append("统计完成")
        return ("成功：" + "，".join(bits)) if bits else "成功"

    # ---- 单步 ---------------------------------------------------------------
    def _exec_step(self, plan: dict, state: dict, confirm_token: str = "") -> dict:
        """执行一步。返回步骤记录；并更新 state（tainted / seen 指纹）。"""
        tool = str(plan.get("tool") or "")
        args = plan.get("args") or {}
        fp = _fingerprint(tool, args)

        step = {"tool": tool, "args": args, "ok": False, "summary": "",
                "tainted": bool(state.get("tainted")), "ms": 0,
                "need_confirm": False, "confirm_token": "", "denied": False}

        if not tool:
            step["summary"] = "规划为空（闲聊）"
            step["done"] = True
            return step

        if fp in state["seen"]:
            step["summary"] = "重复步骤（防循环），取消剩余"
            step["loop"] = True
            step["cancel_rest"] = True
            return step

        t0 = time.monotonic()
        res = self.rt.call(tool, args, confirm_token)
        step["ms"] = int((time.monotonic() - t0) * 1000)
        if not isinstance(res, dict):
            res = {"ok": False, "message": "工具返回格式不对"}

        step["ok"] = bool(res.get("ok"))
        step["summary"] = self.compress(tool, res)
        if res.get("need_confirm"):
            step["need_confirm"] = True
            step["confirm_token"] = str(res.get("confirm_token") or "")
        if res.get("denied"):
            step["denied"] = True
        if res.get("snapshot_id"):
            step["snapshot_id"] = res.get("snapshot_id")
        if res.get("untrusted"):
            state["tainted"] = True   # 读了外部数据 → 后续全带污点
        step["tainted"] = bool(state.get("tainted"))

        state["seen"].add(fp)
        return step

    # ---- 多步 ---------------------------------------------------------------
    def run(self, text: str, confirm_token: str = "") -> dict:
        text = str(text or "")
        # 方案§5：通知规划器新回合开始（ModelPlanner 据此重置云端调用计数）
        try:
            begin = getattr(self.planner, "begin_turn", None)
            if callable(begin):
                begin()
        except Exception:
            pass
        plan_id = uuid.uuid4().hex[:12]
        state = {"seen": set(), "tainted": False}
        steps: list = []
        t0 = time.monotonic()
        cancelled = False
        pending_step: dict | None = None
        resume_text = text

        for i in range(self.max_steps):
            if (time.monotonic() - t0) * 1000 > self.budget_ms:
                cancelled = True
                break
            plan = self.planner.plan(resume_text)
            tool = str(plan.get("tool") or "")
            if not tool and steps:
                # 链中间规划为空（没下一步了）→ 直接结束，不记"闲聊"步骤
                #（只有第一步的空规划才算步骤，那是"这句话不需要动手"）
                break
            step = self._exec_step(plan, state, confirm_token if i == 0 else "")
            step["step"] = i + 1
            steps.append(step)

            if step.get("cancel_rest"):
                cancelled = True
                break
            if step.get("denied"):
                cancelled = True   # 硬拒 → 取消剩余
                break
            if step.get("need_confirm") and not confirm_token:
                pending_step = step     # 挂起等令牌
                break
            if not step["ok"] or not tool:
                break              # 失败/闲聊 → 停
            # 成功一步后：把观察喂给规划器决定要不要继续
            resume_text = "（上一步：%s %s → %s）%s" % (
                tool, "、".join(str(k) for k in (step.get("args") or {})),
                step["summary"], text)

        out = {"ok": all(s.get("ok") for s in steps) if steps else True,
               "plan_id": plan_id, "steps": steps,
               "steps_total": len(steps),
               "cancelled": cancelled,
               "tainted": bool(state.get("tainted")),
               "elapsed_ms": int((time.monotonic() - t0) * 1000),
               "planner": getattr(self.planner, "name", "?")}

        if pending_step is not None:
            out["pending"] = True
            out["need_confirm"] = True
            out["confirm_token"] = pending_step.get("confirm_token") or ""
            out["pending_tool"] = pending_step.get("tool") or ""
            out["pending_args"] = pending_step.get("args") or {}
            out["resume_text"] = resume_text
            # 存挂起状态供 /api/agent/resume。挂起步的指纹要从 seen 里摘掉——
            # 那一步还没真正执行（在等令牌），不摘的话 resume 会被防循环误杀
            fp = _fingerprint(pending_step.get("tool") or "",
                              pending_step.get("args") or {})
            state["seen"].discard(fp)
            self.pending[plan_id] = {"state": state, "steps": steps,
                                     "resume_text": resume_text,
                                     "ts": time.time(),
                                     "tool": pending_step.get("tool") or "",
                                     "args": pending_step.get("args") or {}}
        return out

    # ---- 续跑 ---------------------------------------------------------------
    def resume(self, plan_id: str, confirm_token: str) -> dict:
        pend = self.pending.pop(str(plan_id or ""), None)
        if pend is None:
            return {"ok": False, "message": "没有挂起的计划：%s" % plan_id}
        if time.time() - float(pend.get("ts") or 0) > 600:
            return {"ok": False, "message": "挂起计划已过期（10 分钟）"}

        tool = pend["tool"]
        args = pend["args"]
        state = pend["state"]
        steps = list(pend["steps"])
        resume_text = pend["resume_text"]
        t0 = time.monotonic()
        cancelled = False

        # 先补执行挂起的那步
        step = self._exec_step({"tool": tool, "args": args}, state,
                               confirm_token)
        step["step"] = len(steps) + 1
        steps.append(step)
        # 补执行成功 → 历史上那条"等待确认"的记录（ok=False）翻成成功，
        # 否则整链 ok 会被它拉低
        if step.get("ok"):
            for prev in steps:
                if prev is not step and prev.get("need_confirm") and \
                        prev.get("tool") == tool:
                    prev["ok"] = True
                    prev["resumed_ok"] = True
        if step.get("cancel_rest") or step.get("denied"):
            cancelled = True
        if step["ok"] and not step.get("need_confirm"):
            resume_text = "（上一步：%s → %s）%s" % (
                tool, step["summary"], resume_text)
            # 继续剩余预算
            used = len(steps)
            for i in range(used, self.max_steps):
                if (time.monotonic() - t0) * 1000 > self.budget_ms:
                    cancelled = True
                    break
                plan = self.planner.plan(resume_text)
                if not str(plan.get("tool") or ""):
                    break            # 没下一步了，不记闲聊步骤
                st = self._exec_step(plan, state)
                st["step"] = i + 1
                steps.append(st)
                if st.get("cancel_rest") or st.get("denied"):
                    cancelled = True
                    break
                if not st["ok"] or not st.get("tool"):
                    break
                resume_text = "（上一步：%s → %s）%s" % (
                    st.get("tool"), st["summary"], resume_text)

        return {"ok": all(s.get("ok") for s in steps) if steps else True,
                "plan_id": plan_id, "resumed": True, "steps": steps,
                "steps_total": len(steps), "cancelled": cancelled,
                "tainted": bool(state.get("tainted")),
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "planner": getattr(self.planner, "name", "?")}
