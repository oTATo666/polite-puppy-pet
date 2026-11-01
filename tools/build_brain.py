#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从真实聊天语料离线构建"本地大脑"brain.json。

这是整个本地角色框架唯一需要跑一次的步骤：把 7695 条小狗发言
压缩成一个几百 KB 的检索式大脑，运行期不再需要任何模型、网络或 GPU。

产出 brain.json 结构（均为纯 JSON，加载即用）：
  kw      : 关键词 -> 该词出现后小狗最可能说的若干句话（语料共现统计）
  freq    : 小狗高频原句（风格底座 + 兜底）
  intents : 意图 -> 该意图下小狗的真实说法
  openers : 开场问候语池（角色主动开口用）
  asks    : 反问/主动提问池（随机搭话用，保证会带上话题词）
  moods   : 情绪 -> 该情绪下的说法
  persona : 人格参数（平均句长、口癖、表情包频率…）

用法：
  python tools/build_brain.py --corpus <日记.txt> --out brain.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---- 过滤：这些是真人隐私/冲突内容，不该被角色复述 ----
BLOCK = [
    "删好友", "逼疯", "道德绑架", "负罪感", "自杀", "去死", "分手",
    "买号", "代练", "手机号", "身份证", "银行卡", "转账", "红包",
]
PHONE_RE = re.compile(r"1[3-9]\d{9}")
URL_RE = re.compile(r"https?://|www\.")
MEDIA_RE = re.compile(r"^\[.*?\]$")

# 标点归一：语料里小狗爱用「。」结尾，保留但要能比较
PUNC_RE = re.compile(r"[\s　]+")

# ---- 意图定义：user_side 用来判定对方意图，dog_side 用来捞小狗的真实说法 ----
INTENTS = {
    "greet":  (r"^(你好|您好|hi|hello|哈喽|嗨|在吗|在么|有人吗|早|早上好|中午好|晚上好)", r"^(你好|哈喽|嗨|hi|在|来啦|我来了|嗯|咋了)"),
    "bye":    (r"(拜拜|再见|走了|先走|睡了|晚安|不聊|下了|下线|回见|撤了)", r"(拜拜|晚安|睡|走|先不聊|下次)"),
    "howare": (r"(咋样|怎么样|还好吗|好吗|最近咋|最近怎么样|过得好)", r"(还行|不咋样|就那样|还活着|凑合|一般)"),
    "play":   (r"(玩游戏|打游戏|开黑|原神|王者|游戏|玩吗|一起玩|陪我玩)", r"(玩|不玩|来|开|打|游戏)"),
    "eat":    (r"(吃饭|吃了吗|饿|外卖|零食|吃什么|好吃的)", r"(吃|饿|饭|零食|没吃|吃了)"),
    "sleep":  (r"(睡觉|困|熬夜|早起|睡不着|通宵|作息)", r"(睡|困|熬夜|早睡|觉)"),
    "sad":    (r"(难过|难受|想哭|哭了|委屈|心烦|崩溃|不开心|emo|低落|累|疲惫|压力大)", r"(咋了|别|陪|抱|没事|我在|不哭|怎么了)"),
    "happy":  (r"(开心|高兴|哈哈|不错|挺好|棒|牛|nice|厉害|成功|过了|赢了)", r"(6|666|牛|不错|厉害|哈哈|可以)"),
    "angry":  (r"(生气|烦死|滚|闭嘴|讨厌|骂|有病|离谱|过分)", r"(别管|不|哼|别|咋了|不管)"),
    "name":   (r"(你叫什么|名字|你是谁|怎么称呼|你是哪只)", r"(千逐|我是|我叫)"),
    "ai":     (r"(你是ai|你是AI|人工智能|机器人|程序|模型|代码)", r"(不是|我是|狗|汪|啥)"),
    "love":   (r"(喜欢你|爱你|想你|亲|抱抱|么么|贴贴|喜欢我吗)", r"(喜欢|爱|想|抱|也|嗯)"),
    "thanks": (r"(谢谢|感谢|多谢|thx|thanks)", r"(不用|客气|没事|嗯)"),
    "photo":  (r"(看图|图片|照片|发你|看看这个|截图|拍的|这张)", r"(这|啥|哪|看|图|拍)"),
    "work":   (r"(上班|工作|加班|忙|开会|学习|作业|考试|写不完|赶)", r"(忙|累|加油|别|先)"),
    "busy":   (r"(等下|等会|一会儿|忙完|晚点|先不)", r"(行|嗯|好|等你|不急)"),
}

# 口癖 / 叠字：人格画像里统计出来的风格指纹
TICS = ["昂", "咋", "啥", "为啥", "不为啥", "嗯", "哦", "6", "666", "拜拜", "不玩", "别管"]


def norm(s: str) -> str:
    return PUNC_RE.sub("", s or "").strip()


def clean_dog(text: str) -> str:
    """判断一句小狗发言能不能进大脑。"""
    t = norm(text)
    if not t:
        return ""
    if MEDIA_RE.match(t):           # [表情包] [图片] 之类
        return ""
    if URL_RE.search(t) or PHONE_RE.search(t):
        return ""
    if any(b in t for b in BLOCK):
        return ""
    if len(t) > 18:                 # 长句基本都是吵架/长故事，不适合陪伴体
        return ""
    if len(t) < 1:
        return ""
    # 纯标点、纯系统提示
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", t):
        return ""
    return t


def clean_user(text: str) -> str:
    t = norm(text)
    if not t or MEDIA_RE.match(t):
        return ""
    if len(t) > 40:
        return ""
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", t):
        return ""
    return t


