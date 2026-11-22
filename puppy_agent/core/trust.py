# -*- coding: utf-8 -*-
"""礼帽小狗 · 插件信任清单（AG-1 / SDK v2）
=============================================

原则（默认拒绝）：
  · **包内自带**的插件（puppy_agent\\plugins\\）——随发布一起构建、
    一起检查，默认信任；
  · **用户自己放的**插件（exe 旁边的 plugins\\）——默认**禁用**；
    用户在设置页逐个启用后才加载；
  · 已启用的插件文件**哈希一变**（被改过/被替换）→ 自动禁用，
    等用户重新确认——防止"启用一次后被人偷偷换内容"。

状态存数据目录 trust.json：
    {"<文件名>": {"enabled": true/false, "sha256": "...", "decided_at": ...}}
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TrustList:
    """插件信任状态。内置目录默认信任；用户目录默认拒绝 + 哈希绑定。"""

    def __init__(self, data_dir: Path, builtin_dir: Path | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.data_dir / "trust.json"
        self.builtin_dir = Path(builtin_dir).resolve() if builtin_dir else None

    # ------------------------------------------------------------ 状态
    def _load(self) -> dict:
        try:
            obj = json.loads(self.file.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return obj
        except (OSError, ValueError):
            pass
        return {}

    def _save(self, data: dict) -> None:
        try:
            self.file.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass   # 写不进去就当没记录（默认拒绝仍然成立，安全侧不放宽）

    # ------------------------------------------------------------ 判定
    def decide(self, path: Path) -> dict:
        """决定要不要加载这个插件文件。

        返回 {"allow": bool, "reason": str, "changed": bool}
        """
        path = Path(path)
        try:
            real = path.resolve()
        except OSError:
            real = path
        # 1) 包内自带 → 信任（发布流程已包含 lint 与打包检查）
        if self.builtin_dir is not None:
            try:
                if real.is_relative_to(self.builtin_dir):
                    return {"allow": True, "reason": "builtin", "changed": False}
            except (OSError, ValueError):
                pass
        # 2) 用户目录 → 查记录
        rec = self._load().get(path.name)
        if not rec:
            return {"allow": False, "reason": "未确认的插件（默认禁用）",
                    "changed": False}
        if not rec.get("enabled"):
            return {"allow": False, "reason": "已被用户禁用", "changed": False}
        try:
            cur = _sha256(path)
        except OSError as exc:
            return {"allow": False, "reason": "读不了文件：%s" % exc,
                    "changed": True}
        if rec.get("sha256") and rec["sha256"] != cur:
            # 哈希变了 → 自动禁用（等用户重新确认）
            rec["enabled"] = False
            rec["auto_disabled_at"] = int(time.time() * 1000)
            data = self._load()
            data[path.name] = rec
            self._save(data)
            return {"allow": False, "reason": "文件内容变了，已自动禁用",
                    "changed": True}
        return {"allow": True, "reason": "已确认且未变化", "changed": False}

    # ------------------------------------------------------------ 操作
    def enable(self, path: Path) -> dict:
        path = Path(path)
        data = self._load()
        try:
            sha = _sha256(path)
        except OSError as exc:
            return {"ok": False, "message": "读不了文件：%s" % exc}
        data[path.name] = {"enabled": True, "sha256": sha,
                           "decided_at": int(time.time() * 1000)}
        self._save(data)
        return {"ok": True, "message": "已启用 %s" % path.name}

    def disable(self, name: str) -> dict:
        data = self._load()
        rec = data.get(name) or {}
        rec["enabled"] = False
        rec["decided_at"] = int(time.time() * 1000)
        data[name] = rec
        self._save(data)
        return {"ok": True, "message": "已禁用 %s" % name}

    def list_all(self, dirs: list) -> list:
        """给设置页看：每个插件文件 + 当前状态。"""
        data = self._load()
        out = []
        for d in dirs:
            d = Path(d)
            if not d.is_dir():
                continue
            is_builtin = (self.builtin_dir is not None
                          and d.resolve() == self.builtin_dir)
            for p in sorted(d.glob("*.py")):
                if p.name.startswith("_"):
                    continue
                rec = data.get(p.name) or {}
                out.append({
                    "name": p.name,
                    "dir": "builtin" if is_builtin else "user",
                    "enabled": True if is_builtin else bool(rec.get("enabled")),
                    "changed": bool(rec.get("sha256"))
                    and rec.get("sha256") != _sha256(p),
                })
        return out
