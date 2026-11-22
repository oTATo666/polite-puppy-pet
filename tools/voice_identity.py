# -*- coding: utf-8 -*-
r"""
小狗「音色身份证」——持久化声线 ID + 音色指纹 + 实时同步
============================================================

要解决的问题
------------
1. **音色漂移**：原来"音色 ID"只是档案目录名（如「小狗」），跟真正的参考音频没有绑定。
   一旦 ref.wav 被换掉、或 f5_clone 回落到别的默认路径，声音变了但 ID 还叫「小狗」，
   谁都发现不了。这里给参考音频算指纹（sha256），ID 与指纹绑定，换了就报警。
2. **静默换声**：f5_clone 里有个指向别处目录的 REF_FALLBACK，参考音读不到时会悄悄
   用另一个人的录音。这里把它收口：身份证解析不到就明确报错，绝不悄悄换声。
3. **实时同步**：所有组件（f5_server / main.py / 界面）都在"要出声的那一刻"来这里取
   当前声线，按 mtime 自动重载，改了音色档案即刻生效，不用重启。

身份证文件：voices/voice_id.json
    {
      "voice_id": "puppy_<名字>_<指纹前8位>",  # 小狗自己的、持久不变的声线 ID
      "owner": "<她的名字>", "display": "小狗自己的声线",
      "profile_id": "小狗",
      "ref": "voices/profiles/小狗/ref.wav",  # 相对项目根
      "ref_sha256": "...", "ref_bytes": 387072,
      "ref_text": "...",
      "params": {"speed":1.45,"seed":20261025,"nfe":12},
      "locked": true,
      "created_at": ..., "updated_at": ...,
      "history": [{"at":..., "ref_sha256":..., "note":"参考音频变更"}]
    }

用法：
    python tools\voice_identity.py show      # 看当前声线身份证
    python tools\voice_identity.py verify    # 校验音色指纹是否漂移
    python tools\voice_identity.py init      # 强制重建（换声源时用）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

def _app_root() -> Path:
    """项目根目录：冻结成 exe 时用 exe 所在目录，源码运行时用 tools 的上一级。

    PyInstaller onefile 下 __file__ 在临时解包目录（_MEIPASS），
    按 __file__ 推出来的 voices/ 是空的，找不到用户磁盘上的音色档案。
    """
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _app_root()
VOICES = ROOT / "voices"
PROFILES = VOICES / "profiles"
ACTIVE = VOICES / "active.json"
ID_FILE = VOICES / "voice_id.json"


def _default_owner() -> str:
    """她的名字从 identity.py 取（N-24），取不到用档案名占位。"""
    try:
        import identity            # 01_程序 根目录
        return identity.pet_name()
    except ImportError:
        return ""


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default if default is not None else {}


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", (s or "").strip())
    return s.strip("_") or "voice"


def sha256_of(path: Path) -> str:
    """参考音频指纹。文件不大（几百 KB），直接全量哈希，稳。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def current_profile_id() -> str:
    """voices/active.json → 当前启用的档案 id。"""
    data = _read_json(ACTIVE, {}) or {}
    pid = str(data.get("id") or "").strip()
    if pid:
        return pid
    # 没指定就挑第一个可用档案
    try:
        dirs = [d.name for d in PROFILES.iterdir() if d.is_dir()]
        return dirs[0] if dirs else ""
    except OSError:
        return ""


def _profile(pid: str) -> dict:
    return _read_json(PROFILES / pid / "profile.json", {}) or {}


def _persona_name() -> str:
    """优先用 persona.json 里她真正的名字，其次才退回档案名。"""
    p = _read_json(ROOT / "persona.json", {}) or {}
    ident = p.get("identity") or {}
    return str(ident.get("name") or "").strip()


def _find_ref(pid: str, prof: dict) -> Path:
    """按 profile.json 的 ref 字段找参考音频；找不到再退到目录里的 ref.wav。"""
    cand = []
    r = str(prof.get("ref") or "").strip()
    if r:
        cand.append(PROFILES / pid / r)
    cand.append(PROFILES / pid / "ref.wav")
    for p in cand:
        if p.is_file():
            return p
    raise FileNotFoundError(f"音色档案「{pid}」里找不到参考音频（试过：{', '.join(str(c) for c in cand)}）")


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except Exception:
        return str(p)


def _new_voice_id(owner: str, ref_sha: str) -> str:
    return "puppy_%s_%s" % (_slug(owner), ref_sha[:8])


