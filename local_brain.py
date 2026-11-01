# -*- coding: utf-8 -*-
"""本地小型角色大脑 —— 不依赖任何外部 API / 模型 / GPU。

它不是一个语言模型，而是一台"角色推理机"：
    感知 → 关联 → 决策 → 生成 → 自检
全部在 CPU 上用纯 Python 完成，常驻内存约 1~2 MB，显存占用 0。

素材来自真实聊天语料离线构建的 brain.json（tools/build_brain.py 生成），
所以角色的每一句话都是"它真的说过的话"，不是模板拼出来的塑料味。

对外接口：
    from local_brain import brain
    brain.reply("今天好累", history)   # history: [{"role": "user"|"assistant", "text": "..."}]
    brain.greet()
    brain.idle(history)
返回：
    {"reply": str, "mood": str, "topic": str, "strategy": str,
     "thought": [str, ...], "source": "local-brain"}
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import threading
from pathlib import Path

# ---------------------------------------------------------------- 数据加载

_DATA_LOCK = threading.Lock()
_DATA: dict | None = None
_DATA_PATH: Path | None = None
_FAILED = ""


def _default_path() -> Path:
    """brain.json 的位置：frozen 时在 exe 旁边，开发时在源码目录。"""
    if getattr(sys, "frozen", False):  # pragma: no cover
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return base / "brain.json"


def data_path(path: str | os.PathLike | None = None) -> Path:
    global _DATA_PATH
    if path:
        _DATA_PATH = Path(path)
    if _DATA_PATH is None:
        _DATA_PATH = _default_path()
    return _DATA_PATH


def load(force: bool = False) -> dict | None:
    """惰性加载：不是本地大脑模式就一分钱内存都不占。"""
    global _DATA, _FAILED
    if _DATA is not None and not force:
        return _DATA
    with _DATA_LOCK:
        if _DATA is not None and not force:
            return _DATA
        p = data_path()
        try:
            raw = p.read_text("utf-8")
            _DATA = json.loads(raw)
            _FAILED = ""
        except FileNotFoundError:
            _FAILED = f"没有大脑数据：{p}（先跑 tools/build_brain.py）"
            _DATA = None
        except (ValueError, OSError) as exc:
            _FAILED = f"大脑数据坏了：{exc}"
            _DATA = None
    return _DATA


def status() -> dict:
    d = load()
    return {
        "ok": d is not None,
        "path": str(data_path()),
        "size_kb": round(data_path().stat().st_size / 1024) if data_path().is_file() else 0,
        "keywords": len((d or {}).get("kw", {})),
        "sentences": len((d or {}).get("freq", [])),
        "error": _FAILED,
    }


# ---------------------------------------------------------------- 文本工具

PUNC = re.compile(r"[\s　]+")
CN = re.compile(r"[\u4e00-\u9fff]+")
Q_RE = re.compile(r"(？|\?|吗|呢|啥|咋|为啥|怎么|谁|哪|几|多少)")

STOP_GRAM = {
    "的时", "的不", "我不", "你不", "就不", "也不", "都不", "还不", "是不",
    "了什", "的了", "吗啊", "啊啊", "什么", "怎么", "这个", "那个", "我们",
    "你们", "他们", "可以", "不是", "就是", "没有", "知道", "不知道",
    # 时间/程度词几乎每句都有，当话题词没有区分度
    "今天", "明天", "昨天", "现在", "时候", "一下", "一点", "有点", "一直",
    "然后", "就是", "还是", "不过", "所以", "如果", "因为", "而且",
}


def norm(s: str) -> str:
    return PUNC.sub("", s or "").strip()


def grams(text: str) -> list[str]:
    out: list[str] = []
    for seg in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text or ""):
        if len(seg) < 2:
            continue
        for i in range(len(seg) - 1):
            out.append(seg[i:i + 2])
        if len(seg) >= 3:
            for i in range(len(seg) - 2):
                out.append(seg[i:i + 3])
    return out


def bigrams(text: str) -> set[str]:
    s = norm(text)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


# ---------------------------------------------------------------- 意图 / 情绪

INTENT_RULES: list[tuple[str, str]] = [
    ("bye",    r"(拜拜|再见|走了|先走|睡了|晚安|不聊|下了|回见|撤了|下线)"),
    ("greet",  r"(你好|您好|哈喽|嗨|hi|hello|在吗|在么|早上好|中午好|晚上好|我来了)"),
    ("sleep",  r"(睡觉|困|熬夜|早起|睡不着|通宵|作息|睡吗)"),
    ("eat",    r"(吃饭|吃了吗|饿了|外卖|零食|吃什么|好吃的|饭没)"),
    ("play",   r"(玩游戏|打游戏|开黑|原神|王者|游戏|玩吗|一起玩|陪我玩)"),
    ("work",   r"(上班|工作|加班|忙|开会|学习|作业|考试|写不完|赶工)"),
    ("sad",    r"(难过|难受|想哭|哭了|委屈|心烦|崩溃|不开心|emo|低落|好累|累了|疲惫|压力大|烦死)"),
    ("happy",  r"(开心|高兴|哈哈|不错|挺好|好棒|牛|厉害|成功|过了|赢了|nice)"),
    ("angry",  r"(生气|滚|闭嘴|讨厌|骂|有病|离谱|过分|烦人)"),
    ("love",   r"(喜欢你|爱你|想你|抱抱|么么|贴贴|喜欢我吗|亲一)"),
    ("thanks", r"(谢谢|感谢|多谢|thx|thanks)"),
    ("name",   r"(你叫什么|名字|你是谁|怎么称呼)"),
    ("ai",     r"(你是ai|你是AI|人工智能|机器人|程序|模型|代码|大模型)"),
    ("photo",  r"(看图|图片|照片|发你|看看这个|截图|拍的|这张|照片里)"),
    ("busy",   r"(等下|等会|一会儿|忙完|晚点|先不|稍等)"),
    ("howare", r"(咋样|怎么样|还好吗|好吗|最近咋|最近怎么样|过得好)"),
]

MOOD_OF_INTENT = {
    "bye": "calm", "greet": "happy", "sleep": "calm", "eat": "happy",
    "play": "happy", "work": "calm", "sad": "sad", "happy": "happy",
    "angry": "angry", "love": "happy", "thanks": "happy", "ai": "angry",
    "photo": "happy", "busy": "calm", "howare": "calm", "name": "calm",
}

# 某些意图下，语料里会混进明显不对味的话（"难过"时冒出"别管"），这里挡掉
BAD_FOR_INTENT: dict[str, str] = {
    "sad":    r"(别管|不管|滚|闭嘴|不玩|拜拜|不理)",
    "angry":  r"(拜拜|爱你|喜欢)",
    "thanks": r"(拜拜|不玩|别管|不理)",
    "love":   r"(拜拜|别管|不玩|滚)",
    "name":   r"(拜拜|别管|不玩)",
    "photo":  r"(拜拜|别管|不玩)",
    "greet":  r"(拜拜|不玩|别管)",
    "howare": r"(拜拜|不玩|别管)",
    "sleep":  r"(不玩|别管|滚)",
    "work":   r"(拜拜|滚|别管|不管|不理)",
    "eat":    r"(拜拜|滚|闭嘴)",
    "play":   r"(拜拜|滚|闭嘴)",
    "ai":     r"(拜拜|闭嘴|滚)",
    "busy":   r"(拜拜|滚|闭嘴|别管)",
}

# 有些意图用词太宽（"别"会捞到"别买""别拉我"），改用白名单收紧
OK_FOR_INTENT: dict[str, str] = {
    "sad":   r"(咋了|怎么了|咋啦|没事|我在|陪|抱|不哭|别哭|别怕|心疼|难受|抱抱|咋整)",
    "work":  r"(忙|累|加油|辛苦|休息|别太|注意|慢慢|熬|下班|早回|先忙|先不聊)",
    "eat":   r"(吃|饿|饭|零食|外卖|好吃|不吃|没吃)",
    "sleep": r"(睡|困|熬夜|早睡|觉|晚安|醒)",
}

# 这些话在任何"非吵架"语境下都不该冒出来（语料里有吵架片段）
GLOBAL_BAD = re.compile(r"(闭嘴|滚开|有病|去死|神经|傻逼|滚蛋)")

# 少数必须说准的（都是它真的说过的话）
OVERRIDE: dict[str, str] = {
    "name": "我是千逐.",
}

# 对方明显低落时，先给一句真正接得住的话（语料里它就这么开头）
EMPATHY = ["咋了", "怎么了", "咋啦", "我在", "没事吧", "咋整", "咋回事"]

# 意图 -> 回话策略（决定"怎么说"，不是"说什么"）
STRATEGY_OF_INTENT = {
    "bye": "接住告别，短促收尾",
    "greet": "回一句招呼，不客套",
    "sleep": "关心作息，别啰嗦",
    "eat": "顺着吃的话题接一句",
    "play": "对玩的事有反应，带点兴趣",
    "work": "知道你在忙，给个台阶",
    "sad": "先接住情绪，再给一句陪伴",
    "happy": "跟着高兴，用它的腔调捧一句",
    "angry": "不硬碰，短句带过",
    "love": "害羞式回应，别太直白",
    "thanks": "不用谢，一带而过",
    "name": "报名字，短",
    "ai": "不承认也不解释，耍赖",
    "photo": "对看到的东西给反应",
    "busy": "表示等着，不催",
    "howare": "给个真实状态",
}


def detect_intent(text: str) -> str:
    t = norm(text)
    for name, pat in INTENT_RULES:
        if re.search(pat, t, re.I):
            return name
    if Q_RE.search(t):
        return "ask"
    return "chat"


# ---------------------------------------------------------------- 大脑

class Brain:
    """角色推理机。无状态（每次从 history 重建上下文），纯 CPU。"""

    MAX_LEN = 18          # 角色平均句长 4.26 字，超过就不像它了
    DEDUP_WINDOW = 6      # 最近这么多轮里说过的话不再说
    MIN_TOPIC_HITS = 6    # 共现次数低于这个的片段不配当话题
    HYBRID_MAX_CHARS = 8  # 借用外部小模型时，过了这个长度就不像它了

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        if path:
            data_path(path)
        self._rng = random.Random()

    # ---- 对外：主动开场 ----
    def greet(self, hour: int | None = None, avoid: list[str] | None = None) -> dict:
        d = load()
        thought: list[str] = []
        if not d:
            return self._dead("greet")
        h = self._hour(hour)
        pool = list(d.get("openers") or [])
        thought.append(f"时机：{h}点，主动开口打招呼")
        # 深夜/清晨先给一句贴合时间的
        if h >= 23 or h < 6:
            pool = [x for x in pool if re.search(r"(睡|晚安|夜|困|还没|咋还)", x)] + pool
        elif 6 <= h < 11:
            pool = [x for x in pool if re.search(r"(早|起了|醒|上午)", x)] + pool
        avoid = set(avoid or [])
        noise = re.compile(r"(邀请|添加|撤回|系统|以上|好友|消息|已添|通话|红包)")
        cands = [x for x in pool
                 if x not in avoid and not noise.search(x) and len(x) <= 10]
        cands = cands or pool or ["咋了"]
        pick = self._rng.choice(cands[:20])
        thought.append(f"候选 {len(cands)} 条 → 选定「{pick}」")
        return self._out(pick, "happy", "", "主动打招呼", thought)

    # ---- 对外：随机搭话（必须挂上前面的话题） ----
    def idle(self, history: list[dict], hour: int | None = None) -> dict:
        d = load()
        if not d:
            return self._dead("idle")
        ctx = self._context(history)
        thought: list[str] = []
        topic = ctx["topic"]
        thought.append(f"回顾：最近话题「{topic or '（还没有）'}」")

        asks = list(d.get("asks") or [])
        recent = ctx["recent_dog"]
        kw = d.get("kw") or {}

        # 依次降级：带话题的问句 → 该话题下它自己说过的问句 → 该话题下它的原话 → 通用问句
        # 全程不拼接，避免出现"加班到咋咋咋咋"这种半截话
        if topic:
            hit = [a for a in asks if topic in a]
            thought.append(f"带话题「{topic}」的问句 {len(hit)} 条")
            if hit and self._rng.random() < 0.8:
                pick = self._rng.choice(hit[:12])
                thought.append(f"决策：接着刚才的「{topic}」往下问")
                return self._out(pick, "calm", topic, "接上文主动搭话", thought)
            kwr = [r for r, _ in (kw.get(topic) or []) if r not in recent]
            q = [r for r in kwr if Q_RE.search(r)]
            if q and self._rng.random() < 0.65:
                pick = self._rng.choice(q[:6])
                thought.append("决策：用它在「%s」这个话题下问过的原话" % topic)
                return self._out(pick, "calm", topic, "接上文主动搭话", thought)
            if kwr and self._rng.random() < 0.5:
                pick = self._rng.choice(kwr[:6])
                thought.append("决策：拿它在「%s」话题下的老话起头" % topic)
                return self._out(pick, "calm", topic, "接上文主动搭话", thought)
        pool = [a for a in asks if a not in recent] or asks or ["咋了"]
        pick = self._rng.choice(pool[:24])
        thought.append("决策：话题不突出，随便起个话头")
        thought.append(f"选定「{pick}」")
        return self._out(pick, "calm", topic, "主动搭话", thought)

    # ---- 对外：正式回答 ----
    def reply(self, user_text: str, history: list[dict] | None = None,
              hour: int | None = None, intimacy: int = 0) -> dict:
        d = load()
        if not d:
            return self._dead("reply")
        text = norm(user_text)
        thought: list[str] = []
        if not text:
            return self._out("嗯？", "calm", "", "没听清", ["听到的是空话，回一个问号"])

        ctx = self._context(history or [])
        thought.append(f"听到：「{text[:24]}」")

        # 1) 感知：意图 + 情绪
        intent = detect_intent(text)
        mood = MOOD_OF_INTENT.get(intent, "calm")
        thought.append(f"感知：意图={intent}，情绪={mood}")

        # 2) 关联：这次的话题 vs 上一轮的话题
        topic = self._topic(text, d) or ctx["topic"]
        same = bool(topic and topic == ctx["topic"])
        thought.append(
            f"关联：本轮话题「{topic or '无'}」，上一轮「{ctx['topic'] or '无'}」"
            + (" → 同一件事，接着说" if same else " → 换话题了，跟着换"))

        # 3) 回指：上一句它是问句，用户现在是在回答
        pending = ctx.get("pending_question", "")
        if pending:
            thought.append(f"回指：上一句我问过「{pending[:10]}」，他这是在答我")
            if len(text) <= 6:
                thought.append("判断：答得很短，先接住再说别的")

        # 4) 决策
        strategy = STRATEGY_OF_INTENT.get(intent, "顺着他的话接一句")
        thought.append(f"决策：{strategy}")

        # 5) 生成候选
        cands: dict[str, float] = {}
        self._add(cands, self._from_kw(text, d), 1.0)
        self._add(cands, self._from_intent(intent, d), 1.0)
        self._add(cands, self._from_mood(mood, d), 0.6)
        if not cands:
            self._add(cands, [t for t, _ in d.get("freq", [])[:80]], 0.3)
        thought.append(f"召回：{len(cands)} 条候选")
        if intent == "name" and self._rng.random() < 0.8:
            pick = OVERRIDE["name"]
            thought.append(f"判断：他在问我名字，直接报：{pick}")
            return self._out(pick, mood, topic or "名字", strategy, thought)

        # 情绪优先：他明显低落时，别拿"嗯"敷衍过去
        if intent == "sad" and self._rng.random() < 0.7:
            recent_now = ctx["recent_dog"]
            pool = [x for x in EMPATHY if x not in recent_now] or EMPATHY
            pick = self._rng.choice(pool)
            thought.append(f"判断：他情绪低落，先接住再说别的 → {pick}")
            return self._out(pick, "sad", topic or "情绪", strategy, thought)

        # 6) 打分
        ub = bigrams(text)
        recent = ctx["recent_dog"]
        scored: list[tuple[str, float]] = []
        for cand, w in cands.items():
            s = math.log(1.0 + w)
            if ub & bigrams(cand):
                s += 2.2                       # 跟他这句话有字面重叠 = 在回应他
            if topic and topic in cand:
                s += 3.0                       # 带着当前话题词 = 上下文连贯
            if same and ctx["topic"] and ctx["topic"] in cand:
                s += 1.2                       # 延续上一轮话题
            if cand in recent:
                s -= 8.0                       # 最近说过，别重复
            if recent and cand == recent[0]:
                s -= 6.0
            L = len(cand)
            if L <= 6:
                s += 1.0                       # 角色爱说短句
            elif L > 12:
                s -= 1.6
            s += self._rng.random() * 0.9      # 留一点随机，别每次都一样
            scored.append((cand, s))
        scored.sort(key=lambda x: -x[1])
        top = scored[:6]
        pick = self._rng.choice(top)[0] if top else "嗯"
        thought.append(f"打分：最高 {top[0][1]:.1f}「{top[0][0]}」→ 选定「{pick}」")

        # 7) 自检：太不像它 / 重复了 → 重挑
        if len(pick) > self.MAX_LEN or (recent and pick == recent[0]):
            alt = next((c for c, _ in scored if c not in recent and len(c) <= self.MAX_LEN), "")
            if alt:
                thought.append(f"自检：原句不合格（太长或刚说过），改「{alt}」")
                pick = alt
        thought.append(f"自检：{len(pick)} 字，符合它 {d['persona']['avg_len']} 字的说话习惯")

        return self._out(pick, mood, topic, strategy, thought)

    # ---- 独立思考：只下判断、不下笔 ----
    def deliberate(self, user_text: str, history: list[dict] | None = None,
                   hour: int | None = None) -> dict:
        """产出一份"这一句该怎么说"的计划，把"判断"和"措辞"分开。

        这是人格层与执行层解耦的关键：
          计划（意图/情绪/话题/策略/硬约束）永远由本机大脑出，
          措辞可以交给别的小模型起稿，但必须过这里的 enforce()。
        """
        d = load()
        text = norm(user_text)
        if not d:
            return self._dead_plan(text)
        ctx = self._context(history or [])
        intent = detect_intent(text)
        mood = MOOD_OF_INTENT.get(intent, "calm")
        topic = self._topic(text, d) or ctx["topic"]
        strategy = STRATEGY_OF_INTENT.get(intent, "顺着他的话接一句")
        return {
            "text": text, "intent": intent, "mood": mood, "topic": topic,
            "strategy": strategy, "same_topic": bool(topic and topic == ctx["topic"]),
            "recent": ctx["recent_dog"], "pending": ctx.get("pending_question", ""),
            "max_chars": self.HYBRID_MAX_CHARS,
            "tics": (d.get("persona") or {}).get("tics", [])[:6],
            "thought": [
                f"听到：「{text[:24]}」",
                f"感知：意图={intent}，情绪={mood}",
                f"关联：话题「{topic or '无'}」",
                f"决策：{strategy}",
            ],
        }

    def _dead_plan(self, text: str) -> dict:
        return {"text": text, "intent": "chat", "mood": "calm", "topic": "",
                "strategy": "先接住", "recent": [], "max_chars": self.HYBRID_MAX_CHARS,
                "tics": [], "thought": [_FAILED or "大脑没加载"]}

    # ---- 人格闸门：外部草稿必须过这一关 ----
    def enforce(self, draft: str, plan: dict, recent: list[str] | None = None) -> dict:
        """把别的小模型起的话按人格过一遍。

        过不了就整句丢掉（返回 ok=False），由调用方回落到语料原句。
        绝不出现"模型口吻 + 小狗皮肤"的缝合怪。
        """
        recent = list(recent if recent is not None else (plan.get("recent") or []))
        p = norm(draft).strip()
        thought = list(plan.get("thought") or [])

        # 去掉各种包装：引号、角色前缀、称呼
        p = p.strip("\"'“”‘’《》（）()")
        p = re.sub(r"^(千逐|小狗|我)[:：]\s*", "", p)
        p = re.sub(r"^[,，、。！？!?.~\s]+", "", p)
        # 只取第一句：它从来不说一长串
        p = re.split(r"[。！？!?；;\n]", p)[0].strip()
        p = re.sub(r"[。！？!?，,、]+$", "", p)

        if not p:
            return {"ok": False, "reason": "空", "thought": thought + ["把关：草稿是空的，弃用"]}
        # 一解释就不像它了
        if re.search(r"(因为|所以|首先|总之|希望|如果|可以帮你|我建议|作为|以下是)\s*", p):
            return {"ok": False, "reason": "在解释", "thought": thought + [f"把关：「{p}」在解释，弃用"]}
        if len(p) > int(plan.get("max_chars") or self.HYBRID_MAX_CHARS):
            return {"ok": False, "reason": "太长", "thought": thought + [f"把关：{len(p)} 字太长，弃用"]}
        if p in recent:
            return {"ok": False, "reason": "最近说过", "thought": thought + [f"把关：「{p}」刚说过，弃用"]}
        thought.append(f"把关：借用小模型的草稿「{p}」，{len(p)} 字，符合它的人设")
        return self._out(p, plan.get("mood", "calm"), plan.get("topic", ""),
                         plan.get("strategy", ""), thought)

    # ------------------------------------------------ 内部

    def _context(self, history: list[dict]) -> dict:
        """从最近几轮重建上下文（无状态，重启也不丢连贯性）。"""
        users, dogs = [], []
        for m in (history or [])[-12:]:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            t = norm(str(m.get("text") or m.get("content") or ""))
            if not t:
                continue
            if role in ("user", "human"):
                users.append(t)
            elif role in ("assistant", "dog", "bot"):
                dogs.append(t)
        topic = ""
        d = load() or {}
        for t in reversed(users[:-1] if len(users) > 1 else []):
            topic = self._topic(t, d)
            if topic:
                break
        pending = ""
        if dogs and Q_RE.search(dogs[-1]):
            pending = dogs[-1]
        return {
            "users": users,
            "recent_dog": dogs[-self.DEDUP_WINDOW:],
            "topic": topic,
            "pending_question": pending,
        }

    def _topic(self, text: str, d: dict) -> str:
        """挑出这句话里最像"话题"的那个词。

        没有分词器，所以用频次关系判断：
        若某个 2 字片段被一个 3 字片段包住、且两者出现次数差不多，
        那它多半是跨词边界的碎片（"今天上班好累" 会切出 "天上"），压掉。
        """
        kw = d.get("kw") or {}
        gs = set(grams(text))
        cnt = {g: sum(n for _, n in (kw.get(g) or [])) for g in gs}
        best, bw = "", 0.0
        for g, c in cnt.items():
            # 语料里只共现过两三次的片段当不了话题，宁可留空去接上一轮的话题
            if not c or c < self.MIN_TOPIC_HITS or g in STOP_GRAM:
                continue
            w = float(c) * (1.0 if len(g) == 2 else 0.65)
            if len(g) == 2:
                for g3, c3 in cnt.items():
                    if len(g3) == 3 and g in g3 and c3 >= 0.9 * c:
                        w *= 0.25
                        break
            if w > bw:
                best, bw = g, w
        return best

    def _from_kw(self, text: str, d: dict) -> dict[str, float]:
        """关键词共现召回：这句话里的词，它以前是怎么接的。"""
        kw = d.get("kw") or {}
        out: dict[str, float] = {}
        seen: set[str] = set()
        for g in grams(text):
            if g in seen or g in STOP_GRAM:
                continue
            seen.add(g)
            for reply, n in (kw.get(g) or []):
                out[reply] = out.get(reply, 0.0) + n
        return out

    def _from_intent(self, intent: str, d: dict) -> dict[str, float]:
        pool = (d.get("intents") or {}).get(intent) or []
        bad = BAD_FOR_INTENT.get(intent)
        out: dict[str, float] = {}
        ok = OK_FOR_INTENT.get(intent)
        ok_re = re.compile(ok) if ok else None
        for t, n in pool[:30]:
            if bad and re.search(bad, t):
                continue
            if ok_re and not ok_re.search(t):
                continue
            if intent != "angry" and GLOBAL_BAD.search(t):
                continue
            out[t] = float(n)
        return out

    def _from_mood(self, mood: str, d: dict) -> dict[str, float]:
        pool = (d.get("moods") or {}).get(mood) or []
        return {t: 1.0 for t in pool[:16]}

    def _add(self, dst: dict[str, float], src: dict[str, float], k: float) -> None:
        for t, w in src.items():
            dst[t] = dst.get(t, 0.0) + w * k

    def _hour(self, hour: int | None) -> int:
        if isinstance(hour, int) and 0 <= hour <= 23:
            return hour
        import time as _t
        return int(_t.strftime("%H"))

    def _out(self, reply: str, mood: str, topic: str, strategy: str,
             thought: list[str]) -> dict:
        # 语料里有些句子以「。」开头（上一条的残留），显示出来很难看
        reply = (reply or "").lstrip("。，、！？!?~ ").strip() or "嗯"
        return {
            "ok": True,
            "reply": reply,
            "mood": mood,
            "topic": topic,
            "strategy": strategy,
            "thought": thought,
            "source": "local-brain",
            "engine": "local",
        }

    def _dead(self, where: str) -> dict:
        return {
            "reply": "（大脑还没装好：跑一下 tools/build_brain.py）",
            "mood": "calm", "topic": "", "strategy": where,
            "thought": [f"失败：{_FAILED or 'brain.json 没加载'}"],
            "source": "local-brain", "error": _FAILED,
        }


# 单例：进程内只加载一份数据
brain = Brain()

# 给 PyInstaller / 静态检查一个明确引用
import sys  # noqa: E402  (放在末尾避免影响上面的类型注解)

__all__ = ["brain", "Brain", "load", "status", "data_path"]
