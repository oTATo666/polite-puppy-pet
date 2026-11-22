# -*- coding: utf-8 -*-
"""假模型（AG-5）：按脚本队列返回预设输出，不联网。

特殊标记：
  "TIMEOUT"      → 抛 TimeoutError（模拟超时）
  "GARBAGE"      → 返回乱码
  其他           → 原样返回（通常是一段 JSON）
队列耗尽后返回 "输出耗尽"（应触发回落）。
"""

from __future__ import annotations

import sys
from pathlib import Path


class FakeModel:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []          # (system, user, timeout)
        self.call_count = 0

    def __call__(self, messages, timeout):
        self.calls.append((messages[0].get("content"), messages[1].get("content"),
                           timeout))
        self.call_count += 1
        if not self.script:
            return "输出耗尽"
        out = self.script.pop(0)
        if out == "TIMEOUT":
            raise TimeoutError("read timed out")
        if out == "GARBAGE":
            return "\x00\x01 乱码输出"
        return out


def make_router(script, base_url="http://127.0.0.1:11434/v1"):
    """构造装好假模型的 ModelRouter。"""
    if str(Path(__file__).resolve().parent.parent.parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from puppy_agent.core.model_router import ModelRouter
    fm = FakeModel(script)
    router = ModelRouter(base_url=base_url, model="fake-model",
                         call_fn=fm)
    return router, fm
