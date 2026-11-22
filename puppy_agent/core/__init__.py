# -*- coding: utf-8 -*-
"""礼帽小狗 · Agent 核心包。

分层：
  core/plugin.py      工具契约（ToolSpec / PluginManifest / RiskLevel）
  core/safety.py      安全层（风险分级 / 确认 / 审计 / 快照）
  core/runtime.py     运行时（注册表 / 插件热加载 / 工具调度）
  core/personality.py 人格风格层（只管"怎么说"）
  plugins/            外部能力插件
"""

from .plugin import (CORE_VERSION, Plugin, PluginManifest, RiskLevel,
                     ToolSpec, core_version_ok, version_tuple)
from .personality import load_persona, persona_prompt, style_hint
from .runtime import AgentRuntime, Registry, RuntimeContext
from .safety import SafetyGate

__all__ = [
    "CORE_VERSION", "Plugin", "PluginManifest", "RiskLevel", "ToolSpec",
    "core_version_ok", "version_tuple", "load_persona", "persona_prompt",
    "style_hint", "AgentRuntime", "Registry", "RuntimeContext", "SafetyGate",
]
