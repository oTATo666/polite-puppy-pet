# -*- coding: utf-8 -*-
r"""
小狗实时语音 · 一键启动（GPT-SoVITS 链路）
==========================================
完整链路：
  桌宠 main.py ──/v1/audio/speech──▶ 网关(9880, puppy_tts_gateway.py)
                                     └──/tts(钉死小狗参考音)──▶ GPT-SoVITS api_v2(9881)

本脚本负责按顺序把服务拉起来，任何一步失败都明确说原因，绝不静默降级：
  1. 模型体检：pretrained / G2PW / nltk 缺就提示先跑 download_gsv_models.py
  2. 依赖体检：对 GPT-SoVITS 用的 python 做子进程 import 测试，缺包给出安装命令
  3. 起 api_v2.py（127.0.0.1:9881，工作目录=仓库根）
  4. 等 /health 就绪
  5. 起网关（127.0.0.1:9880，桌宠 .env 已指向这里）
  6. Ctrl+C 一键收摊

用法：
  python tools\start_puppy_gsv.py
  python tools\start_puppy_gsv.py --gsv-python <python路径> --repo <仓库路径>
  python tools\start_puppy_gsv.py --check            # 只体检不启动
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = Path(r"C:\Users\zheng'zheng\WorkBuddy\2026-10-29-01-01-48\GPT-SoVITS")
DEFAULT_GSV_PY = Path(r"C:\Users\zheng'zheng\WorkBuddy\voice-clone-env\Scripts\python.exe")

API_PORT = 9881
GW_PORT = 9880

# api_v2 推理必需的第三方包（不等同 requirements.txt 全集，训练用的不查）
NEEDED = ["funasr", "librosa", "cn2an", "pypinyin", "jieba", "jieba_fast",
          "g2p_en", "opencc", "sentencepiece", "yaml", "scipy", "soundfile"]


def probe_imports(python: Path, mods: list) -> list:
    """在目标 python 子进程里逐个 import，返回缺失清单。"""
    code = (
        "import importlib,sys\n"
        "missing=[]\n"
        "for m in %r:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception:\n"
        "        missing.append(m)\n"
        "print('|'.join(missing))\n" % mods
    )
    try:
        out = subprocess.run([str(python), "-E", "-c", code],
                             capture_output=True, text=True, timeout=180).stdout.strip()
    except Exception as exc:
        return ["<子进程执行失败: %s>" % exc]
    return [m for m in out.split("|") if m] if out else []


def py_prefix(python: Path) -> Path:
    """取目标 python 的 sys.prefix（nltk_data 官方就解压在这里）。"""
    try:
        out = subprocess.run([str(python), "-E", "-c", "import sys;print(sys.prefix)"],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        if out:
            return Path(out)
    except Exception:
        pass
    return Path(sys.prefix)


def check_models(repo: Path, gpy: Path) -> list:
    sentinels = [
        "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s2G2333k.pth",
        "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt",
        "GPT_SoVITS/pretrained_models/chinese-hubert-base",
        "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        "GPT_SoVITS/text/G2PWModel",
    ]
    missing = []
    for s in sentinels:
        p = repo.joinpath(*s.split("/"))
        if not p.exists():
            missing.append(s)
    # nltk_data：解压在「引擎」python 的 sys.prefix 下
    nltk = py_prefix(gpy) / "nltk_data"
    if not nltk.is_dir():
        missing.append("nltk_data（%s）" % nltk)
    return missing


def wait_health(url: str, timeout_s: float, name: str) -> bool:
    print("[等待] %s 就绪（最多 %.0fs，首次加载模型会久一点）…" % (name, timeout_s))
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                print("[就绪] %s: %s" % (name, resp.read().decode("utf-8", "ignore")[:120]))
                return True
        except Exception:
            time.sleep(2)
    print("[超时] %s 未就绪，看上面 api_v2 的报错日志" % name)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="小狗实时语音一键启动（GPT-SoVITS）")
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--gsv-python", default=str(DEFAULT_GSV_PY))
    ap.add_argument("--api-port", type=int, default=API_PORT)
    ap.add_argument("--gw-port", type=int, default=GW_PORT)
    ap.add_argument("--check", action="store_true", help="只体检不启动")
    args = ap.parse_args()

    repo, gpy = Path(args.repo), Path(args.gsv_python)
    print("=" * 64)
    print("小狗实时语音（GPT-SoVITS）启动器")
    print("  仓库: %s" % repo)
    print("  引擎Python: %s" % gpy)
    print("=" * 64)

    if not repo.is_dir() or not (repo / "api_v2.py").is_file():
        print("[错误] 找不到 GPT-SoVITS 仓库（api_v2.py 不存在）：%s" % repo)
        return 1
    if not gpy.is_file():
        print("[错误] 找不到引擎 Python：%s" % gpy)
        print("  可用 --gsv-python 指定，或先创建环境后重试")
        return 1

    # 1) 模型体检
    miss_models = check_models(repo, gpy)
    if miss_models:
        print("[未就绪] 预训练模型缺失：")
        for m in miss_models:
            print("  -", m)
        print("  → 先运行：python tools\\download_gsv_models.py --repo \"%s\"" % repo)
    else:
        print("[OK] 预训练模型齐全")

    # 2) 依赖体检
    miss_pkgs = probe_imports(gpy, NEEDED)
    if miss_pkgs:
        print("[未就绪] 引擎环境缺包：%s" % " ".join(miss_pkgs))
        print("  → 安装（清华镜像）：")
        print('    "%s" -m pip install %s -i https://pypi.tuna.tsinghua.edu.cn/simple'
              % (gpy, " ".join(miss_pkgs)))
        print("  注意：GPT-SoVITS 要求 transformers>=4.51,<5，若当前版本更高请降级")
    else:
        print("[OK] 引擎依赖齐全")

    ok = not miss_models and not miss_pkgs
    if args.check or not ok:
        print("\n体检结论：%s" % ("可以启动" if ok else "尚有缺失，按上面提示补齐后重试"))
        return 0 if ok else 1

    # 3) 起 api_v2
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    api_proc = subprocess.Popen(
        [str(gpy), "-E", "api_v2.py", "-a", "127.0.0.1", "-p", str(args.api_port)],
        cwd=str(repo), env=env)
    print("[启动] api_v2 pid=%s → http://127.0.0.1:%d" % (api_proc.pid, args.api_port))

    if not wait_health("http://127.0.0.1:%d/health" % args.api_port, 300, "GPT-SoVITS api_v2"):
        api_proc.terminate()
        return 1

    # 4) 起网关
    gw_proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "puppy_tts_gateway.py"),
         "--port", str(args.gw_port),
         "--upstream", "http://127.0.0.1:%d" % args.api_port],
        cwd=str(ROOT), env=env)
    print("[启动] 网关 pid=%s → http://127.0.0.1:%d（桌宠 .env 指向这里）" % (gw_proc.pid, args.gw_port))

    time.sleep(1.5)
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % args.gw_port, timeout=5) as resp:
            print("[网关体检] %s" % resp.read().decode("utf-8", "ignore")[:200])
    except Exception as exc:
        print("[警告] 网关体检失败：%s" % exc)

    print("\n链路已就绪：桌宠 → 9880(网关,音色钉死) → 9881(GPT-SoVITS)。Ctrl+C 收摊。")
    try:
        while True:
            time.sleep(5)
            if api_proc.poll() is not None:
                print("[退出] api_v2 已停止（code=%s）" % api_proc.returncode)
                break
            if gw_proc.poll() is not None:
                print("[退出] 网关已停止（code=%s）" % gw_proc.returncode)
                break
    except KeyboardInterrupt:
        print("\n[收摊] 正在停止两个服务…")
    finally:
        for p in (gw_proc, api_proc):
            try:
                p.terminate()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
