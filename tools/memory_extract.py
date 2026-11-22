# -*- coding: utf-8 -*-
r"""
礼貌小狗 · 记忆抽取器（把一句话变成事实）

三件事：
  1. 规则抽取：20 条正则覆盖「我叫X / 我在X上学 / 我喜欢X / 我讨厌X / 别叫我X /
     我X点睡 / 我养了X / 我住X …」，命中即 1.0（他明说的）。
  2. 小模型兜底：规则没命中时问一句本地模型，timeout=1.5s。
     **超时或出错就返回空，只存原始情景，绝不拖慢回复**。
  3. 去重合并：同一 key 下新旧 value 相似度 > 0.9 就合并，保留最新证据、追加证据链。

置信度：明说 1.0 / 上下文推断 0.6 / 模型猜 0.4。**< 0.5 的不进 prompt**（存着，检索时过滤）。

只用标准库：re / difflib / json / urllib / sqlite3。不装包、不联网（模型走本机 Ollama）。

用法：
    python tools\memory_extract.py --selftest        # 跑 15 条单测
    python tools\memory_extract.py "我叫小诺"          # 抽一句话看看
    python tools\memory_extract.py --file 记忆\memory.db "我讨厌香菜"   # 抽完直接存库
"""

import difflib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 常量
MODEL_TIMEOUT = 1.5     # 秒：超时就放弃，只存原始情景
SIM_MERGE = 0.9         # 相似度超过这个值就合并
MIN_CONF_FOR_PROMPT = 0.5   # 低于这个置信度不进 prompt

# 句读切分：抽取值时遇到这些就算说完
STOP = r"[，,。！!？?、；;\s…~～]"

# ---------------------------------------------------------------- 20 条规则
# (正则, key, 置信度)。value 统一取第 1 个分组。顺序：先特殊后一般。
RULES = [
    # ---- 称呼 ----
    (r"(?:别|不要|不许|别再)叫我" + "(.+?)(?=" + STOP + r"|$)", "忌称", 1.0),
    (r"我叫(.+?)(?=" + STOP + r"|$)", "称呼", 1.0),
    (r"我的名字(?:叫|是)(.+?)(?=" + STOP + r"|$)", "称呼", 1.0),
    (r"(?<![别要])叫我(.+?)(?=" + STOP + r"|$)", "称呼", 1.0),
    # ---- 学校 ----
    (r"我在(.+?)(?:上学|读书|念书)", "学校", 1.0),
    (r"我(?:现在)?(?:是|在)(.+?)(?:的学生)", "学校", 1.0),
    # ---- 喜欢 / 讨厌 ----
    (r"我(?:超|很|最|特别)?喜欢(.+?)(?=" + STOP + r"|$)", "喜欢", 1.0),
    (r"我(?:超|很|最|特别)?爱(.+?)(?=" + STOP + r"|$)", "喜欢", 1.0),
    (r"我(?:很|最|特别)?讨厌(.+?)(?=" + STOP + r"|$)", "讨厌", 1.0),
    (r"我(?:很|最|特别)?不喜欢(.+?)(?=" + STOP + r"|$)", "讨厌", 1.0),
    (r"我烦(.+?)(?=" + STOP + r"|$)", "讨厌", 1.0),
    # ---- 作息 ----
    (r"我(.{1,5}?)(?:点|點)(?:钟)?(?:就)?(?:睡|睡觉|睡了)", "睡觉时间", 1.0),
    # ---- 养的 / 住的 ----
    (r"我(?:家)?养了?(.{1,12}?)(?=" + STOP + r"|$)", "养", 1.0),
    (r"我(?:家)?住(?:在)?(.+?)(?=" + STOP + r"|$)", "住址", 1.0),
    # ---- 年龄 / 专业 / 工作 ----
    (r"我(?:今年|现在)?(\d{1,2}|[一二三四五六七八九十]{1,4})岁", "年龄", 1.0),
    (r"我(?:学|读)的是?(.+?)专业", "专业", 1.0),
    (r"我在(.+?)(?:上班|工作)", "工作地点", 1.0),
    # ---- 怕 / 忌口 ----
    (r"我(?:很|最|特别)?怕(.+?)(?=" + STOP + r"|$)", "害怕", 1.0),
    (r"我(?:从来)?不吃(.+?)(?=" + STOP + r"|$)", "忌口", 1.0),
    # ---- 计划 ----
    (r"我(?:想|打算|准备)(.+?)(?=" + STOP + r"|$)", "计划", 1.0),
    # ---- 生日 ----
    (r"我(?:的)?生日(?:是)?(.+?)(?=" + STOP + r"|$)", "生日", 1.0),
]

