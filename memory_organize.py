# -*- coding: utf-8 -*-
r"""
记忆离线整理（P1-1 第二半）
==========================

规则抽取器的覆盖本来就低（用户消息中位数 7 个字，20 条正则兜不住），
所以每天再做一次"回头看"：把**新增的对话**交给云端模型归成
事实 / 关系 / 情绪摘要，写进记忆库，来源标记 `by=model`，置信度不超过 0.6。

四条边界（都写死在这里，不做成可配项）
--------------------------------------
1. **默认关**。开启会把你和她的聊天发到云端接口，属于 P0-3 管的外发行为，
   必须由用户在设置页显式打开，并在打开时看到外发提示。
2. **只发新增的对话**，按 episodes.id 递增，一次最多 60 条 / 3000 字；
   发过的不重发（游标存在 记忆\organize_state.json）。
3. **不覆盖规则抽出来的事实**：merge_facts 遇到同 key 相似值只做合并，
   模型给的值置信度封顶 0.6，规则给的 1.0 不会被降级。
4. **失败不抛**：网络/额度/解析任何一步出错都只记状态，等下一次。

对外只有三个函数：status() / run() / set_enabled()，都不 import main，
由 main.py 传数据库和状态文件路径进来，避免循环依赖。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

# 一次整理最多看多少条 / 多少字：控制上下文长度，别把整天的聊天一次塞进去
MAX_ITEMS = 60
MAX_CHARS = 3000
# 少于这么多条就不值得跑一次云端
MIN_ITEMS = 5
# 模型给的事实置信度封顶（方案要求 ≤ 0.6）
MODEL_CONF = 0.6
# 一次最多收几条，防模型话痨
MAX_FACTS = 6
MAX_RELATIONS = 2

TIMEOUT = 45.0

SYSTEM_PROMPT = (
    "你在帮一个桌面陪伴程序整理记忆。输入是一段「他」和「她」的聊天记录。\n"
    "只输出一个 JSON 对象，不要解释、不要 markdown 代码块。格式：\n"
    '{"facts":[{"key":"称呼|学校|喜欢|讨厌|作息|计划|情绪|其他","value":"具体值"}],'
    '"relations":[{"kind":"shared_event|promise","title":"短标题","detail":"一句话"}],'
    '"mood":"他这段时间的情绪，一句话"}\n'
    "规则：\n"
    "1. 只抽「他」明确说过或强烈暗示的；拿不准就不要写；不许编。\n"
    "2. facts 每条 value 不超过 20 字，最多 6 条；没有就给空数组。\n"
    "3. relations 只记「一起发生过的事」和「他答应过 / 她答应过的事」，最多 2 条。\n"
    "4. 抽不出来就全部给空数组，mood 给空字符串。"
)


# ---------------------------------------------------------------- 状态文件
def _load_state(state_path: Path) -> dict:
    state = {"enabled": False, "last_day": "", "last_episode_id": 0,
             "last_run_at": 0, "last_result": {}}
    try:
        raw = state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            state.update({k: v for k, v in data.items() if k in state})
    except (OSError, ValueError):
        pass
    return state


def _save_state(state_path: Path, state: dict) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- 云端调用
def _chat(base_url: str, api_key: str, model: str, transcript: str) -> str:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": transcript}],
        "temperature": 0.2,
        "max_tokens": 700,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key,
                 "User-Agent": "PolitePuppy/2.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
    data = json.loads(raw)
    content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    return str(content)


def _parse_json(text: str) -> dict:
    """从模型回复里抠出 JSON 对象（容忍 ```json 围栏和前后废话）。"""
    t = str(text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(t[start:end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 取新增对话
def _recent_episodes(db_path: Path, after_id: int, limit: int) -> list:
    """取 id > after_id 的在用对话（他 + 她），按 id 升序（把积压的先做完）。"""
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, role, text FROM episodes "
            "WHERE deleted=0 AND id>? AND role IN ('user','assistant') "
            "ORDER BY id LIMIT ?", (int(after_id), int(limit))).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _max_episode_id(db_path: Path) -> int:
    """库里最大的 episode id（用来把游标"从现在开始"，不回头啃历史）。"""
    if not db_path.is_file():
        return 0
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        row = conn.execute("SELECT max(id) FROM episodes").fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _transcript(rows: list) -> str:
    """拼成紧凑对话，控字数（从最新往回收，保证看到的是最近的部分）。"""
    lines = []
    total = 0
    for row in reversed(rows):
        who = "他" if str(row.get("role")) == "user" else "她"
        text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
        if not text:
            continue
        if len(text) > 120:
            text = text[:120] + "…"
        line = "%s：%s" % (who, text)
        if total + len(line) > MAX_CHARS:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(reversed(lines))


# ---------------------------------------------------------------- 写入
def _write(db_path: Path, data: dict) -> dict:
    """把模型给的东西写进库：facts（by=model，conf≤0.6）+ relations + 情绪。"""
    out = {"facts": 0, "relations": 0, "mood": "", "skipped": 0}
    facts = data.get("facts") if isinstance(data.get("facts"), list) else []
    rels = data.get("relations") if isinstance(data.get("relations"), list) else []
    mood = str(data.get("mood") or "").strip()[:40]

    items = []
    for it in facts[:MAX_FACTS]:
        if not isinstance(it, dict):
            out["skipped"] += 1
            continue
        key = str(it.get("key") or "").strip()[:32]
        value = str(it.get("value") or "").strip()[:60]
        if not key or not value or len(value) > 30:
            out["skipped"] += 1
            continue
        items.append({"key": key, "value": value,
                      "confidence": MODEL_CONF, "by": "model"})
    if mood:
        items.append({"key": "情绪", "value": mood,
                      "confidence": MODEL_CONF, "by": "model"})

    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        if items:
            # 复用规则抽取器的合并逻辑（墓碑不复活，只记 suppressed），两边口径一致
            try:
                import memory_extract as ME
            except ImportError:      # 独立运行时补上 tools 目录
                import sys
                sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
                import memory_extract as ME
            store = ME.merge_facts(conn, items, evidence="organize")
            out["facts"] = int(store.get("inserted", 0))
        now = int(time.time() * 1000)
        for it in rels[:MAX_RELATIONS]:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()[:200]
            if not title:
                continue
            kind = str(it.get("kind") or "shared_event")[:32]
            if kind not in ("shared_event", "promise", "intimacy"):
                kind = "shared_event"
            conn.execute(
                "INSERT INTO relations(kind, title, detail, ts, status) "
                "VALUES (?,?,?,?,?)",
                (kind, title, str(it.get("detail") or "")[:2000] or None,
                 now, "open"))
            out["relations"] += 1
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    out["mood"] = mood
    return out


# ---------------------------------------------------------------- 对外接口
def status(db_path: Path, state_path: Path) -> dict:
    """给设置页看的状态：开没开、上次什么时候跑的、跑出几条、为什么没跑。"""
    st = _load_state(Path(state_path))
    today = time.strftime("%Y-%m-%d")
    key = _env_key()
    return {
        "ok": True,
        "enabled": bool(st.get("enabled")),
        "last_day": st.get("last_day") or "",
        "last_run_at": st.get("last_run_at") or 0,
        "last_episode_id": st.get("last_episode_id") or 0,
        "last_result": st.get("last_result") or {},
        "has_key": bool(key),
        "can_run": bool(st.get("enabled")) and bool(key),
        "ran_today": (st.get("last_day") == today),
        "today": today,
        "model": _env_model(),
        "limit": {"items": MAX_ITEMS, "chars": MAX_CHARS},
    }


def set_enabled(db_path: Path, state_path: Path, enabled: bool) -> dict:
    """开 / 关。

    开启的那一刻如果还没有游标，就把游标定在"当前最新一条"——
    只整理**开启之后**的新对话，绝不把历史几万条一次性发到云端。
    """
    st = _load_state(Path(state_path))
    st["enabled"] = bool(enabled)
    if enabled and not st.get("last_episode_id"):
        st["last_episode_id"] = _max_episode_id(Path(db_path))
        st["cursor_from"] = "enable"
    _save_state(Path(state_path), st)
    return {"ok": True, "enabled": st["enabled"],
            "cursor": int(st.get("last_episode_id") or 0)}


def run(db_path: Path, state_path: Path, force: bool = False) -> dict:
    """跑一次整理。force=True 忽略"今天跑过"和开关（设置页手动点）。"""
    db_path = Path(db_path)
    state_path = Path(state_path)
    st = _load_state(state_path)
    today = time.strftime("%Y-%m-%d")

    if not force and not st.get("enabled"):
        return {"ok": False, "skipped": "off", "message": "离线整理没开"}
    key = _env_key()
    if not key:
        return {"ok": False, "skipped": "no_key",
                "message": "没配云端 Key（.env 里的 DEEPSEEK_API_KEY），整理不外发"}
    if not force and st.get("last_day") == today:
        return {"ok": False, "skipped": "done_today",
                "message": "今天已经整理过了"}

    rows = _recent_episodes(db_path, int(st.get("last_episode_id") or 0), MAX_ITEMS)
    if len(rows) < (1 if force else MIN_ITEMS):
        return {"ok": False, "skipped": "too_few",
                "message": "新增对话不足 %d 条，先攒着" % MIN_ITEMS,
                "count": len(rows)}

    transcript = _transcript(rows)
    try:
        raw = _chat(_env_base(), key, _env_model(), transcript)
    except urllib.error.HTTPError as exc:
        st["last_result"] = {"ok": False, "message": "HTTP %s" % exc.code,
                             "at": int(time.time() * 1000)}
        _save_state(state_path, st)
        return {"ok": False, "skipped": "http", "message": "HTTP %s" % exc.code}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        st["last_result"] = {"ok": False, "message": "连不上：%s" % exc,
                             "at": int(time.time() * 1000)}
        _save_state(state_path, st)
        return {"ok": False, "skipped": "offline", "message": "连不上云端接口"}
    except ValueError as exc:
        return {"ok": False, "skipped": "bad_json", "message": str(exc)[:120]}

    data = _parse_json(raw)
    if not data:
        st["last_result"] = {"ok": False, "message": "模型没给出可解析的 JSON",
                             "at": int(time.time() * 1000)}
        _save_state(state_path, st)
        return {"ok": False, "skipped": "unparsable",
                "message": "模型没给出可解析的 JSON"}

    written = _write(db_path, data)
    st.update({
        "last_day": today,
        "last_episode_id": int(rows[-1].get("id") or 0),
        "last_run_at": int(time.time() * 1000),
        "last_result": {"ok": True, "scanned": len(rows), "written": written,
                        "at": int(time.time() * 1000)},
    })
    _save_state(state_path, st)
    return {"ok": True, "skipped": "", "scanned": len(rows),
            "written": written, "message": "整理完了"}


# ---------------------------------------------------------------- 配置来源
def _env_base() -> str:
    import os
    return (os.environ.get("DEEPSEEK_BASE_URL")
            or os.environ.get("OLLAMA_BASE_URL")
            or "https://api.deepseek.com/v1").strip().rstrip("/")


def _env_model() -> str:
    import os
    return (os.environ.get("DEEPSEEK_MODEL")
            or os.environ.get("OLLAMA_MODEL")
            or "deepseek-chat").strip()


def _env_key() -> str:
    import os
    return (os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OLLAMA_API_KEY") or "").strip()
