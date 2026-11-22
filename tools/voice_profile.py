# -*- coding: utf-8 -*-
"""音色档案管理：把"克隆一次"的结果固化成可复用的音色源。

一个音色档案 = 参考音频 + 参考文本 + 台词库 + 元数据，放在
    voices/profiles/<id>/
        profile.json     音色元数据（引擎、创建时间、参数、状态）
        ref.wav          参考音频（24kHz / 单声道 / PCM16，已清洗）
        manifest.json    台词库（VoicePack 直接吃：texts/lines/chat_text）
        chat.wav, *.wav  预生成台词（命中就 0 延迟播，不用再合成）

    voices/active.json   {"id": "小狗"}   当前启用哪个音色

用法：
    python tools/voice_profile.py list
    python tools/voice_profile.py build --name 小狗 --ref D:\\ref.wav --text "参考音频里说的话"
    python tools/voice_profile.py build --name 小狗 --ref ref.wav --text "..." --from-pack voices/我的声音
    python tools/voice_profile.py use 小狗
    python tools/voice_profile.py show 小狗

注意：build 只做"清洗 + 固化 + 归档"，不跑模型；要补预生成台词用 --gen（会调 F5，CPU 上很慢）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICES = ROOT / "voices"
PROFILES = VOICES / "profiles"
ACTIVE = VOICES / "active.json"
REF_SR = 24000


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------
def _slug(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", (name or "").strip())
    return s.strip("_") or "voice"


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default if default is not None else {}


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


def active_id() -> str:
    raw = _read_json(ACTIVE, {})
    if isinstance(raw, dict) and raw.get("id"):
        return str(raw["id"])
    ids = [p.name for p in PROFILES.iterdir() if (p / "profile.json").exists()] if PROFILES.exists() else []
    return ids[0] if ids else ""


def set_active(pid: str) -> bool:
    if not (PROFILES / pid / "profile.json").exists():
        return False
    _write_json(ACTIVE, {"id": pid, "at": int(time.time())})
    return True


def profile_ids() -> list[str]:
    if not PROFILES.exists():
        return []
    return sorted(p.name for p in PROFILES.iterdir() if (p / "profile.json").exists())


def load_profile(pid: str | None = None) -> dict:
    pid = pid or active_id()
    p = PROFILES / pid / "profile.json"
    data = _read_json(p, {})
    if data:
        data["id"] = pid
        data["dir"] = str(PROFILES / pid)
    return data


# --------------------------------------------------------------------------
# 参考音频清洗
# --------------------------------------------------------------------------
def _clean_ref(src: Path, dst: Path) -> dict:
    """转 24k 单声道 PCM16，裁掉首尾静音，做峰值归一。

    返回 {"seconds":..., "peak":..., "sr":...} 供 profile.json 记录。
    """
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(str(src), dtype="float32", always_2d=True)
    if data.size == 0:
        raise RuntimeError("参考音频是空的")
    mono = data.mean(axis=1)
    if sr != REF_SR:
        # 线性插值重采样：参考音频只用于条件，音质影响很小，够用且零依赖
        n = int(round(len(mono) * REF_SR / sr))
        x_old = np.linspace(0, 1, num=len(mono), endpoint=False)
        x_new = np.linspace(0, 1, num=n, endpoint=False)
        mono = np.interp(x_new, x_old, mono).astype(np.float32)
        sr = REF_SR

    # 裁首尾静音（-40dB）
    peak = float(np.max(np.abs(mono))) or 1.0
    db = 20 * np.log10(np.maximum(np.abs(mono), 1e-9) / peak)
    idx = np.where(db > -40)[0]
    if len(idx):
        mono = mono[max(0, idx[0] - int(0.05 * sr)): idx[-1] + int(0.05 * sr)]

    # 峰值归一化到 0.95，避免削波
    peak2 = float(np.max(np.abs(mono))) or 1.0
    mono = (mono / peak2) * 0.95

    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), mono.astype(np.float32), sr, subtype="PCM_16")
    return {"seconds": round(len(mono) / sr, 2), "sr": sr, "peak": round(peak2, 4)}


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------
def build(name: str, ref: Path, ref_text: str, from_pack: Path | None = None,
          phrases: list[str] | None = None, gen: bool = False,
          speed: float = 1.45, note: str = "") -> Path:
    if not ref.exists():
        raise SystemExit(f"参考音频不存在：{ref}")
    ref_text = (ref_text or "").strip()
    if not ref_text:
        raise SystemExit("必须给出参考音频里说的那句话（--text），否则克隆不能对齐")

    pid = _slug(name)
    pdir = PROFILES / pid
    pdir.mkdir(parents=True, exist_ok=True)

    info = _clean_ref(ref, pdir / "ref.wav")

    # 台词库：优先从现成的语音包搬（wav + manifest），再把短语清单补进去
    manifest = {"name": name, "strict": False, "texts": {}, "lines": {}}
    if from_pack and from_pack.exists():
        man = _read_json(from_pack / "manifest.json", {})
        if man:
            manifest.update({
                "name": name,
                "strict": bool(man.get("strict")),
                "texts": dict(man.get("texts") or {}),
                "lines": dict(man.get("lines") or {}),
                "chat_text": man.get("chat_text", ""),
            })
        for wav in from_pack.glob("*.wav"):
            target = pdir / wav.name
            if not target.exists() or target.stat().st_size != wav.stat().st_size:
                shutil.copy2(wav, target)

    # 预生成额外短语（可选，会调 F5）
    if gen and phrases:
        for text in phrases:
            text = text.strip()
            if not text:
                continue
            key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
            out = pdir / f"bank_{key}.wav"
            if out.exists():
                manifest["texts"][text] = out.name
                continue
            try:
                sys.path.insert(0, str(ROOT / "tools"))
                import f5_clone  # type: ignore

                f5_clone.synth(text, speed=speed, nfe=12, out=str(out),
                               ref_audio=str(pdir / "ref.wav"), ref_text=ref_text)
                manifest["texts"][text] = out.name
                print(f"  预生成：{text} → {out.name}")
            except Exception as exc:
                print(f"  ! 预生成失败：{text}（{exc}）")

    _write_json(pdir / "manifest.json", manifest)
    profile = {
        "id": pid,
        "name": name,
        "engine": "f5-tts",
        "created_at": int(time.time()),
        "ref": "ref.wav",
        "ref_text": ref_text,
        "checkpoint": "",          # 预留：微调后的模型路径（P1 用）
        "speed": speed,
        "seed": 20261025,
        "nfe": 12,
        "bank": len(manifest.get("texts") or {}),
        "note": note,
        "source": {"ref_name": ref.name, **info},
    }
    _write_json(pdir / "profile.json", profile)
    set_active(pid)
    return pdir


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="音色档案管理（持久化克隆音色）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="从一段参考录音构建音色档案")
    b.add_argument("--name", required=True)
    b.add_argument("--ref", required=True, type=Path)
    b.add_argument("--text", required=True, help="参考音频里说的那句话")
    b.add_argument("--from-pack", type=Path, default=None, help="沿用现成语音包的台词与 wav")
    b.add_argument("--phrases", nargs="*", default=[], help="要预生成的短语")
    b.add_argument("--phrases-file", type=Path, default=None, help="一行一句的短语清单")
    b.add_argument("--gen", action="store_true", help="真的调用 F5 预生成（CPU 上很慢）")
    b.add_argument("--speed", type=float, default=1.45)
    b.add_argument("--note", default="")

    sub.add_parser("list", help="列出所有音色")
    u = sub.add_parser("use", help="切换到某个音色")
    u.add_argument("id")
    s = sub.add_parser("show", help="查看音色详情")
    s.add_argument("id", nargs="?", default=None)

    args = ap.parse_args(argv)

    if args.cmd == "build":
        phrases = list(args.phrases or [])
        if args.phrases_file and args.phrases_file.exists():
            phrases += [ln.strip() for ln in args.phrases_file.read_text("utf-8").splitlines() if ln.strip()]
        pdir = build(args.name, args.ref, args.text, args.from_pack, phrases, args.gen, args.speed, args.note)
        print(f"已构建音色：{pdir}")
        print(f"当前启用：{active_id()}")
        return 0

    if args.cmd == "list":
        cur = active_id()
        ids = profile_ids()
        if not ids:
            print("（还没有音色档案）")
            return 0
        for pid in ids:
            p = load_profile(pid)
            mark = "*" if pid == cur else " "
            print(f"{mark} {pid:12s} {p.get('name',''):10s} 台词 {p.get('bank',0):3d} 条 "
                  f"参考 {p.get('source',{}).get('seconds','?')}s  {p.get('engine','')}")
        return 0

    if args.cmd == "use":
        if not set_active(args.id):
            print(f"没有这个音色：{args.id}")
            return 1
        print(f"已切换：{args.id}")
        return 0

    if args.cmd == "show":
        p = load_profile(args.id)
        if not p:
            print("没有这个音色")
            return 1
        print(json.dumps(p, ensure_ascii=False, indent=2))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