# 上下文推断：不是他明说的，是从话里推出来的 → 0.6
INFER_RULES = [
    (r"(?:考砸|没考好|考了\s*\d+\s*分)", "状态", "考试失利", 0.6),
    (r"(?:熬夜|通宵|一晚上没睡)", "习惯", "熬夜", 0.6),
    (r"(?:分手|吵起来了|掰了)", "状态", "感情不顺", 0.6),
]


def clean(v):
    """去掉句尾的语气词和标点残留。"""
    v = (v or "").strip()
    v = re.sub(r"[的了呀呢吧啊哦嗯嘛]+$", "", v)
    v = v.strip("，,。！!？?、；;：: ")
    return v


def extract_rules(text):
    """规则抽取：返回 [{key, value, confidence, by}]"""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t:
        return []
    out, seen = [], set()
    for pat, key, conf in RULES:
        for m in re.finditer(pat, t):
            val = clean(m.group(1))
            if not val or len(val) > 30:
                continue
            sig = (key, val)
            if sig in seen:
                continue
            seen.add(sig)
            out.append({"key": key, "value": val, "confidence": conf, "by": "rule"})
    for pat, key, val, conf in INFER_RULES:
        if re.search(pat, t):
            sig = (key, val)
            if sig not in seen:
                seen.add(sig)
                out.append({"key": key, "value": val, "confidence": conf, "by": "infer"})
    return out


def extract_model(text, base=None, model=None, timeout=MODEL_TIMEOUT):
    """小模型兜底：问一句本地模型，让它给出 key/value。

    **任何异常都吞掉返回空列表**——超时/没模型/返回不是 JSON，都只当没抽到，
    调用方会把原始情景存下来，回复该多快还是多快。
    """
    base = (base or os.environ.get("OLLAMA_BASE_URL")
            or "http://127.0.0.1:11434/v1").rstrip("/")
    model = model or os.environ.get("OLLAMA_MODEL") or ""
    if not model:
        return []
    sys_msg = ("从用户的一句话里抽「关于他的事实」，只输出 JSON 数组，不要解释。\n"
               "格式：[{\"key\":\"称呼|学校|喜欢|讨厌|住址|作息|计划|其他\","
               "\"value\":\"具体值\"}]\n"
               "抽不出来就输出 []。不要编。")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": sys_msg},
                     {"role": "user", "content": str(text)[:200]}],
        "temperature": 0.1,
        "max_tokens": 120,
        "stream": False,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []
    try:
        content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    except (IndexError, AttributeError):
        return []
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    if isinstance(arr, list):
        for it in arr:
            if not isinstance(it, dict):
                continue
            k, v = clean(it.get("key")), clean(it.get("value"))
            if k and v and len(v) <= 30:
                out.append({"key": k, "value": v,
                            "confidence": 0.4, "by": "model"})
    return out


def extract(text, use_model=False, base=None, model=None):
    """一句话 → 事实列表。规则优先；规则没命中且开了 use_model 才问模型。"""
    items = extract_rules(text)
    if not items and use_model:
        t0 = time.time()
        got = extract_model(text, base=base, model=model)
        # 超时兜底由 urlopen 的 timeout 保证；这里再兜一层，防止解析卡住
        if time.time() - t0 > MODEL_TIMEOUT * 2:
            return []
        items = got
    return items


def similar(a, b):
    """两个字符串的相似度（0~1）。"""
    return difflib.SequenceMatcher(None, str(a), str(b)).ratio()


def _has_by(conn):
    """facts 表有没有 by 列（老库没有，退回不写来源，避免整条插入失败）。"""
    try:
        return any(str(c[1]) == "by" for c in conn.execute("PRAGMA table_info(facts)"))
    except sqlite3.Error:
        return False


def _has_suppressed(conn):
    """facts 表有没有 suppressed 列（N-3 新加；老库要等 main 那边迁移）。"""
    try:
        return any(str(c[1]) == "suppressed"
                   for c in conn.execute("PRAGMA table_info(facts)"))
    except sqlite3.Error:
        return False


