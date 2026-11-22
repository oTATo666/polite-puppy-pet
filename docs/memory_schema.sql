-- 礼貌小狗 · 记忆库 schema v1
-- 用法：sqlite3 记忆/memory.db < memory_schema.sql
-- 说明：单文件、零部署。全文用 trigram 分词（中文按 3 字滑窗切）。
--   ⚠ trigram 只覆盖 ≥3 个字符的查询；1~2 字（「难过」「错题」）必须走 LIKE '%词%' 兜底，
--     已实测 2 万条情景记忆下 LIKE 全表扫描 < 30ms，单用户量级完全够用。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 情景记忆
CREATE TABLE IF NOT EXISTS episodes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,              -- 毫秒时间戳
  day         TEXT    NOT NULL,              -- YYYY-MM-DD，按天翻
  role        TEXT    NOT NULL DEFAULT 'user',   -- user / assistant / system
  kind        TEXT    NOT NULL DEFAULT 'chat',   -- chat / event / promise
  text        TEXT    NOT NULL,
  mood        TEXT,                          -- 情绪标签（可空）
  importance  REAL    NOT NULL DEFAULT 0.5,  -- 0~1，决定衰减速度
  source      TEXT    NOT NULL DEFAULT 'desktop', -- desktop / web / qq / wecom
  deleted     INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
);
CREATE INDEX IF NOT EXISTS idx_episodes_day  ON episodes(day);
CREATE INDEX IF NOT EXISTS idx_episodes_ts   ON episodes(ts);
CREATE INDEX IF NOT EXISTS idx_episodes_live ON episodes(deleted, ts);

-- 全文索引（外部内容表 + 触发器同步）
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
  text,
  content='episodes',
  content_rowid='id',
  tokenize='trigram'
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

-- ---------------------------------------------------------------- 语义记忆
CREATE TABLE IF NOT EXISTS facts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  key         TEXT NOT NULL,                 -- 称呼 / 学校 / 忌口 / 作息
  value       TEXT NOT NULL,
  confidence  REAL NOT NULL DEFAULT 0.6,     -- 明说 1.0 / 推断 0.6 / 猜测 0.4
  evidence    TEXT,                          -- 引用 episodes.id，逗号分隔
  updated_at  INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
  deleted     INTEGER NOT NULL DEFAULT 0,       -- 1 = 用户删掉的墓碑，不再自动复活
  by          TEXT NOT NULL DEFAULT 'rule',  -- 来源：rule / infer / model
  suppressed  INTEGER NOT NULL DEFAULT 0,   -- 删掉之后又被提到的次数（只计数，不复活）
  UNIQUE(key, value)
);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);

-- ---------------------------------------------------------------- 关系记忆
CREATE TABLE IF NOT EXISTS relations (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  kind    TEXT NOT NULL,                     -- intimacy / promise / shared_event
  title   TEXT,
  detail  TEXT,
  ts      INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
  status  TEXT NOT NULL DEFAULT 'open'       -- open / done / dropped
);
CREATE INDEX IF NOT EXISTS idx_rel_kind ON relations(kind, status);

-- ---------------------------------------------------------------- 元信息
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
INSERT INTO meta(k, v) VALUES ('schema_version', '1')
  ON CONFLICT(k) DO UPDATE SET v = excluded.v;
INSERT INTO meta(k, v) VALUES ('migrated_from_localstorage', '0')
  ON CONFLICT(k) DO NOTHING;
