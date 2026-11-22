#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按 `02_文档/验收清单_v1.md` 的八项做一次自动化模拟。

这份脚本**不改任何真实数据**：数据目录、记忆库、日记、日志、配置全部指到临时目录
（做法和 tests/_ctx.py 一致），跑完就丢。它替人做那些能在电脑上自动判断的部分：

  1. 版本对得上          —— /show 返回的 version、/api/health 的 layers
  2. 聊天与延迟          —— 服务器响应耗时 + 前端令牌等待（node 复现真实函数）
  3. 事实层真的开始长    —— 20 句话走 /api/memory/extract，看 facts 有没有入库
  4. 事实能删、不复活    —— DELETE 后重开库再搜
  5. 云端知情与开关      —— index.html 里的默认值 + 真实函数求值
  6. Agent 沙盒          —— 读 .ssh/.env 拒绝、列目录要确认、沙盒写入要确认
  7. 语音与音色          —— /api/voice/diag、/api/voice/id
  8. 七个热键            —— 逐个解析 + 真实注册/立刻注销（不占用）

用法：
    python scripts/simulate_acceptance.py
    python scripts/simulate_acceptance.py --json out.json
    python scripts/simulate_acceptance.py --exe .\\PolitePuppy.exe   # 额外启动 exe 验版本

退出码：0 = 没有 FAIL（SKIP 会单独列出来）；1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# N-21：证据文件会进审阅包——所有 print 在写出前先打码（TEMP 路径 ->
# %TEMP%，用户名/8.3 短名 -> %USERPROFILE%），不再让短名这类痕迹
# 随输出流出去。子进程输出被本脚本转发 print 的同样会过这一层。
import private_words  # noqa: E402


class _RedactedOut:
    def __init__(self, raw):
        self._raw = raw

    def write(self, s):
        return self._raw.write(private_words.redact_text(s))

    def flush(self):
        return self._raw.flush()

    def __getattr__(self, name):
        return getattr(self._raw, name)


sys.stdout = _RedactedOut(sys.stdout)

PROG = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="puppy_accept_"))
os.environ["POLITE_PUPPY_DATA"] = str(TMP / "appdata")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

if str(PROG) not in sys.path:
    sys.path.insert(0, str(PROG))

import main  # noqa: E402  （必须在 POLITE_PUPPY_DATA 之后）

# ---------------------------------------------------------------------------
# 把数据面全搬进临时目录：跑模拟不能碰用户真实的记忆库
# ---------------------------------------------------------------------------
main.DATA_DIR = TMP / "appdata"
main.LOG_FILE = main.DATA_DIR / "pet.log"
main.CONFIG_FILE = main.DATA_DIR / "settings.json"
main.DIARY_FILE = main.DATA_DIR / "memory_diary.txt"
main.MEMORY_DIR = main.DATA_DIR / "记忆"
main.MEMORY_DB = main.MEMORY_DIR / "memory.db"
main.MEMORY_BACKUP_DIR = main.MEMORY_DIR / "backup"
main.MEMORY_ORGANIZE_STATE = main.MEMORY_DIR / "organize_state.json"
main.DAYLOG_DIR = main.DATA_DIR / "daylog"
main.TOOL_AUDIT_FILE = main.DATA_DIR / "tools_audit.log"
main.DATA_DIR.mkdir(parents=True, exist_ok=True)
main.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
main._MEM_INITED = False
main.AGENT_STATE.update({"ready": False, "runtime": None, "booted": False, "error": ""})
main.setup_logging(False)
# 真机启动时会先读 .env；模拟也得读，否则语音/模型那几项会被判成"没配"
main.load_dotenv(PROG / ".env")

RESULTS: list[dict] = []


def add(item: str, name: str, status: str, detail: str = "", ms: int = 0) -> None:
    RESULTS.append({"item": item, "name": name, "status": status,
                    "detail": detail, "ms": ms})


def ok(item: str, name: str, cond: bool, detail: str = "", ms: int = 0) -> bool:
    add(item, name, "PASS" if cond else "FAIL", detail, ms)
    return bool(cond)


