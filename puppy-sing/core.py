# -*- coding: utf-8 -*-
"""
礼貌小狗·唱歌 — 核心模块
================================
负责：歌曲数据模型 / 曲库读写 / 联网热歌榜拉取与自更新 /
      本地实时旋律合成引擎(零外部依赖、低延迟) / 可选 edge-tts 真实语音演唱 / 播放。

设计要点
--------
* 低延迟：默认使用「本地旋律合成引擎」，在内存里用 numpy 实时生成 WAV，
  点击即唱（无需联网、无模型加载），真正零等待。
* 曲库自更新：后台线程定时调用热歌榜接口（api-hot.imsyy.top 抖音热歌榜 /
  网易云热歌榜），把新歌写入「想唱队列」，失败则回退到缓存或内置榜。
* 自动学习 + 合成：热歌入队后自动尝试「学习」（取歌词/旋律）。对受版权保护的
  歌曲，在没有授权歌词/旋律数据时，不擅自复刻原唱录音，而是用「生成旋律 + 报歌名」
  的方式演唱，并在曲库中标为「待学习」。已内置若干原创 demo 歌曲用于真实演唱。
"""
from __future__ import annotations

import json
import os
import time
import threading
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from typing import Optional
import shutil
import sys

# ----------------------------------------------------------------------------
# 基础配置
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
CACHE_DIR = os.path.join(HERE, "cache")
SONGS_JSON = os.path.join(DATA_DIR, "songs.json")
TRENDING_CACHE = os.path.join(DATA_DIR, "trending_cache.json")

# 打包成 exe 后，puppy-sing 被解包到 _MEIPASS（只读），但曲库/缓存要可写。
# 这里把会写的目录整体挪到 %APPDATA%/PolitePuppyPet/puppy-sing，并在首次
# 运行时把内置曲库（打包进 exe 的副本）搬过去，保留用户已学过的歌。
if getattr(sys, "frozen", False):
    _WB = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"),
        "PolitePuppyPet", "puppy-sing",
    )
    os.makedirs(_WB, exist_ok=True)
    _bundled_songs = os.path.join(HERE, "data", "songs.json")
    _user_songs = os.path.join(_WB, "data", "songs.json")
    if os.path.isfile(_bundled_songs) and not os.path.exists(_user_songs):
        os.makedirs(os.path.dirname(_user_songs), exist_ok=True)
        shutil.copyfile(_bundled_songs, _user_songs)
    HERE = _WB
    DATA_DIR = os.path.join(HERE, "data")
    CACHE_DIR = os.path.join(HERE, "cache")
    SONGS_JSON = os.path.join(DATA_DIR, "songs.json")
    TRENDING_CACHE = os.path.join(DATA_DIR, "trending_cache.json")

# 热歌榜接口（免密钥公共聚合，由 DailyHotApi 提供）
TRENDING_SOURCES = [
    {"name": "抖音热歌榜", "url": "https://api-hot.imsyy.top/douyin_music/"},
    {"name": "网易云热歌榜", "url": "https://api-hot.imsyy.top/netease_music_toplist/?type=4"},
]

# 离线兜底榜（接口不可达时使用，保证程序永远有「热歌」可唱）
FALLBACK_TRENDING = [
    ("李荣浩 - 恋人", "短视频热歌榜"),
    ("白小白 - 人生路漫漫", "短视频热歌榜"),
    ("伯爵Johnny/唐伯虎Annie - 西厢寻他", "短视频热歌榜"),
    ("TINY7 - 零距离的思念", "短视频热歌榜"),
    ("陈默之 - 冷夜", "短视频热歌榜"),
    ("周杰伦 - 晴天", "抖音年度热歌"),
    ("周深 - 奔腾", "抖音年度热歌"),
    ("凤凰传奇 - 奢香夫人", "抖音年度热歌"),
    ("海来阿木 - 嘉禾望岗", "抖音年度热歌"),
    ("G.E.M.邓紫棋 - 光年之外", "抖音热歌"),
]

SR = 22050  # 采样率（本地引擎）
BEAT = 0.34  # 每拍秒数

