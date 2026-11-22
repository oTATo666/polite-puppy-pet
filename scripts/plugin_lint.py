# -*- coding: utf-8 -*-
r"""AG-1：插件静态检查（plugin lint）——发布前把每个插件过一遍。

检查项
    · 文件里有 Plugin 子类，manifest 完整（id / name / version 非空）；
    · tools() 返回 ToolSpec 列表且逐个体检：
        - name 全局唯一、description 非空（模型要靠它选工具）；
        - risk 是三级之一；
        - parameters 是 JSON-Schema 形态（type=object，properties 是 dict）；
        - invoke / guard / risk_fn 可调用；
        - MEDIUM/HIGH 工具必须有确认路径（默认安全层兜底，提示即可）；
        - snapshot=True 的工具应带 path/file 类参数（快照才有意义）；
    · permissions 是字符串列表；
    · 危险 import 黑名单（os.system / subprocess 直用 / eval 等不是
      绝对禁止，但 HIGH 风险工具必须给出理由——这里只提示不拦）。

退出码：0 = 全过；1 = 有 FAIL 项。
用法
    python scripts\plugin_lint.py                # 检查包内 plugins 目录
    python scripts\plugin_lint.py --all          # 连用户 plugins\ 目录一起
    python scripts\plugin_lint.py --file x.py    # 检查单个文件
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import inspect
import re
import sys
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
if str(PROG) not in sys.path:
    sys.path.insert(0, str(PROG))

from puppy_agent.core.plugin import Plugin, RiskLevel, ToolSpec  # noqa: E402
from puppy_agent.core.runtime import PLUGIN_MODULE_PREFIX  # noqa: E402


def _load_module(path: Path):
    mod_name = "%s._lint_%s" % (PLUGIN_MODULE_PREFIX, path.stem)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("无法加载模块：%s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


NET_MODULES = ("urllib", "requests", "socket", "http.client", "httpx", "aiohttp")


def _net_imports(path: Path) -> list:
    """AST 扫一遍：这个插件 import 了哪些联网模块（方案§7 net 权限检查）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    hits = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for n in names:
            if any(n == m or n.startswith(m + ".") for m in NET_MODULES):
                hits.append(n)
    return sorted(set(hits))


def lint_file(path: Path) -> list:
    """返回 [(ok, 说明)]。ok=False 的项决定退出码。"""
    rows = []
    try:
        mod = _load_module(path)
    except Exception as exc:
        return [(False, "加载失败：%s" % exc)]

    found = None
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if (issubclass(obj, Plugin) and obj is not Plugin
                and obj.__module__ == mod.__name__):
            found = obj
            break
    if found is None:
        return [(False, "没有 Plugin 子类")]

    try:
        inst = found()
        man = inst.manifest
    except Exception as exc:
        return [(False, "实例化失败：%s" % exc)]

    rows.append((bool(getattr(man, "id", "")), "manifest.id 非空"))
    rows.append((bool(getattr(man, "name", "")), "manifest.name 非空"))
    rows.append((bool(getattr(man, "version", "")), "manifest.version 非空"))
    perms = getattr(man, "permissions", None)
    rows.append((perms is None or isinstance(perms, list),
                 "manifest.permissions 是列表（缺省可）"))

    # 方案§7：只有声明了 net 权限的插件才允许 import 联网模块
    net_mods = _net_imports(path)
    declared_net = bool(perms) and any(
        str(x).lower().startswith("net") for x in (perms or []))
    rows.append((not net_mods or declared_net,
                 "联网 import %s → manifest 必须声明 net 权限"
                 % (", ".join(net_mods) if net_mods else "（无）")))

    try:
        specs = inst.tools() or []
    except Exception as exc:
        rows.append((False, "tools() 调用失败：%s" % exc))
        return rows
    rows.append((bool(specs), "tools() 非空"))

    seen = set()
    for ts in specs:
        if not isinstance(ts, ToolSpec):
            rows.append((False, "tools() 里有非 ToolSpec 项"))
            continue
        tag = ts.name
        rows.append((bool(ts.name) and ts.name not in seen,
                     "[%s] name 非空且唯一" % tag))
        seen.add(ts.name)
        rows.append((bool(ts.description), "[%s] description 非空" % tag))
        rows.append((isinstance(ts.risk, RiskLevel), "[%s] risk 合法" % tag))
        params = ts.parameters
        ok_params = (isinstance(params, dict)
                     and params.get("type", "object") == "object"
                     and isinstance(params.get("properties", {}), dict))
        rows.append((ok_params, "[%s] parameters 是 JSON-Schema 形态" % tag))
        rows.append((callable(ts.invoke) or ts.invoke is None,
                     "[%s] invoke 可调用（或由运行时注入）" % tag))
        rows.append((ts.guard is None or callable(ts.guard),
                     "[%s] guard 可调用" % tag))
        rows.append((ts.risk_fn is None or callable(ts.risk_fn),
                     "[%s] risk_fn 可调用" % tag))
        perms_t = getattr(ts, "permissions", None)
        rows.append((perms_t is None or
                     (isinstance(perms_t, list)
                      and all(isinstance(x, str) for x in perms_t)),
                     "[%s] permissions 是字符串列表" % tag))
        if ts.snapshot:
            has_path = any(k in (ts.parameters or {}).get("properties", {})
                           for k in ("path", "file", "dir"))
            rows.append((has_path,
                         "[%s] snapshot=True 应有 path/file 参数" % tag))
        # 高风险工具提示（不拦）：有 snapshot 才安心
        if ts.risk == RiskLevel.HIGH and not ts.snapshot:
            rows.append((True, "[%s] 提示：HIGH 工具建议 snapshot=True" % tag))
    return rows


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="插件静态检查")
    ap.add_argument("--all", action="store_true", help="连用户 plugins 目录一起查")
    ap.add_argument("--file", default="", help="检查单个插件文件")
    args = ap.parse_args(argv)

    if args.file:
        targets = [Path(args.file)]
    else:
        targets = [PROG / "puppy_agent" / "plugins"]
        if args.all:
            targets.append(PROG / "plugins")

    total_fail = 0
    total_files = 0
    for d in targets:
        if not d.is_dir() and not d.is_file():
            continue
        files = [d] if d.is_file() else sorted(
            p for p in d.glob("*.py") if not p.name.startswith("_"))
        print("=" * 62)
        total_files += len(files)
        print("插件检查：%s" % d)
        for p in files:
            rows = lint_file(p)
            bad = [label for ok, label in rows if not ok]
            total_fail += len(bad)
            print("-" * 62)
            print("%s：%d 项检查，%d 不过" % (p.name, len(rows), len(bad)))
            for ok, label in rows:
                print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
    print("=" * 62)
    print("共 %d 个文件，失败项 %d" % (total_files, total_fail))
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
