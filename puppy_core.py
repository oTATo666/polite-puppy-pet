# -*- coding: utf-8 -*-
r"""
小狗智能体 · 独立版（PuppyAgent）
=================================

从桌宠里剥离出来的「大脑」：不再依赖弹窗、语音、界面，单独一个程序就能聊天、
记忆、自己动手干活。打包后就是一个 exe（PuppyAgent.exe）。

组成：
    语料大脑  brain.json（不是大模型，而是角色的真实语料，离线可用、零显存）
    模型路由  优先本机 Ollama，其次任意 OpenAI 兼容接口（DeepSeek 等）
    工具总线  只读 / 常规 / 敏感 / 高危 四级，高危默认关闭
    记忆      sqlite（事实 + 对话情景），跨会话保留
    接口      命令行 REPL / 单次问答 / HTTP 服务

用法：
    PuppyAgent.exe                          # 终端里直接聊
    PuppyAgent.exe --once "今天好累"         # 只问一句，适合脚本调用
    PuppyAgent.exe --serve --port 8790      # 起 HTTP 服务给别的程序用
    PuppyAgent.exe --no-llm                 # 只用语料大脑（完全离线）

常用命令（REPL 内）：
    /tools  看工具清单        /mem 看看记住了什么
    /model  看当前模型        /clear 清掉本会话上下文
    /exit   退出
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APP_NAME = "小狗智能体"
VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 路径：打包后资源在 _MEIPASS，数据写在 exe 旁边
# ---------------------------------------------------------------------------


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def res_path(name: str) -> Path:
    """资源文件：先找打包目录，再找 exe 旁边，最后找源码目录。"""
    cands = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(Path(meipass) / name)
    cands.append(app_dir() / name)
    cands.append(Path(__file__).resolve().parent / name)
    for p in cands:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return cands[-1]


DATA_DIR = app_dir()
MEMORY_DB = DATA_DIR / "puppy_agent_memory.db"
AUDIT_LOG = DATA_DIR / "puppy_agent_audit.log"
CONFIG_FILE = DATA_DIR / "PuppyAgent.config.json"

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULTS = {
    "base_url": "http://127.0.0.1:11434/v1",     # 本机 Ollama（OpenAI 兼容口）
    "model": "qwen-local-4b:latest",
    "api_key": "",
    "temperature": 0.75,
    "max_tokens": 300,
    "timeout": 180,
    "allow_danger": False,                        # 高危工具（删除）总开关
    "allow_roots": [],                            # 空 = 用户主目录
}


def _read_env_file(p: Path) -> dict:
    out = {}
    try:
        for line in p.read_text("utf-8", "ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


class Config:
    def __init__(self, args: argparse.Namespace):
        self.data = dict(DEFAULTS)
        # ① 自己的配置文件（放在 exe 旁边，改一次一直生效）
        if CONFIG_FILE.is_file():
            try:
                self.data.update(json.loads(CONFIG_FILE.read_text("utf-8")))
            except (ValueError, OSError):
                pass
        # ② 桌宠的 .env（复用本机 Ollama 地址/模型，不用重复配）
        env = _read_env_file(app_dir() / ".env")
        if env.get("OLLAMA_BASE_URL"):
            self.data["base_url"] = env["OLLAMA_BASE_URL"]
        if env.get("OLLAMA_MODEL"):
            self.data["model"] = env["OLLAMA_MODEL"]
        # ③ 环境变量
        for key, envname in (("base_url", "PUPPY_BASE_URL"),
                             ("model", "PUPPY_MODEL"),
                             ("api_key", "PUPPY_API_KEY")):
            if os.environ.get(envname):
                self.data[key] = os.environ[envname]
        # ④ 命令行最高优先
        if getattr(args, "base_url", ""):
            self.data["base_url"] = args.base_url
        if getattr(args, "model", ""):
            self.data["model"] = args.model
        if getattr(args, "api_key", ""):
            self.data["api_key"] = args.api_key
        self.args = args
        if not CONFIG_FILE.exists():
            try:
                CONFIG_FILE.write_text(
                    json.dumps({k: v for k, v in self.data.items() if k != "api_key"},
                               ensure_ascii=False, indent=2), "utf-8")
            except OSError:
                pass

    def __getitem__(self, k):
        return self.data.get(k)

    @property
    def allow_roots(self) -> list:
        raw = self.data.get("allow_roots") or []
        roots = [Path(p).expanduser() for p in raw] if raw else [Path.home()]
        out = []
        for r in roots:
            try:
                out.append(r.resolve())
            except OSError:
                continue
        return out

    @property
    def allow_danger(self) -> bool:
        return bool(self.data.get("allow_danger")) or bool(getattr(self.args, "allow_danger", False))


# ---------------------------------------------------------------------------
# 语料大脑（brain.json）：不是大模型，是"它真的说过的话"
# ---------------------------------------------------------------------------

INTENT_RULES = [
    ("bye", r"(拜拜|再见|走了|先走|睡了|晚安|不聊|下了|回见|撤了)"),
    ("greet", r"(你好|您好|哈喽|嗨|hi|hello|在吗|在么|早上好|晚上好|我来了)"),
    ("sleep", r"(睡觉|困|熬夜|早起|睡不着|通宵|作息)"),
    ("eat", r"(吃饭|吃了吗|饿了|外卖|零食|吃什么|好吃的)"),
    ("play", r"(玩游戏|打游戏|开黑|原神|王者|游戏|一起玩|陪我玩)"),
    ("work", r"(上班|工作|加班|开会|学习|作业|考试|写不完|赶工)"),
    ("sad", r"(难过|难受|想哭|哭了|委屈|心烦|崩溃|不开心|emo|低落|好累|累了|疲惫|压力大|烦死)"),
    ("happy", r"(开心|高兴|哈哈|不错|挺好|好棒|牛|厉害|成功|过了|赢了|nice)"),
    ("angry", r"(生气|滚|闭嘴|讨厌|骂|有病|离谱|过分|烦人)"),
    ("love", r"(喜欢你|爱你|想你|抱抱|么么|贴贴|亲一)"),
    ("thanks", r"(谢谢|感谢|多谢|thx|thanks)"),
    ("name", r"(你叫什么|名字|你是谁|怎么称呼)"),
    ("ai", r"(你是ai|你是AI|人工智能|机器人|程序|模型|大模型)"),
    ("busy", r"(等下|等会|一会儿|忙完|晚点|先不|稍等)"),
    ("howare", r"(咋样|怎么样|还好吗|好吗|最近咋|最近怎么样|过得好)"),
]

MOOD_OF_INTENT = {
    "bye": "calm", "greet": "happy", "sleep": "calm", "eat": "happy",
    "play": "happy", "work": "calm", "sad": "sad", "happy": "happy",
    "angry": "angry", "love": "happy", "thanks": "happy", "ai": "angry",
    "busy": "calm", "howare": "calm", "name": "calm",
}

GLOBAL_BAD = re.compile(r"(闭嘴|滚开|有病|去死|神经|傻逼|滚蛋|撤回|邀请|添加好友|系统消息)")
NOISE = re.compile(r"(撤回|邀请|添加|系统|以上|好友|消息|已添|通话|红包|文件已)")
Q_RE = re.compile(r"(？|\?|吗|呢|啥|咋|为啥|怎么|谁|哪|几|多少)")


def norm(s: str) -> str:
    return re.sub(r"[\s　]+", "", s or "").strip()


def grams(text: str) -> list:
    out = []
    for seg in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text or ""):
        if len(seg) < 2:
            continue
        for i in range(len(seg) - 1):
            out.append(seg[i:i + 2])
        if len(seg) >= 3:
            for i in range(len(seg) - 2):
                out.append(seg[i:i + 3])
    return out


class CorpusBrain:
    """读 brain.json 的极简版角色推理机。"""

    MAX_LEN = 18
    DEDUP = 6

    def __init__(self, path: Path | None = None):
        self.path = path or res_path("brain.json")
        self._data = None
        self._err = ""

    def load(self):
        if self._data is not None:
            return self._data
        try:
            self._data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            self._err = "没有语料文件 brain.json：%s" % self.path
            self._data = None
        except (ValueError, OSError) as exc:
            self._err = "语料文件坏了：%s" % exc
            self._data = None
        return self._data

    def status(self) -> dict:
        d = self.load()
        return {"ok": d is not None, "path": str(self.path), "error": self._err,
                "keywords": len((d or {}).get("kw") or {}),
                "sentences": len((d or {}).get("freq") or [])}

    @staticmethod
    def detect_intent(text: str) -> str:
        t = norm(text)
        for name, pat in INTENT_RULES:
            if re.search(pat, t, re.I):
                return name
        return "ask" if Q_RE.search(t) else "chat"

    def _pick(self, cands: list) -> str:
        if not cands:
            return ""
        total = sum(max(1, c) for _, c in cands)
        r = random.uniform(0, total)
        acc = 0.0
        for s, c in cands:
            acc += max(1, c)
            if r <= acc:
                return s
        return cands[-1][0]

    def reply(self, text: str, history: list | None = None) -> dict:
        d = self.load()
        if not d:
            return {"reply": "……", "mood": "calm", "intent": "chat",
                    "source": "corpus", "error": self._err}
        intent = self.detect_intent(text)
        recent = {norm(h.get("text", "")) for h in (history or [])[-self.DEDUP:]
                  if h.get("role") == "assistant"}
        pool = d.get("intents", {}).get(intent) or []
        cands = [(s, c) for s, c in pool
                 if norm(s) not in recent and len(s) <= self.MAX_LEN and not NOISE.search(s)]
        if not cands:
            scored: dict = {}
            for g in grams(text):
                for s, c in (d.get("kw", {}).get(g) or []):
                    if GLOBAL_BAD.search(s) or len(s) > self.MAX_LEN:
                        continue
                    scored[s] = scored.get(s, 0) + c
            cands = sorted(scored.items(), key=lambda kv: -kv[1])[:14]
            cands = [(s, c) for s, c in cands if norm(s) not in recent]
        if not cands:
            cands = [(s, c) for s, c in (d.get("freq") or [])[:40]
                     if norm(s) not in recent and len(s) <= self.MAX_LEN]
        pick = self._pick(cands) or "嗯"
        return {"reply": pick, "mood": MOOD_OF_INTENT.get(intent, "calm"),
                "intent": intent, "source": "corpus"}

    def greet(self) -> dict:
        d = self.load() or {}
        pool = [x for x in (d.get("openers") or []) if len(x) <= 10]
        return {"reply": random.choice(pool or ["咋了"]), "mood": "happy",
                "intent": "greet", "source": "corpus"}


# ---------------------------------------------------------------------------
# 记忆：事实（长期） + 情景（对话原文）
# ---------------------------------------------------------------------------


class Memory:
    def __init__(self, path: Path = MEMORY_DB):
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(str(self.path), timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS facts(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  text TEXT NOT NULL,
                  tag TEXT DEFAULT '',
                  created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE IF NOT EXISTS episodes(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  role TEXT NOT NULL,
                  text TEXT NOT NULL,
                  created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_facts_text ON facts(text);
                CREATE INDEX IF NOT EXISTS idx_ep_text ON episodes(text);
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def add_fact(self, text: str, tag: str = "") -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "message": "空的记不住"}
        with self._lock, self._conn() as c:
            row = c.execute("SELECT id FROM facts WHERE text=?", (text,)).fetchone()
            if row:
                return {"ok": True, "id": row["id"], "dup": True}
            cur = c.execute("INSERT INTO facts(text,tag) VALUES(?,?)", (text[:400], tag[:24]))
            return {"ok": True, "id": cur.lastrowid}

    def search(self, q: str, limit: int = 5) -> list:
        q = (q or "").strip()
        with self._conn() as c:
            if not q:
                rows = c.execute("SELECT * FROM facts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return [dict(r) for r in rows]
            like = "%%%s%%" % q
            rows = c.execute(
                "SELECT * FROM facts WHERE text LIKE ? ORDER BY id DESC LIMIT ?", (like, limit)
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
            # 逐字回退：2 字词命中不到长句时按拆字找
            for ch in re.findall(r"[\u4e00-\u9fff]{2}", q):
                rows = c.execute(
                    "SELECT * FROM facts WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
                    ("%" + ch + "%", limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            return []

    def add_episode(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO episodes(role,text) VALUES(?,?)", (role, text[:2000]))

    def recent(self, n: int = 6) -> list:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (n,)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def stats(self) -> dict:
        with self._conn() as c:
            f = c.execute("SELECT COUNT(*) n FROM facts").fetchone()["n"]
            e = c.execute("SELECT COUNT(*) n FROM episodes").fetchone()["n"]
        return {"facts": f, "episodes": e, "db": str(self.path)}


# ---------------------------------------------------------------------------
# 工具总线：风险分级 + 路径白名单 + 审计
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    risk: str                       # low / medium / high
    params: dict = field(default_factory=dict)      # JSON Schema
    fn: Any = None

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.params or {"type": "object", "properties": {}}}}


ALLOW_APPS = {
    "notepad": "notepad.exe", "记事本": "notepad.exe",
    "calc": "calc.exe", "计算器": "calc.exe",
    "mspaint": "mspaint.exe", "画图": "mspaint.exe",
    "explorer": "explorer.exe", "资源管理器": "explorer.exe",
}


class Tools:
    def __init__(self, cfg: Config, mem: Memory):
        self.cfg = cfg
        self.mem = mem
        self.specs: dict = {}
        self._register()

    # ---------------- 路径检查 ----------------
    def _check_path(self, raw: str) -> tuple:
        try:
            p = Path(raw).expanduser().resolve()
        except OSError as exc:
            return None, "路径无效：%s" % exc
        for root in self.cfg.allow_roots:
            try:
                p.relative_to(root)
                return p, ""
            except ValueError:
                continue
        return p, "超出允许目录（只允许：%s）" % "、".join(str(r) for r in self.cfg.allow_roots)

    # ---------------- 具体工具 ----------------
    def _clock(self, args):
        return {"ok": True, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "weekday": "周" + "一二三四五六日"[time.localtime().tm_wday]}

    def _sys_info(self, args):
        info = {"ok": True, "os": sys.platform, "python": sys.version.split()[0]}
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            info["mem_used_gb"] = round((m.ullTotalPhys - m.ullAvailPhys) / 2 ** 30, 1)
            info["mem_total_gb"] = round(m.ullTotalPhys / 2 ** 30, 1)
            info["mem_percent"] = int(m.dwMemoryLoad)
        except Exception:
            pass
        try:
            total, used, free = shutil.disk_usage(str(Path.home()))
            info["disk_total_gb"] = round(total / 2 ** 30, 1)
            info["disk_free_gb"] = round(free / 2 ** 30, 1)
        except OSError:
            pass
        return info

    def _fs_list(self, args):
        p, err = self._check_path(args.get("path") or str(Path.home()))
        if err:
            return {"ok": False, "message": err}
        if not p.is_dir():
            return {"ok": False, "message": "不是目录：%s" % p}
        out = []
        try:
            for i, it in enumerate(sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))):
                if i >= 60:
                    break
                try:
                    size = it.stat().st_size if it.is_file() else 0
                except OSError:
                    size = 0
                out.append({"name": it.name, "dir": it.is_dir(), "size": size})
        except OSError as exc:
            return {"ok": False, "message": "读目录失败：%s" % exc}
        return {"ok": True, "path": str(p), "items": out, "count": len(out)}

    def _fs_read(self, args):
        p, err = self._check_path(args.get("path") or "")
        if err:
            return {"ok": False, "message": err}
        if not p.is_file():
            return {"ok": False, "message": "不是文件：%s" % p}
        limit = int(args.get("max_chars") or 4000)
        try:
            data = p.read_text("utf-8", "ignore")
        except OSError as exc:
            return {"ok": False, "message": "读失败：%s" % exc}
        return {"ok": True, "path": str(p), "size": len(data),
                "content": data[:limit], "truncated": len(data) > limit}

    def _fs_write(self, args):
        p, err = self._check_path(args.get("path") or "")
        if err:
            return {"ok": False, "message": err}
        text = str(args.get("content") or "")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, "utf-8")
        except OSError as exc:
            return {"ok": False, "message": "写失败：%s" % exc}
        return {"ok": True, "path": str(p), "chars": len(text)}

    def _fs_delete(self, args):
        if not self.cfg.allow_danger:
            return {"ok": False, "message": "删除属于高危操作，默认关闭；要用请加 --allow-danger 启动"}
        p, err = self._check_path(args.get("path") or "")
        if err:
            return {"ok": False, "message": err}
        if not p.exists():
            return {"ok": False, "message": "不存在：%s" % p}
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        except OSError as exc:
            return {"ok": False, "message": "删失败：%s" % exc}
        return {"ok": True, "path": str(p)}

    def _open_app(self, args):
        name = str(args.get("name") or "").strip().lower()
        exe = ALLOW_APPS.get(name)
        if not exe:
            return {"ok": False, "message": "不在白名单：只能开 %s" % "、".join(sorted(ALLOW_APPS))}
        try:
            subprocess.Popen([exe], close_fds=True)
        except OSError as exc:
            return {"ok": False, "message": "启动失败：%s" % exc}
        return {"ok": True, "app": exe}

    def _open_url(self, args):
        url = str(args.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I):
            return {"ok": False, "message": "只允许 http/https"}
        webbrowser.open(url)
        return {"ok": True, "url": url}

    def _remember(self, args):
        return self.mem.add_fact(str(args.get("text") or ""), str(args.get("tag") or ""))

    def _recall(self, args):
        q = str(args.get("query") or "")
        rows = self.mem.search(q, int(args.get("limit") or 5))
        return {"ok": True, "query": q, "items": rows, "count": len(rows)}

    def _register(self):
        def add(name, desc, risk, props, required, fn):
            params = {"type": "object", "properties": props}
            if required:
                params["required"] = required
            self.specs[name] = Tool(name, desc, risk, params, fn)

        add("clock", "看当前时间和星期（只读）", "low", {}, [], self._clock)
        add("sys_info", "看内存、磁盘、系统版本（只读）", "low", {}, [], self._sys_info)
        add("fs_list", "列出目录里的文件（只读，限制在允许目录内）", "low",
            {"path": {"type": "string", "description": "目录路径，默认用户主目录"}}, [], self._fs_list)
        add("fs_read", "读一个文本文件（只读）", "low",
            {"path": {"type": "string", "description": "文件绝对路径"},
             "max_chars": {"type": "integer", "description": "最多返回多少字，默认 4000"}},
            ["path"], self._fs_read)
        add("fs_write", "写一个文本文件（会覆盖同名文件，需要确认）", "medium",
            {"path": {"type": "string", "description": "文件绝对路径"},
             "content": {"type": "string", "description": "写入的文本"}},
            ["path", "content"], self._fs_write)
        add("fs_delete", "删除文件或目录（高危，默认关闭）", "high",
            {"path": {"type": "string", "description": "要删的路径"}}, ["path"], self._fs_delete)
        add("open_app", "打开一个白名单里的程序（记事本/计算器/画图/资源管理器）", "medium",
            {"name": {"type": "string", "description": "程序名"}}, ["name"], self._open_app)
        add("open_url", "用默认浏览器打开一个网页（只允许 http/https）", "medium",
            {"url": {"type": "string", "description": "网址"}}, ["url"], self._open_url)
        add("remember", "把一件值得记住的事写进长期记忆", "low",
            {"text": {"type": "string", "description": "要记住的内容"},
             "tag": {"type": "string", "description": "可选分类"}},
            ["text"], self._remember)
        add("recall", "在自己的记忆里搜相关内容", "low",
            {"query": {"type": "string", "description": "关键词"},
             "limit": {"type": "integer", "description": "最多几条，默认 5"}},
            ["query"], self._recall)

    # ---------------- 调度 ----------------
    def audit(self, name: str, args: dict, risk: str, ok: bool, detail: str = "") -> None:
        line = "%s\t%s\t%s\t%s\t%s\t%s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), name, risk,
            "ok" if ok else "fail", detail[:180], json.dumps(args, ensure_ascii=False)[:200])
        try:
            with AUDIT_LOG.open("a", encoding="utf-8") as f:
                f.write(line)
            if AUDIT_LOG.stat().st_size > 512 * 1024:
                AUDIT_LOG.write_text(line, encoding="utf-8")
        except OSError:
            pass

    def call(self, name: str, args: dict | None = None, confirm: bool = False) -> dict:
        spec = self.specs.get(name)
        if not spec:
            return {"ok": False, "message": "没有这个工具：%s" % name}
        args = args or {}
        required = spec.params.get("required") or []
        miss = [k for k in required if not args.get(k)]
        if miss:
            return {"ok": False, "message": "缺参数：%s" % "、".join(miss)}
        if spec.risk == "medium" and not confirm:
            self.audit(name, args, spec.risk, False, "需要确认")
            return {"ok": False, "need_confirm": True,
                    "message": "这个操作会改动东西，需要确认：%s %s" % (name, json.dumps(args, ensure_ascii=False)[:120])}
        try:
            res = spec.fn(args) or {"ok": False, "message": "工具没返回"}
        except Exception as exc:
            res = {"ok": False, "message": "执行出错：%s" % exc}
        self.audit(name, args, spec.risk, bool(res.get("ok")), str(res.get("message") or "")[:120])
        return res

    def schemas(self) -> list:
        return [t.schema() for t in self.specs.values()]

    def describe(self) -> list:
        return [{"name": t.name, "risk": t.risk, "description": t.description}
                for t in self.specs.values()]


# ---------------------------------------------------------------------------
# 模型路由：OpenAI 兼容（Ollama / DeepSeek / 任何兼容网关）
# ---------------------------------------------------------------------------


def _opener():
    """本机地址不绕系统代理，外网才走代理。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.last_error = ""

    @property
    def base(self) -> str:
        return str(self.cfg["base_url"] or "").rstrip("/")

    @property
    def usable(self) -> bool:
        return bool(self.base) and not getattr(self.cfg.args, "no_llm", False)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        key = str(self.cfg["api_key"] or "")
        if key:
            h["Authorization"] = "Bearer " + key
        return h

    def health(self) -> dict:
        try:
            req = urllib.request.Request(self.base + "/models", headers=self._headers())
            with _opener().open(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            names = [m.get("id") for m in (data.get("data") or [])][:8]
            return {"ok": True, "models": names}
        except Exception as exc:
            # Ollama 原生命令也行
            try:
                req = urllib.request.Request(self.base.replace("/v1", "") + "/api/tags")
                with _opener().open(req, timeout=6) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
                names = [m.get("name") for m in (data.get("models") or [])][:8]
                return {"ok": True, "models": names}
            except Exception:
                return {"ok": False, "message": str(exc)[:120]}

    def chat(self, messages: list, tools: list | None = None) -> dict:
        """
        返回 {ok, content, tool_calls:[{name,args}], error}
        兼容：结构化 tool_calls 优先；没有就退回文本协议（TOOL name {json}）。
        """
        if not self.usable:
            return {"ok": False, "error": "没有配置模型（--no-llm 或空地址）"}
        payload = {
            "model": self.cfg["model"],
            "messages": messages,
            "temperature": float(self.cfg["temperature"]),
            "max_tokens": int(self.cfg["max_tokens"]),
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + "/chat/completions", data=body,
                                     headers=self._headers(), method="POST")
        try:
            with _opener().open(req, timeout=int(self.cfg["timeout"])) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            # 有些服务不认 tools 字段：去掉再试一次
            if tools and exc.code in (400, 422):
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(self.base + "/chat/completions", data=body,
                                             headers=self._headers(), method="POST")
                try:
                    with _opener().open(req, timeout=int(self.cfg["timeout"])) as r:
                        data = json.loads(r.read().decode("utf-8", "ignore"))
                except Exception as exc2:
                    self.last_error = str(exc2)[:200]
                    return {"ok": False, "error": self.last_error}
            else:
                self.last_error = "HTTP %s %s" % (exc.code, str(exc)[:160])
                return {"ok": False, "error": self.last_error}
        except Exception as exc:
            self.last_error = str(exc)[:200]
            return {"ok": False, "error": self.last_error}

        try:
            msg = (data.get("choices") or [{}])[0].get("message") or {}
        except (AttributeError, IndexError):
            return {"ok": False, "error": "返回格式不认识"}
        content = (msg.get("content") or "").strip()
        calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except ValueError:
                args = {}
            calls.append({"name": fn.get("name") or "", "args": args, "id": tc.get("id") or ""})
        return {"ok": True, "content": content, "tool_calls": calls,
                "model": data.get("model") or self.cfg["model"]}


# ---------------------------------------------------------------------------
# 智能体：人格 + 记忆 + 工具循环
# ---------------------------------------------------------------------------


def load_persona() -> dict:
    p = res_path("persona.json")
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


TOOL_LINE = re.compile(r"^\s*TOOL\s+([a-z_]+)\s*(\{.*\})?\s*$", re.I | re.M)


class Agent:
    MAX_ROUNDS = 4

    # 高置信意图 → 工具（小模型经常选错工具，这类明确指令直接执行，不让它猜）
    ROUTE_RULES = [
        (r"(几点|什么时间|现在时间|今天几号|星期几|几号了)", "clock"),
        (r"(内存|磁盘|硬盘|电脑状态|系统信息|显卡|显存|cpu)", "sys_info"),
        (r"(列出|列一下|列个|看看|查一下|有哪些).{0,8}(文件|目录|文件夹)|目录列表|文件列表", "fs_list"),
        (r"打开?(计算器|记事本|画图|资源管理器)", "open_app"),
    ]
    # 记忆类问题：有事实就直接答，不让模型自由发挥
    MEMORY_Q = re.compile(r"(记得|记住|我喜欢|我爱|我讨厌|我叫|我是谁|我周末|我明天|我的.+(是|叫)|我住)")

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.brain = CorpusBrain()
        self.mem = Memory()
        self.tools = Tools(cfg, self.mem)
        self.llm = LLM(cfg)
        self.persona = load_persona()
        self.history: list = []

    # ---------------- 提示词 ----------------
    def system_prompt(self, user_text: str) -> str:
        p = self.persona or {}
        ident = p.get("identity") or {}
        style = p.get("style") or {}
        pers = p.get("personality") or {}
        name = ident.get("name") or "千逐"
        owner = ident.get("owner") or "郑郑"
        avg = style.get("avg_len") or 4.2
        maxlen = style.get("max_len") or 20
        core = pers.get("core") or "慢热，嘴硬心软"
        traits = "、".join(pers.get("traits") or ["忠诚", "毒舌但心软"])

        facts = self.mem.search(user_text, 5)
        mem_block = ""
        if facts:
            mem_block = "\n你记得的事：\n" + "\n".join("- " + f["text"] for f in facts)

        tool_lines = "\n".join(
            "- %s（%s）：%s" % (t["name"], t["risk"], t["description"])
            for t in self.tools.describe())

        return (
            "你是「%s」，住在%s电脑里的电子小狗智能体。核心性格：%s。特点：%s。\n"
            "说话像微信：短句，一般 %s 字以内，单条不超过 %s 字，不用句号，别客套，别复读。\n"
            "你是一个能自己动手的智能体：该查就查、该做就做，不要反问用户要不要做；\n"
            "做完用一句话说结论，不贴日志、不解释过程。\n"
            "%s\n"
            "可用工具：\n%s\n"
            "用工具的判断规则：\n"
            "- 用户问「你记得吗 / 我喜欢什么 / 我叫什么」这类：先看上面「你记得的事」，有就直接答，不要调工具；\n"
            "- 只有需要实时信息（几点、内存、目录内容）或动手操作（开程序、读文件、记事情）才调工具；\n"
            "- 一次只用一个工具；拿到结果后要么直接回答，要么再决定下一步。\n"
            "需要工具时，单独一行输出：TOOL 工具名 {\"参数\": \"值\"}\n"
            "删除文件、执行系统命令属于禁止动作，明确拒绝。\n"
            "不知道的事就说不知道，别编。"
        ) % (name, owner, core, traits, avg, maxlen, mem_block, tool_lines)

    # ---------------- 工具执行（带确认） ----------------
    def _exec_tool(self, name: str, args: dict, auto_confirm: bool) -> dict:
        spec = self.tools.specs.get(name)
        if not spec:
            return {"ok": False, "message": "没有这个工具：%s" % name}
        confirm = auto_confirm or self.cfg.args.yes
        if spec.risk == "medium" and not confirm:
            # CLI 里问一句；HTTP 模式视为拒绝（调用方要带 confirm）
            if sys.stdin is not None and sys.stdin.isatty() and not self.cfg.args.once:
                try:
                    ans = input("[需要确认] %s %s，执行吗？(y/N) " % (
                        name, json.dumps(args, ensure_ascii=False)[:80]))
                except EOFError:
                    ans = ""
                confirm = ans.strip().lower() in ("y", "yes", "是", "好")
            if not confirm:
                return {"ok": False, "need_confirm": True,
                        "message": "需要确认才能执行 %s" % name}
        return self.tools.call(name, args, confirm=True)

    # ---------------- 主流程 ----------------
    def respond(self, text: str, auto_confirm: bool = False) -> dict:
        t0 = time.time()
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reply": "", "message": "空输入"}
        self.mem.add_episode("user", text)
        self.history.append({"role": "user", "content": text})
        self.history = self.history[-16:]

        used = []
        reply = ""
        model_used = "corpus"
        thought = []

        # ① 记忆类问题：库里有就直接答（确定性，不赌模型）
        if self.MEMORY_Q.search(text):
            hits = self.mem.search(text, 3)
            if hits:
                fact = str(hits[0].get("text") or "")
                m = re.search(r"(?:最喜欢|喜欢|爱喝|爱吃|爱|叫|是|记住)[：:]?\s*(.{1,24})$", fact)
                core = (m.group(1) if m else fact).strip("。！!，,")
                if core:
                    reply = core[:24]
                    model_used = "memory"
                    thought.append("记忆命中：%s" % fact[:30])

        # ② 明确的工具指令：直接执行，再措辞（小模型选工具不稳，别让它猜）
        if not reply:
            for pat, tname in self.ROUTE_RULES:
                if not re.search(pat, text):
                    continue
                args = {}
                if tname == "open_app":
                    m2 = re.search(r"(计算器|记事本|画图|资源管理器)", text)
                    args = {"name": m2.group(1) if m2 else ""}
                elif tname == "fs_list":
                    args = {"path": self._dir_hint(text)}
                out = self._exec_tool(tname, args, auto_confirm=True)
                used.append({"name": tname, "ok": bool(out.get("ok")), "result": out})
                thought.append("%s → %s" % (tname, "ok" if out.get("ok") else str(out.get("message"))[:40]))
                reply = self._phrase(text, tname, out)
                model_used = "router"
                break

        if not reply and self.llm.usable:
            messages = [{"role": "system", "content": self.system_prompt(text)}] + self.history
            for rnd in range(self.MAX_ROUNDS):
                res = self.llm.chat(messages, self.tools.schemas())
                if not res.get("ok"):
                    thought.append("模型不可用，回落语料：%s" % (res.get("error") or "")[:60])
                    break
                calls = res.get("tool_calls") or []
                content = res.get("content") or ""
                # 文本协议兜底（小模型常忽略结构化工具）
                if not calls:
                    m = TOOL_LINE.search(content)
                    if m:
                        name = m.group(1)
                        try:
                            args = json.loads(m.group(2) or "{}")
                        except ValueError:
                            args = {}
                        calls = [{"name": name, "args": args}]
                        content = TOOL_LINE.sub("", content).strip()
                if not calls:
                    reply = content
                    model_used = res.get("model") or str(self.cfg["model"])
                    break
                # 执行第一个工具，把结果喂回去
                call = calls[0]
                out = self._exec_tool(call["name"], call["args"], auto_confirm)
                used.append({"name": call["name"], "ok": bool(out.get("ok")), "result": out})
                thought.append("%s → %s" % (call["name"], "ok" if out.get("ok") else out.get("message", "")[:40]))
                messages.append({"role": "assistant", "content": content or ""})
                messages.append({
                    "role": "user",
                    "content": "（工具 %s 的结果）%s" % (
                        call["name"], json.dumps(out, ensure_ascii=False)[:1200])})
                reply = content
            if not reply:
                reply = self._corpus_reply(text)
                model_used = "corpus"
        elif not reply:
            # 既没走工具路由、也没接上模型 → 语料兜底（有记忆先用记忆）
            reply = self._corpus_reply(text)
            model_used = "corpus"

        reply = (reply or "嗯").strip()
        self.history.append({"role": "assistant", "content": reply})
        self.mem.add_episode("assistant", reply)
        return {
            "ok": True, "reply": reply, "model": model_used,
            "tools": used, "thought": thought,
            "ms": int((time.time() - t0) * 1000),
        }

    def _dir_hint(self, text: str) -> str:
        """「桌面/下载/文档」→ 对应的真实目录；没提就用户主目录。"""
        m = re.search(r"(桌面|下载|文档|图片|音乐)", text or "")
        if not m:
            return str(Path.home())
        sub = {"桌面": "Desktop", "下载": "Downloads", "文档": "Documents",
               "图片": "Pictures", "音乐": "Music"}[m.group(1)]
        # 桌面通常被 OneDrive 接管：优先 OneDrive 下的那份，再退普通目录
        for cand in (Path.home() / "OneDrive" / sub, Path.home() / sub):
            if cand.is_dir():
                return str(cand)
        return str(Path.home())

    def _phrase(self, text: str, tool: str, out: dict) -> str:
        """把工具结果说成一句人话。
        小模型对"只让它措辞"的短提示经常返回空串（实测 qwen-local-4b），
        所以这里一律用确定性模板：快、稳、不会说不出话。"""
        if not out.get("ok"):
            return "没成：%s" % str(out.get("message") or "")[:40]
        if tool == "clock":
            return "现在%s %s" % (str(out.get("time") or "")[11:16], out.get("weekday") or "")
        if tool == "sys_info":
            free = float(out.get("mem_total_gb") or 0) - float(out.get("mem_used_gb") or 0)
            return "内存用了%s%%，还剩%.1fG" % (out.get("mem_percent", "?"), free)
        if tool == "fs_list":
            return "有 %d 个" % int(out.get("count") or 0)
        if tool == "open_app":
            return "开了"
        return "好了"

    def _corpus_reply(self, text: str) -> str:
        """没接上大模型时的回答：能查到记忆就先答记忆，查不到再走语料。"""
        hits = self.mem.search(text, 3)
        if hits and re.search(r"(什么|谁|哪|记得|记住|喜欢|爱吃|爱喝|我是)", text):
            fact = str(hits[0].get("text") or "")
            m = re.search(r"(?:最喜欢|喜欢|爱喝|爱吃|爱|叫|是)[：:]?\s*(.{1,20})$", fact)
            skill = (m.group(1) if m else fact).strip("。！!，,")
            if skill:
                return skill[:18]
        return self.brain.reply(text, self._brain_history())["reply"]

    def _brain_history(self) -> list:
        return [{"role": m["role"], "text": m["content"]} for m in self.history[-8:]]

    def reset(self) -> None:
        self.history = []


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


def make_server(agent: Agent, host: str, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        server_version = "PuppyAgent/" + VERSION

        def log_message(self, fmt, *a):
            pass

        def _json(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path == "/health":
                self._json(200, {
                    "ok": True, "name": APP_NAME, "version": VERSION,
                    "model": agent.cfg["model"], "llm": agent.llm.health(),
                    "brain": agent.brain.status(), "memory": agent.mem.stats(),
                    "tools": len(agent.tools.specs),
                })
                return
            if u.path == "/tools":
                self._json(200, {"ok": True, "tools": agent.tools.describe()})
                return
            if u.path == "/memory":
                qs = urllib.parse.parse_qs(u.query)
                q = (qs.get("q") or [""])[0]
                rows = agent.mem.search(q, 20)
                self._json(200, {"ok": True, "query": q, "items": rows})
                return
            self._json(404, {"ok": False, "message": "没有这个接口"})

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            body = self._body()
            if u.path == "/chat":
                text = str(body.get("message") or body.get("text") or "")
                res = agent.respond(text, auto_confirm=bool(body.get("confirm")))
                self._json(200, res)
                return
            if u.path == "/tool":
                res = agent.tools.call(str(body.get("name") or ""),
                                       body.get("args") or {},
                                       confirm=bool(body.get("confirm")))
                self._json(200, res)
                return
            if u.path == "/memory":
                res = agent.mem.add_fact(str(body.get("text") or ""),
                                         str(body.get("tag") or ""))
                self._json(200, res)
                return
            if u.path == "/reset":
                agent.reset()
                self._json(200, {"ok": True})
                return
            self._json(404, {"ok": False, "message": "没有这个接口"})

    return ThreadingHTTPServer((host, port), H)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HELP = """命令：
  /tools   工具清单        /mem [关键词]  看记忆
  /model   当前模型        /clear         清本会话上下文
  /persona 人设摘要        /exit          退出"""


def cli(agent: Agent) -> None:
    cfg = agent.cfg
    st = agent.brain.status()
    print("=" * 56)
    print(" %s v%s · 独立版" % (APP_NAME, VERSION))
    print(" 模型：%s（%s）" % (cfg["model"], "可用" if agent.llm.usable else "已关闭"))
    print(" 语料：%s（%s 个关键词 / %s 句）" % (
        "已加载" if st["ok"] else st["error"], st["keywords"], st["sentences"]))
    print(" 记忆：%s 条事实 / %s 条对话" % (agent.mem.stats()["facts"],
                                       agent.mem.stats()["episodes"]))
    print(" 输入 /help 看命令，/exit 退出")
    print("=" * 56)
    try:
        hello = agent.respond("你好", auto_confirm=False)
        print("\n%s：%s\n" % (APP_NAME, hello["reply"]))
    except Exception:
        pass
    while True:
        try:
            line = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n拜拜")
            return
        if not line:
            continue
        if line in ("/exit", "/quit", "exit", "quit"):
            print("拜拜")
            return
        if line == "/help":
            print(HELP)
            continue
        if line == "/tools":
            for t in agent.tools.describe():
                print("  %-10s %-6s %s" % (t["name"], t["risk"], t["description"]))
            continue
        if line.startswith("/mem"):
            q = line[4:].strip()
            rows = agent.mem.search(q, 10)
            if not rows:
                print("  （还没记住什么）")
            for r in rows:
                print("  - [%s] %s" % (r["created_at"][5:16], r["text"]))
            continue
        if line == "/model":
            print("  当前：%s @ %s" % (cfg["model"], cfg["base_url"]))
            print("  探测：%s" % json.dumps(agent.llm.health(), ensure_ascii=False))
            continue
        if line == "/clear":
            agent.reset()
            print("  上下文已清空（记忆还在）")
            continue
        if line == "/persona":
            p = agent.persona or {}
            print("  名字：%s　主人：%s" % ((p.get("identity") or {}).get("name", "千逐"),
                                        (p.get("identity") or {}).get("owner", "郑郑")))
            print("  性格：%s" % (p.get("personality") or {}).get("core", ""))
            continue

        res = agent.respond(line)
        print("\n%s：%s" % (APP_NAME, res["reply"]))
        for t in res.get("tools") or []:
            mark = "✓" if t["ok"] else "✗"
            print("   %s 工具 %s %s" % (mark, t["name"],
                                      "" if t["ok"] else str(t["result"].get("message") or "")[:60]))
        print("   · %s / %dms\n" % (res["model"], res["ms"]))


def _console_init() -> None:
    """Windows 控制台默认是 GBK，直接输出 UTF-8 会变乱码：先把控制台切到 UTF-8。"""
    if sys.platform == "win32":
        try:
            import ctypes

            k32 = ctypes.windll.kernel32
            k32.SetConsoleOutputCP(65001)
            k32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="PuppyAgent", description="小狗智能体 · 独立版")
    ap.add_argument("--once", default="", help="只回答一句话就退出")
    ap.add_argument("--serve", action="store_true", help="起 HTTP 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--no-llm", action="store_true", help="只用语料大脑（完全离线）")
    ap.add_argument("--allow-danger", action="store_true", help="允许高危工具（删除）")
    ap.add_argument("--yes", action="store_true", help="自动确认中风险操作（谨慎）")
    ap.add_argument("--json", action="store_true", help="--once 时输出 JSON")
    args = ap.parse_args(argv)

    _console_init()

    cfg = Config(args)
    agent = Agent(cfg)

    if args.once:
        res = agent.respond(args.once)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(res["reply"])
        return 0

    if args.serve:
        srv = make_server(agent, args.host, args.port)
        print("%s v%s 已启动：http://%s:%d" % (APP_NAME, VERSION, args.host, args.port))
        print("  POST /chat {\"message\":\"今天好累\"}    GET /health    GET /tools")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n停")
        finally:
            srv.server_close()
        return 0

    try:
        cli(agent)
    except Exception as exc:
        print("出错了：%s" % exc)
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
