# -*- coding: utf-8 -*-
"""把"这个 exe 是哪份源码打的"写成一个可验证的小文件。

为什么需要
    onefile 的 pyc 在 exe 里是压缩的，用"在二进制里搜版本号"判断新旧会误判。
    构建完成后跑一次本脚本，在 exe 旁边生成 build_info.json：
      - 版本号（读 main.py 的 APP_VERSION）
      - main.py / index.html 的 SHA256（构建时那一份）
      - exe 自身的 SHA256
    之后 scripts\\pack_check.py --exe 会拿它逐项比对。

用法
    python scripts\\write_build_info.py --exe 01_程序\\PolitePuppy.exe
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
WATCH = ("main.py", "index.html")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def app_version() -> str:
    for line in (PROG / "main.py").read_text(encoding="utf-8").splitlines():
        m = re.match(r'^APP_VERSION\s*=\s*"([^"]+)"', line)
        if m:
            return m.group(1)
    return ""


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 build_info.json")
    ap.add_argument("--exe", required=True, help="刚打好的 exe")
    ap.add_argument("--out", default="", help="输出路径（默认 exe 同目录）")
    args = ap.parse_args(argv)

    exe = Path(args.exe).resolve()
    if not exe.is_file():
        print("找不到 exe：%s" % exe)
        return 1

    # N-13：内置表情清单——exe 旁边表情包目录的文件名快照。运行期只有
    # 清单里的文件免令牌，用户后来丢进去的图一律走签名。
    stickers = []
    st_dir = exe.parent / "表情包"
    if st_dir.is_dir():
        stickers = sorted(p.name for p in st_dir.iterdir()
                          if p.is_file() and p.suffix.lower()
                          in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
    try:
        (exe.parent / "sticker_manifest.json").write_text(
            json.dumps(stickers, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        print("已写入 sticker_manifest.json（内置表情 %d 个）" % len(stickers))
    except OSError as exc:
        print("sticker_manifest.json 写失败（表情包将全部走签名）：%s" % exc)

    info = {
        "version": app_version(),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "exe": exe.name,
        "exe_bytes": exe.stat().st_size,
        "exe_sha256": sha256(exe),
        "source": {name: sha256(PROG / name) for name in WATCH if (PROG / name).is_file()},
    }
    out = Path(args.out) if args.out else (exe.parent / "build_info.json")
    out.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已写入 %s" % out)
    print("版本 %s | exe %.2f MB | %s" % (info["version"], info["exe_bytes"] / 1048576.0,
                                        info["exe_sha256"][:16]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