def skip(item: str, name: str, detail: str) -> None:
    add(item, name, "SKIP", detail)


# ---------------------------------------------------------------------------
# HTTP 客户端（带 P1-5 令牌）
# ---------------------------------------------------------------------------
PORT = 0


def http(method: str, path: str, body: dict | None = None, *, token: bool = True,
         headers: dict | None = None, timeout: float = 40.0):
    import http.client

    hdrs = {"Host": "127.0.0.1:%d" % PORT}
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
        hdrs["Content-Length"] = str(len(payload))
    if token and main.API_TOKEN:
        hdrs[main.API_TOKEN_HEADER] = main.API_TOKEN
    hdrs.update(headers or {})
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        ms = int((time.perf_counter() - t0) * 1000)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            data = {}
        return resp.status, data, raw, ms
    finally:
        conn.close()


class _StubPet:
    """只实现 /show 用到的那一个方法。"""

    def restore_pet(self) -> dict:
        return {"ok": True, "shown": True, "stub": True}


# ---------------------------------------------------------------------------
# 第 1 项：版本对得上
# ---------------------------------------------------------------------------
def check_version() -> None:
    item = "1. 版本"
    main._PET_APP = _StubPet()  # type: ignore[attr-defined]
    code, data, _, ms = http("GET", "/show")
    ok(item, "/show 不带令牌也能读", code == 200 and bool(data.get("ok")),
       "HTTP %s" % code, ms)
    ok(item, "/show 的版本 = %s" % main.APP_VERSION,
       str(data.get("version") or "") == main.APP_VERSION,
       "实际 %r" % data.get("version"))

    code, data, _, ms = http("GET", "/api/health")
    layers = (data.get("layers") or {})
    ok(item, "/api/health 版本一致", code == 200 and data.get("version") == main.APP_VERSION,
       "HTTP %s / %r" % (code, data.get("version")), ms)
    ok(item, "四层自检齐全", set(layers) == {"brain", "ollama", "cloud", "tts"},
       "layers=%s" % ",".join(sorted(layers)))
    ok(item, "brain 层可用", bool((layers.get("brain") or {}).get("ok")),
       "%s 句 / %s 词" % ((layers.get("brain") or {}).get("sentences"),
                          (layers.get("brain") or {}).get("keywords")))

    code, _, _, _ = http("GET", "/api/health", token=False)
    ok(item, "老 exe 拿不到令牌就 401", code == 401, "HTTP %s" % code)


# ---------------------------------------------------------------------------
# 第 2 项：聊天与延迟
# ---------------------------------------------------------------------------
def _extract_js(html: str, start: str, end: str) -> str:
    i = html.find(start)
    if i < 0:
        return ""
    j = html.find(end, i)
    if j < 0:
        return ""
    return html[i:j]


