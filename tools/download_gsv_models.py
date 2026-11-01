# -*- coding: utf-8 -*-
r"""
GPT-SoVITS 预训练模型下载器（与官方 install.ps1 / install.sh 同源同解压位）
==========================================================================
下载三个包并按官方脚本的位置解压：
  pretrained_models.zip → <仓库>\GPT_SoVITS\            （底模/hubert/roberta/sv）
  G2PWModel.zip         → <仓库>\GPT_SoVITS\text\       （中文文本前端，必装）
  nltk_data.zip         → <python>\nltk_data\           （g2p_en 用）

源：默认 modelscope（国内快），可用 --source hf-mirror / hf 切换。
用法：
  python tools\download_gsv_models.py                 # 全部缺的都下
  python tools\download_gsv_models.py --only pretrained
  python tools\download_gsv_models.py --check         # 只检查不下载
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_REPO = r"C:\Users\zheng'zheng\WorkBuddy\2026-10-29-01-01-48\GPT-SoVITS"
DEFAULT_PY = r"C:\Users\zheng'zheng\WorkBuddy\voice-clone-env"

SOURCES = {
    "modelscope": "https://www.modelscope.cn/models/XXXXRT/GPT-SoVITS-Pretrained/resolve/master/{name}",
    "hf-mirror": "https://hf-mirror.com/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/{name}",
    "hf": "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/{name}",
}

ITEMS = {
    "pretrained": {
        "zip": "pretrained_models.zip",
        "target": "repo/GPT_SoVITS",
        "sentinels": [  # 解压后应存在的关键文件（相对仓库根）
            "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s2G2333k.pth",
            "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt",
            "GPT_SoVITS/pretrained_models/chinese-hubert-base",
            "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        ],
    },
    "g2pw": {
        "zip": "G2PWModel.zip",
        "target": "repo/GPT_SoVITS/text",
        "sentinels": ["GPT_SoVITS/text/G2PWModel"],
    },
    "nltk": {
        "zip": "nltk_data.zip",
        "target": "py/nltk_data",
        "sentinels": [],
    },
}


def repo() -> Path:
    return Path(sys.argv[sys.argv.index("--repo") + 1]) if "--repo" in sys.argv else Path(DEFAULT_REPO)


def gsv_py() -> Path:
    return Path(sys.argv[sys.argv.index("--python") + 1]) if "--python" in sys.argv else Path(DEFAULT_PY)


def resolve_target(spec: str) -> Path:
    kind, _, rest = spec.partition("/")
    if kind == "repo":
        base = repo()
        out = base.joinpath(*rest.split("/"))
        out.parent.mkdir(parents=True, exist_ok=True)
        return out
    if kind == "py":
        out = gsv_py() / rest
        out.parent.mkdir(parents=True, exist_ok=True)
        return out
    raise ValueError(spec)


def item_done(key: str) -> bool:
    it = ITEMS[key]
    if not it["sentinels"]:
        tgt = resolve_target(it["target"])
        return tgt.is_dir() and any(tgt.iterdir())
    root = repo()
    for s in it["sentinels"]:
        p = root.joinpath(*s.split("/"))
        if not p.exists():
            return False
    return True


def download(name: str, source: str, dest: Path) -> None:
    url = SOURCES[source].format(name=name)
    print("[下载] %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "puppy-tts/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print("\r  %.1f / %.1f MB (%.0f%%)" % (done / 1e6, total / 1e6, 100.0 * done / total),
                      end="", flush=True)
    print()


def fetch(key: str, source: str) -> None:
    it = ITEMS[key]
    name = it["zip"]
    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / name
        download(name, source, zpath)
        target = resolve_target(it["target"])
        print("[解压] %s → %s" % (name, target))
        with zipfile.ZipFile(zpath) as z:
            z.extractall(target)


def main() -> int:
    ap = argparse.ArgumentParser(description="GPT-SoVITS 预训练模型下载（官方同源）")
    ap.add_argument("--source", default="modelscope", choices=list(SOURCES))
    ap.add_argument("--only", default="pretrained,g2pw,nltk")
    ap.add_argument("--check", action="store_true", help="只检查不下载")
    args = ap.parse_args()

    print("仓库: %s" % repo())
    print("Python: %s" % gsv_py())
    todo = []
    for key in [k.strip() for k in args.only.split(",") if k.strip()]:
        ok = item_done(key)
        print("  %-10s %s" % (key, "已就绪" if ok else "缺失"))
        if not ok:
            todo.append(key)
    if args.check or not todo:
        return 0

    for key in todo:
        print("=== 处理 %s ===" % key)
        try:
            fetch(key, args.source)
        except Exception as exc:
            print("[失败] %s：%s" % (key, exc))
            print("  可换源重试：--source hf-mirror")
            return 1
    print("全部完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