def merge_facts(conn, items, evidence=""):
    """把事实写进 facts 表：同 key 且相似度 >0.9 就合并（保留最新证据 + 追加证据链）。

    N-3：**被用户删掉的事实是墓碑，不再自动复活。**
      老逻辑命中软删行会 UPDATE … deleted=0，等于用户点过「删除」之后，
      下次聊到同样的话又偷偷写回来——「忘掉」这个承诺就失效了。
      现在命中软删行只把 suppressed 计数 +1（表示"又提到了，但仍然屏蔽"），
      内容、置信度、证据、时间都不动。只有用户在设置页手动点「恢复」才会复活。

    ⚠ 另一个曾经把事实层写空的坑也保留着：facts 有 UNIQUE(key, value)，
      查重必须**连软删的一起看**，否则再次插入会撞约束，一路被 except 吞掉。

    N-14（审阅意见的四处细节）：
      1. 先截断再查重——key[:64] / value[:500] 的口径与 UNIQUE 索引一致，
         否则截断后才撞约束，前面查重全白做；
      2. 先找精确相同值、再找相似值——避免"库里已有同值行，却又 INSERT
         一条新值撞 UNIQUE(key, value)"；
      3. 同类命中里优先**在用行**而不是第一行——第一行可能是墓碑，
         命中墓碑只会 +1 计数，等于放着在用的那条不更新；
      4. 证据链保留**尾部**而不是头部——ev[:500] 留的是最旧的证据，
         新证据反而被截掉。

    返回 {"inserted":n, "merged":n, "suppressed":n, "ids":[...], "failed":[...]}
    """
    ins = mrg = sup = 0
    ids = []
    failed = []
    now = int(time.time() * 1000)
    with_by = _has_by(conn)
    with_sup = _has_suppressed(conn)
    for it in items:
        # N-14：先截断再查重（与 UNIQUE 索引同口径）
        key = clean(it.get("key"))[:64]
        val = clean(it.get("value"))[:500]
        if not key or not val:
            continue
        conf = float(it.get("confidence") or 0.6)
        by = str(it.get("by") or "rule")[:16]
        try:
            # 同 key 下找目标行（**连软删的一起看**）
            old = conn.execute(
                "SELECT id, value, confidence, evidence, deleted FROM facts "
                "WHERE key=?", (key,)).fetchall()
            # N-14：精确 > 相似；同类里优先在用行，最后才考虑墓碑
            exact_live = [r for r in old
                          if str(r[1]) == val and not int(r[4] or 0)]
            sim_live = [r for r in old
                        if similar(str(r[1]), val) > SIM_MERGE
                        and not int(r[4] or 0)]
            exact_tomb = [r for r in old if str(r[1]) == val]
            sim_tomb = [r for r in old if similar(str(r[1]), val) > SIM_MERGE]
            hit = None
            for cand in (exact_live, sim_live, exact_tomb, sim_tomb):
                if cand:
                    hit = cand[0]
                    break
            if hit and int(hit[4] or 0):
                # 墓碑：只记"又被提到了"，不复活、不改内容
                if with_sup:
                    conn.execute(
                        "UPDATE facts SET suppressed = suppressed + 1 WHERE id=?",
                        (hit[0],))
                sup += 1
            elif hit:
                # N-14：证据链保留尾部（最新的），老证据让位
                ev = (str(hit[3] or "") + "," + str(evidence or "")).strip(",")
                sql = ("UPDATE facts SET value=?, confidence=?, evidence=?, updated_at=?"
                       + (", by=?" if with_by else "") + " WHERE id=?")
                args = [val, max(float(hit[2] or 0), conf), ev[-500:] or None, now]
                if with_by:
                    args.append(by)
                args.append(hit[0])
                conn.execute(sql, tuple(args))
                mrg += 1
                ids.append(hit[0])
            else:
                if with_by:
                    cur = conn.execute(
                        "INSERT INTO facts(key, value, confidence, evidence,"
                        " updated_at, by) VALUES (?,?,?,?,?,?)",
                        (key, val, conf, str(evidence or "")[:500] or None,
                         now, by))
                else:
                    cur = conn.execute(
                        "INSERT INTO facts(key, value, confidence, evidence, updated_at) "
                        "VALUES (?,?,?,?,?)",
                        (key, val, conf, str(evidence or "")[:500] or None, now))
                ins += 1
                ids.append(cur.lastrowid)
        except sqlite3.Error as exc:
            failed.append({"key": key, "error": str(exc)[:160]})
            continue
    conn.commit()
    return {"inserted": ins, "merged": mrg, "suppressed": sup,
            "ids": ids, "failed": failed}


