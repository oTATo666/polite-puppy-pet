# -*- coding: utf-8 -*-
"""
插件：记忆库
============

让她能查自己的记忆。直接读桌宠的 SQLite 记忆库（同库同表），
不经过 HTTP，也不外发任何内容。

- mem_search：查记忆（只读）
- mem_recall：按话题召回最相关的几条（只读）
- mem_forget：软删除一条记忆（高风险，物理不删）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..core.plugin import Plugin, PluginManifest, RiskLevel, ToolSpec


class MemoryToolsPlugin(Plugin):
    manifest = PluginManifest(
        id="memory",
        name="记忆库工具",
        version="1.0.0",
        description="查询、召回与遗忘（软删除）她的长期记忆",
    )

    def setup(self, ctx) -> None:
        self.ctx = ctx

    def _db(self) -> Path:
        r"""
        记忆库文件位置。

        优先用运行时给的 config["memory_db"]（桌宠会把真实库路径告诉插件）；
        没有就退回 data_dir\记忆\memory.db（自己起一套，测试时用）。
        """
        conf = getattr(self.ctx, "config", None) or {}
        given = str(conf.get("memory_db") or "").strip()
        if given:
            return Path(given)
        return Path(self.ctx.data_dir) / "记忆" / "memory.db"

    def _connect(self):
        db = self._db()
        if not db.is_file():
            raise FileNotFoundError("记忆库不存在：%s" % db)
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def tools(self) -> list:
        return [
            ToolSpec(
                name="mem_search",
                description="在长期记忆里按关键词搜索，返回最相关的条目",
                risk=RiskLevel.LOW,
                parameters={
                    "type": "object",
                    "properties": {
                        "q": {"type": "string", "description": "要搜的词"},
                        "limit": {"type": "integer",
                                  "description": "最多返回几条，默认 10"},
                    },
                    "required": ["q"],
                },
                invoke=self._search,
                tags=["记忆", "只读"],
            ),
            ToolSpec(
                name="mem_stats",
                description="统计记忆库里有多少情景、事实与关系",
                risk=RiskLevel.LOW,
                parameters={"type": "object", "properties": {}},
                invoke=self._stats,
                tags=["记忆", "只读"],
            ),
            ToolSpec(
                name="mem_forget",
                description="让一条记忆被遗忘（软删除，记录仍在，可恢复）",
                risk=RiskLevel.HIGH,
                destructive=True,
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "要遗忘的记忆 id"},
                        "type": {"type": "string",
                                 "description": "类型：episode / fact / relation"},
                    },
                    "required": ["id"],
                },
                invoke=self._forget,
                tags=["记忆", "删除"],
            ),
        ]

    # ------------------------------------------------------------- 具体实现
    def _search(self, args: dict) -> dict:
        q = (args.get("q") or "").strip()
        if not q:
            return {"ok": False, "message": "搜什么？q 不能空"}
        limit = max(1, min(int(args.get("limit") or 10), 100))
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, ts, day, role, kind, text FROM episodes "
                "WHERE deleted = 0 AND text LIKE ? "
                "ORDER BY ts DESC LIMIT ?", ("%" + q + "%", limit)).fetchall()
            return {"ok": True, "query": q, "count": len(rows),
                    "items": [dict(r) for r in rows]}
        except sqlite3.Error as exc:
            return {"ok": False, "message": "查询失败：%s" % exc}
        finally:
            conn.close()

    def _stats(self, _args: dict) -> dict:
        conn = self._connect()
        try:
            out = {}
            for table in ("episodes", "facts", "relations"):
                try:
                    out[table] = conn.execute(
                        "SELECT count(*) FROM %s WHERE deleted = 0" % table
                    ).fetchone()[0]
                except sqlite3.Error:
                    out[table] = 0
            return {"ok": True, "stats": out}
        finally:
            conn.close()

    def _forget(self, args: dict) -> dict:
        mid = int(args.get("id") or 0)
        mtype = args.get("type") or "episode"
        # 类型名 → 实际表名
        table = {"episode": "episodes", "fact": "facts",
                 "relation": "relations"}.get(mtype)
        if not table:
            return {"ok": False, "message": "类型只能是 episode / fact / relation"}
        conn = sqlite3.connect(str(self._db()), timeout=5)
        try:
            cur = conn.execute(
                "UPDATE %s SET deleted = 1 WHERE id = ?" % table, (mid,))
            conn.commit()
            if cur.rowcount == 0:
                return {"ok": False, "message": "没有这条记忆：%s %d" % (mtype, mid)}
            return {"ok": True, "id": mid, "type": mtype, "soft_deleted": True,
                    "message": "忘掉了（记录还在，只是不再被想起）"}
        except sqlite3.Error as exc:
            return {"ok": False, "message": "删除失败：%s" % exc}
        finally:
            conn.close()
