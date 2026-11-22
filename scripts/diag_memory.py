# -*- coding: utf-8 -*-
r"""
记忆库诊断：只输出计数，不输出任何一条真实对话。

背景（P1-1）
    交付包里 episodes 有一万四千条，facts / relations 却是 0 行。要么线上链路
    memNote → /api/memory/extract → merge_facts 没把事实留住，要么被清空过。
    这个脚本把两种可能分开：一路看库里的实际分布，一路在临时库里把
    "写入 → 软删 → 再写入" 走一遍，看第二步会不会被 UNIQUE 约束挡死。

用法
    python scripts\diag_memory.py
    python scripts\diag_memory.py --db "D:\某个\memory.db"
    python scripts\diag_memory.py --sample 500      # 只回放前 N 条用户话，跑得快

输出示例
    episodes: 14189 行（在用 13988 / 软删 201）
    facts   : 0 行（在用 0 / 软删 0）
    ...
    规则回放：13988 条用户话里 216 条能抽出事实（2.9%），共 231 条事实
    写入链路模拟：[FAIL] 软删过的事实无法再次写入：UNIQUE constraint failed
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PROG = SCRIPTS.parent
sys.path.insert(0, str(PROG / "tools"))

try:
    import memory_extract as ME
except Exception as exc:  # pragma: no cover
    print("无法加载 tools/memory_extract.py：%s" % exc, file=sys.stderr)
    raise SystemExit(2)


def _ro(path: Path) -> sqlite3.Connection:
    """只读打开：不建 -wal、不改库。"""
    uri = "file:%s?mode=ro" % path.as_posix()
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    try:
        return int(conn.execute(sql, args).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def stats(db: Path) -> dict:
    conn = _ro(db)
    try:
        out = {}
        for name in ("episodes", "facts", "relations"):
            if not _table_exists(conn, name):
                out[name] = None
                continue
            if name == "relations":
                total = _one(conn, "SELECT count(*) FROM relations")
                live = _one(conn, "SELECT count(*) FROM relations WHERE status<>'dropped'")
                out[name] = {"total": total, "live": live, "deleted": total - live}
            else:
                total = _one(conn, "SELECT count(*) FROM %s" % name)
                live = _one(conn, "SELECT count(*) FROM %s WHERE deleted=0" % name)
                out[name] = {"total": total, "live": live, "deleted": total - live}

        if out.get("episodes"):
            out["ep_user"] = _one(
                conn, "SELECT count(*) FROM episodes WHERE deleted=0 AND role='user'")
            out["ep_asst"] = _one(
                conn, "SELECT count(*) FROM episodes WHERE deleted=0 AND role='assistant'")
            row = conn.execute(
                "SELECT avg(length(text)) FROM episodes WHERE deleted=0 AND role='user'"
            ).fetchone()
            out["avg_len"] = float(row[0] or 0)
        if out.get("facts") and out["facts"]["total"]:
            keys = conn.execute(
                "SELECT key, count(*) FROM facts WHERE deleted=0 GROUP BY key "
                "ORDER BY count(*) DESC LIMIT 12").fetchall()
            out["fact_keys"] = [(str(r[0]), int(r[1])) for r in keys]
            has_by = any(str(c[1]) == "by" for c in conn.execute("PRAGMA table_info(facts)"))
            out["facts_has_by"] = has_by
            if has_by:
                out["fact_by"] = [(str(r[0]), int(r[1])) for r in conn.execute(
                    "SELECT by, count(*) FROM facts WHERE deleted=0 GROUP BY by").fetchall()]
        return out
    finally:
        conn.close()


def replay(db: Path, sample: int = 0) -> dict:
    """把用户话离线回放给规则抽取器：只统计数量，不打印内容。"""
    conn = _ro(db)
    try:
        sql = ("SELECT id, text FROM episodes WHERE deleted=0 AND role='user'"
               + (" LIMIT %d" % int(sample) if sample and sample > 0 else ""))
        total = hit = facts = 0
        by_key: dict[str, int] = {}
        for row in conn.execute(sql):
            total += 1
            items = ME.extract(str(row[1] or ""), use_model=False)
            if not items:
                continue
            hit += 1
            facts += len(items)
            for it in items:
                k = str(it.get("key") or "")
                by_key[k] = by_key.get(k, 0) + 1
        return {"total": total, "hit": hit, "facts": facts, "by_key": by_key}
    finally:
        conn.close()


def simulate_write() -> dict:
    """在临时库里跑一遍 写入 → 软删 → 再写入 → 手动恢复。

    验的是 N-3 之后的语义：
      1. 第一次写入能落库；
      2. 软删之后再说同样的话，**不复活**（deleted 仍是 1），只把 suppressed +1；
      3. 手动恢复之后重新变成在用。
    这样既确认 UNIQUE(key,value) 不会再挡死写入，也确认"删掉就是删掉"。
    """
    tmp = Path(tempfile.mkdtemp(prefix="diag_mem_")) / "t.db"
    conn = sqlite3.connect(str(tmp))
    try:
        conn.executescript(
            "CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " key TEXT NOT NULL, value TEXT NOT NULL,"
            " confidence REAL NOT NULL DEFAULT 0.6, evidence TEXT,"
            " updated_at INTEGER NOT NULL DEFAULT 0,"
            " deleted INTEGER NOT NULL DEFAULT 0,"
            " suppressed INTEGER NOT NULL DEFAULT 0, UNIQUE(key, value));")
        row = [{"key": "称呼", "value": "小诺", "confidence": 1.0, "by": "rule"}]
        step1 = ME.merge_facts(conn, row, "1")
        conn.execute("UPDATE facts SET deleted=1")
        conn.commit()
        try:
            step2 = ME.merge_facts(conn, row, "2")
            alive = conn.execute(
                "SELECT count(*) FROM facts WHERE deleted=0").fetchone()[0]
            sup_row = conn.execute(
                "SELECT suppressed FROM facts ORDER BY id LIMIT 1").fetchone()
            sup = int(sup_row[0] if sup_row else 0)
            conn.execute("UPDATE facts SET deleted=0, suppressed=0")
            conn.commit()
            alive2 = conn.execute(
                "SELECT count(*) FROM facts WHERE deleted=0").fetchone()[0]
            ok = (alive == 0 and int(step2.get("suppressed") or 0) == 1
                  and sup == 1 and alive2 == 1)
            if ok:
                msg = ""
            elif alive != 0:
                msg = "删掉的事实又被自动写回来了（墓碑失效）"
            elif int(step2.get("suppressed") or 0) != 1:
                msg = "再次提到时没有累加 suppressed"
            else:
                msg = "手动恢复之后没有回到在用状态"
            return {"ok": ok, "step1": step1, "step2": step2,
                    "alive": alive, "suppressed": sup, "restored": alive2,
                    "message": msg}
        except sqlite3.IntegrityError as exc:
            return {"ok": False, "step1": step1, "step2": None, "alive": 0,
                    "message": "软删过的事实无法再次写入：%s" % exc}
        except Exception as exc:
            return {"ok": False, "step1": step1, "step2": None, "alive": 0,
                    "message": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(tmp) + suffix).unlink()
            except OSError:
                pass


def _default_db() -> Path:
    """默认找数据目录里的库；老版本放在程序目录下的也认。"""
    import os
    candidates = []
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or "").strip()
    if base:
        candidates.append(Path(base) / "PolitePuppyPet" / "data" / "记忆" / "memory.db")
    override = (os.environ.get("POLITE_PUPPY_DATA") or "").strip()
    if override:
        candidates.insert(0, Path(override) / "data" / "记忆" / "memory.db")
    candidates.append(PROG / "记忆" / "memory.db")      # 老位置兜底
    for p in candidates:
        if p.is_file():
            return p
    return candidates[0]


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="记忆库诊断（只输出计数）")
    ap.add_argument("--db", default="", help="memory.db 路径（默认自动找数据目录）")
    ap.add_argument("--sample", type=int, default=0, help="只回放前 N 条用户话")
    args = ap.parse_args(argv)

    db = Path(args.db) if args.db else _default_db()
    if not db.is_file():
        print("找不到记忆库：%s" % db, file=sys.stderr)
        return 2

    print("=" * 64)
    print("礼貌小狗 · 记忆库诊断（只输出计数，不读内容）")
    print("库：%s" % db.name)
    print("=" * 64)
    st = stats(db)
    for name, label in (("episodes", "episodes"), ("facts", "facts"),
                        ("relations", "relations")):
        v = st.get(name)
        if v is None:
            print("%-9s: 表不存在" % label)
            continue
        print("%-9s: %d 行（在用 %d / 软删 %d）"
              % (label, v["total"], v["live"], v["deleted"]))
    if "ep_user" in st:
        print("           其中用户话 %d 条、她的话 %d 条，用户话平均 %d 字"
              % (st["ep_user"], st["ep_asst"], round(st["avg_len"])))
    if st.get("fact_keys"):
        print("facts 按 key：%s" % "、".join("%s=%d" % kv for kv in st["fact_keys"]))
    if "facts_has_by" in st:
        print("facts 有 by 列：%s%s" % (
            st["facts_has_by"],
            "" if not st.get("fact_by") else "；按来源：" +
            "、".join("%s=%d" % kv for kv in st["fact_by"])))

    print("-" * 64)
    rp = replay(db, args.sample)
    rate = (rp["hit"] * 100.0 / rp["total"]) if rp["total"] else 0.0
    print("规则回放：%d 条用户话里 %d 条能抽出事实（%.1f%%），共 %d 条事实"
          % (rp["total"], rp["hit"], rate, rp["facts"]))
    if rp["by_key"]:
        top = sorted(rp["by_key"].items(), key=lambda kv: -kv[1])[:10]
        print("           按 key：%s" % "、".join("%s=%d" % kv for kv in top))

    print("-" * 64)
    sim = simulate_write()
    print("写入链路模拟：%s %s" % ("[PASS]" if sim["ok"] else "[FAIL]",
                                  sim["message"]))
    if sim.get("step1"):
        print("           第一次写入：inserted=%s" % sim["step1"].get("inserted"))
    if sim.get("step2"):
        print("           删后再提：不复活，suppressed=%s（在用 %s 行）"
              % (sim["step2"].get("suppressed"), sim.get("alive")))
        print("           手动恢复：在用 %s 行" % sim.get("restored"))

    print("-" * 64)
    if st.get("facts") and st["facts"]["live"] == 0 and rp["facts"] > 0:
        print("结论：库里有可抽成事实的原句，但 facts 是空的 → 断点在写入链路。")
        if not sim["ok"]:
            print("      写入链路自检没过，看上面那行原因（UNIQUE / 抽取器 / 迁移）。")
    elif rp["facts"] == 0:
        print("结论：规则抽取命中率 0，需检查抽取器或被抽样范围。")
    else:
        print("结论：facts 有数据，链路正常；如需更多事实请开离线整理。")
    return 0 if sim["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