# F5-TTS 小狗原声引擎：复用桌宠已有的 f5_server HTTP 服务（端口 9880）
PUPPY_ROOT = os.path.dirname(HERE)                       # 礼貌小狗桌宠/
F5_SERVER_URL = os.environ.get("F5_SERVER_URL", "http://127.0.0.1:9880")
VOICE_CLONE_ENV = os.environ.get(
    "VOICE_CLONE_ENV",
    r"C:\Users\zheng'zheng\WorkBuddy\voice-clone-env\Scripts\python.exe")
F5_SERVER_SCRIPT = os.path.join(PUPPY_ROOT, "tools", "f5_server.py")
F5_REF_AUDIO = os.path.join(PUPPY_ROOT, "voices", "profiles", "小狗", "ref.wav")


# ----------------------------------------------------------------------------
# 歌曲数据模型
# ----------------------------------------------------------------------------
@dataclass
class Song:
    id: str
    title: str
    artist: str = ""
    source: str = "demo"      # demo | trending | learned
    licensed: bool = False    # 是否拥有可完整合成的授权歌词/旋律
    lyric: str = ""
    notes: list = field(default_factory=list)  # [[text, midi, beats], ...]
    url: str = ""
    hot: int = 0
    learned_at: str = ""
    status: str = "ready"     # ready | pending | learned |待学习

    def notes_list(self):
        """统一返回 [(text, midi, beats), ...]，兼容 dict/list 两种存储。"""
        out = []
        for n in self.notes:
            if isinstance(n, dict):
                out.append((n.get("t", ""), n.get("m", 64), n.get("b", 1.0)))
            else:
                text, midi, beats = (list(n) + ["", 64, 1.0])[:3]
                out.append((text, midi, beats))
        return out

    def to_dict(self):
        return asdict(self)


def make_song_id(title: str, artist: str = "") -> str:
    base = f"{title}::{artist}".strip(":")
    return "s_" + str(abs(hash(base)) % (10 ** 12))


# ----------------------------------------------------------------------------
# 旋律工具
# ----------------------------------------------------------------------------
def midi_to_freq(midi: int) -> float:
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


def default_melody_from_title(title: str, beats: int = 16) -> list:
    """
    为没有授权歌词/旋律的热歌生成一段「专属旋律」。
    用歌名做确定性哈希映射到 C 大调音阶，保证同一首歌每次旋律一致、不同歌各异。
    用「啦」作为哼唱音节（不复制原唱）。
    """
    scale = [60, 62, 64, 65, 67, 69, 71, 72, 74]  # C D E F G A B C5 D5
    h = abs(hash(title))
    seq = []
    for i in range(beats):
        deg = (h >> (i * 2)) % len(scale)
        seq.append(["啦", scale[deg], 1.0])
    # 给开头和结尾一点长短变化，更像旋律
    if seq:
        seq[0][2] = 1.5
        seq[-1][2] = 2.0
    return seq


def _syllables(lyric: str) -> list:
    """把歌词拆成单元：汉字逐字；英文/数字连成词；标点单独成单元（乐句分隔）。"""
    out, buf = [], ""
    for ch in lyric:
        if "一" <= ch <= "鿿" or ch in "，。！？、；：…—":
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        elif ch.isalnum():
            buf += ch
        else:
            if buf:
                out.append(buf)
                buf = ""
    if buf:
        out.append(buf)
    return out


def auto_melody_from_lyric(lyric: str, style: str = "smooth") -> list:
    """
    把「授权歌词」自动编排成旋律：每个音节映射到一个音高。
    style: smooth(抒情波浪) / happy(欢快上行) / sad(下行) / random(种子随机)。
    返回 [[text, midi, beats], ...]，可直接作为 Song.notes 让小狗唱出词来。
    """
    scale = [60, 62, 64, 65, 67, 69, 71, 72, 74, 76]  # C D E F G A B C5 D5 E5
    phrase_break = set("，。！？、；：…—")
    units = [u for u in _syllables(lyric) if u not in phrase_break]
    if not units:
        units = [c for c in lyric if c.strip()]
    patterns = {
        "smooth": [0, 2, 4, 2, 4, 5, 4, 2, 0, 2, 1, 0, 2, 4, 2, 0],
        "happy":  [0, 2, 4, 5, 4, 5, 7, 5, 4, 2, 4, 5, 7, 9, 7, 5],
        "sad":    [4, 3, 2, 1, 2, 0, 1, -1, 0, -1, -2, 0, 1, 0],
        "random": None,
    }
    pat = patterns.get(style, patterns["smooth"])
    seq, idx = [], 0
    for i, u in enumerate(units):
        if pat is None:
            deg = abs(hash(u + str(i))) % len(scale)
        else:
            deg = pat[i % len(pat)] % len(scale)
        beats = 1.0
        pos = lyric.find(u, idx)
        if pos != -1:
            idx = pos + len(u)
            after = lyric[idx:idx + 1]
            if after in phrase_break or after == "":
                beats = 2.0
        seq.append([u, scale[deg], beats])
    if seq:
        seq[0][2] = max(seq[0][2], 1.5)
    return seq


