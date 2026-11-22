# -*- coding: utf-8 -*-
"""
插件：文件工具
==============

真实可用的文件操作能力。风险分级严格按"读 / 写 / 删"区分，
删除走快照 + 高风险确认。

沙盒（P0-2）
------------
原来 allow_roots 为空时回退到 Path.home()，等于把整个用户目录（含 .ssh、
AppData、浏览器配置、本项目的 .env 和 memory.db）都开放给 Agent 读，而
fs_read / fs_list 是 LOW 风险，安全层直接放行、连确认都没有。读到的内容
还会拼进对话上下文，一旦配了云端模型就发出去了。

现在的规则是：
  1. 默认只用沙盒 —— agent_data\\workspace，第一次用到时自动创建；
  2. 敏感位置一票否决 —— .ssh / AppData / 浏览器 profile / .env / *.db /
     voices（声纹）等，写了 allow_roots 也不给读；
  3. 沙盒外的普通位置 —— 降为「需确认」，用户点过确认才读；
  4. 读到的内容一律标记 untrusted，拼进 prompt 时要求当成不可信数据。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..core.plugin import Plugin, PluginManifest, RiskLevel, ToolSpec

# 显式授权的目录（配置或环境变量，os.pathsep 分隔）。
# **故意没有默认值**：不写就是「只有沙盒」，不再回退到用户主目录。
ALLOW_ROOTS_ENV = "PUPPY_AGENT_ALLOW_ROOTS"

# 沙盒目录名：<agent_data>\\workspace
SANDBOX_NAME = "workspace"

# 一票否决：路径里出现这些目录名就不给读
SENSITIVE_PARTS = {
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".git",
    "appdata", "application data", "记忆", "voices",
}
# 一票否决：路径里出现这些片段（小写、正斜杠）就不给读
SENSITIVE_MARKERS = (
    "chrome/user data", "edge/user data", "brave-browser/user data",
    "firefox/profiles", "opera software", "microsoft/credentials",
    "google/chrome", "microsoft/edge",
)
# 一票否决：这些后缀 / 文件名（记忆库、凭据、cookie）
SENSITIVE_SUFFIXES = {".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3"}
# N-6：日记也进来——里面是最私人的记录，Agent 读了就会拼进对话上下文，
# 配了云端模型等于把日记发出去。
SENSITIVE_NAMES = {".env", "login data", "cookies", "id_rsa", "id_ed25519",
                   "memory.db", "settings.json", "memory_diary.txt"}


def _sandbox(ctx) -> Path:
    """Agent 的默认工作目录：<agent_data>\\workspace。"""
    root = Path(getattr(ctx, "data_dir", None) or Path.cwd())
    d = root / SANDBOX_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # 必须 resolve：data_dir 可能是 8.3 短路径（带 ~1 的那种），
    # 而待检查的路径已经展开成长路径，不归一化会比不出"在沙盒里"。
    try:
        return d.resolve()
    except OSError:
        return d


def _allowed_roots(ctx) -> list:
    """用户显式授权的目录。不配就是空 —— 空 = 只用沙盒。"""
    raw = (ctx.config or {}).get("allow_roots") or os.environ.get(ALLOW_ROOTS_ENV) or ""
    roots = [Path(p) for p in str(raw).split(os.pathsep) if str(p).strip()]
    out = []
    for r in roots:
        try:
            out.append(r.expanduser().resolve())
        except OSError:
            continue
    return out


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sensitive(path: Path) -> str:
    """敏感位置检查。返回非空字符串 = 原因（直接拒绝，不给确认机会）。"""
    low = path.as_posix().lower()
    for part in path.parts:
        if part.lower() in SENSITIVE_PARTS:
            return ("%s 里有敏感目录「%s」，不读也不列（.ssh / AppData / "
                    "浏览器配置 / 记忆库 / 声纹目录一律不开放）" % (path, part))
    for mark in SENSITIVE_MARKERS:
        if mark in low:
            return "%s 落在浏览器/凭据配置目录里，不开放读取" % path
    name = path.name.lower()
    if name in SENSITIVE_NAMES:
        return "%s 是密钥/记忆库/配置文件，不开放读取" % path
    if name.startswith(".env.") and not name.endswith(".example"):
        return "%s 是环境变量文件（可能含 Key），不开放读取" % path
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return "%s 是数据库文件（记忆库里是真人对话），不开放读取" % path
    return ""


def _data_root(ctx) -> Path | None:
    """硬拒绝范围的根：完整数据目录 data_dir()（N-19）。

    里面有：日记、记忆库、agent_data（审计/快照/沙盒）、日志……
    主程序通过 config["app_data_dir"] 把这一层告诉插件；没配置的老环境
    才退回 ctx.data_dir（agent_data 那层），总比没有强。

    沙盒（agent_data\\workspace）不在这条检查里被拒——它在 _probe 里
    先于本检查放行。
    """
    cfg = getattr(ctx, "config", None) or {}
    raw = (cfg.get("app_data_dir") if isinstance(cfg, dict) else None) \
        or getattr(ctx, "data_dir", None)
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except OSError:
        return Path(raw)


def _probe(ctx, path: str) -> dict:
    """判定一个路径怎么处理。

    返回 {"path": Path|None, "err": str, "confirm": bool}
      err 非空   → 直接拒绝
      confirm    → 不在沙盒里，读取需要用户确认
    """
    raw = str(path or "").strip()
    if not raw:
        return {"path": None, "err": "缺 path 参数", "confirm": False}
    try:
        p = Path(raw).expanduser().resolve()
    except OSError as exc:
        return {"path": None, "err": "路径无效：%s" % exc, "confirm": False}

    sandbox = _sandbox(ctx)
    if _under(p, sandbox):
        return {"path": p, "err": "", "confirm": False}

    # N-19：完整数据目录里除 workspace 沙盒外一律硬拒绝——日记、记忆库、
    # agent_data（审计/快照）、日志全在这，Agent 没有任何理由碰它们；
    # 就算把 allow_roots 配成这里也不行（这一条在敏感检查之前，不给确认机会）。
    data_root = _data_root(ctx)
    if data_root is not None and _under(p, data_root):
        return {"path": p,
                "err": ("%s 在程序数据目录里（日记/记忆库/Agent 数据的所在地），"
                        "不开放给 Agent 读写") % p,
                "confirm": False}

    why = _sensitive(p)
    if why:
        return {"path": p, "err": why, "confirm": False}

    for root in _allowed_roots(ctx):
        if _under(p, root):
            return {"path": p, "err": "", "confirm": False}

    return {"path": p, "err": "", "confirm": True}


class FileToolsPlugin(Plugin):
    manifest = PluginManifest(
        id="filesystem",
        name="文件工具",
        version="1.1.0",
        description="沙盒内文件的读取、列表、写入与删除（越界需确认，敏感路径拒绝）",
    )

    def setup(self, ctx) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------- 风险判定
    def _guard_path(self, args: dict) -> str:
        """硬拒绝：敏感路径连确认都不给。"""
        return _probe(self.ctx, args.get("path", ""))["err"]

    def _risk_read(self, args: dict) -> RiskLevel:
        """沙盒外读文件 → 升为需确认。"""
        return RiskLevel.MEDIUM if _probe(self.ctx, args.get("path", ""))["confirm"] \
            else RiskLevel.LOW

    def tools(self) -> list:
        return [
            ToolSpec(
                name="fs_read",
                description="读取一个文本文件的内容（沙盒内直接读，越界需确认）",
                risk=RiskLevel.LOW,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "文件的绝对路径"},
                        "max_chars": {"type": "integer",
                                      "description": "最多返回多少字符，默认 4000"},
                    },
                    "required": ["path"],
                },
                invoke=self._read,
                tags=["文件", "只读"],
                guard=self._guard_path,
                risk_fn=self._risk_read,
            ),
            ToolSpec(
                name="fs_list",
                description="列出某个目录下的文件（沙盒内直接列，越界需确认）",
                risk=RiskLevel.LOW,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "目录的绝对路径"},
                        "max_items": {"type": "integer",
                                      "description": "最多返回多少项，默认 100"},
                    },
                    "required": ["path"],
                },
                invoke=self._list,
                tags=["文件", "只读"],
                guard=self._guard_path,
                risk_fn=self._risk_read,
            ),
            ToolSpec(
                name="fs_write",
                description="写入或覆盖一个文本文件（会改掉原内容）",
                risk=RiskLevel.MEDIUM,
                destructive=True,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "文件的绝对路径"},
                        "content": {"type": "string",
                                    "description": "要写入的文本内容"},
                        "append": {"type": "boolean",
                                   "description": "true 表示追加，false 表示覆盖"},
                    },
                    "required": ["path", "content"],
                },
                invoke=self._write,
                tags=["文件", "写"],
                guard=self._guard_path,
            ),
            ToolSpec(
                name="fs_delete",
                description="删除一个文件或目录（破坏性，执行前会先做快照）",
                risk=RiskLevel.HIGH,
                destructive=True,
                snapshot=True,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "要删除的文件或目录的绝对路径"},
                    },
                    "required": ["path"],
                },
                invoke=self._delete,
                tags=["文件", "删除"],
                guard=self._guard_path,
            ),
        ]

    # ------------------------------------------------------------- 具体实现
    def _read(self, args: dict) -> dict:
        probe = _probe(self.ctx, args.get("path", ""))
        if probe["err"]:
            return {"ok": False, "message": probe["err"], "denied": True}
        p = probe["path"]
        if not p.is_file():
            return {"ok": False, "message": "文件不存在：%s" % p}
        # N-6：只读前 max_chars 字符——以前是全文 read_text 再切片，一个
        # 几百 MB 的日志/导出文件会被整个读进内存、然后整段喂给模型。
        try:
            limit = int(args.get("max_chars") or 4000)
        except (TypeError, ValueError):
            limit = 4000
        limit = max(1, min(limit, 200000))
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(limit)
                truncated = bool(f.read(1))   # 还能再读出字符 = 没读全
        except OSError as exc:
            return {"ok": False, "message": "读取失败：%s" % exc}
        # untrusted：文件内容是别人写的，里面可能藏着"忽略上面的指令"这类话
        return {"ok": True, "path": str(p), "chars": len(text),
                "truncated": truncated, "content": text,
                "untrusted": True, "outside_sandbox": bool(probe["confirm"])}

    def _list(self, args: dict) -> dict:
        probe = _probe(self.ctx, args.get("path", ""))
        if probe["err"]:
            return {"ok": False, "message": probe["err"], "denied": True}
        p = probe["path"]
        if not p.is_dir():
            return {"ok": False, "message": "不是目录：%s" % p}
        limit = int(args.get("max_items") or 100)
        items = []
        try:
            for child in sorted(p.iterdir(), key=lambda x: x.name):
                try:
                    size = child.stat().st_size
                    is_dir = child.is_dir()
                except OSError:
                    size, is_dir = 0, False
                items.append({"name": child.name, "dir": is_dir, "size": size})
                if len(items) >= limit:
                    break
        except OSError as exc:
            return {"ok": False, "message": "列目录失败：%s" % exc}
        return {"ok": True, "path": str(p), "count": len(items), "items": items,
                "untrusted": True, "outside_sandbox": bool(probe["confirm"])}

    def _write(self, args: dict) -> dict:
        probe = _probe(self.ctx, args.get("path", ""))
        if probe["err"]:
            return {"ok": False, "message": probe["err"], "denied": True}
        p = probe["path"]
        content = args.get("content") or ""
        append = bool(args.get("append"))
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if append and p.is_file():
                with open(p, "a", encoding="utf-8") as f:
                    f.write(content)
            else:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
        except OSError as exc:
            return {"ok": False, "message": "写入失败：%s" % exc}
        return {"ok": True, "path": str(p), "bytes": len(content.encode("utf-8")),
                "append": append}

    def _delete(self, args: dict) -> dict:
        probe = _probe(self.ctx, args.get("path", ""))
        if probe["err"]:
            return {"ok": False, "message": probe["err"], "denied": True}
        p = probe["path"]
        if not p.exists():
            return {"ok": False, "message": "路径不存在：%s" % p}
        # 兜底：不允许删整个盘根
        if str(p) in ("/", "C:\\", "C:/") or len(p.parts) <= 2:
            return {"ok": False, "message": "拒绝删除这么危险的位置：%s" % p}
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        except OSError as exc:
            return {"ok": False, "message": "删除失败：%s" % exc}
        return {"ok": True, "path": str(p), "deleted": True}