def check_latency() -> None:
    item = "2. 聊天与延迟"

    # 2a 服务器侧：连发 10 个轻接口，不该有任何一个卡到秒级
    spans = []
    for _ in range(10):
        code, data, _, ms = http("GET", "/api/memory/stats")
        spans.append(ms)
        if code != 200:
            ok(item, "/api/memory/stats 可用", False, "HTTP %s" % code)
            break
    else:
        spans.sort()
        worst = spans[-1]
        ok(item, "接口响应 < 800ms（10 次最慢）", worst < 800,
           "最快 %d / 中位 %d / 最慢 %d ms" % (spans[0], spans[len(spans) // 2], worst))

    # 2b 本地大脑真的能回话（brain 模式不依赖任何模型）
    code, data, _, ms = http("POST", "/api/brain", {"kind": "greet"})
    reply = str((data or {}).get("reply") or "")
    ok(item, "本地大脑能出话", code == 200 and bool(data.get("ok")) and len(reply) >= 1,
       "「%s」%d ms" % (reply[:20], ms), ms)

    # 2c 关键：用 node 跑 index.html 里真实的那段 tokenReady()
    node = shutil.which("node")
    if not node:
        skip(item, "前端令牌等待", "没装 node，跳过（只影响这一条模拟）")
        return
    html = (PROG / "index.html").read_text(encoding="utf-8", errors="replace")
    code = _extract_js(html, 'var PUPPY_TOKEN = "";', "var rawFetch = window.fetch.bind(window);")
    if not code:
        ok(item, "取出 tokenReady 源码", False, "index.html 结构变了，找不到那段")
        return

    js = r"""
const fs = require('fs');
const path = process.argv[2];
const mode = process.argv[3];
const html = fs.readFileSync(path, 'utf8');
function slice(a, b) {
  const i = html.indexOf(a); if (i < 0) return '';
  const j = html.indexOf(b, i); if (j < 0) return '';
  return html.slice(i, j);
}
const code = slice('var PUPPY_TOKEN = "";', 'var rawFetch = window.fetch.bind(window);');
global.window = { PUPPY_TOKEN: "", pywebview: undefined };
if (mode === 'bridge') {
  window.pywebview = { api: { get_token: () => Promise.resolve('tok-sim-123') } };
  // 浏览器里 window 就是全局对象；node 里得补一下，否则 `pywebview.api` 取不到
  global.pywebview = window.pywebview;
}
eval(code);
(async () => {
  const t0 = Date.now();
  await tokenReady();
  const first = Date.now() - t0;
  const t1 = Date.now();
  await tokenReady();
  const second = Date.now() - t1;
  const t2 = Date.now();
  await tokenReady();
  const third = Date.now() - t2;
  process.stdout.write(JSON.stringify({
    mode, first, second, third, token: PUPPY_TOKEN,
    gaveUp: (typeof tokenGaveUp !== 'undefined') ? tokenGaveUp : null
  }));
})().catch(e => { console.error(e); process.exit(2); });
"""
    js_file = TMP / "token_probe.js"
    js_file.write_text(js, encoding="utf-8")

    def run_node(mode: str):
        try:
            out = subprocess.run([node, str(js_file), str(PROG / "index.html"), mode],
                                 capture_output=True, timeout=30)
            return json.loads(out.stdout.decode("utf-8", "replace") or "{}")
        except Exception as exc:
            return {"error": str(exc)}

    a = run_node("bridge")
    if a.get("error"):
        ok(item, "前端令牌等待（桥就绪）", False, a["error"])
    else:
        ok(item, "令牌一到就放行（<200ms）", a["first"] < 200,
           "第一次 %sms，第二次 %sms，token=%r" % (a["first"], a["second"], a["token"]))
        ok(item, "后续请求不再等", a["second"] < 30 and a["third"] < 30,
           "第二/三次 %s/%s ms" % (a["second"], a["third"]))

    b = run_node("nobridge")
    if b.get("error"):
        ok(item, "前端令牌等待（旧 exe 场景）", False, b["error"])
    else:
        ok(item, "没桥时只等一次（旧 exe 不再每请求 +3s）",
           b["first"] >= 2500 and b["second"] < 30 and b["third"] < 30 and b.get("gaveUp"),
           "首次 %sms（预期 ~3000），之后 %s/%s ms，gaveUp=%s"
           % (b["first"], b["second"], b["third"], b.get("gaveUp")))


# ---------------------------------------------------------------------------
# 第 3 项：事实层真的开始长
# ---------------------------------------------------------------------------
FACT_SENTENCES = [
    "我叫小诺",
    "我住在杭州",
    "我晚上十一点睡",
    "我从来不吃香菜",
    "我很讨厌香菜",
    "我喜欢喝冰美式",
]
CHAT_SENTENCES = [
    "今天风好大", "刚吃完饭", "有点困了", "外面在下雨", "刚看完一集动画",
    "地铁上人好多", "今天加班到九点", "想吃点甜的", "手机快没电了", "周末想出去玩",
    "刚刚被打断了一下", "这个bug改了半天", "水温刚好", "楼下在装修", "猫又踩键盘了",
]


def check_facts() -> None:
    item = "3. 事实层"
    if main.MEMORY_DB.exists():
        main.MEMORY_DB.unlink()
    main._MEM_INITED = False

    stored = 0
    for text in FACT_SENTENCES + CHAT_SENTENCES:
        code, data, _, _ = http("POST", "/api/memory/extract",
                                {"text": text, "role": "user", "source": "desktop",
                                 "kind": "chat"})
        if code != 200 or not data.get("ok"):
            ok(item, "抽取接口可用", False, "「%s」→ HTTP %s %s" % (text, code, data))
            return
        stored += int(((data.get("store") or {}).get("inserted") or 0))
        stored += int(((data.get("store") or {}).get("merged") or 0))

    code, stats, _, _ = http("GET", "/api/memory/stats")
    facts = int((stats or {}).get("facts") or 0)
    episodes = int((stats or {}).get("episodes") or 0)
    ok(item, "20 句都进了情景库", episodes >= 20, "episodes=%s" % episodes)
    ok(item, "事实入库（≥4 条）", facts >= 4, "facts=%s（抽取写入 %s）" % (facts, stored))

    code, found, _, _ = http("GET", "/api/memory?type=fact&limit=50&q=")
    items = (found or {}).get("items") or []
    keys = sorted({str((it or {}).get("text") or "").split("：")[0] for it in items})
    ok(item, "搜得到事实条目", bool(items), "共 %s 条：%s" % (len(items), "、".join(keys[:8])))
    ok(item, "来源标成 desktop", all((it or {}).get("source") == "desktop" for it in items),
       "来源=%s" % sorted({(it or {}).get("source") for it in items}))
    ok(item, "称呼/住址/忌口 都抽到了", {"称呼", "住址", "忌口"} <= set(keys),
       "抽到的 key：%s" % "、".join(keys))


# ---------------------------------------------------------------------------
# 第 4 项：事实能删、删了不复活
# ---------------------------------------------------------------------------
def check_delete() -> None:
    item = "4. 事实删除"
    code, found, _, _ = http("GET", "/api/memory?type=fact&limit=50&q=%E5%B0%8F%E8%AF%BA")
    items = [it for it in ((found or {}).get("items") or [])
             if "称呼" in str((it or {}).get("text") or "")]
    if not items:
        ok(item, "先能找到「称呼」这条", False, "没找到可删的事实")
        return
    target = items[0]
    mid = int(target["id"])

    code, res, _, _ = http("DELETE", "/api/memory/%d?type=fact" % mid)
    ok(item, "软删除返回成功", code == 200 and bool((res or {}).get("ok")),
       "HTTP %s / %s" % (code, (res or {}).get("message", "")))

    code, found, _, _ = http("GET", "/api/memory?type=fact&limit=50&q=%E5%B0%8F%E8%AF%BA")
    ids = [int((it or {}).get("id") or 0) for it in ((found or {}).get("items") or [])]
    ok(item, "删完立刻搜不到", mid not in ids, "还在：%s" % ids)

    # 模拟重启：重开库（_MEM_INITED 复位），连接都是每次新开的
    main._MEM_INITED = False
    code, found, _, _ = http("GET", "/api/memory?type=fact&limit=50&q=%E5%B0%8F%E8%AF%BA")
    ids = [int((it or {}).get("id") or 0) for it in ((found or {}).get("items") or [])]
    ok(item, "重启后也不回来", mid not in ids, "还在：%s" % ids)

    # 库里只数行数，不读内容
    import sqlite3
    conn = sqlite3.connect(str(main.MEMORY_DB))
    try:
        alive = conn.execute("SELECT COUNT(*) FROM facts WHERE deleted=0").fetchone()[0]
        dead = conn.execute("SELECT COUNT(*) FROM facts WHERE deleted=1").fetchone()[0]
    finally:
        conn.close()
    ok(item, "库里是软删（deleted=1）", dead >= 1 and alive >= 3,
       "在用 %s 条 / 软删 %s 条" % (alive, dead))


# ---------------------------------------------------------------------------
# 第 5 项：云端知情与「不带记忆片段」
# ---------------------------------------------------------------------------
def check_cloud() -> None:
    item = "5. 云端知情"
    html = (PROG / "index.html").read_text(encoding="utf-8", errors="replace")

    ok(item, "cloudNoMem 默认开", "cloudNoMem: true," in html,
       "DEFAULT 里没有 cloudNoMem: true")
    ok(item, "首启知情弹窗存在", "function cloudNotice()" in html
       and "cloudNoticeAck" in html, "找不到 cloudNotice / cloudNoticeAck")
    ok(item, "切云端会先弹提示", 'if (which === "cloud" && !state.cloudNoticeAck)' in html,
       "pickMode 里没有拦截")
    ok(item, "prompt 拼装前会拦一道",
       "if (memOutboundBlocked()) return \"\";" in html, "memPromptBlock 没这个判断")

    node = shutil.which("node")
    if not node:
        skip(item, "memOutboundBlocked 求值", "没装 node")
        return
    js = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
function fn(name) {
  const i = html.indexOf('function ' + name + '(');
  if (i < 0) return '';
  let depth = 0, started = false;
  for (let k = i; k < html.length; k++) {
    const c = html[k];
    if (c === '{') { depth++; started = true; }
    else if (c === '}') { depth--; if (started && depth === 0) return html.slice(i, k + 1); }
  }
  return '';
}
let kind = 'local';
let state = { cloudNoMem: true };
function modeKind() { return kind; }
eval(fn('memOutboundBlocked'));
const out = {};
kind = 'local'; state.cloudNoMem = true;  out.local_on = memOutboundBlocked();
kind = 'cloud'; state.cloudNoMem = true;  out.cloud_default = memOutboundBlocked();
kind = 'cloud'; state.cloudNoMem = false; out.cloud_off = memOutboundBlocked();
process.stdout.write(JSON.stringify(out));
"""
    js_file = TMP / "cloud_probe.js"
    js_file.write_text(js, encoding="utf-8")
    try:
        out = subprocess.run([node, str(js_file), str(PROG / "index.html")],
                             capture_output=True, timeout=20)
        got = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except Exception as exc:
        ok(item, "memOutboundBlocked 求值", False, str(exc))
        return
    ok(item, "本地模式不拦 / 云端默认拦 / 关掉开关就不拦",
       got.get("local_on") is False and got.get("cloud_default") is True
       and got.get("cloud_off") is False,
       "local=%s cloud默认=%s 关开关=%s"
       % (got.get("local_on"), got.get("cloud_default"), got.get("cloud_off")))


# ---------------------------------------------------------------------------
# 第 6 项：Agent 沙盒
# ---------------------------------------------------------------------------
def _call(tool: str, args: dict, token: str = "") -> dict:
    _, data, _, _ = http("POST", "/api/agent/call",
                         {"tool": tool, "args": args, "confirm_token": token})
    return data or {}


def check_agent() -> None:
    item = "6. Agent 沙盒"
    home = Path.home()

    # 6a 一句话路由到读文件（清单里的原句）
    _, act, _, ms = http("POST", "/api/agent/act",
                         {"text": "读一下 %s" % (home / ".ssh" / "config")})
    used = (act or {}).get("used") or [{}]
    first = used[0] if used else {}
    ok(item, "「读一下 .ssh/config」会真的去读",
       first.get("tool") == "fs_read", "路由到 %r（%s）"
       % (first.get("tool"), (act or {}).get("reason")), ms)
    ok(item, "读 .ssh 直接拒绝、不给确认",
       first.get("ok") is False and not first.get("need_confirm"),
       str(first.get("summary") or (act or {}).get("hint") or "")[:120])

    # 6b 直接调工具：敏感路径
    for name, path in (("密钥目录", home / ".ssh" / "config"),
                       ("环境变量文件", PROG / ".env"),
                       ("记忆库", main.MEMORY_DB)):
        res = _call("fs_read", {"path": str(path)})
        ok(item, "%s 拒绝读取" % name,
           res.get("ok") is False and not res.get("need_confirm"),
           str(res.get("message") or "")[:100])

    # 6c 越界列目录：要确认，确认后能列
    res = _call("fs_list", {"path": str(home)})
    token = str(res.get("confirm_token") or "")
    ok(item, "列用户主目录要先确认", res.get("ok") is False and res.get("need_confirm")
       and bool(token), "need_confirm=%s" % res.get("need_confirm"))
    if token:
        res2 = _call("fs_list", {"path": str(home)}, token)
        ok(item, "确认后能列出来", bool(res2.get("ok")) and int(res2.get("count") or 0) > 0,
           "count=%s" % res2.get("count"))
        # 令牌一次性：同一令牌不能再用
        res3 = _call("fs_list", {"path": str(home)}, token)
        ok(item, "确认令牌只能一次", res3.get("ok") is False,
           str(res3.get("message") or "")[:80])

    # 6d 沙盒里写文件：要确认 → 确认后成功 → 读回来标成不可信
    #    沙盒是 <data_dir>\agent_data\workspace；注意 data_dir() = %LOCALAPPDATA%\PolitePuppyPet\data
    sandbox = main.data_dir() / "agent_data" / "workspace"
    sandbox.mkdir(parents=True, exist_ok=True)
    target = sandbox / "acceptance.txt"
    res = _call("fs_write", {"path": str(target), "content": "验收模拟写入"})
    token = str(res.get("confirm_token") or "")
    ok(item, "沙盒写入要确认", res.get("ok") is False and res.get("need_confirm"),
       "need_confirm=%s" % res.get("need_confirm"))
    if token:
        res = _call("fs_write", {"path": str(target), "content": "验收模拟写入"}, token)
        ok(item, "确认后写入成功", bool(res.get("ok")) and target.is_file(),
           str(res.get("message") or "")[:80])
        res = _call("fs_read", {"path": str(target)})
        ok(item, "读回来标成不可信数据", bool(res.get("untrusted")) and res.get("ok") is True,
           "untrusted=%s" % res.get("untrusted"))

    _, state, _, _ = http("GET", "/api/agent/state")
    ok(item, "插件/工具数量对得上（3 插件 9 工具）",
       int((state or {}).get("plugins") or 0) == 3 and int((state or {}).get("tools") or 0) == 9,
       "plugins=%s tools=%s" % ((state or {}).get("plugins"), (state or {}).get("tools")))


# ---------------------------------------------------------------------------
# 第 7 项：语音与音色
# ---------------------------------------------------------------------------
def check_voice() -> None:
    item = "7. 语音与音色"
    code, diag, _, ms = http("GET", "/api/voice/diag", timeout=90)
    ok(item, "语音自检能跑完", code == 200 and bool((diag or {}).get("ok")),
       "HTTP %s / %s" % (code, (diag or {}).get("message", "")), ms)
    ok(item, "pythonnet 可用（听得见的前提）", bool((diag or {}).get("pythonnet")),
       str((diag or {}).get("pythonnet_error") or "ok"))
    voices = (diag or {}).get("tts_voices") or []
    ok(item, "系统有中文语音", bool((diag or {}).get("tts_zh")),
       "已装 %d 个：%s" % (len(voices), "、".join(voices[:3])))

    code, ident, _, _ = http("GET", "/api/voice/id")
    ok(item, "声线身份证在", code == 200 and bool((ident or {}).get("voice_id")),
       "%s / %s" % ((ident or {}).get("voice_id"), (ident or {}).get("display")))
    ok(item, "音色没漂移", (ident or {}).get("drift") is False,
       "%s，drift=%s" % ((ident or {}).get("message"), (ident or {}).get("drift")))

    # 真出声这一段没法在无窗口进程里验；能验的是"合成器真的产出了这段音频"。
    url = (os.environ.get("VOICE_TTS_URL") or "").strip()
    if not url:
        skip(item, "实际合成一句", "没配 VOICE_TTS_URL（只走系统语音）")
        return
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    try:
        with socket.create_connection((host, port), timeout=2):
            pass
    except Exception:
        skip(item, "实际合成一句",
             "F5 服务没在跑（%s）——双击 exe 时它会自动拉起，这一步留给真机" % url)
        return

    # 服务在跑：合成到内存里检查 RIFF 头，不播放（夜里不吵人）
    body = json.dumps({"model": "f5", "input": "今天也要好好的",
                       "voice": "小狗"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            wav = resp.read()
        ms = int((time.perf_counter() - t0) * 1000)
    except Exception as exc:
        ok(item, "实际合成一句", False, "调用 %s 失败：%s" % (url, exc))
        return
    ok(item, "实际合成一句（不播放）", wav[:4] == b"RIFF" and len(wav) > 20 * 1024,
       "%d 字节 / %d ms" % (len(wav), ms), ms)


# ---------------------------------------------------------------------------
# 第 8 项：七个热键
# ---------------------------------------------------------------------------
def check_hotkeys() -> None:
    item = "8. 热键"
    bindings = main.load_hotkeys({})
    ok(item, "默认绑定是 7 个", len(bindings) == 7,
       "、".join("%s=%s" % (k, v) for k, v in bindings.items()))
    bad = [k for k, v in bindings.items() if main.parse_hotkey(v) is None]
    ok(item, "7 个都能解析", not bad, "解析不了：%s" % bad)

    if sys.platform != "win32":
        skip(item, "真实注册", "不是 Windows")
        return
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                      wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL

    failed = []
    busy = []
    for i, (action, spec) in enumerate(sorted(bindings.items())):
        parsed = main.parse_hotkey(spec)
        if not parsed:
            failed.append("%s(%s) 解析失败" % (action, spec))
            continue
        mods, vk = parsed
        hid = 9100 + i
        if user32.RegisterHotKey(None, hid, mods, vk):
            user32.UnregisterHotKey(None, hid)      # 立刻还回去，不占用户的热键
        else:
            err = ctypes.get_last_error()
            # 1409 = 已被别的程序占用。这不是本程序的缺陷，是环境冲突：
            # 换一个绑定，或关掉占着它的那个程序。标成 SKIP，别误判成代码坏了。
            if err == 1409:
                busy.append("%s(%s)" % (action, spec))
            else:
                failed.append("%s(%s) err=%s" % (action, spec, err))
    if failed:
        ok(item, "7 个都能注册（注册后立刻注销）", False,
           "注册失败：%s" % "；".join(failed))
    elif busy:
        # 1409 = 已被别的程序占用。不是本程序的缺陷，是环境冲突：
        # 换一个绑定，或关掉占着它的那个程序。标成 SKIP，别误判成代码坏了。
        skip(item, "7 个都能注册（注册后立刻注销）",
             "被别的程序占用：%s（换绑定或关掉占用它的程序再试）" % "、".join(busy))
    else:
        ok(item, "7 个都能注册（注册后立刻注销）", True, "全部可用")


# ---------------------------------------------------------------------------
# 附加：启动真实 exe，确认它自报的版本（清单第 1 项的真机版）
# ---------------------------------------------------------------------------
def _pet_running() -> bool:
    for p in range(8765, 8795):
        try:
            with socket.create_connection(("127.0.0.1", p), timeout=0.2):
                return True
        except Exception:
            continue
    return False


def check_exe(exe: Path) -> None:
    item = "1b. 真实 exe"
    if not exe.is_file():
        ok(item, "exe 存在", False, str(exe))
        return
    if _pet_running():
        skip(item, "启动 exe 验版本", "已经有桌宠在跑，先退出再试")
        return

    env = os.environ.copy()
    env["POLITE_PUPPY_DATA"] = str(TMP / "exe_data")
    env["VOICE_TTS_URL"] = ""          # 别为了验版本把 F5 模型拉起来
    env["VOICE_TTS_MODE"] = ""
    t0 = time.time()
    proc = subprocess.Popen([str(exe), "--no-tray", "--allow-multi"],
                            cwd=str(exe.parent), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    found: dict = {}
    found_port = 0
    try:
        deadline = time.time() + 45
        while time.time() < deadline:
            for p in range(8765, 8795):
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d/show" % p,
                                                timeout=0.4) as r:
                        j = json.loads(r.read().decode("utf-8", "replace") or "{}")
                    # 不能用 pid 比对：onefile 的 bootloader 和真正干活的子进程
                    # 是两个 PID，/show 报的是后者。启动前已经确认没有别的实例。
                    if j.get("ok") and j.get("version"):
                        found, found_port = j, p
                        break
                except Exception:
                    continue
            if found:
                break
            # 父进程退了不代表应用退了（onefile 会转手），所以只做提示不退出
            if proc.poll() is not None and time.time() - t0 > 10:
                break
            time.sleep(0.4)

        ok(item, "exe 能启动并应答 /show", bool(found),
           ("端口 %d，启动用了 %.1f 秒" % (found_port, time.time() - t0)) if found
           else "45 秒内没有实例应答")
        if not found:
            return
        ok(item, "exe 自报版本 = %s" % main.APP_VERSION,
           str(found.get("version") or "") == main.APP_VERSION,
           "实际 %r" % found.get("version"))

        # 起来就退是最坑的：隔 3 秒再问一次
        time.sleep(3.0)
        alive = False
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/show" % found_port,
                                        timeout=1.0) as r:
                alive = bool(json.loads(r.read().decode("utf-8", "replace") or "{}").get("ok"))
        except Exception:
            alive = False
        ok(item, "启动 3 秒后仍活着", alive,
           "端口 %d %s" % (found_port, "还在" if alive else "已经没了"))
    finally:
        pids = {int(found.get("pid") or 0), proc.pid}
        for pid in [x for x in pids if x]:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(exe: Path | None) -> int:
    global PORT
    srv = main.LocalServer(PROG, preferred_ports=[0])
    PORT = srv.start()
    print("模拟环境：%s" % TMP)
    print("本地服务：127.0.0.1:%d（令牌已启用）\n" % PORT)
    try:
        check_version()
        check_latency()
        check_facts()
        check_delete()
        check_cloud()
        check_agent()
        check_voice()
        check_hotkeys()
        if exe is not None:
            check_exe(exe)
    finally:
        srv.stop()

    width = {"PASS": "通过", "FAIL": "失败", "SKIP": "跳过"}
    current = ""
    for r in RESULTS:
        if r["item"] != current:
            current = r["item"]
            print("\n【%s】" % current)
        tail = ("  — " + r["detail"]) if r["detail"] else ""
        print("  [%s] %s%s" % (width.get(r["status"], r["status"]), r["name"], tail))

    n = {k: sum(1 for r in RESULTS if r["status"] == k) for k in ("PASS", "FAIL", "SKIP")}
    print("\n合计：通过 %d / 失败 %d / 跳过 %d" % (n["PASS"], n["FAIL"], n["SKIP"]))
    if n["FAIL"]:
        print("有失败项：先修，再谈发布。")
    elif n["SKIP"]:
        print("没有失败项；跳过的 %d 项要真机才能验（见上）。" % n["SKIP"])
    else:
        print("全部通过（%d 项）。" % n["PASS"])
    return 0 if n["FAIL"] == 0 else 1


def main_cli(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="八项验收的自动化模拟")
    ap.add_argument("--json", default="", help="把结果写成 JSON（给审阅包留证据）")
    ap.add_argument("--exe", default="", help="额外启动这个 exe 验版本（会短暂显示立绘）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录，便于排查")
    args = ap.parse_args(argv)

    exe = Path(args.exe).expanduser().resolve() if args.exe else None
    code = run(exe)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": main.APP_VERSION,
            "port": PORT,
            "results": RESULTS,
            "summary": {k: sum(1 for r in RESULTS if r["status"] == k)
                        for k in ("PASS", "FAIL", "SKIP")},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写入：%s" % out)

    if not args.keep:
        shutil.rmtree(TMP, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main_cli())