def learn_song(song: Song, lyric: str, style: str = "smooth") -> None:
    """用授权歌词「学习」一首歌：自动编旋律并标为已学习/可唱。"""
    song.lyric = lyric.strip()
    song.notes = auto_melody_from_lyric(song.lyric, style)
    song.licensed = True
    song.status = "learned"
    song.learned_at = time.strftime("%Y-%m-%d %H:%M")


# ----------------------------------------------------------------------------
# 曲库读写
# ----------------------------------------------------------------------------
def load_library(path: str = SONGS_JSON) -> dict:
    """返回 {song_id: Song}。"""
    lib = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for d in json.load(f):
                    s = Song(**d)
                    lib[s.id] = s
        except Exception as e:
            print(f"[曲库] 读取失败，使用空库: {e}")
    return lib


def save_library(lib: dict, path: str = SONGS_JSON) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([s.to_dict() for s in lib.values()], f, ensure_ascii=False, indent=2)


def merge_trending(lib: dict, trending: list) -> list:
    """把热歌并入曲库，返回新增的 Song 列表（待学习）。"""
    added = []
    for s in trending:
        if s.id not in lib:
            s.status = "待学习"  # 无授权歌词/旋律，默认待学习
            lib[s.id] = s
            added.append(s)
        else:
            # 更新热度/排名信息
            lib[s.id].hot = s.hot
            lib[s.id].url = s.url or lib[s.id].url
    return added


# ----------------------------------------------------------------------------
# 联网热歌榜拉取
# ----------------------------------------------------------------------------
def _fetch_json(url: str, timeout: int = 10) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "PuppySing/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_items(items: list) -> list:
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        raw = str(it.get("title", "")).strip()
        if not raw:
            continue
        artist = str(it.get("author", "") or "").strip()
        name = raw
        if (" - " in raw) and not artist:
            name, artist = raw.split(" - ", 1)
        sid = str(it.get("id") or make_song_id(name, artist))
        out.append(Song(
            id=sid, title=name.strip(), artist=artist.strip(),
            source="trending", url=str(it.get("url", "") or ""),
            hot=int(it.get("hot", 0) or 0), status="待学习",
        ))
    return out


def fetch_trending(timeout: int = 10) -> tuple:
    """
    拉取热歌榜。返回 (songs:list[Song], meta:dict)。
    全部接口失败时回退到缓存，再回退到内置榜。
    """
    meta = {"ok": False, "used": "", "count": 0, "error": ""}
    for src in TRENDING_SOURCES:
        try:
            data = _fetch_json(src["url"], timeout)
            if not data or "data" not in data:
                continue
            songs = _parse_items(data["data"])
            if songs:
                _cache_trending(songs)
                meta.update(ok=True, used=src["name"], count=len(songs))
                return songs, meta
        except Exception as e:
            meta["error"] = f"{src['name']}: {e}"
            continue
    # 回退：缓存
    cached = _load_cached_trending()
    if cached:
        meta.update(ok=False, used="本地缓存", count=len(cached))
        return cached, meta
    # 回退：内置榜
    fb = [Song(id=make_song_id(t, a), title=t.split(" - ")[0].strip(),
               artist=(t.split(" - ")[1].strip() if " - " in t else ""),
               source="trending", status="待学习") for t, a in FALLBACK_TRENDING]
    meta.update(ok=False, used="内置兜底榜", count=len(fb))
    return fb, meta


