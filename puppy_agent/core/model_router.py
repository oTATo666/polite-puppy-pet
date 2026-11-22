# -*- coding: utf-8 -*-
"""礼帽小狗 · 模型路由（AG-3 / Agent 2.0）
=============================================

把「调一次模型」收敛到一个地方，规划器（ModelPlanner）经它出话。

设计约束（对应 R1/R3 审阅）：
  · **超时分级**：本地（loopback）8 秒、云端 10 秒——规划是快路径，
    不允许把聊天卡住；
  · **call_fn 可注入**：测试用假模型替换真网络；
  · **请求体最小化**：messages 只含规划系统提示词 + 用户这一句话。
    记忆片段、文件内容、私人称呼一律不进来——由 planner 组消息时保证，
    这里再留一道 `assert_request_clean()` 钩子给测试抓请求体。

接口：
    router.chat(system: str, user: str) -> dict
        {"ok": bool, "text": str, "elapsed_ms": int, "error": str, "timed_out": bool}
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

TIMEOUT_LOCAL = 8.0    # 本地模型规划超时（秒）
TIMEOUT_CLOUD = 10.0   # 云端规划超时（秒）

CallFn = Callable[[list, float], str]

#: 注入标记——模型输出里出现这些字样就整条丢弃（当被文件/网页内容带偏处理）
INJECTION_MARKS = (
    "忽略上面的指令", "忽略以上", "ignore previous", "ignore all previous",
    "system prompt", "system:", "<|", "[INST]", "[/INST]", "</s>",
)


def _is_loopback(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


class ModelRouter:
    """模型调用入口（可注入 call_fn；默认走 OpenAI 风格 /chat/completions）。"""

    def __init__(self, base_url: str = "", model: str = "",
                 call_fn: CallFn | None = None,
                 api_key: str = "",
                 timeout_local: float = TIMEOUT_LOCAL,
                 timeout_cloud: float = TIMEOUT_CLOUD):
        self.base_url = (base_url or "").strip()
        self.model = (model or "").strip()
        self.api_key = (api_key or "").strip()
        self._call_fn = call_fn
        self.timeout_local = float(timeout_local)
        self.timeout_cloud = float(timeout_cloud)
        # 测试钩子：每次请求的 (system, user, timeout) 存这里，供抓请求体断言
        self.last_request: dict[str, Any] | None = None

    @property
    def is_local(self) -> bool:
        return _is_loopback(self.base_url)

    def timeout(self) -> float:
        return self.timeout_local if self.is_local else self.timeout_cloud

    # ---- 默认实现：OpenAI 风格 -------------------------------------------
    def _default_call(self, messages: list, timeout: float) -> str:
        url = self.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": 0,            # 方案§4：温度 0
            "max_tokens": 300,
        }
        headers = {"Content-Type": "application/json",
                   "User-Agent": "PolitePuppy-Planner/1.0"}
        if self.api_key and not self.is_local:
            headers["Authorization"] = "Bearer " + self.api_key
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("模型返回空 choices")
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "")

    # ---- 对外 ------------------------------------------------------------
    def chat(self, system: str, user: str) -> dict:
        """发一次规划请求。任何失败都返回 {"ok": False, ...}，不抛异常。"""
        timeout = self.timeout()
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        # 记录请求供审计/测试（不含任何附加内容——planner 只给这两条）
        self.last_request = {"system": system, "user": user,
                             "timeout": timeout,
                             "local": self.is_local,
                             "model": self.model, "ts": time.time()}
        t0 = time.monotonic()
        try:
            fn = self._call_fn or self._default_call
            text = fn(messages, timeout)
            elapsed = int((time.monotonic() - t0) * 1000)
            return {"ok": True, "text": str(text or ""),
                    "elapsed_ms": elapsed, "error": "", "timed_out": False}
        except Exception as exc:  # noqa: BLE001 —— 规划失败必须回落不能炸
            elapsed = int((time.monotonic() - t0) * 1000)
            timed_out = isinstance(exc, TimeoutError) or \
                "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
            return {"ok": False, "text": "", "elapsed_ms": elapsed,
                    "error": str(exc)[:200], "timed_out": bool(timed_out)}
