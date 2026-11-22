# -*- coding: utf-8 -*-
"""
礼帽小狗 · Agent 工具契约
=========================

所有对外能力都实现为自描述的 ToolSpec，Agent 只认契约，不关心实现细节。
契约是向后兼容的：ToolSpec 只增字段不改旧字段，新增字段一律带默认值。

设计依据：《桌宠Agent_可插拔架构设计》§3。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

# 核心版本号：插件用它声明最低兼容版本
# 1.1.0（AG-1/SDK v2）：ToolSpec 新增 permissions（工具声明自己需要的权限，
# 默认空 = 不需要额外权限）、信任清单（trust.py）、插件 lint。
CORE_VERSION = "1.1.0"


class RiskLevel(str, Enum):
    """工具风险等级 —— 决定执行前要不要跟用户确认。"""

    LOW = "low"        # 只读 / 本地创建：可直接执行
    MEDIUM = "medium"  # 修改 / 移动 / 联网：执行前摘要确认
    HIGH = "high"      # 删除 / 外发 / 付费：必须显式授权


# 风险等级排序，用于取最大值
_RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


def max_risk(*levels: RiskLevel) -> RiskLevel:
    """取多个风险等级里最高的那个。"""
    best = RiskLevel.LOW
    for lv in levels:
        if _RISK_ORDER[lv] > _RISK_ORDER[best]:
            best = lv
    return best


@dataclass
class ToolSpec:
    """一个工具的自描述契约。"""

    name: str                                   # 全局唯一，如 fs_read
    description: str                            # 给模型看的自然语言说明
    risk: RiskLevel = RiskLevel.LOW             # 风险等级
    parameters: dict = field(default_factory=dict)   # JSON-Schema，用于校验
    invoke: Callable[[dict], dict] | None = None      # 入参 dict -> 出参 dict（含 ok）
    # ---- 以下为向后兼容的扩展字段（都有默认值，不影响旧插件） ----
    plugin_id: str = ""                         # 归属插件 id
    destructive: bool = False                   # 是否破坏性（删除/覆盖）
    snapshot: bool = False                      # 执行前是否需要快照
    tags: list = field(default_factory=list)   # 便于筛选，如 ["文件", "只读"]
    # 硬拒绝（P0-2）：返回非空字符串 = 直接拒绝，连确认都不给。
    # 用途是「无论用户怎么授权都不该放行」的路径，比如 .ssh / AppData / .env。
    guard: Callable[[dict], str] | None = None
    # 动态风险（P0-2）：同一个工具按参数调整风险级。
    # 例：fs_read 在沙盒内是 LOW（直接读），出了沙盒就是 MEDIUM（先确认）。
    risk_fn: Callable[[dict], "RiskLevel"] | None = None
    # SDK v2（AG-1）：本工具声明需要的权限（如 "app_open"、"clipboard"）。
    # 默认空 = 不需要额外权限；运行时可按权限启用/停用整组工具。
    permissions: list = field(default_factory=list)
    # SDK v2（方案§7）：数据分级——public（可上云）/ local_only（默认，不外发）/
    # secret（记忆、凭据）。规划器与观察压缩按它决定能不能把结果给云端看。
    data_class: str = "local_only"
    # SDK v2（方案§7）：幂等工具可以安全重试/重复调用（读类为 True）
    idempotent: bool = False
    # SDK v2（方案§7）：单次执行超时（秒），0 = 不限制
    timeout_s: int = 10
    # SDK v2（方案§7）：给模型的参数示例（≤2 条，只放合成数据）
    examples: list = field(default_factory=list)

    def schema(self) -> dict:
        """转成给模型 / 前端看的字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk.value,
            "parameters": self.parameters or {"type": "object", "properties": {}},
            "plugin_id": self.plugin_id,
            "destructive": self.destructive,
            "snapshot": self.snapshot,
            "tags": list(self.tags),
            "permissions": list(self.permissions),   # SDK v2（AG-1）
            "data_class": str(self.data_class),      # SDK v2（方案§7）
            "idempotent": bool(self.idempotent),
            "timeout_s": int(self.timeout_s),
            "examples": list(self.examples)[:2],     # 方案§7：至多 2 条
        }

    # ---- 参数校验：只做最朴素的结构检查，不引第三方依赖 ----
    def validate(self, args: dict) -> list:
        """返回错误信息列表，空列表表示通过。"""
        errs = []
        args = args or {}
        if not isinstance(args, dict):
            return ["参数必须是对象"]
        props = (self.parameters or {}).get("properties") or {}
        required = (self.parameters or {}).get("required") or []
        for key in required:
            if key not in args or args[key] in (None, ""):
                errs.append("缺少必填参数：%s" % key)
        for key, val in args.items():
            spec = props.get(key)
            if not spec:
                continue
            want = spec.get("type")
            if want == "string" and not isinstance(val, str):
                errs.append("参数 %s 必须是字符串" % key)
            elif want == "integer" and not isinstance(val, int):
                errs.append("参数 %s 必须是整数" % key)
            elif want == "number" and not isinstance(val, (int, float)):
                errs.append("参数 %s 必须是数字" % key)
            elif want == "boolean" and not isinstance(val, bool):
                errs.append("参数 %s 必须是布尔" % key)
            elif want == "array" and not isinstance(val, list):
                errs.append("参数 %s 必须是数组" % key)
        return errs


@dataclass
class PluginManifest:
    """插件自描述信息。"""

    id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str = "unknown"
    min_core_version: str = CORE_VERSION
    enabled_by_default: bool = True
    # SDK v2（AG-1）：插件声明需要的全部权限（工具 permissions 的并集可超出它）
    permissions: list = field(default_factory=list)


class Plugin:
    """所有插件的基类。子类实现 tools()，可选实现 setup()。"""

    manifest: PluginManifest = PluginManifest(id="base", name="base")

    def tools(self) -> list:
        """返回本插件提供的 ToolSpec 列表。"""
        return []

    def setup(self, ctx) -> None:  # noqa: D401 - 子类可选实现
        """注入配置 / 人格 / 网关等运行时上下文。"""


def parse_manifest(obj: dict) -> PluginManifest:
    """从 dict 构造 manifest，缺字段用默认值补齐。"""
    return PluginManifest(
        id=str(obj.get("id") or "unknown"),
        name=str(obj.get("name") or obj.get("id") or "unknown"),
        version=str(obj.get("version") or "1.0.0"),
        description=str(obj.get("description") or ""),
        author=str(obj.get("author") or "unknown"),
        min_core_version=str(obj.get("min_core_version") or CORE_VERSION),
        enabled_by_default=bool(obj.get("enabled_by_default", True)),
    )


def version_tuple(v: str) -> tuple:
    """把 '1.2.3' 解析成 (1,2,3)，解析不了的部分当 0。"""
    out = []
    for part in str(v).split("."):
        m = re.search(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def core_version_ok(min_need: str, core: str = CORE_VERSION) -> bool:
    """核心版本是否满足插件的最低要求。"""
    return version_tuple(core) >= version_tuple(min_need)


def dumps(obj: Any) -> str:
    """统一 JSON 序列化（中文不转义）。"""
    return json.dumps(obj, ensure_ascii=False, indent=2)