def grams(text: str) -> list[str]:
    """把一句中文切成 2~3 字片段（无分词器的轻量做法）。"""
    seg = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text)
    out: list[str] = []
    for s in seg:
        if len(s) < 2:
            continue
        for i in range(len(s) - 1):
            out.append(s[i:i + 2])
        if len(s) >= 3:
            for i in range(len(s) - 2):
                out.append(s[i:i + 3])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="小狗日记_按日期.txt")
    ap.add_argument("--out", default="brain.json")
    ap.add_argument("--max-kw", type=int, default=1600)
    ap.add_argument("--max-reply", type=int, default=8)
    args = ap.parse_args()

    corpus = Path(args.corpus)
    if not corpus.is_file():
        print(f"找不到语料：{corpus}")
        return 2

    # ---- 1. 解析语料为 (说话人, 文本) 序列 ----
    line_re = re.compile(r"^\[\d{2}:\d{2}\]\s*(小狗|郑郑)：(.*)$")
    turns: list[tuple[str, str]] = []
    day_first: list[str] = []
    seen_day = False
    with corpus.open("r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if line.startswith("📅"):
                seen_day = True
                continue
            m = line_re.match(line.strip())
            if not m:
                continue
            who, text = m.group(1), m.group(2)
            if who == "小狗":
                t = clean_dog(text)
                if t:
                    if seen_day:
                        day_first.append(t)
                        seen_day = False
                    turns.append(("dog", t))
            else:
                t = clean_user(text)
                if t:
                    turns.append(("user", t))
    if not turns:
        print("语料里没解析出对话")
        return 2
    print(f"解析：{len(turns)} 条有效发言（小狗 {sum(1 for w, _ in turns if w == 'dog')}）")

    # ---- 2. 小狗原句频次 ----
    dog_freq: Counter = Counter(t for w, t in turns if w == "dog")
    print(f"不同原句：{len(dog_freq)}")

    # ---- 3. 关键词 -> 小狗回应（真正的"语料共现"） ----
    pair: dict[str, Counter] = defaultdict(Counter)
    for i, (who, text) in enumerate(turns):
        if who != "user":
            continue
        # 往后看 2 条，取第一条小狗发言
        reply = ""
        for j in range(i + 1, min(i + 3, len(turns))):
            if turns[j][0] == "dog":
                reply = turns[j][1]
                break
        if not reply:
            continue
        seen = set()
        for g in grams(text):
            if g in seen:
                continue
            seen.add(g)
            pair[g][reply] += 1

    # 文档频率过滤：只在 1~2 条里出现过的片段基本是噪声
    kw_items = [(k, c) for k, c in pair.items() if sum(c.values()) >= 3]
    kw_items.sort(key=lambda kv: -sum(kv[1].values()))
    kw_items = kw_items[: args.max_kw]
    kw = {k: [[r, n] for r, n in c.most_common(args.max_reply)] for k, c in kw_items}
    print(f"关键词索引：{len(kw)} 条")

    # ---- 4. 意图 -> 小狗真实说法 ----
    intents: dict[str, list[list]] = {}
    for name, (u_pat, d_pat) in INTENTS.items():
        dr = re.compile(d_pat)
        hits = [(t, n) for t, n in dog_freq.items() if dr.search(t)]
        hits.sort(key=lambda x: (-x[1], len(x[0])))
        intents[name] = [[t, n] for t, n in hits[:40]]
        print(f"  意图 {name}: {len(intents[name])} 条")

    # ---- 5. 开场问候 / 主动提问 ----
    openers: list[str] = []
    o_re = re.compile(r"^(你好|哈喽|嗨|hi|在|来啦|我来了|早|晚上好|咋了|汪|哼)")
    for t, n in dog_freq.most_common(400):
        if o_re.search(t) and len(t) <= 8:
            openers.append(t)
    for t in day_first[:400]:
        if len(t) <= 10 and t not in openers:
            openers.append(t)
    openers = (openers or ["咋了", "我在呢", "汪", "哼", "你来了"])[:60]
    print(f"开场语池：{len(openers)}")

    a_re = re.compile(r"(？|\?|咋|啥|为啥|怎么|吗|呢|谁|哪|几)")
    asks = [t for t, n in dog_freq.most_common(1500)
            if a_re.search(t) and 1 <= len(t) <= 10]
    asks = asks[:80] or ["咋了", "你在干嘛", "啥呀", "为啥"]
    print(f"主动提问池：{len(asks)}")

    # ---- 6. 情绪 -> 说法 ----
    moods = {
        "sad": [t for t, n in dog_freq.most_common(1200) if re.search(r"(咋了|别|陪|抱|没事|我在|怎么了|不哭)", t)][:24],
        "happy": [t for t, n in dog_freq.most_common(1200) if re.search(r"(6|666|牛|哈哈|不错|厉害|行)", t)][:24],
        "angry": [t for t, n in dog_freq.most_common(1200) if re.search(r"(别管|不管|哼|不|不要)", t)][:24],
        "calm": [t for t, n in dog_freq.most_common(600)][:40],
    }

    total = sum(dog_freq.values())
    brain = {
        "v": 1,
        "built_from": corpus.name,
        "persona": {
            "name": "千逐",
            "user": "郑郑",
            "avg_len": round(sum(len(t) * n for t, n in dog_freq.items()) / max(total, 1), 2),
            "sticker_rate": 0.21,
            "tics": TICS,
            "style_note": "极短句为主；爱用叠字、反问、语气词；不解释、不客套。",
        },
        "kw": kw,
        "freq": [[t, n] for t, n in dog_freq.most_common(1500)],
        "intents": intents,
        "openers": openers,
        "asks": asks,
        "moods": moods,
    }

    out = Path(args.out)
    out.write_text(json.dumps(brain, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size = out.stat().st_size
    print(f"\n已写入 {out}  {size/1024:.0f} KB")
    print(f"平均句长 {brain['persona']['avg_len']} 字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