def build_identity(force: bool = False) -> dict:
    """
    生成 / 刷新声线身份证。
    voice_id 一旦定下就持久不变；参考音频换了只更新指纹并记入 history（漂移可被发现），
    不悄悄改 ID——这样"小狗自己的声线"始终能被认出来。
    """
    pid = current_profile_id()
    if not pid:
        return {"ok": False, "message": "没有任何音色档案，也没指定 active.json"}
    prof = _profile(pid)
    try:
        ref = _find_ref(pid, prof)
    except FileNotFoundError as exc:
        return {"ok": False, "message": str(exc)}

    ref_sha = sha256_of(ref)
    ref_bytes = ref.stat().st_size
    owner = _persona_name() or str(prof.get("name") or prof.get("id")
                                   or _default_owner() or "puppy")
    params = {
        "speed": float(prof.get("speed") or 1.45),
        "seed": int(prof.get("seed") or 20261025),
        "nfe": int(prof.get("nfe") or 12),
    }
    ref_text = str(prof.get("ref_text") or "").strip()

    old = _read_json(ID_FILE, {}) or {}
    now = int(time.time())
    ident = {
        "voice_id": old.get("voice_id") or _new_voice_id(owner, ref_sha),
        "owner": owner,
        "display": str(prof.get("display") or "小狗自己的声线"),
        "engine": str(prof.get("engine") or "f5-tts"),
        "profile_id": pid,
        "ref": _rel(ref),
        "ref_sha256": ref_sha,
        "ref_bytes": ref_bytes,
        "ref_text": ref_text,
        "params": params,
        "locked": True,
        "created_at": int(old.get("created_at") or now),
        "updated_at": now,
        "history": list(old.get("history") or []),
    }
    # 参考音频换了 → 留痕（ID 保持不变，以便认出"还是那只狗"）
    if old.get("ref_sha256") and old["ref_sha256"] != ref_sha:
        ident["history"].append({
            "at": now,
            "from": old["ref_sha256"],
            "to": ref_sha,
            "note": "参考音频变更（音色可能变化，已留痕）",
        })
    if force or not ID_FILE.is_file() or old != ident:
        _write_json(ID_FILE, ident)
    return dict(ident, ok=True, ref_abs=str(ref))


# 实时同步：同时盯住「身份证 + active.json + 当前档案」三个文件的 mtime，
# 任何一处改动都会立刻重载——切换音色不用重启服务。
_cache: dict = {"key": None, "data": None}


def _mtime_key() -> tuple:
    vals = []
    for p in (ID_FILE, ACTIVE):
        try:
            vals.append(p.stat().st_mtime if p.is_file() else 0.0)
        except OSError:
            vals.append(0.0)
    pid = current_profile_id()
    if pid:
        try:
            pf = PROFILES / pid / "profile.json"
            vals.append(pf.stat().st_mtime if pf.is_file() else 0.0)
        except OSError:
            vals.append(0.0)
    return tuple(vals)


def id_file_missing() -> bool:
    """N-26：声线身份证文件是否存在。

    外发包不带 voice_id.json（声纹不外发）——调用方据此判断"缺档案"
    （回落系统语音），并和"克隆服务临时挂了"（保持严格不回落）区分开。
    """
    return not ID_FILE.is_file()


def sync_now() -> dict:
    """每次要出声前来取一次当前声线。档案一改就自动重载，不用重启服务。"""
    key = _mtime_key()
    if _cache["data"] and _cache["key"] == key:
        return _cache["data"]
    ident = build_identity()
    if ident.get("ok"):
        ident["ref_abs"] = str(ROOT / ident["ref"])
    _cache["key"], _cache["data"] = key, ident
    return ident


def verify() -> dict:
    """校验当前参考音频与身份证记录的是否一致（音色有没有漂移）。"""
    ident = sync_now()
    if not ident.get("ok"):
        return {"ok": False, "message": ident.get("message", "无声线身份证")}
    ref = Path(ident.get("ref_abs") or "")
    if not ref.is_file():
        return {"ok": False, "voice_id": ident.get("voice_id"),
                "drift": True, "message": f"身份证指向的参考音频不见了：{ref}"}
    cur = sha256_of(ref)
    drift = (cur != ident.get("ref_sha256"))
    return {
        "ok": True,
        "voice_id": ident.get("voice_id"),
        "owner": ident.get("owner"),
        "profile_id": ident.get("profile_id"),
        "ref": ident.get("ref"),
        "drift": drift,
        "sha256": cur,
        "recorded": ident.get("ref_sha256"),
        "message": ("音色一致 ✅" if not drift else "⚠️ 参考音频已变更，音色可能漂移"),
        "params": ident.get("params"),
    }


def resolve(voice_id: str | None = None) -> dict:
    """
    取可用的声线配置：{ref_audio, ref_text, speed, seed, nfe, voice_id}。
    解析不到就抛错，绝不悄悄回落到别人的参考音频。
    """
    ident = sync_now()
    if not ident.get("ok"):
        raise RuntimeError(f"声线身份证不可用：{ident.get('message')}")
    if voice_id and ident.get("voice_id") != voice_id:
        raise RuntimeError(f"声线 ID 不符：要 {voice_id}，当前是 {ident.get('voice_id')}")
    ref = Path(ident["ref_abs"])
    if not ref.is_file():
        raise RuntimeError(f"参考音频丢失：{ref}（拒绝回落到默认音源，以免音色悄悄变了）")
    p = ident.get("params") or {}
    return {
        "voice_id": ident.get("voice_id"),
        "ref_audio": str(ref),
        "ref_text": ident.get("ref_text") or "",
        "speed": float(p.get("speed") or 1.45),
        "seed": int(p.get("seed") or 20261025),
        "nfe": int(p.get("nfe") or 12),
        "profile_id": ident.get("profile_id"),
    }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="小狗音色身份证")
    ap.add_argument("cmd", nargs="?", default="show", choices=["show", "verify", "init"])
    a = ap.parse_args(argv)
    if a.cmd == "init":
        out = build_identity(force=True)
    elif a.cmd == "verify":
        out = verify()
    else:
        out = sync_now()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