def _cache_trending(songs: list) -> None:
    try:
        with open(TRENDING_CACHE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "songs": [s.to_dict() for s in songs]},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_cached_trending() -> list:
    try:
        if os.path.exists(TRENDING_CACHE):
            with open(TRENDING_CACHE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return [Song(**x) for x in d.get("songs", [])]
    except Exception:
        pass
    return []


# ----------------------------------------------------------------------------
# 本地实时旋律合成引擎（零外部依赖 / 低延迟）
# ----------------------------------------------------------------------------
def _adsr(n: int, sr: int, dur: float):
    """简单 ADSR 包络。"""
    import numpy as np
    attack = int(0.012 * sr)
    release = int(0.05 * sr)
    env = np.ones(n)
    for i in range(min(attack, n)):
        env[i] = i / max(attack, 1)
    for i in range(max(0, n - release), n):
        env[i] = max(0.0, (n - i) / max(release, 1))
    return env


def _voice_tone(freq: float, dur: float, sr: int = SR) -> "np.ndarray":
    """生成一段「类人声」音：基频 + 谐波 + 轻微颤音 + ADSR。"""
    import numpy as np
    n = max(1, int(sr * dur))
    t = np.linspace(0, dur, n, endpoint=False)
    vib = 1.0 + 0.012 * np.sin(2 * np.pi * 5.5 * t)  # 颤音
    f = freq * vib
    sig = (np.sin(2 * np.pi * f * t)
           + 0.45 * np.sin(2 * np.pi * 2 * f * t)
           + 0.22 * np.sin(2 * np.pi * 3 * f * t)
           + 0.10 * np.sin(2 * np.pi * 4 * f * t))
    sig *= _adsr(n, sr, dur)
    peak = np.max(np.abs(sig)) + 1e-9
    return (sig / peak * 0.62).astype(np.float32)


def _notes_for_song(song: Song) -> list:
    notes = song.notes_list()
    if notes:
        return notes
    # 热歌无授权旋律 -> 生成专属旋律（哼唱）
    return default_melody_from_title(song.title)


def synthesize_local(song: Song, out_path: str, beat: float = BEAT, sr: int = SR) -> str:
    """本地合成：把旋律实时渲染成 WAV。返回文件路径。"""
    import numpy as np
    notes = _notes_for_song(song)
    chunks = []
    gap = np.zeros(int(sr * 0.02), dtype=np.float32)  # 极小气口，保留歌唱连贯
    for text, midi, beats in notes:
        freq = midi_to_freq(int(midi))
        dur = max(0.12, float(beats) * beat)
        chunks.append(_voice_tone(freq, dur, sr))
        chunks.append(gap)
    if not chunks:
        chunks = [_voice_tone(midi_to_freq(64), 1.0, sr)]
    audio = np.concatenate(chunks)
    # 归一化 + 转 int16
    audio = (audio / (np.max(np.abs(audio)) + 1e-9) * 0.9 * 32767).astype(np.int16)
    _write_wav(out_path, audio, sr)
    return out_path


def _write_wav(path: str, samples: np.ndarray, sr: int) -> None:
    import wave
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())


# ----------------------------------------------------------------------------
# 可选：edge-tts 真实语音演唱（按音节映射音高）
# ----------------------------------------------------------------------------
def synthesize_edge(song: Song, out_dir: str, beat: float = BEAT) -> list:
    """
    用 edge-tts 把每个音节按对应音高合成 mp3，返回 mp3 路径列表（顺序即旋律）。
    需要: pip install edge-tts playsound
    注意：本函数仅做音频合成，不复制任何受版权保护的录音；歌词/旋律须为授权内容。
    """
    try:
        import edge_tts
    except Exception as e:
        raise RuntimeError(f"未安装 edge-tts: {e}")
    import asyncio
    notes = _notes_for_song(song)
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    voice = "zh-CN-XiaoxiaoNeural"  # 中文女声，适合演唱

    async def _gen(text, midi, idx):
        # 以 E4(64) 为 0Hz 基准，每半音约 ±8Hz，限制在 edge-tts 范围内
        pitch_hz = max(-100, min(100, (int(midi) - 64) * 8))
        # 短音节唱快一点
        rate = max(-30, min(80, int((1.0 - float(beat)) * 60)))
        p = os.path.join(out_dir, f"{idx:03d}.mp3")
        comm = edge_tts.Communicate(text or "啦", voice,
                                    pitch=f"{pitch_hz:+d}Hz", rate=f"{rate:+d}%")
        await comm.save(p)
        return p

    async def _run():
        res = []
        for i, (text, midi, beats) in enumerate(notes):
            # 长音适当放慢，保证可听
            res.append(await _gen(text, midi, i))
        return res

    paths = asyncio.run(_run())
    return paths


