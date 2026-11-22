# -*- coding: utf-8 -*-
"""
礼帽小狗 · 安全层
=================

职责：决定一次工具调用到底放不放行。

原则（对齐两份设计文档）：
1. 默认拒绝 —— 没显式授权的能力一律不执行；
2. 三级风险 —— 低风险自动执行；中风险要摘要确认；高风险要显式授权 + 快照；
3. 留痕 —— 谁、何时、调了什么、结果如何，全部落盘可查；
4. 可回滚 —— 破坏性操作前先快照，失败或后悔能还原。

重要：人格（她的毒舌）不参与这里的判断。人格只管"怎么说"，
执行与否只看这里的风险分级与用户的显式指令。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .plugin import RiskLevel, ToolSpec

# 确认令牌的有效期（秒）：过了就得重新确认
TOKEN_TTL = 300

# 快照上限（P0-2）：目录太大就不做快照，直接中止删除。
# 不设这个的话，一句"把下载文件夹删了"会让程序把几十 GB 拷进 .pet_trash。
SNAPSHOT_MAX_FILES = 2000
SNAPSHOT_MAX_BYTES = 200 * 1024 * 1024


def _now_ms() -> int:
    return int(time.time() * 1000)


class SafetyGate:
    """风险分级 + 确认 + 审计 + 快照。"""

    def __init__(self, data_dir: Path, *, enabled: bool = True,
                 audit_name: str = "agent_audit.log",
                 trash_name: str = ".pet_trash"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit_file = self.data_dir / audit_name
        self.trash_dir = self.data_dir / trash_name
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        self.enabled = enabled
        # 待确认的令牌：token -> {tool, args, risk, expires}
        self._pending: dict = {}
        # 只读白名单：即使被标成高风险也不拦（如"读一个文件"）
        self.readonly_names: set = set()
        # 用户显式授权过的工具（会话内）
        self.granted: set = set()

    # ------------------------------------------------------------------ 审计
    def audit(self, tool: str, args: dict, risk: str, ok: bool,
              detail: str = "", ms: int = 0, extra: dict | None = None) -> None:
        """把一次调用写进审计日志（JSONL，一行一条）。"""
        rec = {
            "ts": _now_ms(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": tool,
            "args": _safe_args(args),
            "risk": risk,
            "ok": bool(ok),
            "detail": str(detail)[:500],
            "ms": int(ms),
        }
        if extra:
            rec.update(extra)
        try:
            with open(self.audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass   # 审计写不进去也不能挡住正事

    def audit_tail(self, n: int = 50) -> list:
        """读最近 n 条审计记录（新的在后）。"""
        try:
            lines = self.audit_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return list(reversed(out))

    def audit_clear(self) -> int:
        """清空审计日志，返回清掉的条数。"""
        try:
            n = len(self.audit_file.read_text(encoding="utf-8").splitlines())
        except OSError:
            return 0
        try:
            self.audit_file.write_text("", encoding="utf-8")
        except OSError:
            pass
        return n

    # ------------------------------------------------------------------ 快照
    def _too_big(self, src: Path) -> str:
        """目录太大就返回原因（非空 = 不许快照）。只看大小和文件数，不读内容。"""
        files = 0
        total = 0
        try:
            for root, _dirs, names in os.walk(src):
                for name in names:
                    files += 1
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        continue
                    if files > SNAPSHOT_MAX_FILES or total > SNAPSHOT_MAX_BYTES:
                        return ("目录超过快照上限（%d 个文件 / %d MB），"
                                "为免把整个目录拷进回收站，这次不做快照，"
                                "请手动确认后自己删。" % (SNAPSHOT_MAX_FILES,
                                                        SNAPSHOT_MAX_BYTES // 1048576))
        except OSError as exc:
            return "统计目录大小失败：%s" % exc
        return ""

    def snapshot(self, path: str) -> dict:
        """把文件复制到回收站，返回快照信息。失败返回 ok=False。"""
        src = Path(path)
        if not src.exists():
            return {"ok": False, "message": "文件不存在：%s" % path}
        if src.is_dir():
            why = self._too_big(src)
            if why:
                return {"ok": False, "message": why}
        sid = "%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:6])
        dst = self.trash_dir / sid
        try:
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                # N-27：copy2 不会自动建目录——不先 mkdir 的话会把快照 id
                # 当成文件名，快照内容直接丢，回滚就废了。单文件也要先建目录。
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst / src.name)
        except OSError as exc:
            return {"ok": False, "message": "快照失败：%s" % exc}
        snap = {"ok": True, "snapshot_id": sid, "src": str(src),
                "snapshot": str(dst), "ts": _now_ms(),
                "kind": "dir" if src.is_dir() else "file"}
        self._write_meta(snap)
        return snap

    def restore(self, snapshot_id: str) -> dict:
        """从快照还原（覆盖回原路径）。"""
        for rec in self._snapshot_index():
            if rec.get("snapshot_id") == snapshot_id:
                src = Path(rec["snapshot"])
                dst = Path(rec["src"])
                if not src.exists():
                    return {"ok": False, "message": "快照文件已不在：%s" % src}
                try:
                    if dst.exists():
                        if dst.is_dir():
                            shutil.rmtree(dst)
                        else:
                            dst.unlink()
                    # N-27：单文件快照（kind=file）的快照目录里存的是原文件，
                    # 按文件拷回；目录快照整目录拷。旧记录没有 kind 字段，
                    # 按快照路径形态回退处理。
                    if rec.get("kind") == "file":
                        inner = src / dst.name
                        if not inner.is_file():
                            files = [x for x in src.iterdir() if x.is_file()]
                            inner = files[0] if files else src
                        shutil.copy2(inner, dst)
                    elif src.is_dir():
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
                except OSError as exc:
                    return {"ok": False, "message": "还原失败：%s" % exc}
                return {"ok": True, "restored": str(dst)}
        return {"ok": False, "message": "找不到快照：%s" % snapshot_id}

    def _snapshot_index(self) -> list:
        """读快照索引文件（每次快照追加一条）。"""
        idx = self.trash_dir / "snapshots.json"
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (OSError, ValueError):
            pass
        return []

    def snapshot_list(self) -> list:
        return self._snapshot_index()

    def _write_meta(self, snap: dict) -> None:
        """把快照元数据追加进索引文件（还原时要靠它找原路径）。"""
        idx = self.trash_dir / "snapshots.json"
        try:
            data = self._snapshot_index()
            data.append({k: v for k, v in snap.items() if k != "ok"})
            idx.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ 判定
    def effective_risk(self, spec: ToolSpec, args: dict) -> RiskLevel:
        """算这次调用的实际风险级：插件给了 risk_fn 就按参数算，否则用静态值。"""
        fn = getattr(spec, "risk_fn", None)
        if not callable(fn):
            return spec.risk
        try:
            got = fn(args)
        except Exception:
            return spec.risk
        if isinstance(got, RiskLevel):
            return got
        try:
            return RiskLevel(str(got))
        except ValueError:
            return spec.risk

    def check(self, spec: ToolSpec, args: dict,
              confirm_token: str = "") -> dict:
        """
        判定这次调用能不能放行。

        返回：
          {"ok": True,  "action": "execute"}
          {"ok": False, "action": "deny",    "message": "..."}
          {"ok": False, "action": "confirm", "message": "...",
           "confirm_token": "..."}
        """
        if not self.enabled:
            return {"ok": True, "action": "execute", "bypass": True}

        # 硬拒绝：guard 说不行的位置（.ssh / AppData / .env / *.db）连确认都不给。
        # 这类路径一旦被读出来就会进对话上下文，授权了也不该放行。
        guard = getattr(spec, "guard", None)
        if callable(guard):
            try:
                why = str(guard(args) or "")
            except Exception as exc:
                why = "路径检查失败：%s" % exc
            if why:
                return {"ok": False, "action": "deny", "message": why}

        risk = self.effective_risk(spec, args)
        # 只读工具永远放行
        if spec.name in self.readonly_names or risk == RiskLevel.LOW:
            return {"ok": True, "action": "execute"}

        # 会话内已授权
        if spec.name in self.granted:
            return {"ok": True, "action": "execute", "granted": True}

        # 带着有效令牌来 → 放行
        if confirm_token:
            pend = self._pending.get(confirm_token)
            if not pend:
                return {"ok": False, "action": "deny",
                        "message": "确认令牌无效或已过期，请重新确认"}
            if pend["tool"] != spec.name:
                return {"ok": False, "action": "deny",
                        "message": "确认令牌与本次工具不匹配"}
            if pend["expires"] < time.time():
                self._pending.pop(confirm_token, None)
                return {"ok": False, "action": "deny",
                        "message": "确认已超时，请重新确认"}
            # 令牌只能用于同一组参数，防止确认完被换内容
            if pend["args_hash"] != _args_hash(args):
                return {"ok": False, "action": "deny",
                        "message": "参数与确认时不一致，请重新确认"}
            self._pending.pop(confirm_token, None)
            return {"ok": True, "action": "execute"}

        # 需要确认
        summary = self.summarize(spec, args, risk)
        token = uuid.uuid4().hex[:16]
        self._pending[token] = {
            "tool": spec.name,
            "args_hash": _args_hash(args),
            "risk": risk.value,
            "expires": time.time() + TOKEN_TTL,
        }
        return {"ok": False, "action": "confirm", "message": summary,
                "confirm_token": token, "risk": risk.value}

    def summarize(self, spec: ToolSpec, args: dict,
                  risk: RiskLevel | None = None) -> str:
        """给用户看的一句话摘要：要做什么、影响什么。"""
        risk = (risk or self.effective_risk(spec, args)).value
        if risk == RiskLevel.HIGH.value:
            head = "【高风险】这一步会改坏东西"
        elif risk == RiskLevel.MEDIUM.value:
            head = "【中风险】这一步会动到你的东西"
        else:
            head = "【低风险】这一步只是看看"
        return "%s：%s；参数：%s" % (head, spec.description, _safe_args(args))

    def grant(self, tool_name: str) -> None:
        """用户显式授权某个工具（本次会话内有效）。"""
        self.granted.add(tool_name)

    def revoke(self, tool_name: str) -> None:
        self.granted.discard(tool_name)

    def pending_count(self) -> int:
        return len(self._pending)


def _args_hash(args: dict) -> str:
    """参数指纹：用来确认"确认过的就是这一组参数"。"""
    import hashlib
    blob = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _safe_args(args: Any) -> Any:
    """审计日志里不写太长/敏感的大段内容。"""
    if isinstance(args, dict):
        out = {}
        for k, v in args.items():
            if isinstance(v, str) and len(v) > 200:
                out[k] = v[:200] + "…"
            else:
                out[k] = v
        return out
    if isinstance(args, str) and len(args) > 200:
        return args[:200] + "…"
    return args
