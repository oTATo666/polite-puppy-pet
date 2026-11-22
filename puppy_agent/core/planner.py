# -*- coding: utf-8 -*-
"""礼帽小狗 · 规划器接口（AG-2 / Agent 2.0）
=============================================

规划器回答一个问题：**这句话要不要动手、动哪个工具、参数是什么**。

接口（所有规划器共用）：
    plan(text: str) -> dict   # {"tool": str, "args": dict, "reason": str}

实现：
  · RulePlanner —— 规则路由（从 main.py::_agent_plan **原样搬来**）。
    确定性、可离线、可单测；默认模式（agentPlanner 默认 rule），
    对原有用例 100% 保持（对照测试锁死）。
  · ModelPlanner —— 模型规划（AG-3，model_planner.py），规则工具同一个接口。

main.py::_agent_plan 变成兼容壳（调 RulePlanner），旧调用点行为不变。
"""

from __future__ import annotations

import re


# 允许由规则自动规划的工具（全部只读）——从 main.py 搬来，保持一致
RULE_ALLOWED_TOOLS = ("fs_read", "fs_list", "mem_search", "mem_stats")

_FILE_EXT = (r"(?:txt|md|json|jsonl|log|py|html?|css|js|csv|tsv|ini|cfg"
             r"|bat|cmd|ps1|ya?ml|sql)")

# "你还记得…吗" 里要剥掉的套话，剩下的才算搜索词
_MEM_STRIP = re.compile(
    r"你还记得|你记不记得|还记得|帮我回忆|回忆一下|回想一下|搜一下记忆|"
    r"查一下记忆|记忆里有没有|记忆库里有没有|记得|吗|呢|吧|啊|呀")


class RulePlanner:
    """规则路由（确定性）。默认规划器；不联网、不调模型、行为逐条可对照。"""

    name = "rule"

    def plan(self, text: str) -> dict:
        return self._plan(str(text or ""))

    def _plan(self, text: str) -> dict:
        """原样搬自 main.py::_agent_plan（AG-2）。对照测试逐条锁死。"""
        t = text.strip()
        if not t:
            return {"tool": "", "args": {}, "reason": "空文本"}
        if len(t) > 200:
            return {"tool": "", "args": {}, "reason": "太长，按闲聊处理"}

        # 1) 读文件：必须出现带扩展名的文件名，才认为是要读文件
        #    路径里允许出现单/双引号（Windows 用户名里真的可能有，比如 o'brien）
        m = re.search(
            r"(?:读一下|读读|读|打开|看看|看一下|瞅瞅|翻翻)\s*[「『\"']?"
            r"([^\s「」『』，。]+?\.(?:" + _FILE_EXT + r"))", t, re.I)
        if m:
            return {"tool": "fs_read", "args": {"path": m.group(1).strip("\"'「」『』")},
                    "reason": "要读文件"}

        # 1b) 没有常规扩展名、但一看就是密钥/配置的文件：.ssh/config、id_rsa、.env。
        #     这些位置会被安全层一票否决；路由必须能走到那一步，给出明确的"不给读"，
        #     而不是当成闲聊——用户说了"读一下"，沉默是最糟的反馈。
        m_s = re.search(
            r"(?:读一下|读读|读|打开|看看|看一下|瞅瞅|翻翻)\s*[「『\"']?"
            r"([^\s「」『』，。]*[\\/](?:config|id_rsa|id_ed25519|known_hosts"
            r"|credentials|\.env|hosts))", t, re.I)
        if m_s:
            return {"tool": "fs_read", "args": {"path": m_s.group(1).strip("\"'「」『』")},
                    "reason": "要读密钥/配置文件"}

        # 2) 列目录：认"列一下 / 有哪些文件 / 目录里有什么"
        if re.search(r"列一下|列个|列出|有哪些文件|有什么文件|目录里有什么|"
                     r"文件夹里有什么|看看目录|列目录|目录下有什么", t):
            mp = re.search(r"([A-Za-z]:[\\/][^\s，。、]*|[^\s，。、]*\\[^\s，。、]+)", t)
            return {"tool": "fs_list", "args": {"path": mp.group(1) if mp else "."},
                    "reason": "要看目录"}

        # 3) 记忆统计
        if re.search(r"记忆库里?有多|记忆库有多|多少条记忆|记住多少|记了多少|"
                     r"记忆库状态|记忆统计|存了多少条", t):
            return {"tool": "mem_stats", "args": {}, "reason": "问记忆库规模"}

        # 4) 记忆搜索（放最后：前面几条都可能带"记得"字样）
        if re.search(r"你还记得|你记不记得|还记得|帮我回忆|回忆一下|回想一下|"
                     r"搜一下记忆|查一下记忆|记忆里有没有|记忆库里有没有", t):
            kw = _MEM_STRIP.sub(" ", t)
            kw = re.sub(r"[\s？?。！!，,、；;：:「」『』\"']+", " ", kw).strip()
            return {"tool": "mem_search", "args": {"q": kw[:30] or t[:20]},
                    "reason": "要翻记忆库"}

        return {"tool": "", "args": {}, "reason": "不需要动手"}


def rule_planner() -> RulePlanner:
    return RulePlanner()