# ----------------------------------------------------------------------------
# F5-TTS 小狗原声引擎（复用桌宠 f5_server HTTP 服务）
# ----------------------------------------------------------------------------
def _f5_health() -> bool:
    try:
        with urllib.request.urlopen(F5_SERVER_URL + "/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _ensure_f5_server() -> None:
    """若 f5_server 没在跑，尝试用 voice-clone-env 拉起它（仅 Windows 可靠）。"""
    if _f5_health():
        return
    if not os.path.exists(F5_SERVER_SCRIPT) or not os.path.exists(VOICE_CLONE_ENV):
        raise RuntimeError("f5_server 未运行且无法自动启动（缺脚本或 voice-clone-env）")
    import subprocess
    cf = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
    subprocess.Popen([VOICE_CLONE_ENV, F5_SERVER_SCRIPT, "--port", "9880"],
                     creationflags=cf)
    for _ in range(40):  # 最多等 40s 预热
        time.sleep(1)
        if _f5_health():
            return
    raise RuntimeError("f5_server 启动超时（请检查 voice-clone-env / 显卡）")


def _f5_synth(text: str, out_wav: str) -> None:
    data = json.dumps({"text": text[:200]}).encode("utf-8")
    req = urllib.request.Request(F5_SERVER_URL + "/tts", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        audio = r.read()
    with open(out_wav, "wb") as f:
        f.write(audio)


def _split_phrases(text: str, maxlen: int = 24) -> list:
    """按标点切乐句，过长再按 maxlen 折行。"""
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in "，。！？、；：…—\n" and cur.strip():
            out.append(cur.strip())
            cur = ""
        elif len(cur) >= maxlen:
            out.append(cur.strip())
            cur = ""
    if cur.strip():
        out.append(cur.strip())
    return [p for p in out if p]


def _has_ffmpeg() -> bool:
    import subprocess
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, timeout=5)
        return True
    except Exception:
        return False


def _pitch_shift(in_wav: str, out_wav: str, semitones: float) -> None:
    """ffmpeg 变调不变速（rubberband 滤镜）；失败退化为 asetrate(变调变速)。"""
    import subprocess
    try:
        subprocess.run(["ffmpeg", "-y", "-i", in_wav, "-af",
                        f"rubberband=pitch={semitones:.3f}", out_wav],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60, check=True)
    except Exception:
        sr0 = 22050
        rate = sr0 * (2 ** (semitones / 12.0))
        subprocess.run(["ffmpeg", "-y", "-i", in_wav, "-af", f"asetrate={rate:.0f}",
                        "-ar", str(sr0), out_wav],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


def _concat_wavs(paths: list, out_wav: str) -> None:
    import wave
    with wave.open(out_wav, "w") as out:
        first = wave.open(paths[0], "rb")
        out.setnchannels(first.getnchannels())
        out.setsampwidth(first.getsampwidth())
        out.setframerate(first.getframerate())
        first.close()
        for p in paths:
            w = wave.open(p, "rb")
            out.writeframes(w.readframes(w.getnframes()))
            w.close()


def synthesize_f5(song: Song, out_dir: str) -> str:
    """
    小狗原声演唱：把歌词按乐句送 f5_server 合成克隆音色，再按旋律映射音高拼接。
    需要桌宠的 f5_server 在跑（本函数会自动尝试启动）。未装/未启动会抛错，
    由 ensure_cache 自动降级到本地引擎，保证「一定能唱」。
    """
    _ensure_f5_server()
    notes = _notes_for_song(song)
    lyric = song.lyric or "".join(t for t, _, _ in notes) or song.title
    phrases = _split_phrases(lyric) or [lyric]
    os.makedirs(out_dir, exist_ok=True)
    total = len(notes)
    parts = []
    for pi, ph in enumerate(phrases):
        start = int(total * pi / len(phrases))
        end = int(total * (pi + 1) / len(phrases))
        seg = notes[start:end] if end > start else notes[start:start + 1] or notes[-1:]
        midi = int(sum(m for _, m, _ in seg) / len(seg))
        semi = midi - 65  # 相对基准(~F4)的半音数
        p_wav = os.path.join(out_dir, f"p{pi:02d}.wav")
        _f5_synth(ph, p_wav)
        if _has_ffmpeg() and abs(semi) > 0.5:
            s_wav = os.path.join(out_dir, f"s{pi:02d}.wav")
            _pitch_shift(p_wav, s_wav, semi)
            parts.append(s_wav)
        else:
            parts.append(p_wav)
        time.sleep(0.05)
    final = os.path.join(out_dir, "song.wav")
    if len(parts) == 1:
        import shutil
        shutil.copy(parts[0], final)
    else:
        _concat_wavs(parts, final)
    return final


# ----------------------------------------------------------------------------
# 播放
# ----------------------------------------------------------------------------
def play_wav(path: str) -> None:
    import winsound
    winsound.PlaySound(path, winsound.SND_FILENAME)


def play_mp3(path: str) -> None:
    try:
        from playsound import playsound
        playsound(path)
    except Exception:
        os.startfile(path)  # Windows 默认播放器兜底


def play_sequence(paths: list) -> None:
    for p in paths:
        if p.lower().endswith(".wav"):
            play_wav(p)
        else:
            play_mp3(p)


def ensure_cache(song: Song, engine: str = "local") -> tuple:
    """
    返回 (kind, payload)。kind='wav' -> payload 是单个 wav 路径；
    kind='mp3s' -> payload 是 mp3 路径列表（顺序播放）。
    已缓存则直接返回，保证点唱零等待（低延迟）。
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    if engine == "edge":
        d = os.path.join(CACHE_DIR, song.id)
        mp3s = [os.path.join(d, f"{i:03d}.mp3")
                for i in range(len(_notes_for_song(song)))]
        if all(os.path.exists(p) for p in mp3s):
            return ("mp3s", mp3s)
        mp3s = synthesize_edge(song, d)
        return ("mp3s", mp3s)
    if engine == "f5":
        d = os.path.join(CACHE_DIR, song.id + "_f5")
        final = os.path.join(d, "song.wav")
        if os.path.exists(final):
            return ("wav", final)
        try:
            final = synthesize_f5(song, d)
            return ("wav", final)
        except Exception as e:
            # 小狗原声不可用 -> 降级本地引擎，保证一定能唱
            print(f"[f5] 不可用，降级本地引擎: {e}")
            wav = os.path.join(CACHE_DIR, f"{song.id}.wav")
            if not os.path.exists(wav):
                synthesize_local(song, wav)
            return ("wav", wav)
    # 默认本地引擎
    wav = os.path.join(CACHE_DIR, f"{song.id}.wav")
    if not os.path.exists(wav):
        synthesize_local(song, wav)
    return ("wav", wav)


# ----------------------------------------------------------------------------
# 后台自动更新（曲库持续自更新）
# ----------------------------------------------------------------------------
class TrendingUpdater(threading.Thread):
    def __init__(self, lib: dict, interval_sec: int = 600, on_update=None):
        super().__init__(daemon=True)
        self.lib = lib
        self.interval = interval_sec
        self.on_update = on_update
        self.running = True

    def run(self):
        while self.running:
            try:
                songs, meta = fetch_trending()
                added = merge_trending(self.lib, songs)
                # 自动「学习」：对有授权数据的歌曲预渲染缓存，保证点唱低延迟
                for s in added + list(self.lib.values()):
                    if s.licensed and s.status != "learned":
                        try:
                            ensure_cache(s, "local")
                            s.status = "learned"
                            s.learned_at = time.strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            pass
                save_library(self.lib)
                if self.on_update:
                    self.on_update(meta, added)
            except Exception as e:
                if self.on_update:
                    self.on_update({"error": str(e)}, [])
            time.sleep(self.interval)

    def stop(self):
        self.running = False


if __name__ == "__main__":
    # 无头自检：拉取热歌 + 本地合成一首 demo 并写出 wav
    lib = load_library()
    print(f"曲库现有 {len(lib)} 首")
    songs, meta = fetch_trending()
    print("热歌拉取:", meta)
    for s in songs[:5]:
        print("  -", s.title, s.artist, "|", s.status)
    if lib:
        first = next(iter(lib.values()))
        out = os.path.join(CACHE_DIR, f"{first.id}.wav")
        synthesize_local(first, out)
        print(f"已合成示例: {out} ({os.path.getsize(out)} bytes)")
