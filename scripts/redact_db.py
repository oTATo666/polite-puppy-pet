# -*- coding: utf-8 -*-
r"""
脱敏脚本：把一个真实的 记忆\memory.db 变成可以外发的干净库。

为什么要它
    原交付说明里提到过 redact_db.py，但包里没有这个文件，外发时只能整目录拷，
    真人对话就跟着一起出去了。这个脚本只做一件事：**只保留表结构，不保留任何
    真实对话**；需要示例数据时，用下面写死的匿名样本，绝不从源库里挑。

两种用法
    :: 只建空库（外发包默认用这个，程序首次启动会自己写数据）
       python scripts\redact_db.py --out 记忆\memory.db

    :: 建空库 + 3 条匿名示例（想让人一打开就有东西可看时用）
       python scripts\redact_db.py --out 记忆\memory.db --samples 3

    :: 从源库读（只读，不写回）——注意：本脚本**不会**把源库内容带出来，
       传 --source 只是为了让脚本在源库不存在时报错，提醒你路径写错了
       python scripts\redact_db.py --source 记忆\memory.db --out 外发\memory.db

隐私约束（与《改进执行方案》第 0 节一致）
    - 不打印、不导出任何一条真实对话；
    - 屏幕输出只有计数：表名 + 行数；
    - 生成的目标库固定 source='sample'，方便日后一眼认出是样例。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

# 建表脚本优先用交付包里的那份；不在就用内嵌副本（与 main.py 完全一致）
SCHEMA_CANDIDATES = (
    Path(__file__).resolve().parent.parent / "文档" / "memory_schema.sql",
    Path(__file__).resolve().parent.parent.parent / "02_文档" / "memory_schema.sql",
)

SCHEMA_FALLBACK = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  day TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  kind TEXT NOT NULL DEFAULT 'chat',
  text TEXT NOT NULL,
  mood TEXT,
  importance REAL NOT NULL DEFAULT 0.5,
  source TEXT NOT NULL DEFAULT 'desktop',
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
);
CREATE INDEX IF NOT EXISTS idx_episodes_day  ON episodes(day);
CREATE INDEX IF NOT EXISTS idx_episodes_ts   ON episodes(ts);
CREATE INDEX IF NOT EXISTS idx_episodes_live ON episodes(deleted, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
  text, content='episodes', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
  INSERT INTO episodes_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO episodes_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.6,
  evidence TEXT,
  updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
  deleted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(key, value)
);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
CREATE TABLE IF NOT EXISTS relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  title TEXT,
  detail TEXT,
  ts INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
  status TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_rel_kind ON relations(kind, status);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
INSERT INTO meta(k, v) VALUES ('schema_version', '1')
  ON CONFLICT(k) DO UPDATE SET v = excluded.v;
INSERT INTO meta(k, v) VALUES ('migrated_from_localstorage', '0')
  ON CONFLICT(k) DO NOTHING;
"""

# 写死的匿名样本：跟任何人无关，也不来自源库
SAMPLE_EPISODES = [
    ("2025-09-01", "user", "示例：今天开学，他说暑假作业一个字没写", "平静", 0.5),
    ("2025-09-02", "assistant", "示例：我笑他半天，叫他今晚补两页先交差", "开心", 0.4),
    ("2025-09-06", "user", "示例：他说食堂的番茄炒蛋是甜的，我说这不正常", "无语", 0.4),
]
SAMPLE_FACTS = [
    ("称呼", "示例用户", 1.0, "sample"),
    ("喜欢", "冰的", 1.0, "sample"),
    ("讨厌", "被比较", 1.0, "sample"),
]
SAMPLE_RELATION = ("shared_event", "示例：一起吐槽食堂",
                   "匿名样例，用来看设置页长什么样", "open")


def schema_sql() -> str:
    for p in SCHEMA_CANDIDATES:
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8")
        except OSError:
            continue
    return SCHEMA_FALLBACK


def build(out_path: Path, samples: int = 0) -> dict:
    """生成干净库。返回计数（只有数字，没有内容）。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        # 覆盖前先删掉 -wal / -shm，否则旧 WAL 会被套到新库上
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(out_path) + suffix).unlink()
            except OSError:
                pass

    conn = sqlite3.connect(str(out_path), timeout=15)
    try:
        conn.executescript(schema_sql())
        n_ep = n_fact = n_rel = 0
        samples = max(0, min(int(samples or 0), len(SAMPLE_EPISODES)))
        if samples:
            now = int(time.time() * 1000)
            for i in range(samples):
                day, role, text, mood, imp = SAMPLE_EPISODES[i]
                conn.execute(
                    "INSERT INTO episodes(ts, day, role, kind, text, mood,"
                    " importance, source) VALUES (?,?,?,?,?,?,?,?)",
                    (now - (samples - i) * 3600000, day, role, "chat",
                     text, mood, imp, "sample"))
                n_ep += 1
            for key, value, conf, ev in SAMPLE_FACTS[:samples]:
                conn.execute(
                    "INSERT OR IGNORE INTO facts(key, value, confidence, evidence)"
                    " VALUES (?,?,?,?)", (key, value, conf, ev))
                n_fact += 1
            if samples >= 3:
                conn.execute(
                    "INSERT INTO relations(kind, title, detail, ts, status)"
                    " VALUES (?,?,?,?,?)",
                    (SAMPLE_RELATION[0], SAMPLE_RELATION[1],
                     SAMPLE_RELATION[2], int(time.time() * 1000), SAMPLE_RELATION[3]))
                n_rel = 1
        conn.commit()
        rows = {
            "episodes": conn.execute("SELECT count(*) FROM episodes").fetchone()[0],
            "facts": conn.execute("SELECT count(*) FROM facts").fetchone()[0],
            "relations": conn.execute("SELECT count(*) FROM relations").fetchone()[0],
        }
        # 落盘并把 WAL 合并回主文件，交付包里不该带 -wal / -shm
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()
    return {"ok": True, "out": str(out_path), "samples": int(samples or 0),
            "inserted": {"episodes": n_ep, "facts": n_fact, "relations": n_rel},
            "counts": rows, "bytes": out_path.stat().st_size if out_path.exists() else 0}


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成脱敏后的记忆库（只保留表结构）")
    ap.add_argument("--out", required=True, help="输出的 memory.db 路径")
    ap.add_argument("--source", default="", help="源库路径（只做存在性校验，内容不会被读出）")
    ap.add_argument("--samples", type=int, default=0,
                    help="附带几条匿名示例（0~3，默认 0 = 空库）")
    args = ap.parse_args(argv)

    if args.source and not Path(args.source).is_file():
        print("源库不存在：%s" % args.source, file=sys.stderr)
        return 2
    if args.source:
        print("已确认源库存在（内容不会被读取、打印或导出）：%s"
              % os.path.basename(args.source))

    res = build(Path(args.out), args.samples)
    print("已生成：%s" % res["out"])
    print("表结构：4 张（episodes / facts / relations / meta）已就绪")
    print("行数：episodes=%d facts=%d relations=%d"
          % (res["counts"]["episodes"], res["counts"]["facts"],
             res["counts"]["relations"]))
    print("大小：%d 字节" % res["bytes"])
    if res["samples"]:
        print("示例：%d 条（全部 source='sample'，与真实用户无关）" % res["samples"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
