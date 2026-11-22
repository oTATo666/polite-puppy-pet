# -*- coding: utf-8 -*-
"""冒烟测试 2：记忆读写 / 软删 / 检索 / 事实层（P1-1）。"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from _ctx import main


class MemoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main._MEM_INITED = False
        cls.first = main._mem_extract({"text": "我叫小诺，我讨厌香菜", "ts": 1730000000000})

    def test_episode_and_facts_written(self):
        self.assertTrue(self.first.get("ok"))
        self.assertTrue(self.first.get("episode_id"))
        keys = {it["key"] for it in self.first.get("items") or []}
        self.assertIn("称呼", keys)
        st = main._mem_stats()
        self.assertGreaterEqual(st.get("facts") or 0, 2)

    def test_fact_has_source(self):
        res = main._mem_search("", "fact", 20)
        self.assertTrue(res.get("ok"))
        items = res.get("items") or []
        self.assertTrue(items, "事实一条都没搜到")
        for it in items:
            # N-12：by="user" 是设置页手动添加的合法来源
            self.assertIn(it.get("by"), ("rule", "infer", "model", "user", ""))

    def test_deleted_fact_stays_deleted(self):
        """N-3：删掉的事实是墓碑——再提到不复活，只累加 suppressed。

        老行为是命中软删行就 deleted=0，等于用户点过「删除」之后，
        下次聊到同样的话又偷偷写回来。这里锁死新语义：
          · 再提到：deleted 仍是 1，suppressed +1；
          · 设置页能看到（include_deleted）；
          · 只有手动恢复才回到在用。
        """
        made = main._mem_add({"type": "fact", "key": "测试墓碑",
                              "value": "临时值", "confidence": 1.0})
        self.assertTrue(made.get("ok"), made)
        fid = int(made["id"])
        self.assertTrue(main._mem_delete(fid, "fact").get("ok"))

        ext = main._mem_extractor()
        self.assertIsNotNone(ext, "抽取器没装上")
        conn = main._mem_open()
        try:
            store = ext.merge_facts(
                conn, [{"key": "测试墓碑", "value": "临时值",
                        "confidence": 1.0, "by": "rule"}], "test")
            row = conn.execute(
                "SELECT deleted, suppressed FROM facts WHERE id=?", (fid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(store.get("failed"), [])
        self.assertGreaterEqual(int(store.get("suppressed") or 0), 1)
        self.assertEqual(int(row["deleted"]), 1, "墓碑被自动复活了")
        self.assertGreaterEqual(int(row["suppressed"]), 1)

        # 设置页「已屏蔽」能看到它
        blocked = main._mem_search("", "fact", 50, include_deleted=True)
        self.assertTrue(any(it["id"] == fid for it in blocked.get("items") or []))

        # 手动恢复之后回到在用
        self.assertTrue(main._mem_restore(fid).get("ok"))
        live = main._mem_search("", "fact", 50)
        self.assertTrue(any(it["id"] == fid for it in live.get("items") or []))

    def test_episode_search_and_patch(self):
        res = main._mem_search("小诺", "episode", 10)
        self.assertTrue(res.get("ok"))
        items = res.get("items") or []
        self.assertTrue(items)
        eid = items[0]["id"]
        patched = main._mem_patch(eid, "episode", {"text": "我叫小诺，改过的那条"})
        self.assertTrue(patched.get("ok"))
        again = main._mem_search("改过的", "episode", 10)
        self.assertTrue(any("改过" in (it.get("text") or "")
                            for it in again.get("items") or []))

    def test_export_has_three_tables(self):
        out = main._mem_export("json")
        self.assertTrue(out.get("ok"))
        for key in ("episodes", "facts", "relations"):
            self.assertIn(key, out)
            self.assertIsInstance(out[key], list)

    def test_organize_off_by_default(self):
        """离线整理默认关：没开就不该有任何外发。"""
        st = main._mem_organize_status()
        self.assertTrue(st.get("ok"), st)
        self.assertFalse(st.get("enabled"))
        self.assertFalse(st.get("can_run"))


class MigrationBackupTest(unittest.TestCase):
    """N-15：补列迁移之前必须先备份；备份写不成功就不迁移。"""

    def setUp(self):
        import tempfile
        import shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="puppy_mig_"))
        # 换到独立库路径，避免碰其他测试建好的库
        self.saved = (main.MEMORY_DIR, main.MEMORY_DB, main._MEM_INITED)
        main.MEMORY_DIR = self.tmp / "记忆"
        main.MEMORY_DB = main.MEMORY_DIR / "memory.db"
        main._MEM_INITED = False

    def tearDown(self):
        (main.MEMORY_DIR, main.MEMORY_DB, main._MEM_INITED) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_old_db(self, rows: int) -> int:
        """手工建一个"老结构"库（facts 没有 by / suppressed 列），返回行数。"""
        import sqlite3
        main.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(main.MEMORY_DB))
        conn.execute(
            "CREATE TABLE facts("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " key TEXT NOT NULL, value TEXT NOT NULL,"
            " confidence REAL DEFAULT 1.0, evidence TEXT, updated_at INTEGER,"
            " deleted INTEGER NOT NULL DEFAULT 0, UNIQUE(key, value))")
        for i in range(rows):
            conn.execute(
                "INSERT INTO facts(key, value, updated_at) VALUES (?,?,?)",
                ("测试键%d" % i, "测试值%d" % i, 1730000000000 + i))
        conn.commit()
        conn.close()
        return rows

    def _backup_dir(self):
        return main.MEMORY_DIR / "backup"

    def test_backup_appears_before_and_matches(self):
        """迁移前备份存在、行数一致、integrity_check 为 ok、原库补上列。"""
        n = self._make_old_db(3)
        main._mem_open()
        bdir = self._backup_dir()
        backups = list(bdir.glob("memory-pre-migrate-*.db"))
        self.assertEqual(len(backups), 1, "迁移前备份没有生成：%s" % list(bdir.glob("*")))
        # 备份是迁移前的老结构快照：行数一致，且能打开、integrity_check = ok
        import sqlite3
        bconn = sqlite3.connect(str(backups[0]))
        try:
            got = bconn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            check = bconn.execute("PRAGMA integrity_check").fetchone()[0]
            bcols = {str(r[1]) for r in bconn.execute("PRAGMA table_info(facts)")}
        finally:
            bconn.close()
        self.assertEqual(int(got), n)
        self.assertEqual(check, "ok")
        self.assertNotIn("suppressed", bcols, "备份应是补列之前的老结构")
        # 原库已经补上列，数据还在
        conn = main._mem_open()
        try:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(facts)")}
            live = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        finally:
            conn.close()
        self.assertIn("suppressed", cols)
        self.assertIn("by", cols)
        self.assertEqual(int(live), n)

    def test_migration_is_idempotent(self):
        """列补齐之后再跑迁移，不再生成新备份。"""
        self._make_old_db(1)
        main._mem_open()
        n1 = len(list(self._backup_dir().glob("memory-pre-migrate-*.db")))
        conn = main._mem_open()
        try:
            main._mem_migrate(conn)      # 再跑一次：列都在，什么都不该做
        finally:
            conn.close()
        n2 = len(list(self._backup_dir().glob("memory-pre-migrate-*.db")))
        self.assertEqual(n1, n2)

    def test_failed_backup_skips_migration(self):
        """备份写失败（目录顶成文件）→ 库保持老结构，不半途改表。"""
        import sqlite3
        self._make_old_db(2)
        # 把 backup "目录" 顶成一个文件，mkdir 会失败 → 备份返回 None
        (main.MEMORY_DIR / "backup").write_text("blocked", encoding="utf-8")
        main._mem_open()
        conn = main._mem_open()
        try:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(facts)")}
        finally:
            conn.close()
        self.assertNotIn("suppressed", cols, "备份失败时迁移不该执行")
        self.assertNotIn("by", cols, "备份失败时迁移不该执行")
        conn2 = sqlite3.connect(str(main.MEMORY_DB))
        try:
            live = conn2.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        finally:
            conn2.close()
        self.assertEqual(int(live), 2, "数据必须原封不动")


class ManualAddTombstoneTest(unittest.TestCase):
    """N-12：手动添加（by="user"）可复活墓碑；自动路径仍不复活并报 blocked。"""

    def test_manual_add_revives_tombstone(self):
        made = main._mem_add({"type": "fact", "key": "N12键", "value": "N12值"})
        self.assertTrue(made.get("ok"))
        fid = int(made["id"])
        self.assertTrue(main._mem_delete(fid, "fact").get("ok"))
        # 设置页手动添加：复活并清零计数
        res = main._mem_add({"type": "fact", "key": "N12键", "value": "N12值",
                             "by": "user"})
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res.get("restored"), res)
        self.assertEqual(int(res["id"]), fid)
        conn = main._mem_open()
        try:
            row = conn.execute("SELECT deleted, suppressed FROM facts WHERE id=?",
                               (fid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(int(row["deleted"]), 0, "手动添加应复活墓碑")
        self.assertEqual(int(row["suppressed"]), 0, "复活时 suppressed 清零")
        live = main._mem_search("N12", "fact", 50)
        self.assertTrue(any(it["id"] == fid for it in live.get("items") or []),
                        "复活后应可见")

    def test_auto_add_hits_tombstone_reports_blocked(self):
        made = main._mem_add({"type": "fact", "key": "N12b键", "value": "N12b值"})
        fid = int(made["id"])
        self.assertTrue(main._mem_delete(fid, "fact").get("ok"))
        # 自动抽取/导入路径（by=rule）：不复活，返回 blocked + 提示
        res = main._mem_add({"type": "fact", "key": "N12b键", "value": "N12b值"})
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res.get("blocked"), res)
        self.assertEqual(int(res["id"]), fid)
        self.assertIn("恢复", res.get("message") or "")
        conn = main._mem_open()
        try:
            row = conn.execute("SELECT deleted FROM facts WHERE id=?",
                               (fid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(int(row["deleted"]), 1, "自动路径不许复活墓碑")
        live = main._mem_search("N12b", "fact", 50)
        self.assertFalse(any(it["id"] == fid for it in live.get("items") or []),
                         "没复活就不该在正常检索里出现")

    def test_import_reports_blocked_count(self):
        made = main._mem_add({"type": "fact", "key": "N12c键", "value": "N12c值"})
        fid = int(made["id"])
        self.assertTrue(main._mem_delete(fid, "fact").get("ok"))
        out = main._mem_import({"items": [{"type": "fact", "key": "N12c键",
                                           "value": "N12c值"}], "mode": "merge"})
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(int(out.get("blocked", {}).get("fact", 0)), 1,
                         out)
        self.assertEqual(int(out.get("skipped", {}).get("fact", 0)), 1)
        conn = main._mem_open()
        try:
            row = conn.execute("SELECT deleted FROM facts WHERE id=?",
                               (fid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(int(row["deleted"]), 1, "导入不许复活墓碑")


class MigratePendingDegradationTest(unittest.TestCase):
    """N-18：迁移被暂停（备份失败）后，老结构库上的读写接口都要能用、不 500。"""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="puppy_deg_"))
        self.saved = (main.MEMORY_DIR, main.MEMORY_DB, main._MEM_INITED,
                      main._MEM_MIGRATE_PENDING)
        main.MEMORY_DIR = self.tmp / "记忆"
        main.MEMORY_DB = main.MEMORY_DIR / "memory.db"
        main._MEM_INITED = False
        main._MEM_MIGRATE_PENDING = False

    def tearDown(self):
        (main.MEMORY_DIR, main.MEMORY_DB, main._MEM_INITED,
         main._MEM_MIGRATE_PENDING) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_old_db(self, rows: int) -> int:
        import sqlite3
        main.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(main.MEMORY_DB))
        conn.execute(
            "CREATE TABLE facts("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " key TEXT NOT NULL, value TEXT NOT NULL,"
            " confidence REAL DEFAULT 1.0, evidence TEXT, updated_at INTEGER,"
            " deleted INTEGER NOT NULL DEFAULT 0, UNIQUE(key, value))")
        for i in range(rows):
            conn.execute(
                "INSERT INTO facts(key, value, updated_at) VALUES (?,?,?)",
                ("旧键%d" % i, "旧值%d" % i, 1730000000000 + i))
        conn.commit()
        conn.close()
        return rows

    def test_apis_work_without_new_columns(self):
        import sqlite3
        n = self._make_old_db(2)
        # 备份必失败：把 backup 目录顶成文件 → 迁移被暂停
        (main.MEMORY_DIR / "backup").write_text("blocked", encoding="utf-8")
        main._mem_open()
        self.assertTrue(main._MEM_MIGRATE_PENDING, "迁移应被暂停")

        # 1) stats：ok + migrate_pending 标志，计数正常
        st = main._mem_stats()
        self.assertTrue(st.get("ok"), st)
        self.assertTrue(st.get("migrate_pending"), st)
        self.assertEqual(int(st.get("facts") or 0), n)

        # 2) 检索：老数据还能搜到
        res = main._mem_search("旧键", "fact", 10)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(res.get("items") or []), n)

        # 3) 手动添加（新行）与复活（墓碑）都不写 suppressed
        made = main._mem_add({"type": "fact", "key": "新键", "value": "新值",
                              "by": "user"})
        self.assertTrue(made.get("ok"), made)
        self.assertTrue(main._mem_delete(int(made["id"]), "fact").get("ok"))
        again = main._mem_add({"type": "fact", "key": "新键", "value": "新值",
                               "by": "user"})
        self.assertTrue(again.get("ok"), again)
        self.assertTrue(again.get("restored"), again)

        # 4) 恢复：老结构库上不走 suppressed 也能恢复
        fid = int(again["id"])
        self.assertTrue(main._mem_delete(fid, "fact").get("ok"))
        self.assertTrue(main._mem_restore(fid).get("ok"))
        live = main._mem_search("新键", "fact", 10)
        self.assertTrue(any(it["id"] == fid for it in live.get("items") or []))

        # 5) 自动路径命中墓碑 → merge_facts 有 _has_suppressed 兜底，不报错
        self.assertTrue(main._mem_delete(fid, "fact").get("ok"))
        ext = main._mem_extractor()
        if ext is not None:
            conn = main._mem_open()
            try:
                store = ext.merge_facts(
                    conn, [{"key": "新键", "value": "新值"}], "test")
            finally:
                conn.close()
            self.assertEqual(int(store.get("failed") or 0), 0)

        # 6) 数据行数没丢：旧行都在用；新键行是墓碑（刚才为测 blocked 又删了）
        conn = sqlite3.connect(str(main.MEMORY_DB))
        try:
            live_n = conn.execute(
                "SELECT count(*) FROM facts WHERE deleted=0").fetchone()[0]
            tomb_n = conn.execute(
                "SELECT count(*) FROM facts WHERE deleted=1").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(live_n), n, "旧数据一条都不能丢")
        self.assertEqual(int(tomb_n), 1, "新键行应为墓碑（软删，非物理删）")


class ReplaceImportTest(unittest.TestCase):
    """N-17：replace 导入先备份、可覆盖墓碑；备份失败就不替换。"""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="puppy_rep_"))
        self.saved = (main.MEMORY_DIR, main.MEMORY_DB, main._MEM_INITED)
        main.MEMORY_DIR = self.tmp / "记忆"
        main.MEMORY_DB = main.MEMORY_DIR / "memory.db"
        main._MEM_INITED = False

    def tearDown(self):
        (main.MEMORY_DIR, main.MEMORY_DB, main._MEM_INITED) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_replace_backs_up_and_revives(self):
        made = main._mem_add({"type": "fact", "key": "R键", "value": "R值"})
        fid = int(made["id"])
        self.assertTrue(main._mem_delete(fid, "fact").get("ok"))
        before = len(list((main.MEMORY_DIR / "backup").glob("memory-pre-migrate-*.db")))
        out = main._mem_import({"items": [{"type": "fact", "key": "R键",
                                           "value": "R值"}], "mode": "replace"})
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(int(out.get("inserted", {}).get("fact", 0)), 1, out)
        self.assertEqual(int(out.get("blocked", {}).get("fact", 0)), 0)
        # 备份在操作前出现（replace 前的快照多了一份）
        after = list((main.MEMORY_DIR / "backup").glob("memory-pre-migrate-*.db"))
        self.assertEqual(len(after), before + 1, "replace 前应生成备份")
        # 同内容事实可见（墓碑被用户授权的替换复活）
        live = main._mem_search("R键", "fact", 10)
        self.assertTrue(any(it["id"] == fid for it in live.get("items") or []),
                        "replace 后同内容事实应可见")

    def test_replace_aborts_when_backup_fails(self):
        made = main._mem_add({"type": "fact", "key": "R2键", "value": "R2值"})
        fid = int(made["id"])
        self.assertTrue(main._mem_delete(fid, "fact").get("ok"))
        (main.MEMORY_DIR / "backup").write_text("blocked", encoding="utf-8")
        out = main._mem_import({"items": [{"type": "fact", "key": "R2键",
                                           "value": "R2值"}], "mode": "replace"})
        self.assertFalse(out.get("ok"), "备份失败时 replace 必须中止")
        self.assertIn("备份", out.get("message") or "")
        # 数据原封不动：没有新增，墓碑还是墓碑
        live = main._mem_search("R2键", "fact", 10)
        self.assertEqual(len(live.get("items") or []), 0)
        tomb = main._mem_search("", "fact", 50, include_deleted=True)
        self.assertTrue(any(it["id"] == fid for it in tomb.get("items") or []))


class MergeFactsDetailsTest(unittest.TestCase):
    """N-14：merge_facts 的四处细节（先截断、先精确、优先在用行、证据保尾）。"""

    def _store(self, items, evidence="test"):
        ext = main._mem_extractor()
        self.assertIsNotNone(ext)
        conn = main._mem_open()
        try:
            return ext.merge_facts(conn, items, evidence)
        finally:
            conn.close()

    def test_prefers_live_row_over_tombstone(self):
        # 两行同 key、值只差 1 字符（ratio ≈ 0.909 > 0.9，都算相似命中）：
        # 行1 在用、行2 是墓碑。合并目标与两行都不精确相同 → 应落 sim_live
        # （行1），而不是先扫到的墓碑。
        live_v, tomb_v, target_v = "我喜欢吃苹果和香蕉abc", "我喜欢吃苹果和香蕉xyz", "我喜欢吃苹果和香蕉abd"
        made = main._mem_add({"type": "fact", "key": "N14键", "value": live_v})
        fid_live = int(made["id"])
        made2 = main._mem_add({"type": "fact", "key": "N14键", "value": tomb_v})
        fid_tomb = int(made2["id"])
        self.assertTrue(main._mem_delete(fid_tomb, "fact").get("ok"))
        store = self._store([{"key": "N14键", "value": target_v}])
        self.assertEqual(store.get("merged"), 1, store)
        self.assertEqual(store.get("suppressed"), 0, store)
        self.assertIn(fid_live, store.get("ids") or [])
        conn = main._mem_open()
        try:
            row = conn.execute("SELECT value FROM facts WHERE id=?",
                               (fid_live,)).fetchone()
            tomb = conn.execute("SELECT deleted FROM facts WHERE id=?",
                                (fid_tomb,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["value"], target_v)
        self.assertEqual(int(tomb["deleted"]), 1, "墓碑不能被动")

    def test_exact_match_beats_similar(self):
        made = main._mem_add({"type": "fact", "key": "N14b键", "value": "相似值一"})
        fid_sim = int(made["id"])
        made2 = main._mem_add({"type": "fact", "key": "N14b键", "value": "精确目标值"})
        fid_exact = int(made2["id"])
        store = self._store([{"key": "N14b键", "value": "精确目标值"}])
        self.assertEqual(store.get("merged"), 1, store)
        self.assertEqual(store.get("inserted"), 0, store)
        self.assertIn(fid_exact, store.get("ids") or [])
        self.assertNotIn(fid_sim, store.get("ids") or [])

    def test_truncates_before_dedup(self):
        long_key = "K" * 80
        main._mem_add({"type": "fact", "key": long_key[:64], "value": "v值"})
        store = self._store([{"key": long_key, "value": "v值"}])
        # key 截断后与已有行同 key 同 value → 合并，而不是 INSERT 撞 UNIQUE
        self.assertEqual(store.get("merged"), 1, store)
        self.assertEqual(store.get("inserted"), 0, store)
        self.assertEqual(store.get("failed"), [])

    def test_evidence_keeps_tail(self):
        main._mem_add({"type": "fact", "key": "N14c键", "value": "证据值",
                       "evidence": "E" * 500})
        self._store([{"key": "N14c键", "value": "证据值"}], evidence="NEW99")
        conn = main._mem_open()
        try:
            row = conn.execute(
                "SELECT evidence FROM facts WHERE key='N14c键'").fetchone()
        finally:
            conn.close()
        ev = str(row["evidence"] or "")
        self.assertEqual(len(ev), 500, "证据链应截到 500")
        self.assertTrue(ev.endswith(",NEW99"), "尾部（新证据）必须保留，got %r" % ev[-20:])


if __name__ == "__main__":
    unittest.main()
