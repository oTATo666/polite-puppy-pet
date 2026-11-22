# -*- coding: utf-8 -*-
"""身份名中心化（N-24）。

所有面向用户的文字与提示词里的「她叫什么 / 怎么称呼用户」都从这里取，
不再硬编码在代码或文档字符串里。

取名优先级（高 → 低）：
  1. 数据目录 settings.json 的 identity.pet_name / identity.user_nick
     （用户在设置里填的，首次启动问「想让她怎么称呼你」存的就是这里）；
  2. exe 旁 persona.json 的 identity.name / identity.owner（随包人设）；
  3. 中性默认：小狗 / 你。

外发版的 persona.json 在组装时被替换为中性版（release/persona.json），
因此同一个 exe：个人版读到真实人设，外发版自然落到中性名——
代码里没有也不需要出现任何真实名字。

模块是独立可 import 的：main.py、puppy_agent 插件、tools 工具都能用，
不依赖 main.py 先启动（路径找不到时安静回落到默认值）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_PET = "小狗"
DEFAULT_USER = "你"

_paths: dict = {"config_file": None, "persona_file": None}
_cache: dict = {"sig": None, "data": {}}


def bind(config_file, persona_file) -> None:
    """main.py 启动时把真实路径告诉本模块（settings.json 与 persona.json）。"""
    _paths["config_file"] = Path(config_file) if config_file else None
    _paths["persona_file"] = Path(persona_file) if persona_file else None
    _cache["sig"] = None    # 路径变了就重算


def _guess_persona_file() -> Path | None:
    """没被 bind 时自己找 persona.json：脚本目录 / exe 目录。"""
    try:
        base = Path(sys.executable).resolve().parent \
            if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    except OSError:
        return None
    p = base / "persona.json"
    return p if p.is_file() else None


def _load() -> dict:
    cfg_path = _paths.get("config_file")
    per_path = _paths.get("persona_file") or _guess_persona_file()
    sig = (str(cfg_path), str(per_path),
           _mtime(cfg_path), _mtime(per_path))
    if _cache["sig"] == sig:
        return _cache["data"]
    data: dict = {}
    if cfg_path and Path(cfg_path).is_file():
        try:
            obj = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
            ident = obj.get("identity")
            if isinstance(ident, dict):
                data.update({k: v for k, v in ident.items() if v})
        except (OSError, ValueError):
            pass
    if per_path and per_path.is_file():
        try:
            obj = json.loads(per_path.read_text(encoding="utf-8"))
            ident = obj.get("identity")
            if isinstance(ident, dict):
                # settings 优先：persona 的值只补空位
                for k in ("pet_name", "user_nick", "name", "owner"):
                    if not data.get(k):
                        data[k] = ident.get(k)
        except (OSError, ValueError):
            pass
    _cache["sig"] = sig
    _cache["data"] = data
    return data


def _mtime(p) -> float:
    if not p:
        return 0.0
    try:
        return Path(p).stat().st_mtime
    except OSError:
        return 0.0


def pet_name() -> str:
    """她叫什么。settings > persona > 「小狗」。"""
    d = _load()
    return str(d.get("pet_name") or d.get("name") or DEFAULT_PET)


def user_nick() -> str:
    """怎么称呼用户。settings > persona > 「你」。"""
    d = _load()
    return str(d.get("user_nick") or d.get("owner") or DEFAULT_USER)


def pet_aliases() -> list:
    """她的所有称呼（名字、网名），用于解析用户指代 / 语料前缀剥离。"""
    d = _load()
    out = []
    for k in ("pet_name", "name", "wechat_name"):
        v = str(d.get(k) or "").strip()
        if v and v not in out:
            out.append(v)
    out.append(DEFAULT_PET)
    return out


def edition() -> str:
    """外发版 persona.json 标 edition=release；个人版空（或 personal）。"""
    per_path = _paths.get("persona_file") or _guess_persona_file()
    if per_path and Path(per_path).is_file():
        try:
            obj = json.loads(Path(per_path).read_text(encoding="utf-8"))
            return str(obj.get("edition") or "")
        except (OSError, ValueError):
            pass
    return ""
