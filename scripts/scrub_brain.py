# -*- coding: utf-8 -*-
r"""N-25：外发版语料脱敏 —— 个人 brain.json → release\brain.json。

为什么必须做（任务书 D2 / N-25）
    brain.json 是从真实聊天记录统计出来的语料库，里面带真实称呼、
    生活细节和可能的他人姓名。外发包里必须是脱敏后的版本；
    个人版保留原语料。

规则（scripts\scrub_rules.json）
    1. 身份名（私人词表里的真实称呼）→ 替换为占位符
       （「你」/「小狗」，与外发版人设一致，local_brain 原生支持）；
    2. 其余私人词表词、身份关键词（河南/高三/学校…）、姓名候选
       （姓氏启发式）、手机号/证件号/邮箱/链接 → 该句丢弃；
    3. 删除 built_from 字段；
    4. 只输出计数（总行数、保留行数、各原因丢弃数），不输出词面。

保留率低于 40% 时退出码 2 并提示——不硬放行（任务书要求停下来问）。

用法
    python scripts\scrub_brain.py                 # 按 above 规则脱敏
    python scripts\scrub_brain.py --check         # 只报告不写文件
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import private_words   # N-21：私人词表共享模块（同目录）

PROG = Path(__file__).resolve().parent.parent
SRC = PROG / "brain.json"
RULES = Path(__file__).resolve().parent / "scrub_rules.json"
OUT_DIR = PROG / "release"
OUT = OUT_DIR / "brain.json"

MIN_KEEP_RATIO = 0.40

# 占位符：与外发版人设（release\persona.json）的名字保持一致，
# local_brain 的 OVERRIDE / 前缀剥离都直接支持这两个词。
PLACEHOLDER_PET = "小狗"
PLACEHOLDER_USER = "你"


class _Rules:
    def __init__(self):
        data = json.loads(RULES.read_text(encoding="utf-8"))
        self.identity_words = [str(x) for x in data.get("identity_words", [])]
        chars = "".join(data.get("surname_heuristics", []))
        # 2-3 个汉字、以姓或"小"开头 → 姓名候选
        self.name_re = re.compile(r"[%s][\u4e00-\u9fa5]{1,2}" % re.escape(chars))
        self.patterns = [(p["name"], re.compile(p["re"]))
                         for p in data.get("patterns", [])]


class Counter:
    def __init__(self):
        self.total = 0
        self.kept = 0
        self.dropped = {}

    def add_total(self, n=1):
        self.total += n

    def keep(self, n=1):
        self.kept += n

    def drop(self, why, n=1):
        self.dropped[why] = self.dropped.get(why, 0) + n

    def report(self) -> dict:
        return {"total": self.total, "kept": self.kept,
                "dropped": self.dropped}


def _load_names() -> list:
    """私人词表（含 8.3 短名派生）。身份名 vs 其他私人词分开用。"""
    return private_words.private_names(PROG)


def _identity_names(names: list) -> list:
    """身份名 = 词表里 2 字及以上的词（任务书点名的那两个称呼）。
    词表里就两个真实称呼 + 派生短名；把短名也当身份名替换。"""
    return [n for n in names if len(n) >= 2]


def _scrub_sentence(s: str, rules: _Rules, id_names: list, other_names: list,
                    cnt: Counter, where: str) -> str | None:
    """处理一句话。返回替换后的文本；None = 丢弃。"""
    cnt.add_total()
    if not s or not str(s).strip():
        cnt.drop("空句")
        return None
    text = str(s)
    # 1) 其他私人词 → 丢弃（宁多勿漏）
    for w in other_names:
        if w and w in text:
            cnt.drop("私人词")
            return None
    # 2) 身份关键词 → 丢弃
    for w in rules.identity_words:
        if w in text:
            cnt.drop("身份关键词")
            return None
    # 3) 姓名候选（姓氏启发式）→ 丢弃
    if rules.name_re.search(text):
        cnt.drop("姓名候选")
        return None
    # 4) 手机号/证件号/邮箱/链接 → 丢弃
    for pname, rx in rules.patterns:
        if rx.search(text):
            cnt.drop(pname)
            return None
    # 5) 身份名 → 占位符（角色映射：用户→「你」，她→「小狗」）
    text = _replace_id_names(text, id_names)
    cnt.keep()
    return text


def _role_mapping(id_names: list) -> dict:
    """身份名 → 占位符的角色映射（N-25）。

    词表里的身份名分两种角色：用户本人（owner）与她（name）——
    从个人版 persona.json 的 identity 里读出来对号入座；对不上的
    一律当她的名字处理（宁可错杀到宠物占位，也不把人名留在外面）。
    8.3 短名（前 6 字符 + ~1）跟随其原词。
    """
    try:
        ident = json.loads((PROG / "persona.json").read_text(encoding="utf-8")
                           ).get("identity") or {}
    except Exception:
        ident = {}
    owner = str(ident.get("owner") or "")
    name = str(ident.get("name") or "")
    mapping = {}
    for w in id_names:
        if w == owner:
            mapping[w] = PLACEHOLDER_USER
        else:
            mapping[w] = PLACEHOLDER_PET
    # 派生短名跟随原词
    for w in id_names:
        if w.endswith("~1") and not mapping.get(w):
            base = w[:-2]                     # 去掉 "~1"
            mapping[w] = mapping.get(base, PLACEHOLDER_PET)
    return mapping


def _replace_id_names(text: str, id_names: list) -> str:
    """身份名 → 占位符。长词先替换，避免短名先替换破坏长名。"""
    mapping = _role_mapping(id_names)
    for w in sorted(mapping, key=len, reverse=True):
        text = text.replace(w, mapping[w])
    return text


def _check_sentence_only(s: str, rules: _Rules, other_names: list) -> str | None:
    """只判断要不要丢（不给计数版用）。None = 丢弃。"""
    if not s or not str(s).strip():
        return None
    text = str(s)
    for w in other_names:
        if w and w in text:
            return None
    for w in rules.identity_words:
        if w in text:
            return None
    if rules.name_re.search(text):
        return None
    for _pname, rx in rules.patterns:
        if rx.search(text):
            return None
    return text


def scrub_brain_data(data: dict, rules: "_Rules", id_names: list,
                     other_names: list) -> tuple:
    """对已加载的 brain dict 做脱敏（纯函数，便于测试）。

    返回 (脱敏后的 dict, 计数报告 dict)。不碰文件系统。
    """
    cnt = Counter()

    # ---- persona 块 ----
    persona = data.get("persona") or {}
    persona_out = dict(persona)
    for key in ("name", "user"):
        cnt.add_total()
        if key in persona:
            persona_out[key] = PLACEHOLDER_PET if key == "name" else PLACEHOLDER_USER
            cnt.keep()
    for key in ("tics", "style_note"):
        cnt.add_total()
        v = persona.get(key)
        if v is None:
            cnt.drop("空句")
            persona_out.pop(key, None)
            continue
        kept = _check_sentence_only(str(v), rules, other_names)
        if kept is None:
            cnt.drop("persona." + key)
            persona_out.pop(key, None)
        else:
            persona_out[key] = _replace_id_names(kept, id_names)
            cnt.keep()
    data["persona"] = persona_out
    data.pop("built_from", None)   # N-25：删除来源痕迹

    # ---- kw：{触发词: [[回复句, 权重], ...]} ----
    kw_out = {}
    kw = data.get("kw") or {}
    for trigger, pairs in kw.items():
        cnt.add_total()
        t = str(trigger)
        # N-25：触发词本身也要过筛——语料里的身份名也会被当成触发词
        # 统计出来。词是整体，用 fullmatch 姓名启发式 +
        # 包含式私人词/身份关键词，命中连键一起丢。
        bad = any((x and x in t) for x in other_names) \
            or any((x and x in t) for x in rules.identity_words) \
            or bool(rules.name_re.fullmatch(t))
        if bad:
            cnt.drop("kw 触发词")
            continue
        kept_pairs = []
        for pair in (pairs or []):
            if not isinstance(pair, (list, tuple)) or not pair:
                continue
            sent = _scrub_sentence(str(pair[0]), rules, id_names,
                                   other_names, cnt, "kw")
            if sent is not None:
                kept_pairs.append([sent, pair[1] if len(pair) > 1 else 1])
        if kept_pairs:
            kw_out[_replace_id_names(t, id_names)] = kept_pairs
        cnt.keep()
    data["kw"] = kw_out

    # ---- freq：[[词, 频次], ...]（高频词，命中隐私即剔除该词） ----
    freq_out = []
    for item in (data.get("freq") or []):
        cnt.add_total()
        if not isinstance(item, (list, tuple)) or not item:
            cnt.drop("空句")
            continue
        w = str(item[0])
        bad = False
        for x in other_names:
            if x in w:
                bad = True
                break
        if not bad:
            for x in rules.identity_words:
                if x in w:
                    bad = True
                    break
        if not bad and rules.name_re.fullmatch(w):
            bad = True
        if bad:
            cnt.drop("freq 词命中")
            continue
        freq_out.append([_replace_id_names(w, id_names), item[1]])
        cnt.keep()
    data["freq"] = freq_out

    # ---- intents / openers / moods / a_asks：句子列表 ----
    def scrub_list(lst, label):
        out = []
        for s in (lst or []):
            kept = _scrub_sentence(str(s), rules, id_names, other_names,
                                   cnt, label)
            if kept is not None:
                out.append(kept)
        return out

    for key in ("openers", "a_asks"):
        if isinstance(data.get(key), list):
            data[key] = scrub_list(data[key], key)
    moods = data.get("moods") or {}
    moods_out = {}
    for mood, lst in moods.items():
        if isinstance(lst, list):
            moods_out[mood] = scrub_list(lst, "moods")
        else:
            moods_out[mood] = lst
    data["moods"] = moods_out
    intents = data.get("intents") or {}
    intents_out = {}
    for intent, lst in intents.items():
        intents_out[intent] = scrub_list(lst, "intents") if isinstance(lst, list) else lst
    data["intents"] = intents_out

    return data, cnt.report()


def scrub(check_only: bool = False) -> int:
    rules = _Rules()
    names = _load_names()
    if not names:
        print("私人词表为空：脱敏需要词表来定位身份名"
              "（PUPPY_PRIVATE_NAMES 或 private_names.local.txt）",
              file=sys.stderr)
        return 1
    id_names = _identity_names(names)
    # 其他私人词 = 词表里非身份名部分（本机词表就身份名一项时为空，
    # 但保留逻辑让未来的词表扩展自动生效）
    other_names = [w for w in names if w not in id_names and len(w) >= 2]

    data = json.loads(SRC.read_text(encoding="utf-8"))
    data, rep = scrub_brain_data(data, rules, id_names, other_names)

    ratio = rep["kept"] / rep["total"] if rep["total"] else 0.0
    print(json.dumps({"total": rep["total"], "kept": rep["kept"],
                      "keep_ratio": round(ratio, 4),
                      "dropped": rep["dropped"]}, ensure_ascii=False, indent=1))

    if ratio < MIN_KEEP_RATIO:
        print("保留率 %.1f%% 低于 %d%% 门槛：不硬放行，停下来等确认"
              % (ratio * 100, MIN_KEEP_RATIO * 100), file=sys.stderr)
        return 2

    if check_only:
        print("--check：不写文件")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    print("已写入 %s" % OUT)
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="外发版语料脱敏")
    ap.add_argument("--check", action="store_true", help="只报告，不写文件")
    args = ap.parse_args(argv)
    return scrub(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
