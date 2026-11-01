# -*- coding: utf-8 -*-
r"""
小狗语音网关：把 GPT-SoVITS 接进桌宠，全程只用"小狗"这一条克隆音色
======================================================================

为什么需要它
------------
桌宠 main.py 的 CloneTTS 已经会说两种协议的其中一种：
  · openai 模式 → POST /v1/audio/speech  {"model","input","voice","response_format"}
  · gsv   模式 → POST /tts              {"text","ref_audio_path","prompt_text",...}
而 GPT-SoVITS 的 api_v2.py 只吃第二种。这个网关两种都收，统一翻译成
api_v2.py 的格式转发过去，**所以 main.py 和 .env 一行都不用改**。

它同时负责三件正事：
  1. 音色钉死：参考音频/提示文本由本网关统一注入，调用方传什么都不好使，
     杜绝"换了个 voice 参数就变成别人的声音"（沿用音色身份证的思路）。
  2. 失败说清楚：上游没起 / 模型没下 / 合成报错，都返回明确原因，
     绝不静默换个嗓子（由 main.py 的严格克隆负责保持安静）。
  3. 关键日志：每次合成都打印用的引擎、参考音频、耗时，便于确认"现在是谁的声音"。

用法
----
    python tools\puppy_tts_gateway.py --port 9880 --upstream http://127.0.0.1:9881
    python tools\puppy_tts_gateway.py --selftest      # 只体检，不起服务

配置：tools\tts_ref.json（参考音频路径 / 提示文本 / 语种）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF_JSON = ROOT / "tools" / "tts_ref.json"

# 默认参考音频：用户提供的那段素材
DEFAULT_REF = r"C:\Users\zheng'zheng\OneDrive\Desktop\礼貌小狗_语音素材\我的声音\示例_任意文字.wav"

DEFAULT_CONFIG = {
    "_说明": "小狗克隆音色的参考素材。ref_wav 是参考音频，prompt_text 是这段音频里说的话（越准越像）。",
    "ref_wav": DEFAULT_REF,
    "prompt_text": "",
    "text_lang": "zh",
    "prompt_lang": "zh",
    "media_type": "wav",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if REF_JSON.is_file():
            data = json.loads(REF_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    except Exception as exc:
        print("[网关] 配置读取失败，用默认值：%s" % exc)
    return cfg


def save_config(cfg: dict) -> None:
    REF_JSON.parent.mkdir(parents=True, exist_ok=True)
    REF_JSON.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def check_ready(cfg: dict) -> list:
    """返回问题清单（空列表 = 就绪）。"""
    problems = []
    ref = str(cfg.get("ref_wav") or "").strip()
    if not ref:
        problems.append("没配参考音频 ref_wav")
    elif not os.path.isfile(ref):
        problems.append(f"参考音频不存在：{ref}")
    if not str(cfg.get("prompt_text") or "").strip():
        problems.append("没填 prompt_text（参考音频里说的话）—— 不填会明显不像，"
                        "可用本地 faster-whisper 自动识别后写入")
    return problems


def upstream_health(url: str, timeout: float = 3.0) -> tuple:
    """探 GPT-SoVITS api_v2.py 是否在跑。"""
    try:
        req = urllib.request.Request(url.rstrip("/") + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8", "ignore")[:200]
    except Exception as exc:
        return False, str(exc)


def synth(cfg: dict, text: str, upstream: str, timeout: float = 120.0) -> bytes:
    """把文本交给 GPT-SoVITS 合成，返回音频字节。"""
    payload = {
        "text": text,
        "text_lang": cfg.get("text_lang") or "zh",
        "ref_audio_path": cfg.get("ref_wav"),
        "prompt_text": cfg.get("prompt_text") or "",
        "prompt_lang": cfg.get("prompt_lang") or "zh",
        "media_type": cfg.get("media_type") or "wav",
        "streaming_mode": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        upstream.rstrip("/") + "/tts", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class Handler(BaseHTTPRequestHandler):
    cfg: dict = {}
    upstream: str = "http://127.0.0.1:9881"

    def log_message(self, *a):
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _audio(self, data: bytes, ms: int):
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Synth-Ms", str(int(ms)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/"):
            problems = check_ready(self.cfg)
            ok_up, up_msg = upstream_health(self.upstream)
            self._json(200, {
                "ok": not problems and ok_up,
                "engine": "gpt-sovits",
                "ref_wav": self.cfg.get("ref_wav"),
                "prompt_text": self.cfg.get("prompt_text"),
                "upstream": self.upstream,
                "upstream_ok": ok_up,
                "upstream_msg": up_msg,
                "problems": problems,
                "ts": int(time.time()),
            })
            return
        self._json(404, {"ok": False, "message": "not found"})

    def do_POST(self):
        route = self.path.split("?")[0]
        if route not in ("/tts", "/v1/audio/speech"):
            self._json(404, {"ok": False, "message": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "message": "bad json"})
            return

        text = str(payload.get("input") or payload.get("text") or "").strip()
        if not text:
            self._json(400, {"ok": False, "message": "no text"})
            return

        problems = check_ready(self.cfg)
        if problems:
            self._json(503, {"ok": False, "message": "；".join(problems)})
            return

        ok_up, up_msg = upstream_health(self.upstream)
        if not ok_up:
            self._json(503, {"ok": False,
                             "message": f"GPT-SoVITS 服务未就绪（{self.upstream}）：{up_msg}"})
            return

        t0 = time.time()
        try:
            audio = synth(self.cfg, text, self.upstream)
        except urllib.error.HTTPError as exc:
            self._json(502, {"ok": False, "message": f"上游返回 {exc.code}：{exc.read()[:200]!r}"})
            return
        except Exception as exc:
            self._json(502, {"ok": False, "message": f"合成失败：{exc}"})
            return
        ms = (time.time() - t0) * 1000
        print("[网关] 合成 %d 字 → %d 字节，耗时 %.0fms（ref=%s）"
              % (len(text), len(audio), ms, os.path.basename(str(self.cfg.get("ref_wav")))))
        self._audio(audio, ms)


def selftest(upstream: str) -> int:
    cfg = load_config()
    print("配置文件:", REF_JSON)
    print("参考音频:", cfg.get("ref_wav"))
    print("提示文本:", repr(cfg.get("prompt_text")))
    problems = check_ready(cfg)
    if problems:
        print("【未就绪】")
        for p in problems:
            print("  -", p)
    else:
        print("【配置就绪】")
    ok, msg = upstream_health(upstream)
    print("上游 GPT-SoVITS:", "在线" if ok else "离线", msg[:160])
    return 0 if (not problems and ok) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="小狗语音网关（GPT-SoVITS）")
    ap.add_argument("--port", type=int, default=9880)
    ap.add_argument("--upstream", default="http://127.0.0.1:9881",
                    help="GPT-SoVITS api_v2.py 的地址")
    ap.add_argument("--selftest", action="store_true", help="只体检不启服务")
    args = ap.parse_args(argv)

    cfg = load_config()
    if not REF_JSON.is_file():
        save_config(cfg)
        print("[网关] 已生成配置模板：%s（请填 prompt_text）" % REF_JSON)

    if args.selftest:
        return selftest(args.upstream)

    Handler.cfg = cfg
    Handler.upstream = args.upstream
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("[网关] 监听 http://127.0.0.1:%d  上游=%s" % (args.port, args.upstream))
    print("[网关] 参考音频=%s" % cfg.get("ref_wav"))
    problems = check_ready(cfg)
    for p in problems:
        print("[网关] 注意：%s" % p)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
