# -*- coding: utf-8 -*-
"""F5-TTS 本地克隆引擎。

用小狗的参考录音（ref_voice）把任意中文文本合成克隆音色。
可命令行调用，也可被本地 TTS 服务 import 后调用 ``synth()``。

依赖：voice-clone-env（torch + f5_tts + vocos + faster-whisper）。
权重：.voice_cache/f5/（F5TTS_v1_Base safetensors + vocos）。

用法：
    python tools\\f5_clone.py "你好呀" -o 试听\\试.wav --speed 1.45
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 把 f5_tts 包的 configs 目录路径交给 hydra/omegaconf
import torch  # noqa: E402  (先 import，确保设备/线程环境就绪)

def _patch_torchaudio():
    """torchaudio 2.9+ 把读音频交给 torchcodec，本机 torchcodec 的 DLL 加载失败
    （libtorchcodec_core4.dll / FFmpeg 依赖缺失）。这里用 soundfile 顶替 torchaudio.load，
    绕开 torchcodec，功能等价（读 wav 返回 (tensor[1,T], sr)）。

    如果 torchaudio 自己就 import 不起来（换 torch 版本时常见），直接塞一个最小替身模块，
    保证 F5 的 utils_infer 里 `import torchaudio` 不会把整个合成链路拖死。
    """
    import sys
    import types

    def _load(path, *a, **k):
        import soundfile as sf
        import torch

        data, sr = sf.read(path, dtype="float32", always_2d=True)  # (T, C)
        return torch.from_numpy(data.T).contiguous(), sr

    try:
        import torchaudio

        torchaudio.load = _load
        print("[f5] torchaudio.load 已用 soundfile 顶替（绕开 torchcodec）", flush=True)
        return
    except Exception as exc:
        print(f"[f5] torchaudio 不可用（{exc}），装一个最小替身", flush=True)

    mod = types.ModuleType("torchaudio")
    mod.load = _load
    mod.__version__ = "0.0-shim"
    sys.modules["torchaudio"] = mod


_patch_torchaudio()

APP_DIR = Path(__file__).resolve().parent.parent
CACHE = APP_DIR / ".voice_cache"
F5_DIR = CACHE / "f5"
CKPT = F5_DIR / "F5TTS_v1_Base" / "model_1250000.safetensors"
VOCOS_DIR = F5_DIR / "vocos"
REF_AUDIO = CACHE / "ref_clean.wav" or (CACHE / "ref_clean.wav")
REF_TEXT_FILE = CACHE / "ref_text.txt"
# 参考音频：优先洗净版，否则回落到 voice_clone 的 48k PCM16
REF_FALLBACK = Path(r"C:\Users\zheng'zheng\WorkBuddy\2026-10-24-16-48-19\voice_clone\audio\ref_48k_s16.wav")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


def _model_cls():
    from hydra.utils import get_class
    from omegaconf import OmegaConf
    from importlib.resources import files
    import f5_tts

    model = "F5TTS_v1_Base"
    model_cfg = OmegaConf.load(str(files("f5_tts").joinpath(f"configs/{model}.yaml")))
    cls = get_class(f"f5_tts.model.{model_cfg.model.backbone}")
    return cls, model_cfg.model.arch, model_cfg


def _ref_audio() -> str:
    """参考音频：优先按「音色身份证」取，保证是小狗自己那条声线。"""
    try:
        import os as _os
        import sys as _sys
        _here = _os.path.dirname(_os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from voice_identity import resolve
        return resolve()["ref_audio"]
    except Exception:
        pass
    for p in (REF_AUDIO, REF_FALLBACK):
        if p.exists():
            return str(p)
    # 都不在：宁可报错，也别拿一个八竿子打不着的路径去合成——那会变成别人的声音
    raise FileNotFoundError(
        "找不到参考音频，也没有可用的音色身份证"
        f"（查过：{REF_AUDIO}、{REF_FALLBACK}）")


def _ref_text() -> str:
    """参考文本同样以身份证为准，避免身份证换过而这里还在用旧文案。"""
    try:
        import os as _os
        import sys as _sys
        _here = _os.path.dirname(_os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from voice_identity import resolve
        t = (resolve().get("ref_text") or "").strip()
        if t:
            return t
    except Exception:
        pass
    if REF_TEXT_FILE.exists():
        t = REF_TEXT_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    return "主要是实属大瓜，以后可以解释了。"


# 惰性加载：模型很重，只在真正要合成时才 load（服务常驻时只 load 一次）
_engine = None


def _load_engine():
    global _engine
    if _engine is not None:
        return _engine

    from f5_tts.infer.utils_infer import load_model, load_vocoder

    cls, arch, model_cfg = _model_cls()
    t0 = time.time()
    print(f"[f5] 加载模型 {CKPT.name} @ {DEVICE} ...", flush=True)
    model = load_model(
        cls, arch, str(CKPT),
        mel_spec_type="vocos",
        vocab_file="",  # 用内置 vocab
        device=DEVICE,
    )
    print(f"[f5] 加载声码器 vocos @ {VOCOS_DIR} ...", flush=True)
    vocoder = load_vocoder(
        vocoder_name="vocos", is_local=True,
        local_path=str(VOCOS_DIR), device=DEVICE,
    )
    print(f"[f5] 引擎就绪，耗时 {time.time() - t0:.1f}s", flush=True)
    _engine = (model, vocoder)
    return _engine


def _prep_ref(ref_audio: str, ref_text: str, preprocess):
    """参考音频的预处理结果缓存（按 路径+文本+修改时间）。

    音色一旦固化，参考音频不会变；每句都重新预处理是纯浪费。
    """
    try:
        key = (str(Path(ref_audio).resolve()), ref_text, int(os.path.getmtime(ref_audio)))
    except OSError:
        key = (str(ref_audio), ref_text)
    hit = _prep_cache.get(key)
    if hit is None:
        hit = preprocess(ref_audio, ref_text, show_info=lambda *a, **k: None)
        _prep_cache.clear()
        _prep_cache[key] = hit
        print(f"[f5] 参考音频已固化缓存：{Path(ref_audio).name}", flush=True)
    return hit


_prep_cache: dict = {}


def synth(text: str, speed: float = 1.45, nfe: int = 24,
          cfg_strength: float = 2.0, out: str | None = None,
          seed: int = 20261024, ref_audio: str | None = None,
          ref_text: str | None = None) -> str:
    """合成一句，返回输出 wav 路径。

    ``ref_audio`` / ``ref_text`` 指定"用哪个音色"（音色档案里的参考音频）。
    不传就用默认的 .voice_cache/ref_clean.wav。
    """
    from f5_tts.infer.utils_infer import (
        infer_process,
        preprocess_ref_audio_text,
    )
    import soundfile as sf

    text = (text or "").strip()
    if not text:
        raise ValueError("空文本")

    model, vocoder = _load_engine()
    ref_audio = ref_audio or _ref_audio()
    ref_text = ref_text or _ref_text()

    # 同一份参考音频 + 同一段参考文本，预处理结果缓存起来：
    # 常驻服务里每句都重新预处理（重采样/特征化）纯属浪费，音色也不会变。
    ref_audio_clean, ref_text_clean = _prep_ref(ref_audio, ref_text, preprocess_ref_audio_text)

    if seed is not None:
        torch.manual_seed(seed)

    t0 = time.time()
    # 半精度加速：autocast 只把矩阵乘落到 fp16，权重仍是 fp32，音质几乎不变
    # （实测 18 字 1.2s → 0.7s 左右）。坏境不支持就自动走 fp32。
    import contextlib

    fp16 = (os.environ.get("F5_FP16") or "0").strip() == "1" and DEVICE == "cuda"
    ctx = torch.autocast("cuda", dtype=torch.float16) if fp16 else contextlib.nullcontext()
    try:
        with ctx:
            audio, sr, _ = infer_process(
                ref_audio_clean, ref_text_clean, text,
                model, vocoder,
                mel_spec_type="vocos",
                target_rms=0.1,
                cross_fade_duration=0.15,
                nfe_step=nfe,
                cfg_strength=cfg_strength,
                sway_sampling_coef=-1.0,
                speed=speed,
                fix_duration=None,
                device=DEVICE,
            )
    except RuntimeError as exc:
        if not fp16:
            raise
        print(f"[f5] 半精度不可用（{exc}），回退 fp32", flush=True)
        audio, sr, _ = infer_process(
            ref_audio_clean, ref_text_clean, text,
            model, vocoder,
            mel_spec_type="vocos",
            target_rms=0.1,
            cross_fade_duration=0.15,
            nfe_step=nfe,
            cfg_strength=cfg_strength,
            sway_sampling_coef=-1.0,
            speed=speed,
            fix_duration=None,
            device=DEVICE,
        )
    # 裁掉句首/句尾静音
    audio = _trim(audio)

    out_path = out or str(APP_DIR / "试听" / "试.wav")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(out_path, audio, sr)
    print(f"[f5] 合成「{text[:20]}」→ {out_path}（{time.time() - t0:.1f}s，{len(audio)/sr:.2f}s 音频）", flush=True)
    return out_path


def _trim(audio, top_db=40):
    """裁掉首尾静音（按能量阈值）。"""
    import numpy as np

    if audio is None or len(audio) == 0:
        return audio
    a = np.asarray(audio)
    if a.ndim > 1:
        a = a.mean(axis=1)
    ref = np.max(np.abs(a)) + 1e-9
    db = 20 * np.log10(np.maximum(np.abs(a), 1e-9) / ref)
    mask = db > -top_db
    idx = np.where(mask)[0]
    if len(idx) == 0:
        return audio
    return audio[idx[0]:idx[-1] + 1]


def main(argv=None):
    ap = argparse.ArgumentParser(description="F5-TTS 克隆合成一句")
    ap.add_argument("text", help="要合成的文本")
    ap.add_argument("-o", "--out", default=None, help="输出 wav 路径")
    ap.add_argument("--speed", type=float, default=1.45)
    ap.add_argument("--nfe", type=int, default=24)
    ap.add_argument("--cfg", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=20261024)
    args = ap.parse_args(argv)
    print(synth(args.text, args.speed, args.nfe, args.cfg, args.out, args.seed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
