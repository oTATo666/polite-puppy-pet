# -*- coding: utf-8 -*-
"""
插件：日常助手工具（AG-7 / Agent 2.0）
=======================================

八个日常小工具，全部**只动 Agent 自己的数据区**，不碰用户文件系统：

  time_now      当前时间（LOW，纯只读）
  calc          四则运算（LOW，ast 白名单解析，**禁 eval**）
  note_add      记一条便签（LOW，写到 agent_data\\notes.json）
  note_list     看便签（LOW）
  remind_set    设提醒（MEDIUM：写 agent_data\\reminders.json）
  remind_list   看提醒（LOW）
  remind_cancel 取消提醒（MEDIUM）
  app_open      打开应用（HIGH：白名单制，**白名单为空 = 一律拒绝**）

提醒的防线（审阅要求）：
  · 静默时段（默认 23:00–07:00）不弹提醒，顺延到静默结束；
  · 提醒总数上限 20 条，满了拒设并提示先清理；
  · 触发由主程序轮询（plugins 不开线程——插件必须无副作用加载）。

calc 的防线：
  · ast.parse 后只允许四则/括号/数字（白名单节点类型），任何名字/属性/
    下标/lambda 全拒——从根上禁掉 eval 的注入面。

app_open 的防线：
  · 白名单存 agent_data\\app_whitelist.json；**空名单 = 全部拒绝**；
  · 白名单里存的是 exe 绝对路径，open 前确认文件存在；
  · HIGH 风险：执行层还有一道确认（安全层语义不变）。
"""

from __future__ import annotations

import ast
import json
import operator
import os
import subprocess
import time
from pathlib import Path

from ..core.plugin import Plugin, PluginManifest, RiskLevel, ToolSpec

# 提醒上限（审阅要求：防塞爆）
REMINDER_LIMIT = 20
# 静默时段（小时）：23:00–07:00 不弹提醒
QUIET_HOURS = (23, 7)

# calc 白名单：二元运算符 + 一元运算符
_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_calc(expr: str):
    """ast 白名单求值。返回 (值, None) 或 (None, 错误消息)。"""
    try:
        tree = ast.parse(str(expr), mode="eval")
    except (SyntaxError, ValueError) as exc:
        return None, "表达式不对：%s" % exc

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and \
                    not isinstance(node.value, bool):
                return node.value, None
            return None, "只允许数字"
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left = walk(node.left)
            right = walk(node.right)
            if left[1] or right[1]:
                return left if left[1] else right
            try:
                return _BIN_OPS[type(node.op)](left[0], right[0]), None
            except ZeroDivisionError:
                return None, "除数为零"
            except (OverflowError, ValueError):
                return None, "算不出来"
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            v = walk(node.operand)
            if v[1]:
                return v
            return _UNARY_OPS[type(node.op)](v[0]), None
        # 名字/调用/属性/下标……一律拒绝（禁 eval 的关键）
        return None, "只支持四则运算（+ - * / % ** 括号）"

    out = walk(tree)
    if out[1]:
        return None, out[1]
    value = out[0]
    if isinstance(value, float) and value == int(value):
        value = int(value)
    return value, None


def _data_dir(ctx) -> Path:
    d = Path(getattr(ctx, "data_dir", ".") or ".")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                 encoding="utf-8")


def _in_quiet_hours(ts_ms: int = 0) -> bool:
    t = time.localtime(ts_ms / 1000 if ts_ms else None)
    h = t.tm_hour
    if QUIET_HOURS[0] > QUIET_HOURS[1]:
        return h >= QUIET_HOURS[0] or h < QUIET_HOURS[1]
    return QUIET_HOURS[0] <= h < QUIET_HOURS[1]


