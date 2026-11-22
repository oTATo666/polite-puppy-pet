# -*- coding: utf-8 -*-
"""
礼帽小狗 · 人格风格层
=====================

只管"怎么说"，不管"做不做"。

这是《桌宠Agent_可插拔架构设计》§0 的核心决策：
  Persona（风格层）→ 语气、用词、表情包、收尾
  Safety（执行层）→ 指令是否真的执行

人格永远无权否决执行。她可以一边毒舌"昂…行吧行吧"，
一边老老实实把事办了。

persona.json 放在项目根，改那个文件就能改人设，不改代码。
名字一律从 identity.py 取（N-24），源码里不出现词面。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import identity as _identity      # 01_程序 根目录的统一取名入口
except ImportError:                   # 独立包测试等场景：中性回落
    _identity = None


def _default_name() -> str:
    return _identity.pet_name() if _identity else "小狗"


DEFAULT_PERSONA = {
    "identity": {"name": _default_name(), "species": "电子小狗"},
    "style": {"max_len": 20, "split_by": "||", "max_bubbles": 3},
    "personality": {"core": "慢热，嘴硬心软"},
    "catchphrases": {"top": [], "frequent_words": []},
    "emotion_tags": {},
    "execution_layer": {"persona_can_veto": False},
}


def load_persona(path: Path) -> dict:
    """读 persona.json；读不到就用默认人设（不能让缺文件炸掉整只狗）。"""
    try:
        p = Path(path)
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = dict(DEFAULT_PERSONA)
                merged.update(data)
                return merged
    except Exception:
        pass
    return dict(DEFAULT_PERSONA)


def persona_prompt(persona: dict, include_identity: bool = True) -> str:
    """
    把人设配置渲染成一段注入 systemPrompt 的文本。

    控制在 400 字以内：太长会稀释模型对当下对话的注意力。

    include_identity=False 时只输出"数据驱动"的部分（口癖、情绪标签、
    说话习惯），适合已经有身份说明的基础提示词时叠加使用。
    """
    ident = persona.get("identity") or {}
    style = persona.get("style") or {}
    pers = persona.get("personality") or {}
    words = (persona.get("catchphrases") or {}).get("frequent_words") or []
    top = (persona.get("catchphrases") or {}).get("top") or []
    emo = persona.get("emotion_tags") or {}

    top5 = [str(t.get("text")) for t in top[:6] if t.get("text")]
    emo3 = [str(k) for k in list(emo.keys())[:5]]

    lines = []
    if include_identity:
        lines.append("# 你是谁（人设卡，照着这个说话）")
        _dname = _default_name()
        lines.append("· 你是%s，%s，是%s养的。认%s这个名。" % (
            ident.get("name") or _dname,
            ident.get("species", "电子小狗"),
            ident.get("owner", "他"),
            ident.get("name") or _dname,
        ))
        lines.append("· 性子：%s" % (pers.get("core", "慢热，嘴硬心软")))
        lines.append("· 说话：一条不超过%d个字；一句一条，用 %s 分开，最多 %d 条；不打句号。"
                     % (int(style.get("max_len", 20)), style.get("split_by", "||"),
                        int(style.get("max_bubbles", 3))))
    else:
        lines.append("# 她说话的习惯（从真实聊天统计出来的，照着用）")
    if style.get("hometown_flavor"):
        lines.append("· 语气词可用：%s（一句话里带一个就够，别句句都带）。"
                     % "、".join(style["hometown_flavor"][:10]))
    if top5:
        lines.append("· 常挂嘴边的：%s" % "、".join(top5))
    if words:
        lines.append("· 这些是他俩常说的词，能搭就搭：%s"
                     % "、".join(words[:12]))
    if emo3:
        lines.append("· 情绪表情：%s" % "、".join(emo3))
    if pers.get("attitude"):
        lines.append("· 态度：%s" % pers["attitude"])
    never = pers.get("never") or []
    if never:
        lines.append("· 禁止：%s" % "；".join(never[:5]))
    lines.append("· 人格只管你怎么说，不管事做不做；他说做的事，照做，别拿脾气当理由拒绝。")
    return "\n".join(lines)


def persona_data_block(persona: dict) -> str:
    """
    只取"数据驱动"的那部分（口癖统计、情绪标签、节奏），≤260 字。

    用途：基础提示词里已经写了身份和语气时，把这份画像**追加**进去，
    让模型照着她真实的说话习惯来，而不是照着一条通用规则。
    """
    block = persona_prompt(persona, include_identity=False)
    # 去掉人格/执行层的说明（那是给设置页看的，不进 prompt）
    lines = [l for l in block.split("\n")
             if "人格只管你怎么说" not in l and not l.startswith("# 她说话的习惯")]
    lines.insert(0, "# 她的说话习惯（从她真实发言里统计出来的）")
    text = "\n".join(lines)
    return text[:260]


def save_persona(path: Path, persona: dict) -> dict:
    """
    保存人格配置。写之前先备份旧文件，出错能退回去。
    返回 {ok, message, path, backup}。
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "message": "目录建不出来：%s" % exc}
    backup = ""
    if p.is_file():
        try:
            import shutil
            import time
            stamp = time.strftime("%Y%m%d_%H%M%S")
            bak = p.with_suffix(".json.bak-%s" % stamp)
            shutil.copy2(str(p), str(bak))
            backup = str(bak)
        except OSError:
            backup = ""
    try:
        p.write_text(json.dumps(persona, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "message": "写不进去：%s" % exc}
    return {"ok": True, "message": "人设已保存", "path": str(p),
            "backup": backup}


def style_hint(reply_text: str, persona: dict) -> dict:
    """
    检查一条回复是否符合人设风格（给测试与设置页用）。
    返回 {"ok":bool, "issues":[...], "stats":{...}}
    """
    style = persona.get("style") or {}
    max_len = int(style.get("max_len", 20))
    split_by = style.get("split_by", "||")
    max_bubbles = int(style.get("max_bubbles", 3))
    text = (reply_text or "").strip()
    issues = []
    bubbles = [b for b in text.split(split_by) if b.strip()]
    if len(bubbles) > max_bubbles:
        issues.append("条数超了：%d > %d" % (len(bubbles), max_bubbles))
    for b in bubbles:
        if len(b) > max_len:
            issues.append("太长了（%d 字）：%s" % (len(b), b[:20]))
    return {
        "ok": not issues,
        "issues": issues,
        "stats": {
            "len": len(text),
            "bubbles": len(bubbles),
            "max_bubble_len": max((len(b) for b in bubbles), default=0),
        },
    }
