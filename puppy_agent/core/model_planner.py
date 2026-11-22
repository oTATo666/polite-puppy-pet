# -*- coding: utf-8 -*-
"""礼帽小狗 · 模型规划器（AG-3 / Agent 2.0）
=============================================

与 RulePlanner 同一个接口（plan(text) -> {"tool","args","reason"}），
但把「这句话要不要动手」交给模型判断。**模型规划是可选增强，规则是底线**：
任何一步不干净（坏 JSON / 未知工具 / schema 不符 / 注入 / 超时 / 超限 /
无云端知情同意）都回落 RulePlanner，绝不硬放行。

防线清单（对应 R1 审阅「模型规划的风险」）：
  1. 闲聊过滤：空文本 / 超长直接走规则，不浪费一次模型调用；
  2. 严格 JSON：提示词要求只输出一个 JSON 对象，解析失败即回落；
  3. schema 校验：tool 必须是注册表里的已知工具、args 必须是 dict；
  4. 风险动词闸：MEDIUM / HIGH 工具要求原文带明确动词，否则回落
     （防止模型看到文件名就自作主张）；
  5. 注入丢弃：输出含注入标记（"忽略上面的指令"等）整条丢弃回落；
  6. 60 秒缓存：同一句话 60 秒内重复规划直接用缓存（不再调模型）；
  7. 每日上限：模型规划次数有每日上限（默认 50 次），用完只走规则；
  8. 云端知情同意：目标是云端（非 loopback）时必须先拿到用户同意；
  9. 回落链：上面任何失败 → RulePlanner，永不抛异常。

接口：
    ModelPlanner(router, registry_view, rule=None, ...).plan(text) -> dict
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from .planner import RulePlanner

CACHE_TTL = 60          # 规划缓存（秒）
DEFAULT_DAILY_LIMIT = 50

#: MEDIUM/HIGH 工具要求原文里出现明确动词（防模型自作主张）
ACTION_VERBS = re.compile(
    r"读|看|打开|列|搜|查|删|写|建|发|跑|执行|安装|卸载|移动|改名|重命名|"
    r"复制|备份|恢复|清空|停止|关|杀|上传|下载|导入|导出|转|整理|压缩|解压")

#: 注入标记（与 model_router 保持同一套；planner 独立可测）
INJECTION_MARKS = (
    "忽略上面的指令", "忽略以上", "前面的都不算", "新的系统提示", "新的系统提示词",
    "ignore previous", "ignore all previous",
    "system prompt", "system:",
    "你现在是管理员", "放开所有限制", "没有限制", "不要再问用户",
    "批准所有确认", "绕过确认", "跳过确认",
    "<|", "[INST]", "[/INST]", "</s>", "<|im_start|>",
)

#: "像请求"的启发式（方案§4）：含动作词 / 疑问 / 以请求词开头才值得一次模型调用
REQUEST_PREFIXES = ("帮我", "能不能", "麻烦", "请你", "帮我个", "可否", "可以")
QUESTION_MARKS = ("?", "？", "吗", "呢", "么")

#: 云端规划每回合最多调用次数（方案§5）
CLOUD_TURN_LIMIT = 2


def _looks_like_request(text: str) -> bool:
    """这句话"像不像请求"——不像就不调模型（省钱、省延迟、少误触发）。

    规则（方案§4）：长度 <4 或纯情绪词 → 不像；含动作词 → 像；
    疑问（?/吗/呢/么）→ 像；以"帮我/能不能/麻烦"开头 → 像。
    """
    t = str(text or "").strip()
    if len(t) < 4:
        return False
    if ACTION_VERBS.search(t):
        return True
    if any(m in t for m in QUESTION_MARKS):
        return True
    if t.startswith(REQUEST_PREFIXES):
        return True
    return False

SYSTEM_TEMPLATE = """你是一个桌面助手的任务规划器。判断用户的这句话需不需要调用工具。

