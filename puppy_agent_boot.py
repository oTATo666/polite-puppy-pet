# -*- coding: utf-8 -*-
r"""
礼帽小狗 · Agent 包引导
========================

为什么要有这个文件？

`puppy_agent` 是一套**可插拔工具**的插件包。源码运行时它就是项目里的一个
普通目录，`import puppy_agent.core.runtime` 直接就能用。

但打包成 exe 后有两种麻烦：

1. PyInstaller 只认「静态 import」，动态加载的 .py 不会被收进 PYZ；
2. 我们不想让它进 PYZ —— 因为插件要能热插拔，必须以**磁盘文件**的形式存在。

所以这里的做法是：

- 打包时用 `--add-data "puppy_agent;puppy_agent"` 把整个包原样拷到解包目录；
- 运行时由本文件动态把 `puppy_agent` 挂到 sys.path / sys.modules 上，
  再 import 它的子模块。

这样 exe 里和源码里的行为完全一致：改插件 .py 重启就生效，不用重打包。
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

# 本文件所在目录：源码=项目根；打包后=exe 旁边的引导文件目录（很少见，
# 因为引导逻辑被内联进 main.py 时用的还是这个相对定位）
_HERE = Path(__file__).resolve().parent


def _pkg_dir() -> Path:
    """找到 puppy_agent 包目录（源码根目录 / exe 解包目录）。"""
    for cand in (_HERE / "puppy_agent",
                 Path(sys.executable).resolve().parent / "puppy_agent",
                 Path(getattr(sys, "_MEIPASS", "")) / "puppy_agent"):
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return _HERE / "puppy_agent"


def _make_shims(pkg: Path) -> None:
    """手工造三个"空壳包"，让相对导入能落到磁盘上的 .py 文件。"""
    for name, path in (("puppy_agent", pkg),
                       ("puppy_agent.core", pkg / "core"),
                       ("puppy_agent.plugins", pkg / "plugins")):
        if name in sys.modules:
            continue
        mod = types.ModuleType(name)
        mod.__path__ = [str(path)]
        mod.__package__ = name
        mod.__file__ = str(path / "__init__.py")
        sys.modules[name] = mod
    setattr(sys.modules["puppy_agent"], "core", sys.modules["puppy_agent.core"])
    setattr(sys.modules["puppy_agent"], "plugins",
            sys.modules["puppy_agent.plugins"])


def boot() -> dict:
    """
    把 puppy_agent 装好。返回一份说明，失败不抛异常（桌宠本体不能因此挂掉）。

    返回：{"ok":bool, "pkg":str, "error":str, "modules":{...}}
    """
    pkg = _pkg_dir()
    parent = str(pkg.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    out = {"ok": False, "pkg": str(pkg), "error": "", "modules": {}}
    if not pkg.is_dir():
        out["error"] = "找不到 puppy_agent 包目录：%s" % pkg
        return out

    # 先试正常导入；不行就用空壳包顶上（exe + --add-data 的典型情况）
    try:
        importlib.import_module("puppy_agent.core.runtime")
    except Exception:
        # 半路失败的残留要先清掉，否则会跟空壳包打架
        for key in [k for k in list(sys.modules) if k.startswith("puppy_agent")]:
            sys.modules.pop(key, None)
        try:
            _make_shims(pkg)
            importlib.import_module("puppy_agent.core.runtime")
        except Exception as exc:
            out["error"] = "加载 puppy_agent 失败：%s" % exc
            return out

    for name in ("puppy_agent.core.plugin", "puppy_agent.core.safety",
                 "puppy_agent.core.runtime", "puppy_agent.core.personality"):
        try:
            importlib.import_module(name)
            out["modules"][name] = True
        except Exception as exc:
            out["modules"][name] = str(exc)
    out["ok"] = True
    return out


def plugin_dirs(extra: list | None = None) -> list:
    """插件目录列表：包内自带 + 用户自己放的（exe 旁边的 plugins\）。"""
    pkg = _pkg_dir()
    dirs = [pkg / "plugins"]
    for p in (extra or []):
        try:
            pp = Path(p)
        except TypeError:
            continue
        if pp.is_dir() and pp not in dirs:
            dirs.append(pp)
    return dirs


if __name__ == "__main__":
    info = boot()
    print(info)
