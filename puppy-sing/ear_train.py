# -*- coding: utf-8 -*-
"""
礼貌小狗·扒歌学习器（ear_train）
================================
让小狗「听原唱自己学会唱歌」：
  音频 → ① 分离伴奏/人声 → ② 从人声提取真实旋律(pyin 音高轨迹→音符)
       → ③ 可选：本地 ASR 认歌词 → ④ 写入曲库，用小狗自己的声线哼唱/演唱

三级分离后端（自动择优，保证一定能跑）：
  1. demucs（若已安装，质量最好，two-stems=vocals）
  2. mid/side 零依赖分离（纯 numpy）：人声≈中置声道 (L+R)/2，伴奏≈侧声道 (L-R)
     —— 对绝大多数流行混音（主唱居中）效果可用，是离线兜底
旋律提取：librosa.pyin → f0 曲线 → 相邻同音高帧合并成音符 → 量化到 core.BEAT 拍
歌词：默认用「啦」哼唱（不复制歌词文本）；可用 --lyric 附加授权歌词逐字对齐

用法：
  python ear_train.py <音频文件或URL> --title 歌名 --artist 歌手
  python ear_train.py <音频> --lyric "授权歌词..."     # 有歌词则逐字对齐旋律
  python ear_train.py --selftest                      # 无头自测
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import core  # noqa: E402  (puppy-sing/core.py)

LEARN_DIR = os.path.join(core.CACHE_DIR, "learn")
MAX_ANALYZE_SEC = 90      # 旋律分析最多取前 90 秒（副歌通常在前面，且控制耗时）
MIN_NOTE_SEC = 0.09       # 短于这个时长的音丢弃（噪声）
REST_GAP_SEC = 0.07       # 音与音之间的停顿判定


# ----------------------------------------------------------------------------
# 音频读取（wav/mp3/flac 通吃：soundfile → librosa → ffmpeg 转码兜底）
# ----------------------------------------------------------------------------
def load_audio(path: str, sr: int = 22050):
    """返回 (mono: np.ndarray, sr)。多声道返回原始多声道矩阵 (ch, n)。"""
    import numpy as np

    def _read(f):
        import soundfile as sf
        data, rate = sf.read(f, always_2d=True, dtype="float32")
        return data.T, rate  # (ch, n)

    try:
        return _read(path)
    except Exception:
        pass
    try:
        import librosa
        y, rate = librosa.load(path, sr=sr, mono=False)
        if y.ndim == 1:
            y = y[None, :]
        return y, rate
    except Exception:
        pass
    # ffmpeg 转码兜底
    if shutil.which("ffmpeg"):
        tmp = os.path.join(LEARN_DIR, "_tmp_conv.wav")
        os.makedirs(LEARN_DIR, exist_ok=True)
        if subprocess.run(["ffmpeg", "-y", "-i", path, "-ar", str(sr), "-ac", "2",
                           tmp], capture_output=True).returncode == 0:
            try:
                return _read(tmp)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    raise RuntimeError("读不了音频（需要 soundfile / librosa / ffmpeg 任一）：%s" % path)


def fetch_audio(src: str, dest_dir: str) -> str:
    """src 是本地文件就直接用；是 http(s) 就下载。返回本地路径。"""
    if os.path.isfile(src):
        return src
    if src.lower().startswith(("http://", "https://")):
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "source" + os.path.splitext(src.split("?")[0])[1] or ".mp3")
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 puppy-sing"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
        return dest
    raise FileNotFoundError(src)


# ----------------------------------------------------------------------------
# ① 伴奏/人声分离
# ----------------------------------------------------------------------------
def separate_vocals(path: str, out_dir: str) -> tuple:
    """
    返回 (vocals_path, accomp_path, backend)。backend: demucs | midside
    demucs 可用时用 demucs（--two-stems=vocals），否则用零依赖 mid/side。
    """
    import numpy as np
    import soundfile as sf

    os.makedirs(out_dir, exist_ok=True)
    voc_path = os.path.join(out_dir, "vocals.wav")
    acc_path = os.path.join(out_dir, "accomp.wav")

    # 后端 1：demucs（若装了）
    if shutil.which("demucs"):
        try:
            r = subprocess.run(["demucs", "--two-stems", "vocals", "-o", out_dir,
                                "--filename", "{stem}/separated", path],
                               capture_output=True, timeout=1800)
            # demucs 输出目录结构：out_dir/htdemucs/{vocals,accompaniment}.wav 之类，
            # 我们不猜具体结构，直接在 out_dir 里搜
            hits = []
            for root, _dirs, files in os.walk(out_dir):
                for fn in files:
                    low = fn.lower()
                    if low.startswith("vocals") and low.endswith((".wav", ".mp3", ".flac")):
                        hits.append((os.path.join(root, fn), "v"))
                    elif low.startswith(("accompaniment", "no_vocals")):
                        hits.append((os.path.join(root, fn), "a"))
            vp = next((p for p, k in hits if k == "v"), None)
            ap = next((p for p, k in hits if k == "a"), None)
            if vp and ap and r.returncode == 0:
                return vp, ap, "demucs"
        except Exception:
            pass  # 落到 mid/side

    # 后端 2：mid/side 零依赖分离
    data, rate = load_audio(path)
    if data.shape[0] == 1:                      # 单声道：无法分离，人声=伴奏=原声
        mid = data[0]
        side = np.zeros_like(mid)
    else:
        mid = (data[0] + data[1]) * 0.5         # 中置≈主唱
        side = (data[0] - data[1]) * 0.5        # 侧向≈伴奏/空间
    peak = max(1e-9, float(np.abs(mid).max()), float(np.abs(side).max()))
    sf.write(voc_path, (mid / peak * 0.95).astype("float32"), rate)
    sf.write(acc_path, (side / peak * 0.95).astype("float32"), rate)
    return voc_path, acc_path, "midside"


# ----------------------------------------------------------------------------
# ② 旋律提取：pyin 音高轨迹 → 音符
# ----------------------------------------------------------------------------
def extract_melody(vocals_path: str, max_sec: float = MAX_ANALYZE_SEC) -> list:
    """
    从人声提取旋律，返回 [[text, midi, beats], ...]（与 core.Song.notes 同格式）。
    text 默认「啦」（哼唱）；后续若有歌词可替换对齐。
    """
    import numpy as np
    import librosa

    y, sr = librosa.load(vocals_path, sr=16000, mono=True, duration=max_sec)
    if len(y) < sr // 4:
        return []

    f0, voiced, _ = librosa.pyin(y, fmin=librosa.note_to_hz("C3"),
                                 fmax=librosa.note_to_hz("C6"), sr=sr,
                                 frame_length=1024, hop_length=256)
    hop_s = 256 / sr
    midi = librosa.hz_to_midi(f0)                       # NaN 处为未发声
    voiced = voiced & np.isfinite(midi)
    midi = np.round(midi).astype(float)
    midi[~voiced] = np.nan

    # 相邻帧同音高合并成段
    notes = []
    cur_m, cur_start, last_voiced = None, 0.0, False
    for i in range(len(midi)):
        m = midi[i]
        if np.isfinite(m):
            if cur_m is None:
                cur_m, cur_start = m, i * hop_s
            elif abs(m - cur_m) > 0.51:               # 换音了：收上一段
                dur = i * hop_s - cur_start
                if dur >= MIN_NOTE_SEC:
                    notes.append((int(round(cur_m)), cur_start, dur))
                cur_m, cur_start = m, i * hop_s
            last_voiced = True
        else:
            if cur_m is not None:                     # 断句/换气：收上一段
                dur = i * hop_s - cur_start
                if dur >= MIN_NOTE_SEC:
                    notes.append((int(round(cur_m)), cur_start, dur))
                cur_m = None
            last_voiced = False
    if cur_m is not None:
        dur = len(midi) * hop_s - cur_start
        if dur >= MIN_NOTE_SEC:
            notes.append((int(round(cur_m)), cur_start, dur))

    # 合并被 REST_GAP 割开的同音段，转成拍
    merged = []
    for m, start, dur in notes:
        if merged and merged[-1][0] == m and start - (merged[-1][1] + merged[-1][2]) < REST_GAP_SEC:
            merged[-1][2] = start + dur - merged[-1][1]
        else:
            merged.append([m, start, dur])
    out = []
    for m, start, dur in merged:
        beats = max(0.25, round(dur / core.BEAT * 4) / 4)   # 1/16 拍精度
        out.append(["啦", int(m), float(beats)])
    return out[:200]                                        # 上限保护


# ----------------------------------------------------------------------------
# ③ 歌词（可选）：faster-whisper 本地 ASR
# ----------------------------------------------------------------------------
def transcribe_lyrics(vocals_path: str, max_sec: float = MAX_ANALYZE_SEC) -> str:
    """本地 faster-whisper 认歌词；模型首次运行会自动下载（小模型约 75MB）。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return ""
    try:
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segs, _ = model.transcribe(vocals_path, language="zh",
                                   vad_filter=True, duration=max_sec)
        return "".join(s.text.strip() for s in segs)
    except Exception:
        return ""