def _defer_until(ts_ms: int) -> int:
    """静默时段内的提醒顺延到静默结束（第二天 07:00）。"""
    t = time.localtime(ts_ms / 1000)
    day = time.strftime("%Y-%m-%d", t)
    end = time.mktime(time.strptime(
        "%s %02d:00:00" % (day if QUIET_HOURS[1] > 0 and
                           (t.tm_hour >= QUIET_HOURS[0] or t.tm_hour < QUIET_HOURS[1])
                           else day, QUIET_HOURS[1]),
        "%Y-%m-%d %H:%M:%S"))
    # 跨天顺延：若目标时刻仍早于提醒时刻，加一天
    if end <= ts_ms / 1000:
        end += 24 * 3600
    return int(end * 1000)


class AssistantPlugin(Plugin):
    manifest = PluginManifest(
        id="assistant",
        name="日常助手",
        version="1.0.0",
        description="时间/计算/便签/提醒/打开应用——只动 Agent 自己的数据区",
        permissions=["data:read", "data:write", "app:whitelisted"],
    )

    def setup(self, ctx) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------- 便签
    def _notes_file(self) -> Path:
        return _data_dir(self.ctx) / "notes.json"

    def _note_add(self, args: dict) -> dict:
        text = str(args.get("text") or "").strip()
        if not text:
            return {"ok": False, "message": "便签内容不能为空"}
        notes = _load_json(self._notes_file(), [])
        if len(notes) >= 200:
            return {"ok": False, "message": "便签太多（上限 200 条），先清一清"}
        item = {"id": "n%d" % int(time.time() * 1000),
                "text": text[:500], "ts": int(time.time() * 1000)}
        notes.append(item)
        _save_json(self._notes_file(), notes)
        return {"ok": True, "id": item["id"], "count": len(notes)}

    def _note_list(self, args: dict) -> dict:
        notes = _load_json(self._notes_file(), [])
        limit = min(50, int(args.get("limit") or 20))
        items = notes[-limit:] if notes else []
        return {"ok": True, "count": len(notes),
                "items": [{"id": n.get("id"), "text": n.get("text"),
                           "ts": n.get("ts")} for n in reversed(items)]}

    # ------------------------------------------------------------- 提醒
    def _reminders_file(self) -> Path:
        return _data_dir(self.ctx) / "reminders.json"

    def _remind_set(self, args: dict) -> dict:
        text = str(args.get("text") or "").strip()
        when = str(args.get("when") or "").strip()
        if not text or not when:
            return {"ok": False, "message": "缺提醒内容或时间（when=HH:MM 或 YYYY-MM-DD HH:MM）"}
        reminders = _load_json(self._reminders_file(), [])
        if len(reminders) >= REMINDER_LIMIT:
            return {"ok": False,
                    "message": "提醒已经排满（上限 %d 条），先取消几条再设"
                               % REMINDER_LIMIT}
        # 解析时间：HH:MM（今天/明天）或 YYYY-MM-DD HH:MM
        ts = self._parse_when(when)
        if ts is None:
            return {"ok": False,
                    "message": "时间看不懂：%s（要 HH:MM 或 YYYY-MM-DD HH:MM）" % when}
        deferred = False
        if _in_quiet_hours(ts):
            ts = _defer_until(ts)
            deferred = True      # 静默时段 → 顺延并告知
        rid = "r%d" % int(time.time() * 1000)
        reminders.append({"id": rid, "text": text[:200], "ts": ts,
                          "original": when, "done": False})
        reminders.sort(key=lambda r: r.get("ts") or 0)
        _save_json(self._reminders_file(), reminders)
        out = {"ok": True, "id": rid, "ts": ts, "count": len(reminders)}
        if deferred:
            out["message"] = "设好了。注意：这个时间在静默时段（23:00–07:00），已顺延到早上 7 点"
        return out

    @staticmethod
    def _parse_when(when: str):
        try:
            if "-" in when and ":" in when:
                t = time.strptime(when, "%Y-%m-%d %H:%M")
            elif ":" in when:
                base = time.localtime()
                t = time.strptime(
                    "%s %s" % (time.strftime("%Y-%m-%d", base), when),
                    "%Y-%m-%d %H:%M")
                if time.mktime(t) < time.time():
                    t = time.strptime(
                        "%s %s" % (time.strftime(
                            "%Y-%m-%d", time.localtime(time.time() + 86400)),
                            when), "%Y-%m-%d %H:%M")
            else:
                return None
            return int(time.mktime(t) * 1000)
        except ValueError:
            return None

    def _remind_list(self, args: dict) -> dict:
        reminders = _load_json(self._reminders_file(), [])
        now = int(time.time() * 1000)
        items = [{"id": r.get("id"), "text": r.get("text"), "ts": r.get("ts"),
                  "quiet_shifted": bool(r.get("ts")) and _in_quiet_hours(r.get("ts") or 0)}
                 for r in reminders if not r.get("done")]
        return {"ok": True, "count": len(items), "items": items,
                "now": now, "quiet_hours": "%02d:00-%02d:00" % QUIET_HOURS}

    def _remind_cancel(self, args: dict) -> dict:
        rid = str(args.get("id") or "").strip()
        if not rid:
            return {"ok": False, "message": "缺提醒 id（先 remind_list 看）"}
        reminders = _load_json(self._reminders_file(), [])
        hit = None
        for r in reminders:
            if r.get("id") == rid and not r.get("done"):
                r["done"] = True
                hit = r
                break
        if hit is None:
            return {"ok": False, "message": "没找到未完成的提醒：%s" % rid}
        _save_json(self._reminders_file(), reminders)
        return {"ok": True, "id": rid, "text": hit.get("text")}

    # ------------------------------------------------------------- app_open
    def _whitelist_file(self) -> Path:
        return _data_dir(self.ctx) / "app_whitelist.json"

    def _app_open_guard(self, args: dict) -> str:
        """硬拒闸：白名单空 或 app 不在名单 → 连确认机会都不给。"""
        wl = _load_json(self._whitelist_file(), [])
        if not wl:
            return "应用白名单是空的：一个都不开。先在设置页加允许的程序"
        name = str(args.get("app") or "").strip().lower()
        if not name:
            return "缺 app 参数（白名单里的名字）"
        for item in wl:
            exe = str(item if isinstance(item, str) else item.get("path") or "")
            p = Path(exe)
            candidates = {exe.lower(), p.name.lower(),
                          p.stem.lower()}       # notepad / notepad.exe 都能认
            if name in candidates:
                return ""
        return "%s 不在应用白名单里（共 %d 项）" % (name, len(wl))

    def _app_open(self, args: dict) -> dict:
        wl = _load_json(self._whitelist_file(), [])
        # guard 已先拦过；这里再查一次是双重保险（直接调 invoke 也安全）
        if not wl:
            return {"ok": False, "denied": True,
                    "message": "应用白名单是空的：一个都不开。"
                               "先在设置页把允许打开的程序加进来"}
        name = str(args.get("app") or "").strip()
        if not name:
            return {"ok": False, "message": "缺 app 参数（白名单里的名字）"}
        # 名字或完整路径匹配
        target = None
        low = name.lower()
        for item in wl:
            exe = str(item if isinstance(item, str) else item.get("path") or "")
            p = Path(exe)
            if low in {exe.lower(), p.name.lower(), p.stem.lower()}:
                target = exe
                break
        if target is None:
            return {"ok": False, "denied": True,
                    "message": "%s 不在白名单里（白名单共 %d 项）"
                               % (name, len(wl))}
        p = Path(target)
        if not p.is_file():
            return {"ok": False, "message": "白名单里的程序不见了：%s" % target}
        try:
            # DETACHED：不阻塞桌宠进程
            subprocess.Popen([str(p)], close_fds=True,
                             creationflags=getattr(os, "DETACHED_PROCESS", 0))
        except OSError as exc:
            return {"ok": False, "message": "启动失败：%s" % exc}
        return {"ok": True, "app": p.name, "path": str(p)}

    def _app_whitelist_get(self, args: dict) -> dict:
        wl = _load_json(self._whitelist_file(), [])
        return {"ok": True, "count": len(wl), "items": wl}

    # ------------------------------------------------------------- 时间/计算
    def _time_now(self, args: dict) -> dict:
        now = time.time()
        return {"ok": True, "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                "week": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][time.localtime().tm_wday],
                "ts": int(now * 1000)}

    def _calc(self, args: dict) -> dict:
        expr = str(args.get("expr") or "").strip()
        if not expr:
            return {"ok": False, "message": "缺 expr"}
        if len(expr) > 200:
            return {"ok": False, "message": "表达式太长（≤200 字符）"}
        value, err = _safe_calc(expr)
        if err:
            return {"ok": False, "message": err}
        return {"ok": True, "expr": expr, "value": value}

    # ------------------------------------------------------------- 注册
    def tools(self) -> list:
        return [
            ToolSpec(
                name="time_now",
                description="现在几点、星期几",
                risk=RiskLevel.LOW,
                parameters={"type": "object", "properties": {}},
                invoke=self._time_now,
                tags=["助手", "只读"],
                data_class="public", idempotent=True,
            ),
            ToolSpec(
                name="calc",
                description="四则运算（只允许数字与 + - * / % ** 和括号）",
                risk=RiskLevel.LOW,
                parameters={"type": "object",
                            "properties": {"expr": {"type": "string"}},
                            "required": ["expr"]},
                invoke=self._calc,
                tags=["助手", "只读"],
                data_class="public", idempotent=True,
            ),
            ToolSpec(
                name="note_add",
                # 方案§7：写类操作 MEDIUM
                description="记一条便签（存在 Agent 自己的数据区）",
                risk=RiskLevel.MEDIUM,
                parameters={"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]},
                invoke=self._note_add,
                tags=["助手"],
            ),
            ToolSpec(
                name="note_list",
                description="看最近的便签",
                risk=RiskLevel.LOW,
                parameters={"type": "object",
                            "properties": {"limit": {"type": "integer"}}},
                invoke=self._note_list,
                tags=["助手", "只读"],
                idempotent=True,
            ),
            ToolSpec(
                name="remind_set",
                # 方案§7：写类操作 MEDIUM
                description="设一个提醒（when=HH:MM 或 YYYY-MM-DD HH:MM；静默时段自动顺延；上限 20 条）",
                risk=RiskLevel.MEDIUM,
                parameters={"type": "object",
                            "properties": {"text": {"type": "string"},
                                           "when": {"type": "string"}},
                            "required": ["text", "when"]},
                invoke=self._remind_set,
                tags=["助手"],
            ),
            ToolSpec(
                name="remind_list",
                description="看现在排着的提醒",
                risk=RiskLevel.LOW,
                parameters={"type": "object", "properties": {}},
                invoke=self._remind_list,
                tags=["助手", "只读"],
                idempotent=True,
            ),
            ToolSpec(
                name="remind_cancel",
                # 方案§7：写类操作 MEDIUM
                description="取消一个提醒（要 id，先 remind_list 查）",
                risk=RiskLevel.MEDIUM,
                parameters={"type": "object",
                            "properties": {"id": {"type": "string"}},
                            "required": ["id"]},
                invoke=self._remind_cancel,
                tags=["助手"],
            ),
            ToolSpec(
                name="app_open",
                description="打开白名单里的应用（白名单为空时一律拒绝；HIGH 需确认）",
                risk=RiskLevel.HIGH,
                parameters={"type": "object",
                            "properties": {"app": {"type": "string"}},
                            "required": ["app"]},
                invoke=self._app_open,
                guard=self._app_open_guard,
                tags=["助手", "高风险"],
            ),
        ]
