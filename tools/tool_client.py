# -*- coding: utf-8 -*-
"""礼貌小狗 · 工具接口客户端（命令行 + Python 两种用法）。

接口契约来自 `GET /api/tool/schema`（机器可读），执行入口是 `POST /api/tool`。
所有工具都受「总开关 → 分级 → 确认」三层控制，调用前不会绕过任何权限。

命令行：
    python tools/tool_client.py schema
    python tools/tool_client.py list
    python tools/tool_client.py call sys_info
    python tools/tool_client.py call screenshot
    python tools/tool_client.py call open_app --args app=记事本
    python tools/tool_client.py call open_url --args url=https://www.bing.com
    python tools/tool_client.py call clipboard_read --confirm

代码里：
    from tool_client import ToolClient
    c = ToolClient()                       # 默认 http://127.0.0.1:8765
    c.schema()                             # 工具清单 + 参数 schema
    c.call("sys_info")                     # 只读，直接执行
    c.call("clipboard_read", confirm=True) # 敏感操作，显式确认
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8765"


class ToolClient:
    """本机桌宠的工具调用客户端（不联网，只打 127.0.0.1）。"""

    def __init__(self, base: str = DEFAULT_BASE, timeout: float = 30.0) -> None:
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        # 本机接口不绕系统代理（这台机器配了 http_proxy，不绕会打不通）
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---- HTTP ----
    def _get(self, path: str) -> dict[str, Any]:
        with self._opener.open(self.base + path, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._opener.open(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---- 公开方法 ----
    def schema(self) -> dict[str, Any]:
        """工具契约：名字、风险级（low/medium/high）、参数 schema、调用方式。"""
        return self._get("/api/tool/schema")

    def list(self) -> dict[str, Any]:
        """当前权限配置 + 工具清单 + 最近 20 条审计。"""
        return self._get("/api/tools")

    def call(self, tool: str, args: dict[str, Any] | None = None,
             confirm: bool = False) -> dict[str, Any]:
        """执行一个工具。need_confirm 的工具不带 confirm=True 时只返回 need_confirm。"""
        return self._post("/api/tool", {"tool": tool, "args": args or {}, "confirm": bool(confirm)})

    def config(self, **patch: Any) -> dict[str, Any]:
        """改权限：enabled / levels / confirm_level / allow_apps。"""
        return self._post("/api/tools/config", patch)

    def stop(self) -> dict[str, Any]:
        """紧急停止：一键关掉电脑控制。"""
        return self._post("/api/tools/stop", {})


def _parse_args(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            continue
        k, _, v = item.partition("=")
        out[k.strip()] = v.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="礼貌小狗工具接口客户端")
    ap.add_argument("--base", default=DEFAULT_BASE, help="桌宠地址，默认 http://127.0.0.1:8765")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("schema", help="打印工具契约（JSON）")
    sub.add_parser("list", help="打印权限配置与审计")
    p_call = sub.add_parser("call", help="执行一个工具")
    p_call.add_argument("tool", help="工具名，见 schema")
    p_call.add_argument("--args", nargs="*", default=[], help="参数 k=v，多个用空格分隔")
    p_call.add_argument("--confirm", action="store_true", help="确认敏感/高风险操作")
    sub.add_parser("stop", help="紧急停止（关闭电脑控制）")

    ns = ap.parse_args(argv)
    client = ToolClient(ns.base)
    try:
        if ns.cmd == "schema":
            data = client.schema()
        elif ns.cmd == "list":
            data = client.list()
        elif ns.cmd == "stop":
            data = client.stop()
        else:
            data = client.call(ns.tool, _parse_args(ns.args), ns.confirm)
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "message": f"连不上桌宠（{exc}）：先启动 PolitePuppy.exe"}, ensure_ascii=False))
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if data.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