def align_lyric_to_notes(notes: list, lyric: str) -> list:
    """把歌词逐字按比例铺到旋律音符上（把「啦」替换成字）。"""
    import re
    units = [c for c in re.sub(r"[\s，。！？、；：…—]", "", lyric or "") if c.strip()]
    if not units or not notes:
        return notes
    n = len(notes)
    out = []
    for i, (t, m, b) in enumerate(notes):
        ch = units[min(len(units) - 1, i * len(units) // n)]
        out.append([ch, m, b])
    return out


# ----------------------------------------------------------------------------
# ④ 编排：学一首歌
# ----------------------------------------------------------------------------
def learn_by_ear(title: str, artist: str = "", src: str = "", lyric: str = "",
                 transcribe: bool = False, progress=None) -> dict:
    """
    完整扒歌学习流程。progress(step, msg) 可选回调（GUI 进度用）。
    返回 dict(ok, song_id, notes_count, backend, lyric_chars, message)。
    """
    def report(step, msg):
        if progress:
            try:
                progress(step, msg)
            except Exception:
                pass
        print("[扒歌][%s] %s" % (step, msg))

    sid = core.make_song_id(title, artist)
    work = os.path.join(LEARN_DIR, sid)
    os.makedirs(work, exist_ok=True)

    # 下载/定位音频
    report("音频", "获取音频源…")
    try:
        audio = fetch_audio(src, work) if src else ""
    except Exception as exc:
        return {"ok": False, "message": "音频获取失败：%s" % exc}
    if not audio:
        return {"ok": False, "message": "没给音频来源（热歌榜的 url 不可直接下载时，请手动给本地文件）"}

    # 分离
    report("分离", "分离伴奏/人声…")
    try:
        voc, acc, backend = separate_vocals(audio, work)
    except Exception as exc:
        return {"ok": False, "message": "分离失败：%s" % exc}
    report("分离", "完成（后端=%s）人声=%s 伴奏=%s" % (backend, os.path.basename(voc), os.path.basename(acc)))

    # 旋律
    report("旋律", "提取人声音高轨迹…")
    try:
        notes = extract_melody(voc)
    except Exception as exc:
        return {"ok": False, "message": "旋律提取失败：%s" % exc}
    if not notes:
        return {"ok": False, "message": "没提取到旋律（人声太弱或全是噪声？换个音源试试）"}
    report("旋律", "提取到 %d 个音符，MIDI 音域 %d~%d" % (
        len(notes), min(n[1] for n in notes), max(n[1] for n in notes)))

    # 歌词
    text = lyric.strip()
    if not text and transcribe:
        report("歌词", "本地 ASR 识别中（首次会下载小模型）…")
        text = transcribe_lyrics(voc)
        report("歌词", "识别 %d 字" % len(text))
    if text:
        notes = align_lyric_to_notes(notes, text)

    # 写入曲库
    lib = core.load_library()
    song = lib.get(sid) or core.Song(id=sid, title=title, artist=artist)
    song.source = "learned"
    song.lyric = text
    song.notes = notes
    song.status = "learned"
    song.learned_at = time.strftime("%Y-%m-%d %H:%M")
    if not song.url and src:
        song.url = src
    lib[sid] = song
    core.save_library(lib)

    # 预渲染缓存（点击即唱）
    try:
        core.ensure_cache(song)
    except Exception as exc:
        report("预渲染", "预渲染失败（点唱时会再试）：%s" % exc)

    report("完成", "%s - %s：%d 个音符，可唱" % (artist, title, len(notes)))
    return {"ok": True, "song_id": sid, "notes_count": len(notes),
            "backend": backend, "lyric_chars": len(text),
            "vocals": voc, "accomp": acc}


# ----------------------------------------------------------------------------
# 自测：合成一段已知旋律+伴奏，验证 mid/side 分离与旋律提取闭环
# ----------------------------------------------------------------------------
def _selftest() -> int:
    import numpy as np
    import soundfile as sf

    sr = 22050
    seq = [(60, 0.5), (64, 0.5), (67, 0.5), (64, 0.5), (72, 0.8), (67, 0.6), (60, 0.9)]
    voc = np.concatenate([0.5 * np.sin(2 * np.pi * 440 * 2 ** ((m - 69) / 12)
                                              * np.arange(int(sr * d)) / sr)
                          for m, d in seq])
    side = 0.2 * np.sin(2 * np.pi * 110 * np.arange(len(voc)) / sr)   # 伴奏低音
    stereo = np.stack([voc + side, voc - side])                        # L/R：vocal 居中
    work = os.path.join(LEARN_DIR, "_selftest")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "fake_song.wav")
    sf.write(src, stereo.T, sr)
    print("测试音频: %s (%.1fs, %d 音符真值)" % (src, len(voc) / sr, len(seq)))

    voc_p, acc_p, backend = separate_vocals(src, work)
    print("分离后端:", backend)

    # 验证分离：mid 应≈voc，side 应≈伴奏
    v, _ = load_audio(voc_p)
    a, _ = load_audio(acc_p)
    vm = v.mean(axis=0) if v.ndim > 1 else v
    am = a.mean(axis=0) if a.ndim > 1 else a
    corr_va = float(np.corrcoef(vm, am)[0, 1])
    print("人声/伴奏相关系数(应低):", round(corr_va, 3))

    notes = extract_melody(voc_p)
    got = [m for _t, m, _b in notes]
    want = [m for m, _d in seq]
    print("真值 midi:", want)
    print("提取 midi:", got)
    ok_notes = len(got) >= 4 and all(any(abs(g - w) <= 1 for g in got) for w in want)
    print("音符校验:", "PASS" if ok_notes else "FAIL")

    # 走曲库闭环
    res = learn_by_ear("测试扒歌", "selftest", src=src, progress=None)
    print("learn_by_ear:", {k: res[k] for k in ("ok", "notes_count", "backend", "lyric_chars")})
    if res.get("ok"):
        lib = core.load_library()
        s = lib[res["song_id"]]
        wav_kind, payload = core.ensure_cache(s)
        ok_cache = os.path.exists(payload[0] if wav_kind == "mp3s" else payload)
        print("预渲染缓存:", "PASS" if ok_cache else "FAIL")
    print("自测结论:", "PASS" if ok_notes else "FAIL")
    return 0 if ok_notes else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="小狗扒歌学习器")
    ap.add_argument("src", nargs="?", help="音频文件路径或 URL")
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--lyric", default="", help="授权歌词（可选，逐字对齐旋律）")
    ap.add_argument("--transcribe", action="store_true", help="本地 ASR 认歌词")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.src or not args.title:
        print("需要 --title 和音频来源（文件路径或 URL）；或用 --selftest 自测")
        return 2
    res = learn_by_ear(args.title, args.artist, args.src, args.lyric, args.transcribe)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