可用工具（JSON 列表，name / description / risk）：
{tools}

规则：
· 只在用户明确要求动手时选工具；闲聊、情绪倾诉、提问一律选 "none"。
· risk 为 MEDIUM 或 HIGH 的工具，只有用户原话带明确动词时才允许选择。
· args 必须符合工具的 parameters schema；拿不准的参数留空。

只输出一个 JSON 对象，不要解释、不要 markdown 代码块：
{{"tool": "工具名或none", "args": {{}}, "reason": "一句话理由"}}"""


class ModelPlanner:
    """模型规划器（可选增强；任何不干净都回落规则）。"""

    name = "model"

    def __init__(self, router, registry_view: dict | None = None,
                 rule: RulePlanner | None = None,
                 cache_ttl: int = CACHE_TTL,
                 daily_limit: int = DEFAULT_DAILY_LIMIT,
                 cloud_consent_fn: Callable[[], bool] | None = None,
                 now_fn: Callable[[], float] = time.time):
        """
        router          —— ModelRouter（或带同签名 chat 的对象）
        registry_view   —— {"tools": [{name,description,risk},...],
                            "known": fn(tool)->bool}
        cloud_consent_fn—— () -> bool：云端知情同意（默认 False=拒绝云端）
        """
        self.router = router
        self.registry_view = registry_view or {"tools": [], "known": lambda t: False}
        self.rule = rule or RulePlanner()
        self.cache_ttl = int(cache_ttl)
        self.daily_limit = int(daily_limit)
        self.cloud_consent_fn = cloud_consent_fn or (lambda: False)
        self._now = now_fn
        self._cache: dict[str, tuple[float, dict]] = {}
        self._daily: dict[str, int] = {}      # 日期 → 调用次数
        self._turn_calls: int = 0             # 本回合已调模型次数（云端限 2）
        self.last_fallback: str = ""          # 最近一次回落原因（审计/测试）

    # ---- 内部：回合（执行链）计数 -----------------------------------------
    def begin_turn(self) -> None:
        """执行器每回合开始时调一次（方案§5：云端每回合 ≤2 次）。"""
        self._turn_calls = 0

    def turn_calls(self) -> int:
        return int(self._turn_calls)

    # ---- 内部：每日计数 ---------------------------------------------------
    def _daily_count(self) -> int:
        today = time.strftime("%Y-%m-%d")
        return int(self._daily.get(today, 0))

    def _bump_daily(self) -> None:
        today = time.strftime("%Y-%m-%d")
        self._daily[today] = self._daily.get(today, 0) + 1

    def _cache_get(self, text: str) -> dict | None:
        hit = self._cache.get(text)
        if hit is None:
            return None
        ts, plan = hit
        if self._now() - ts > self.cache_ttl:
            self._cache.pop(text, None)
            return None
        return plan

    def _cache_put(self, text: str, plan: dict) -> None:
        self._cache[text] = (self._now(), plan)
        # 缓存条数上限（顺带防内存涨）
        if len(self._cache) > 64:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:16]:
                self._cache.pop(k, None)

    # ---- 内部：模型输出解析 ------------------------------------------------
    def _parse(self, text: str) -> dict | None:
        """严格解析模型输出。任何不干净返回 None。"""
        raw = str(text or "").strip()
        if not raw:
            return None
        # 注入标记 → 整条丢弃
        low = raw.lower()
        for mark in INJECTION_MARKS:
            if mark in raw or mark.lower() in low:
                return None
        # 剥掉可能包裹的 markdown 代码块（只剥一层；剥不出来就按原文解析）
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z0-9]*\s*", "", raw)
            raw = re.sub(r"\s*```\s*$", "", raw).strip()
        # 只取第一个 {...} JSON 对象（模型偶尔加一句话前缀）
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        tool = str(data.get("tool") or "").strip()
        args = data.get("args")
        reason = str(data.get("reason") or "")
        if tool != "none" and not self.registry_view.get("known", lambda t: False)(tool):
            return None                      # 未知工具
        # 方案§4：参数不过 schema 就整个计划丢弃（不做"宽容置空"，
        # 否则必填参数缺失会到执行层才炸，还可能带着空参数过风险闸）
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return None
        return {"tool": "" if tool == "none" else tool,
                "args": args, "reason": reason or "模型规划"}

    def _risk_of(self, tool: str) -> str:
        for t in (self.registry_view.get("tools") or []):
            if t.get("name") == tool:
                return str(t.get("risk") or "LOW")
        return "LOW"

    # ---- 对外接口 ----------------------------------------------------------
    def plan(self, text: str) -> dict:
        t = str(text or "")
        stripped = t.strip()

        # 0) 闲聊过滤：空/超长直接规则（与 RulePlanner 同口径）
        if not stripped or len(stripped) > 200:
            fb = self.rule.plan(t)
            fb["planner"] = "rule"
            self.last_fallback = "闲聊或超长"
            return fb

        # 0b) 混合策略（方案§4）：规则命中（明确文件名/列目录/记忆）直接用
        #     规则——快、省钱、可预期；模型只在规则没命中且"像请求"时才调。
        rule_plan = self.rule.plan(t)
        if rule_plan.get("tool"):
            out = dict(rule_plan)
            out["planner"] = "rule"
            out["hybrid"] = True
            return out

        # 0c) "像请求"启发式（方案§4）：不像请求就不调模型（规则已判闲聊）
        if not _looks_like_request(stripped):
            out = dict(rule_plan)
            out["planner"] = "rule"
            self.last_fallback = "不像请求"
            return out

        # 1) 缓存命中
        cached = self._cache_get(stripped)
        if cached is not None:
            out = dict(cached)
            out["cached"] = True
            out["planner"] = "model"
            return out

        # 2) 云端知情同意
        if not getattr(self.router, "is_local", True) and not self.cloud_consent_fn():
            fb = self.rule.plan(t)
            fb["planner"] = "rule"
            self.last_fallback = "云端无知情同意"
            return fb

        # 3) 每日上限
        if self._daily_count() >= self.daily_limit:
            fb = self.rule.plan(t)
            fb["planner"] = "rule"
            self.last_fallback = "每日上限"
            return fb

        # 3b) 云端每回合限额（方案§5）：本地不限，云端一回合最多 2 次
        if not getattr(self.router, "is_local", True) and \
                self._turn_calls >= CLOUD_TURN_LIMIT:
            fb = self.rule.plan(t)
            fb["planner"] = "rule"
            self.last_fallback = "云端每回合上限"
            return fb

        # 4) 调模型
        tools = self.registry_view.get("tools") or []
        system = SYSTEM_TEMPLATE.format(
            tools=json.dumps(tools, ensure_ascii=False))
        res = self.router.chat(system, stripped)
        self._bump_daily()
        self._turn_calls += 1

        if not res.get("ok"):
            fb = self.rule.plan(t)
            fb["planner"] = "rule"
            self.last_fallback = "模型调用失败：" + str(res.get("error") or "")[:80]
            return fb

        plan = self._parse(res.get("text"))
        if plan is None:
            fb = self.rule.plan(t)
            fb["planner"] = "rule"
            self.last_fallback = "输出不干净（坏 JSON/未知工具/注入）"
            return fb

        # 5) 风险动词闸：MEDIUM/HIGH 必须原文带动词
        if plan["tool"]:
            risk = self._risk_of(plan["tool"]).upper()
            if risk in ("MEDIUM", "HIGH") and not ACTION_VERBS.search(stripped):
                fb = self.rule.plan(t)
                fb["planner"] = "rule"
                self.last_fallback = "%s 风险工具但原话无动词" % risk
                return fb

        self._cache_put(stripped, plan)
        out = dict(plan)
        out["planner"] = "model"
        return out
