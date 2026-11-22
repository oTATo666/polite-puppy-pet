# -*- coding: utf-8 -*-
"""
礼帽小狗 · Agent 运行时
=======================

职责：插件注册表 + 工具调度。

- 发现：扫描 plugins/ 目录，用 importlib 动态导入含 Plugin 子类的模块；
- 注册：调 plugin.tools() 把 ToolSpec 注入注册表，同名工具直接报错不覆盖；
- 热插拔：支持运行时 load / unload 单个插件，不影响其余模块；
- 版本约束：min_core_version 不满足就拒绝加载，保证升级不崩；
- 调度：一次调用走「选 → 校验 → 分级 → 确认 → 执行 → 回执」。
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
import time
from pathlib import Path
from typing import Any

from .plugin import (CORE_VERSION, Plugin, RiskLevel, ToolSpec,
                     core_version_ok, version_tuple)
from .safety import SafetyGate
from .trust import TrustList

PLUGIN_FILE_PREFIX = ""      # 扫描所有 .py
PLUGIN_MODULE_PREFIX = "puppy_agent.plugins"


class RuntimeContext:
    """注入给插件的上下文：配置、人格、数据目录、安全门。"""

    def __init__(self, data_dir: Path, safety: SafetyGate,
                 persona: dict | None = None, config: dict | None = None):
        self.data_dir = Path(data_dir)
        self.safety = safety
        self.persona = persona or {}
        self.config = config or {}
        # 插件可以往这里放东西，供其它插件/前端读取
        self.shared: dict = {}


class Registry:
    """工具注册表。"""

    def __init__(self):
        self._tools: dict = {}          # name -> ToolSpec
        self._plugins: dict = {}        # plugin_id -> Plugin

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError("工具名冲突：%s" % spec.name)
        self._tools[spec.name] = spec

    def unregister_plugin(self, plugin_id: str) -> int:
        """卸下某个插件的所有工具，返回卸掉几个。"""
        names = [n for n, s in self._tools.items() if s.plugin_id == plugin_id]
        for n in names:
            self._tools.pop(n, None)
        self._plugins.pop(plugin_id, None)
        return len(names)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list(self) -> list:
        return sorted(self._tools.values(), key=lambda s: s.name)

    def schemas(self) -> list:
        return [s.schema() for s in self.list()]

    def by_risk(self, risk: str) -> list:
        return [s for s in self.list() if s.risk.value == risk]

    @property
    def plugins(self) -> dict:
        return dict(self._plugins)


class AgentRuntime:
    """插件加载 + 工具调度。"""

    def __init__(self, plugins_dir: Path, data_dir: Path,
                 safety: SafetyGate | None = None,
                 persona: dict | None = None,
                 extra_dirs: list | None = None,
                 config: dict | None = None):
        self.plugins_dir = Path(plugins_dir)
        # 额外的插件目录（比如用户自己放的 plugins\），跟自带的一起扫
        self.extra_dirs = [Path(d) for d in (extra_dirs or [])]
        self.data_dir = Path(data_dir)
        self.safety = safety or SafetyGate(self.data_dir)
        self.ctx = RuntimeContext(self.data_dir, self.safety, persona, config)
        self.registry = Registry()
        # AG-1：插件信任清单——内置目录默认信任，用户目录默认禁用 +
        # 哈希绑定（改动自动禁用）。builtin_dir 取第一个插件目录（包内）。
        builtin = self.plugins_dir if not self.extra_dirs else self.plugins_dir
        self.trust = TrustList(self.data_dir, builtin_dir=builtin)
        self._plugin_file: dict = {}   # plugin_id -> 文件路径（内容变更时反查卸载）
        self.log: list = []      # 加载日志（给设置页看）

    # ------------------------------------------------------------ 插件发现
    def discover(self) -> list:
        """扫描 plugins/ 目录，返回可加载的模块文件列表。"""
        out = []
        seen = set()
        for root in [self.plugins_dir] + self.extra_dirs:
            if not root.is_dir():
                continue
            for p in sorted(root.glob("*.py")):
                if p.name.startswith("_"):
                    continue
                key = p.name.lower()
                if key in seen:      # 同名插件只加载一次，别互相覆盖
                    continue
                seen.add(key)
                out.append(p)
        return out

    def load_all(self) -> dict:
        """加载全部插件。返回统计。"""
        ok, fail = 0, 0
        for path in self.discover():
            r = self.load_plugin(path)
            if r.get("ok"):
                ok += 1
            else:
                fail += 1
        return {"ok": True, "loaded": ok, "failed": fail,
                "tools": len(self.registry.list())}

    def load_plugin(self, path: Path) -> dict:
        """加载单个插件文件。"""
        path = Path(path)
        # AG-1：信任检查前置——未确认/被禁用/内容变过的插件不加载。
        # 内容变过（changed）时连已注册的工具一起卸下：宁可停功能，
        # 也不能继续用旧代码顶替新文件。
        verdict = self.trust.decide(path)
        if not verdict.get("allow"):
            if verdict.get("changed"):
                try:
                    real = path.resolve()
                except OSError:
                    real = path
                for pid, f in list(self._plugin_file.items()):
                    if f == real:
                        self.unload_plugin(pid)
                        break
            msg = "跳过 %s：%s" % (path.name, verdict.get("reason"))
            self.log.append(msg)
            return {"ok": False, "message": msg, "skipped": True}

        mod_name = "%s.%s" % (PLUGIN_MODULE_PREFIX, path.stem)
        # 信任检查已确认这是当前磁盘内容——总是重新执行，不吃旧模块缓存
        sys.modules.pop(mod_name, None)
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("无法加载模块：%s" % path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        except Exception as exc:
            msg = "加载失败 %s：%s" % (path.name, exc)
            self.log.append(msg)
            return {"ok": False, "message": msg}

        # 找 Plugin 子类（排除基类本身）
        found = None
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if (issubclass(obj, Plugin) and obj is not Plugin
                    and obj.__module__ == mod_name):
                found = obj
                break
        if found is None:
            msg = "跳过 %s：没有 Plugin 子类" % path.name
            self.log.append(msg)
            return {"ok": False, "message": msg}

        try:
            inst = found()
        except Exception as exc:
            msg = "实例化失败 %s：%s" % (path.name, exc)
            self.log.append(msg)
            return {"ok": False, "message": msg}

        man = inst.manifest
        if not core_version_ok(man.min_core_version, CORE_VERSION):
            msg = "拒绝加载 %s：需要核心 >= %s，当前 %s" % (
                man.id, man.min_core_version, CORE_VERSION)
            self.log.append(msg)
            return {"ok": False, "message": msg}

        try:
            inst.setup(self.ctx)
            specs = inst.tools() or []
        except Exception as exc:
            msg = "插件初始化失败 %s：%s" % (man.id, exc)
            self.log.append(msg)
            return {"ok": False, "message": msg}

        added = 0
        for ts in specs:
            if not ts.plugin_id:
                ts.plugin_id = man.id
            try:
                self.registry.register(ts)
                added += 1
            except ValueError as exc:
                msg = "注册失败 %s：%s" % (man.id, exc)
                self.log.append(msg)
                return {"ok": False, "message": msg}

        self.registry._plugins[man.id] = inst
        try:
            self._plugin_file[man.id] = path.resolve()
        except OSError:
            self._plugin_file[man.id] = path
        msg = "已加载 %s v%s（%d 个工具）" % (man.name, man.version, added)
        self.log.append(msg)
        return {"ok": True, "message": msg, "plugin": man.id,
                "tools": added}

    def unload_plugin(self, plugin_id: str) -> dict:
        n = self.registry.unregister_plugin(plugin_id)
        msg = "已卸载 %s（%d 个工具）" % (plugin_id, n)
        self.log.append(msg)
        return {"ok": True, "message": msg, "removed": n}

    # ------------------------------------------------------------ 工具调度
    def call(self, name: str, args: dict | None = None,
             confirm_token: str = "", meta: dict | None = None) -> dict:
        """
        调用一个工具。完整走一遍：选 → 校验 → 分级 → 确认 → 执行 → 回执。
        返回统一 {ok:..., ...}。

        meta（AG-6）：执行链上下文（plan_id / step / planner / model /
        tainted），原样并入审计行——旧字段不变，新增字段只做兼容扩展。
        """
        args = args or {}
        spec = self.registry.get(name)
        if spec is None:
            return {"ok": False, "message": "没有这个工具：%s" % name}

        # 这次调用真正的风险级（插件可按参数升降，比如沙盒外读文件要确认）
        risk = self.safety.effective_risk(spec, args)
        extra_meta = dict(meta or {})

        errs = spec.validate(args)
        if errs:
            msg = "参数不对：%s" % "；".join(errs)
            self.safety.audit(name, args, risk.value, False, msg,
                              extra=extra_meta)
            return {"ok": False, "message": msg}

        verdict = self.safety.check(spec, args, confirm_token)
        if not verdict.get("ok"):
            self.safety.audit(name, args, risk.value, False,
                              verdict.get("message", ""),
                              extra={"action": verdict.get("action"),
                                     **extra_meta})
            out = {"ok": False, "message": verdict.get("message", "被安全层拦下")}
            if verdict.get("action") == "confirm":
                out["need_confirm"] = True
                out["confirm_token"] = verdict.get("confirm_token")
                out["risk"] = verdict.get("risk")
                out["summary"] = verdict.get("message")
            elif verdict.get("action") == "deny":
                out["denied"] = True
                out["risk"] = risk.value
            return out

        # 高风险 + 需要快照 → 先快照
        snap = None
        if spec.snapshot and risk == RiskLevel.HIGH:
            target = args.get("path") or args.get("file") or ""
            if target:
                snap = self.safety.snapshot(target)
                if not snap.get("ok"):
                    self.safety.audit(name, args, risk.value, False,
                                      "快照失败：%s" % snap.get("message"),
                                      extra=extra_meta)
                    return {"ok": False,
                            "message": "快照失败，已中止：%s" % snap.get("message")}

        t0 = time.time()
        try:
            res = spec.invoke(args) if spec.invoke else {"ok": False,
                                                         "message": "工具没有实现"}
        except Exception as exc:
            res = {"ok": False, "message": "执行出错：%s" % exc}
        ms = int((time.time() - t0) * 1000)

        ok = bool(res.get("ok")) if isinstance(res, dict) else bool(res)
        detail = ""
        if isinstance(res, dict):
            detail = res.get("message") or res.get("result") or ""
        self.safety.audit(name, args, risk.value, ok, detail, ms,
                          extra={"snapshot_id": (snap or {}).get("snapshot_id"),
                                 **extra_meta})
        if isinstance(res, dict):
            res.setdefault("ok", ok)
            res["ms"] = ms
            if snap and snap.get("ok"):
                res["snapshot_id"] = snap.get("snapshot_id")
            return res
        return {"ok": ok, "result": res, "ms": ms}

    def describe(self) -> dict:
        """给前端 / 设置页看的整体状态。"""
        return {
            "core_version": CORE_VERSION,
            "plugins_dir": str(self.plugins_dir),
            "plugins": [
                {"id": pid,
                 "name": getattr(p.manifest, "name", pid),
                 "version": getattr(p.manifest, "version", "?"),
                 "description": getattr(p.manifest, "description", "")}
                for pid, p in self.registry.plugins.items()
            ],
            "tools": self.registry.schemas(),
            "counts": {
                "total": len(self.registry.list()),
                "low": len(self.registry.by_risk("low")),
                "medium": len(self.registry.by_risk("medium")),
                "high": len(self.registry.by_risk("high")),
            },
            "log": self.log[-30:],
        }
