# -*- coding: utf-8 -*-
"""
插件：消息代发
==============

按《桌宠Agent_可插拔架构设计》§4 的语义边界实现：

  ① 代用户发出的消息（微信/QQ/邮件正文）—— 内容是用户的本意，
     可以由 Agent 起草，但发送前必须预览确认；**不注入小狗人设**，
     否则会把"你的话"变成"狗的话"。
  ② 小狗对用户的播报 —— 用小狗语气，只显示在桌宠这一侧，
     由 personality 层负责，不经过这里。

平台落地状态（不夸大能力）：
  email  —— 可完整实现（标准 SMTP，需要用户自己填邮箱与授权码）
  wechat —— 接口已定义，默认"待接入"（个人号无官方 API）
  qq     —— 接口已定义，默认"待接入"（个人号无官方 API）

风险：外发一律 HIGH，必须显式确认 + 留痕。
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from ..core.plugin import Plugin, PluginManifest, RiskLevel, ToolSpec


class MessageAdapter:
    """所有代发平台的统一接口。"""

    platform = "base"
    status = "待接入"

    def scope(self) -> str:
        """声明本适配器的实现范围与风险。"""
        return "未实现"

    def send(self, peer: str, content: str, opts: dict | None = None) -> dict:
        raise NotImplementedError


class EmailAdapter(MessageAdapter):
    platform = "email"
    status = "可实现（需 SMTP 授权码）"

    def scope(self) -> str:
        return ("标准 SMTP 发信，支持正文/主题；需用户在本机填写邮箱与授权码，"
                "凭据只存本机不外发；无平台封号风险。")

    def send(self, peer: str, content: str, opts: dict | None = None) -> dict:
        opts = opts or {}
        host = opts.get("smtp_host") or "smtp.qq.com"
        port = int(opts.get("smtp_port") or 465)
        user = opts.get("account") or ""
        password = opts.get("password") or ""
        subject = opts.get("subject") or "来自礼帽小狗"
        if not (user and password):
            return {"ok": False, "platform": "email",
                    "message": "没填邮箱或授权码，先在设置里补上"}
        msg = EmailMessage()
        msg["From"] = user
        msg["To"] = peer
        msg["Subject"] = subject
        msg.set_content(content)
        try:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(user, password)
                s.send_message(msg)
        except Exception as exc:
            return {"ok": False, "platform": "email",
                    "message": "发送失败：%s" % exc}
        return {"ok": True, "platform": "email", "peer": peer,
                "message": "已发出"}


class WeChatAdapter(MessageAdapter):
    platform = "wechat"
    status = "待接入"

    def scope(self) -> str:
        return ("接口已定义，实现路径：(A) 企业微信 API（合规，需企业号）"
                "/ (B) 微信 PC 客户端桌面自动化 / (C) WeChaty 协议桥（有封号风险）。"
                "个人号无官方开放 API，默认不启用。")

    def send(self, peer: str, content: str, opts: dict | None = None) -> dict:
        return {"ok": False, "platform": "wechat", "status": self.status,
                "message": "微信代发还没接上。%s" % self.scope()}


class QQAdapter(MessageAdapter):
    platform = "qq"
    status = "待接入"

    def scope(self) -> str:
        return ("接口已定义，实现路径：(A) QQ PC 桌面自动化 / "
                "(B) onebot / go-cqhttp 协议桥（有风险）。"
                "个人号无官方 API，默认不启用。")

    def send(self, peer: str, content: str, opts: dict | None = None) -> dict:
        return {"ok": False, "platform": "qq", "status": self.status,
                "message": "QQ 代发还没接上。%s" % self.scope()}


class MessengerPlugin(Plugin):
    manifest = PluginManifest(
        id="messenger",
        name="消息代发",
        version="1.0.0",
        description="代发消息的统一入口（邮件可真发，微信/QQ 接口已备好待接入）",
    )

    def setup(self, ctx) -> None:
        self.ctx = ctx
        self.adapters = {
            "email": EmailAdapter(),
            "wechat": WeChatAdapter(),
            "qq": QQAdapter(),
        }

    def tools(self) -> list:
        return [
            ToolSpec(
                name="msg_send",
                description=("代发一条消息给指定联系人。内容是用户的本意，"
                             "不要加小狗的语气。外发属高风险，发送前会先请求确认。"),
                risk=RiskLevel.HIGH,
                # 外发出去就收不回来了，标成破坏性：调用方一眼能看出不可逆
                destructive=True,
                parameters={
                    "type": "object",
                    "properties": {
                        "platform": {"type": "string",
                                     "description": "email / wechat / qq"},
                        "peer": {"type": "string",
                                 "description": "收件人（邮箱地址 / 对方账号）"},
                        "content": {"type": "string",
                                    "description": "要发送的正文"},
                        "subject": {"type": "string",
                                    "description": "邮件主题（其它平台可留空）"},
                    },
                    "required": ["platform", "peer", "content"],
                },
                invoke=self._send,
                tags=["代发", "外发", "高风险"],
            ),
            ToolSpec(
                name="msg_scope",
                description="查各平台代发的实现范围与是否可用",
                risk=RiskLevel.LOW,
                parameters={"type": "object", "properties": {}},
                invoke=self._scope,
                tags=["代发", "只读"],
            ),
        ]

    def _send(self, args: dict) -> dict:
        platform = (args.get("platform") or "").lower()
        ad = self.adapters.get(platform)
        if ad is None:
            return {"ok": False,
                    "message": "不认识这个平台：%s（可用：%s）"
                               % (platform, "、".join(self.adapters))}
        peer = (args.get("peer") or "").strip()
        content = args.get("content") or ""
        if not peer or not content:
            return {"ok": False, "message": "peer 和 content 都不能空"}
        # 关键：代发正文绝不做人格包装
        opts = {k: v for k, v in (args.get("opts") or {}).items()}
        if args.get("subject"):
            opts["subject"] = args["subject"]
        return ad.send(peer, content, opts)

    def _scope(self, _args: dict) -> dict:
        return {"ok": True, "platforms": [
            {"platform": a.platform, "status": a.status, "scope": a.scope()}
            for a in self.adapters.values()]}