def extract_and_store(db_path, text, episode_id=None, use_model=False,
                      base=None, model=None):
    """一步到位：抽 → 存库。返回 {"ok":.., "items":.., "store":..}"""
    items = extract(text, use_model=use_model, base=base, model=model)
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS facts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              key TEXT NOT NULL, value TEXT NOT NULL,
              confidence REAL NOT NULL DEFAULT 0.6,
              evidence TEXT,
              updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
              deleted INTEGER NOT NULL DEFAULT 0,
              by TEXT NOT NULL DEFAULT 'rule',
              suppressed INTEGER NOT NULL DEFAULT 0,
              UNIQUE(key, value));
        """)
        store = merge_facts(conn, items, evidence=str(episode_id or ""))
    finally:
        conn.close()
    return {"ok": True, "items": items, "store": store}


# ---------------------------------------------------------------- 15 条单测
CASES = [
    ("我叫小诺", "称呼", "小诺"),
    ("我叫小诺，你记住啊", "称呼", "小诺"),
    ("我的名字是小舟", "称呼", "小舟"),   # N-24：测试数据一律合成，不再借真实名
    ("我在实验中学上学", "学校", "实验中学"),
    ("我喜欢吃辣条", "喜欢", "辣条"),
    ("我超爱喝冰的", "喜欢", "喝冰"),
    ("我讨厌香菜", "讨厌", "香菜"),
    ("我不喜欢被比较", "讨厌", "被比较"),
    ("别叫我小狗", "忌称", "小狗"),
    ("我十一点睡", "睡觉时间", "十一"),
    ("我养了一只猫", "养", "一只猫"),
    ("我家住在学校旁边", "住址", "学校旁边"),
    ("我今年十七岁", "年龄", "十七"),
    ("我学的是计算机专业", "专业", "计算机"),
    ("我怕黑", "害怕", "黑"),
    ("我不吃香菜", "忌口", "香菜"),
    ("我想考到六十分", "计划", "考到六十分"),
    ("我的生日是三月五号", "生日", "三月五号"),
]


def selftest():
    print("=" * 62)
    print("记忆抽取器 · 单测（%d 条）" % len(CASES))
    ok = 0
    slow = []
    for text, key, val in CASES:
        t0 = time.time()
        items = extract(text, use_model=False)
        cost = (time.time() - t0) * 1000
        if cost > MODEL_TIMEOUT * 1000:
            slow.append((text, cost))
        hit = next((i for i in items if i["key"] == key and val in str(i["value"])), None)
        good = bool(hit)
        ok += 1 if good else 0
        print("  [%s] %-16s -> %s" % (
            "PASS" if good else "FAIL", text,
            (("%s=%s" % (hit["key"], hit["value"])) if hit
             else "抽到 %s" % ([(i["key"], i["value"]) for i in items] or "空"))))
    print("\n抽取正确：%d / %d（%.0f%%）" % (ok, len(CASES), ok * 100.0 / len(CASES)))

    # 去重：同一句话导两次，facts 里只应有一条
    import tempfile
    db = os.path.join(tempfile.mkdtemp(prefix="puppy_ext_"), "t.db")
    r1 = extract_and_store(db, "我叫小诺", use_model=False)
    r2 = extract_and_store(db, "我叫小诺", use_model=False)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT count(*) FROM facts WHERE key='称呼' AND deleted=0").fetchone()[0]
    conn.close()
    dup_ok = (n == 1)
    print("  [%s] 重复句不产生重复 fact（facts 里称呼条数=%d，期望 1）"
          % ("PASS" if dup_ok else "FAIL", n))

    # 相似度合并：措辞略有不同也应合并
    r3 = extract_and_store(db, "我叫小诺啊", use_model=False)
    conn = sqlite3.connect(db)
    n2 = conn.execute("SELECT count(*) FROM facts WHERE key='称呼' AND deleted=0").fetchone()[0]
    conn.close()
    merge_ok = (n2 == 1)
    print("  [%s] 相似句合并（「我叫小诺啊」后仍为 %d 条，期望 1）"
          % ("PASS" if merge_ok else "FAIL", n2))

    # 单条耗时
    t0 = time.time()
    for text, _, _ in CASES:
        extract(text, use_model=False)
    per = (time.time() - t0) * 1000 / len(CASES)
    time_ok = per < MODEL_TIMEOUT * 1000
    print("  [%s] 单条抽取耗时 %.2f ms（要求 < %.0f ms）"
          % ("PASS" if time_ok else "FAIL", per, MODEL_TIMEOUT * 1000))

    print("=" * 62)
    allok = (ok >= 12) and dup_ok and merge_ok and time_ok and not slow
    print("总判定：%s（抽对 %d/%d，要求 ≥12）"
          % ("PASS" if allok else "FAIL", ok, len(CASES)))
    return 0 if allok else 1


def main():
    args = [a for a in sys.argv[1:]]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--selftest":
        return selftest()
    dbp = None
    if args[0] == "--file" and len(args) >= 3:
        dbp = args[1]
        args = args[2:]
    text = " ".join(args)
    if dbp:
        r = extract_and_store(dbp, text, use_model=False)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(extract(text, use_model=False), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
