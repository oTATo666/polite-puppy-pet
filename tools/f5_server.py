# -*- coding: utf-8 -*-
"""本地 F5-TTS 克隆音色服务（常驻 HTTP）。

桌宠后端 CloneTTS 配 VOICE_TTS_URL 指向这里，把任意文本合成为小狗的克隆音色。

接口（OpenAI 兼容 + 简化版）：
    POST /v1/audio/speech   body={"model":"f5","input":"文本","voice":"小狗"}
    POST /tts               body={"text":"文本"}   （简化版）
    GET  /health            返回 {"ok":true,...}

启动（必须用 voice-clone-env 的 python）：
    "C:\\Users\\zheng'zheng\\WorkBuddy\\voice-clone-env\\Scripts\\python.exe" ^
        "C:\\Users\\zheng'zheng\\OneDrive\\Desktop\\礼貌小狗桌宠\\tools\\f5_server.py" --port 9880
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))


def _load_env() -> None:
    """读项目根目录 .env 里的 F5_* 配置。

    独立启动（不是桌宠拉起）时也能拿到显存上限/空闲卸载等参数，
    否则这些限制只在"由桌宠启动"时生效，容易误以为没限住。
    """
    try:
        raw = (ROOT / ".env").read_text("utf-8-sig")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key.startswith("F5_") and key not in os.environ:
            os.environ[key] = val


_load_env()

# 合成是重活，串行排队，别让并发把机器拖垮
_synth_lock = threading.Lock()
_state: dict[str, object] = {"ready": False, "device": "", "error": "", "profile": ""}
_last_use = time.time()


def _vram_cap() -> None:
    """给这块 GPU 显存封顶，别让语音把显卡吃满。"""
    frac = os.environ.get("F5_VRAM_FRACTION") or ""
    try:
        frac = float(frac)
    except ValueError:
        frac = 0.0
    if frac <= 0 or frac > 1:
        return
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.set_per_process_memory_fraction(frac)
            print(f"[f5-server] 显存上限设为 {frac:.0%}", flush=True)
    except Exception as exc:
        print(f"[f5-server] 设显存上限失败：{exc}", flush=True)


def _free_vram() -> None:
    """合成完立刻把缓存还给显卡；空闲久了连模型一起卸载。"""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _idle_watch() -> None:
    """没人说话超过 N 分钟就把模型卸掉，显存归零（下次自动重新加载）。"""
    try:
        minutes = float(os.environ.get("F5_IDLE_UNLOAD_MIN") or "0")
    except ValueError:
        minutes = 0.0
    if minutes <= 0:
        return
    while True:
        time.sleep(30)
        try:
            if time.time() - _last_use < minutes * 60:
                continue
            import f5_clone
            if getattr(f5_clone, "_ENGINE", None) is None:
                continue
            f5_clone._ENGINE = None
            _state["ready"] = False
            _free_vram()
            print(f"[f5-server] 空闲 {minutes:.0f} 分钟，模型已卸载，显存已释放", flush=True)
        except Exception:
            pass


def active_profile() -> dict:
    """读 voices/active.json → 音色档案（参考音频 + 参考文本）。

    这就是"持久化音源"的入口：换音色 = 改一个 id，服务不用重启、模型不用重载。
    """
    try:
        data = json.loads((ROOT / "voices" / "active.json").read_text("utf-8"))
        pid = str(data.get("id") or "")
    except (OSError, ValueError):
        pid = ""
    if not pid:
        return {}
    pdir = ROOT / "voices" / "profiles" / pid
    try:
        prof = json.loads((pdir / "profile.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    ref = pdir / str(prof.get("ref") or "ref.wav")
    prof["_id"] = pid
    prof["_ref"] = str(ref) if ref.exists() else ""
    prof["_dir"] = str(pdir)

    # 用「音色身份证」校正：参考音频与合成参数以身份证为准。
    # 这样出的声始终是小狗自己那条声线，不会因为默认路径/参数变化而悄悄漂移。
    try:
        import voice_identity
        ident = voice_identity.sync_now()
        if ident.get("ok") and ident.get("ref_abs"):
            prof["_ref"] = ident["ref_abs"]
            prof["ref_text"] = ident.get("ref_text") or prof.get("ref_text") or ""
            prm = ident.get("params") or {}
            if prm.get("speed"):
                prof["speed"] = prm["speed"]
            if prm.get("seed"):
                prof["seed"] = prm["seed"]
            prof["_voice_id"] = ident.get("voice_id")
    except Exception:
        pass
    return prof


def _warmup() -> None:
    """开机就把模型和声码器加载好，并合成一句短的。

    不预热的话，第一句要等模型加载（CPU 十几秒、GPU 也几秒），
    用户听到的就是"开了说话但半天没声"。
    """
    try:
        import f5_clone

        prof = active_profile()
        _state["device"] = f5_clone.DEVICE
        _state["profile"] = prof.get("_id") or ""
        print(f"[f5-server] 预热模型（device={f5_clone.DEVICE}，音色={prof.get('name') or '默认'}）…", flush=True)
        t0 = time.time()
        with _synth_lock:
            out = f5_clone.CACHE / "_warm.wav"
            f5_clone.synth("嗯", speed=float(prof.get("speed") or 1.45), nfe=8, out=str(out),
                           seed=int(prof.get("seed") or 20261025),
                           ref_audio=prof.get("_ref") or None,
                           ref_text=prof.get("ref_text") or None)
        _state["ready"] = True
        print(f"[f5-server] 预热完成，{time.time() - t0:.1f}s（device={f5_clone.DEVICE}）", flush=True)
    except Exception as exc:
        _state["error"] = str(exc)
        print(f"[f5-server] 预热失败：{exc}", flush=True)


def do_synth(text: str, speed: float = 1.45, prof: dict | None = None) -> bytes:
    """合成并返回 wav 字节。

    F5 对个别文本 + 随机种子会退化成近似静音（实测 15 字只出 0.7s）。
    实测固定种子 20261025 / 42 稳定正常，所以按「已验证种子 → 随机」的
    顺序重试（共 4 次）；全部退化就抛错，让调用方保持安静，
    绝不把碎片音频当正常结果返回。
    """
    import f5_clone

    import soundfile as sf

    prof = prof or {}
    ref_audio = prof.get("_ref") or None
    ref_text = prof.get("ref_text") or None
    seed0 = int(prof.get("seed") or 20261025)
    scratch = Path(prof.get("_dir") or f5_clone.CACHE) / "_tts_out.wav"
    # 期望时长下限：太严会把"汪"这种一字句判成退化，白跑 4 次、延迟翻三倍。
    # 短句给个 0.35s 的地板，长句按 0.10s/字估。
    expect_min = max(0.35, 0.10 * len(text) + 0.30)
    seeds = [seed0, 42]          # GPU 上两次足够；再多只是徒增延迟
    # GPU 上 12 步足够；CPU 上要更少步数才勉强能等
    try:
        nfe = int(os.environ.get("F5_NFE") or (12 if f5_clone.DEVICE == "cuda" else 8))
    except ValueError:
        nfe = 12

    with _synth_lock:
        for i, seed in enumerate(seeds):
            f5_clone.synth(text, speed=speed, nfe=nfe, out=str(scratch), seed=seed,
                           ref_audio=ref_audio, ref_text=ref_text)
            try:
                dur = sf.info(str(scratch)).duration
            except Exception:
                dur = 0.0
            if dur >= expect_min:
                break
            print(f"[f5-server] 第 {i + 1} 次生成只有 {dur:.2f}s（期望>={expect_min:.2f}s），换种子重试",
                  flush=True)
        else:
            raise RuntimeError(f"连续 {len(seeds)} 次合成都退化（文本可能不适合当前参考音色）")

    return scratch.read_bytes()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _wav(self, data: bytes, ms: int = 0):
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Synth-Ms", str(int(ms)))   # 服务端自报耗时，方便定位慢在哪
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/"):
            self._json(200, {
                "ok": True, "engine": "f5-tts",
                "device": _state.get("device") or "",
                "ready": bool(_state.get("ready")),
                "error": _state.get("error") or "",
                "ts": int(time.time() * 1000),
            })
            return
        # 实时同步：随时可查"现在用的是不是小狗自己的声线"（含音色指纹校验）
        if self.path.split("?")[0] in ("/voice", "/voice/id"):
            try:
                import voice_identity
                self._json(200, dict(ok=True, **voice_identity.verify()))
            except Exception as exc:
                self._json(500, {"ok": False, "message": str(exc)})
            return
        self._json(404, {"ok": False, "message": "not found"})

    def do_POST(self):
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
        if len(text) > 200:
            text = text[:200]

        try:
            speed = float(payload.get("speed") or 1.45)
        except (TypeError, ValueError):
            speed = 1.45

        global _last_use
        t0 = time.time()
        _last_use = t0
        prof = active_profile()
        try:
            data = do_synth(text, speed, prof)
        except Exception as exc:
            _free_vram()
            self._json(500, {"ok": False, "message": f"合成失败：{exc}"})
            return
        finally:
            _last_use = time.time()
        print(f"[f5-server] 合成 {len(text)} 字，耗时 {time.time()-t0:.1f}s"
              f"（音色={prof.get('name') or '默认'}）", flush=True)
        self._wav(data, (time.time() - t0) * 1000)
        # 注意：这里**不要**每次都 empty_cache()。实测清空后再合成要重新向驱动
        # 申请显存，一句从 1.0s 涨到 3.2s。显存由空闲卸载线程负责归还。


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9880)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    _vram_cap()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[f5-server] 监听 http://{args.host}:{args.port} (engine=f5-tts)", flush=True)
    threading.Thread(target=_warmup, name="warmup", daemon=True).start()
    threading.Thread(target=_idle_watch, name="idle-watch", daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
