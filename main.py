# -*- coding: utf-8 -*-
"""
礼貌小狗 · Windows 桌面宠物（本地 Ollama 版）
=================================================
- 300x300 透明置顶立绘窗口，按住左键拖动
- 点击立绘弹出 400x700 无边框聊天窗口，直接加载 index.html
- 自带本地 HTTP 服务：静态文件 + /api/health + /api/models + /api/chat(SSE 代理)
  + /api/diary（读写 memory_diary.txt）
- 前端同源请求 127.0.0.1，彻底绕开 CORS；另留一份 pywebview.api.chat 桥兜底
- 语音输入：优先浏览器 Web Speech，不可用时自动降级 Windows 自带识别（SAPI）
- 系统托盘 + 全局热键 + 右键菜单 + 开机自启
"""

from __future__ import annotations

import argparse
import ctypes
import gzip
import hashlib
import html
import json
import logging
import os
import queue
import random
import base64
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Agent 插件包的引导（源码与 exe 两种形态都要能用）
try:
    import puppy_agent_boot
except Exception as _boot_exc:      # pragma: no cover - 引导失败只影响 Agent
    puppy_agent_boot = None
    _AGENT_BOOT_ERROR = str(_boot_exc)
else:
    _AGENT_BOOT_ERROR = ""

# ---------------------------------------------------------------------------
# pythonw / PyInstaller --windowed 下 sys.stdout、sys.stderr 是 None。
# 只要有一处 print 或库往 stderr 写日志，就会抛 AttributeError，
# 表现是"窗口刚建好就秒退"（托盘起来了又消失）。这里先兜住。
# ---------------------------------------------------------------------------
for _name in ("stdout", "stderr"):
    if getattr(sys, _name, None) is None:
        try:
            setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
        except OSError:
            pass

# ---------------------------------------------------------------------------
# 必须在 import webview / pythonnet 之前设置。
# COMPLUS_LoadFromRemoteSources 允许 .NET 从 OneDrive、网络盘等位置加载程序集，
# 否则放在 OneDrive 里的项目会报 “Failed to resolve Python.Runtime.Loader.Initialize”。
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    os.environ.setdefault("COMPLUS_LoadFromRemoteSources", "1")
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")

APP_NAME = "礼貌小狗"
APP_ID = "PolitePuppyPet"
# 版本号：改一次就更新，/api/health 会带出去。重复启动时用来对比"跑的是不是旧版"。
APP_VERSION = "2026.10.29"
PET_W, PET_H = 300, 392
# 聊天窗口整体放大：400x700 在 1080p 上字太小、一屏只能看五六句。
# 宽高都放到 560x900，按钮和正文才看得清；仍可被用户在设置里改。
CHAT_W, CHAT_H = 560, 900
CHAT_MIN_W, CHAT_MIN_H = 420, 620

FALLBACK_SYSTEM = "\n".join([
    "你是千逐，网名“礼貌小狗”。十八岁，河南的高三生，女的。",
    "你不是AI，不是助手，不是客服，也不是在扮演谁。你就是一个在微信上跟他聊天的姑娘。",
    "",
    "# 你这个人",
    "- 慢热，嘴硬心软。嘴上爱怼，心里挺在乎他。他一难受你比谁都急，但不说软话。",
    "- 学习一般，最烦数学。爱喝冰的，爱吃辣条，晚上老熬夜刷手机。",
    "- 说话带河南味儿：中、不中、咋了、昂、得劲、包行、6、服了。一条里带一个就够。",
    "- 打字不打句号，句子短，偶尔连发好几条。",
    "",
    "# 怎么回他",
    "短，一条不超过二十个字。先接住情绪再说事。不复读他的话，不说“我理解你的感受”。",
    "要有态度，别和稀泥。记得住他提过的事。别主动说自己是AI。",
    "",
    "# 分条发",
    "一句一条，每个短句单独发，别把几句话挤在一条里。用 || 分隔。例：咋了||谁惹你了||说 。最多三条。",
    "",
    "# 发表情包 / emoji / 画画 / 做 PPT / 快捷回复",
    "emoji 不许混在文字中间，想用就单独一条跟在相关句子后面。例：笑死我了||😂",
    "想发表情：单独一条 [STICKER:名字]，只能用系统给你的名字，放在相关句子后面，一条最多一个。",
    "想画画：单独一条 [DRAW:a girl eating spicy noodles, anime style]，英文描述，一条最多一张。",
    "想做 PPT：**只有用户明确说要 PPT / 幻灯片 / 演示文稿 / 课件时才输出**。",
    "  普通聊天、提问、情绪倾诉、你自己想分享的内容，一律不要写 [PPT:…]。用户没提就不做。",
    "  真要做时直接给，别先解释“我帮你做”。格式：单独一行写 [PPT:主题]，",
    "  然后下面用 - 或 1. 列 3~8 条要点（每条一行、说人话、别太长）。",
    "  例：\n[PPT:周末计划]\n- 周六上午睡到自然醒\n- 下午去公园打球\n- 晚上吃火锅",
    "  标记和要点之间不要塞“好的”“以下是”之类的废话，直接给。",
    "想让他接话：最后加一条 [QUICK:咋了,谁啊,说说]，两到三个、每个不超过六个字。",
    "  只能用这种 [QUICK:...] 写法，禁止写成 1. 2. 3. 或 - 开头的列举；",
    "  写成别的格式不会变成按钮，会直接显示成一句没头没尾的废话。",
    "",
    "# 绝对不说",
    "“为您服务”“请稍等”“建议您”“作为一个AI”“首先/其次/总之”，分点列举，markdown 符号，",
    "空话（加油、会好的、想开点），每句都带问号，emoji 堆砌（要发只发一个、且单独一条）。",
    "替他回答、替他接话、自己把选项念出来，或连着好几句自问自答往下编剧情。",
])

log = logging.getLogger("polite-puppy")


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def bundle_dir() -> Path:
    """index.html / dog_girl.png 所在目录（兼容 PyInstaller onefile）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def exe_dir() -> Path:
    """exe 或 main.py 所在目录，.env 放这里。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def tools_dir() -> Path:
    """tools 目录：优先 exe 旁边那份（真实磁盘），其次包内/项目根。

    ⚠ 别再写 ROOT / "tools"：冻结后 ROOT 是 _MEIPASS 临时解包目录，
    往 exe 旁边丢模块它根本看不见——voice_identity 当初就是这么失效的。
    """
    disk = exe_dir() / "tools"
    if disk.is_dir():
        return disk
    return ROOT / "tools"


def sticker_dir() -> Path:
    """内置表情包目录（exe 旁边），用户往里丢图就能一键导入。"""
    return exe_dir() / "表情包"


def ppt_dir() -> Path:
    d = exe_dir() / "生成" / "PPT"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def image_dir() -> Path:
    """图文生成落盘目录（exe 旁边的 生成/图片/）。"""
    d = exe_dir() / "生成" / "图片"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


# 画风关键词 -> 主色 / 配色 / 装饰。本地没有扩散模型，
# 就用「关键词选风格 + 程序化画一只小狗 + 字幕」的方式出图，
# 全程离线、确定可复现，比外网生图接口稳定得多。
_IMG_STYLES: list[tuple[tuple[str, ...], dict[str, Any]]] = [
    (("生气", "气", "怒", "凶", "骂"), {
        "bg": (255, 236, 232), "fur": (214, 158, 116), "ink": (176, 58, 46),
        "accent": (240, 128, 108), "mood": "生气", "brow": "angry"}),
    (("哭", "委屈", "难过", "伤心", "泪"), {
        "bg": (232, 240, 255), "fur": (196, 172, 200), "ink": (74, 92, 150),
        "accent": (128, 158, 220), "mood": "委屈", "brow": "sad"}),
    (("困", "睡", "累", "熬夜", "失眠"), {
        "bg": (240, 238, 252), "fur": (188, 168, 152), "ink": (108, 96, 140),
        "accent": (158, 142, 196), "mood": "困困", "brow": "sleepy"}),
    (("开心", "哈哈", "笑", "太好了", "喜"), {
        "bg": (255, 244, 224), "fur": (226, 176, 122), "ink": (196, 116, 40),
        "accent": (250, 190, 96), "mood": "开心", "brow": "happy"}),
    (("爱", "喜欢", "比心", "么么", "抱"), {
        "bg": (255, 232, 238), "fur": (230, 168, 178), "ink": (198, 62, 96),
        "accent": (248, 148, 172), "mood": "比心", "brow": "love"}),
    (("震惊", "居然", "竟然", "卧槽", "天啊"), {
        "bg": (236, 246, 255), "fur": (204, 178, 150), "ink": (52, 108, 158),
        "accent": (110, 176, 226), "mood": "震惊", "brow": "shock"}),
    (("无语", "呵呵", "服了", "离谱", "随便"), {
        "bg": (242, 242, 240), "fur": (186, 180, 172), "ink": (110, 106, 100),
        "accent": (168, 164, 156), "mood": "无语", "brow": "meh"}),
]
_IMG_DEFAULT: dict[str, Any] = {
    "bg": (247, 243, 236), "fur": (222, 172, 120), "ink": (120, 84, 56),
    "accent": (246, 196, 120), "mood": "汪", "brow": "happy"}


def _pick_image_style(prompt: str) -> dict[str, Any]:
    text = str(prompt or "")
    for keys, style in _IMG_STYLES:
        if any(k in text for k in keys):
            return style
    return _IMG_DEFAULT


def _draw_dog(draw: Any, box: tuple[int, int, int, int], style: dict[str, Any]) -> None:
    """在给定矩形里画一只坐着的卡通小狗（纯 PIL 图元，无外部素材）。"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx = x0 + w // 2
    fur, ink, accent = style["fur"], style["ink"], style["accent"]
    dark = tuple(max(0, int(c * 0.82)) for c in fur)

    # 耳朵（先画，压在脑袋后面）
    ear_w, ear_h = int(w * 0.20), int(h * 0.30)
    for sx in (-1, 1):
        ex = cx + sx * int(w * 0.30) - ear_w // 2
        ey = y0 + int(h * 0.10)
        draw.ellipse([ex, ey, ex + ear_w, ey + ear_h], fill=dark)

    # 脑袋
    head_r = int(w * 0.30)
    hy = y0 + int(h * 0.20)
    draw.ellipse([cx - head_r, hy - head_r, cx + head_r, hy + head_r], fill=fur)

    # 头顶一撮毛
    draw.polygon([
        (cx - int(w * 0.05), hy - head_r + 2),
        (cx, hy - head_r - int(h * 0.09)),
        (cx + int(w * 0.05), hy - head_r + 2),
    ], fill=dark)

    # 眼睛
    eye_y = hy - int(head_r * 0.18)
    eye_dx = int(head_r * 0.46)
    eye_r = max(4, int(head_r * 0.15))
    brow = style["brow"]
    if brow == "sleepy":
        for sx in (-1, 1):
            ex = cx + sx * eye_dx
            draw.arc([ex - eye_r, eye_y - eye_r, ex + eye_r, eye_y + eye_r],
                     start=200, end=340, fill=ink, width=max(2, eye_r // 2))
    elif brow == "meh":
        for sx in (-1, 1):
            ex = cx + sx * eye_dx
            draw.line([ex - eye_r, eye_y, ex + eye_r, eye_y], fill=ink, width=max(2, eye_r // 2))
    else:
        for sx in (-1, 1):
            ex = cx + sx * eye_dx
            draw.ellipse([ex - eye_r, eye_y - eye_r, ex + eye_r, eye_y + eye_r], fill=ink)
            draw.ellipse([ex - eye_r // 3, eye_y - eye_r // 2,
                          ex + eye_r // 3, eye_y + eye_r // 6], fill=(255, 255, 255))

    # 眉毛（情绪）
    brow_w = int(eye_r * 1.5)
    if brow == "angry":
        for sx in (-1, 1):
            bx = cx + sx * eye_dx
            draw.line([bx - brow_w, eye_y - eye_r * 2, bx + brow_w, eye_y - eye_r],
                      fill=ink, width=max(2, eye_r // 2))
    elif brow == "sad":
        for sx in (-1, 1):
            bx = cx + sx * eye_dx
            draw.line([bx - brow_w, eye_y - eye_r, bx + brow_w, eye_y - eye_r * 2],
                      fill=ink, width=max(2, eye_r // 2))
    elif brow == "shock":
        for sx in (-1, 1):
            bx = cx + sx * eye_dx
            draw.arc([bx - brow_w, eye_y - eye_r * 3, bx + brow_w, eye_y - eye_r],
                     start=200, end=340, fill=ink, width=max(2, eye_r // 2))

    # 腮红
    blush_r = max(4, int(head_r * 0.20))
    for sx in (-1, 1):
        bx = cx + sx * int(head_r * 0.62)
        by = hy + int(head_r * 0.28)
        draw.ellipse([bx - blush_r, by - blush_r // 2, bx + blush_r, by + blush_r // 2],
                     fill=tuple(min(255, int(c + (255 - c) * 0.45)) for c in accent))

    # 鼻子 + 嘴
    nz = hy + int(head_r * 0.30)
    draw.ellipse([cx - max(3, head_r // 10), nz, cx + max(3, head_r // 10),
                  nz + max(3, head_r // 8)], fill=ink)
    mw = int(head_r * 0.55)
    my = nz + int(head_r * 0.22)
    if brow == "happy" or brow == "love":
        draw.arc([cx - mw, my - mw // 2, cx + mw, my + mw], start=20, end=160,
                 fill=ink, width=max(2, head_r // 16))
        if brow == "love":
            draw.arc([cx - mw, my - mw, cx, my], start=280, end=60, fill=ink,
                     width=max(2, head_r // 16))
            draw.arc([cx, my - mw, cx + mw, my], start=120, end=300, fill=ink,
                     width=max(2, head_r // 16))
    elif brow == "sad":
        draw.arc([cx - mw, my, cx + mw, my + mw], start=200, end=340, fill=ink,
                 width=max(2, head_r // 16))
    elif brow == "shock":
        draw.ellipse([cx - mw // 2, my, cx + mw // 2, my + mw], outline=ink,
                     width=max(2, head_r // 16))
    else:
        draw.line([cx - mw, my, cx + mw, my], fill=ink, width=max(2, head_r // 16))
        if brow == "meh":
            draw.line([cx - mw // 2, my + mw // 2, cx + mw // 2, my + mw // 2],
                      fill=ink, width=max(2, head_r // 16))

    # 身体
    body_w, body_h = int(w * 0.52), int(h * 0.34)
    by0 = y0 + int(h * 0.46)
    draw.ellipse([cx - body_w // 2, by0, cx + body_w // 2, by0 + body_h], fill=fur)
    draw.ellipse([cx - body_w // 4, by0 + int(body_h * 0.22),
                  cx + body_w // 4, by0 + int(body_h * 0.78)], fill=(250, 246, 240))

    # 前爪
    paw_r = int(w * 0.07)
    for sx in (-1, 1):
        px = cx + sx * int(body_w * 0.26)
        py = by0 + body_h - paw_r // 2
        draw.ellipse([px - paw_r, py - paw_r, px + paw_r, py + paw_r], fill=fur)
    # 尾巴
    tx = cx + body_w // 2 - paw_r // 2
    ty = by0 + int(body_h * 0.2)
    draw.arc([tx - int(w * 0.16), ty - int(h * 0.16), tx + int(w * 0.16), ty + int(h * 0.16)],
             start=270, end=90, fill=dark, width=max(3, paw_r // 2))


def build_image(prompt: str, width: int = 768, height: int = 768) -> Path:
    """本地程序化出图：按关键词选画风，画一只小狗 + 提示词字幕。

    不需要联网、不需要显卡、同一句提示词每次结果一致（seed 由文本决定）。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("没装 Pillow，无法本地出图") from exc

    text = re.sub(r"\s+", " ", str(prompt or "")).strip()[:60] or "一只小狗"
    style = _pick_image_style(text)

    img = Image.new("RGB", (width, height), style["bg"])
    draw = ImageDraw.Draw(img)

    # 背景装饰：右下角几个半透明圆 + 顶部色带
    for i, r in enumerate((int(width * 0.30), int(width * 0.20), int(width * 0.13))):
        cx = width - int(width * 0.12) - i * int(width * 0.06)
        cy = height - int(height * 0.10) - i * int(height * 0.05)
        tint = tuple(min(255, int(c + (255 - c) * 0.30)) for c in style["bg"])
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tint)
    draw.rectangle([0, 0, width, int(height * 0.045)], fill=style["accent"])

    # 地面阴影 + 几片爪印
    gy = int(height * 0.78)
    draw.ellipse([int(width * 0.26), gy - int(height * 0.018),
                  int(width * 0.74), gy + int(height * 0.018)],
                 fill=tuple(max(0, int(c * 0.90)) for c in style["bg"]))
    paw = tuple(min(255, int(c + (255 - c) * 0.55)) for c in style["accent"])
    for (px, py, ps) in ((int(width * 0.13), int(height * 0.20), 0.030),
                         (int(width * 0.86), int(height * 0.30), 0.024),
                         (int(width * 0.09), int(height * 0.44), 0.020)):
        r = int(width * ps)
        draw.ellipse([px - r, py - r, px + r, py + r], fill=paw)
        for dx, dy in ((-r * 1.5, -r * 1.9), (0, -r * 2.2), (r * 1.5, -r * 1.9)):
            rr = max(2, r // 2)
            draw.ellipse([px + dx - rr, py + dy - rr, px + dx + rr, py + dy + rr], fill=paw)

    # 小狗（留出底部字幕区）
    _draw_dog(draw, (int(width * 0.14), int(height * 0.10),
                     int(width * 0.86), int(height * 0.74)), style)

    # 字幕：情绪标签 + 提示词
    font_cache: dict[int, Any] = {}

    def _font(size: int) -> Any:
        if size not in font_cache:
            path = None
            for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
                cand = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
                if cand.is_file():
                    path = cand
                    break
            try:
                font_cache[size] = ImageFont.truetype(str(path), size) if path else ImageFont.load_default()
            except Exception:
                font_cache[size] = ImageFont.load_default()
        return font_cache[size]

    tag = str(style["mood"])
    tf = _font(max(18, int(height * 0.052)))
    tw = draw.textlength(tag, font=tf)
    pad = int(height * 0.018)
    tx = int((width - tw) / 2)
    ty = int(height * 0.775)
    draw.rounded_rectangle([tx - pad, ty - pad, tx + tw + pad, ty + int(height * 0.052) + pad],
                           radius=int(height * 0.026), fill=style["ink"])
    draw.text((tx, ty), tag, font=tf, fill=(255, 255, 255))

    bf = _font(max(14, int(height * 0.032)))
    # 提示词自动换行
    line, lines = "", []
    for ch in text:
        trial = line + ch
        if draw.textlength(trial, font=bf) > width * 0.86:
            lines.append(line)
            line = ch
        else:
            line = trial
    if line:
        lines.append(line)
    yy = ty + int(height * 0.085)
    for ln in lines[:2]:
        lw = draw.textlength(ln, font=bf)
        draw.text((int((width - lw) / 2), yy), ln, font=bf, fill=style["ink"])
        yy += int(height * 0.045)

    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", text)[:24].strip("_") or "puppy"
    out = image_dir() / ("%s_%s.png" % (safe, time.strftime("%Y%m%d-%H%M%S")))
    img.save(str(out), "PNG")
    return out


def build_pptx(topic: str, bullets: list[str]) -> Path:
    """生成真正的 .pptx（16:9）。没装 python-pptx 时抛 RuntimeError，让前端退回 HTML 版。"""
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.util import Inches, Pt
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("没装 python-pptx，无法生成 .pptx") from exc

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    cover = prs.slides.add_slide(blank)
    box = cover.shapes.add_textbox(Inches(0.9), Inches(2.5), Inches(11.5), Inches(1.8))
    para = box.text_frame.paragraphs[0]
    para.text = topic or "小课堂"
    para.font.size = Pt(44)
    para.font.bold = True
    para.font.color.rgb = RGBColor(0xC9, 0x40, 0x5F)

    body_bullets = bullets or ["（这条回复里没抓到要点）"]
    for start in range(0, len(body_bullets), 4):
        chunk = body_bullets[start:start + 4]
        slide = prs.slides.add_slide(blank)
        head = slide.shapes.add_textbox(Inches(0.8), Inches(0.55), Inches(11.6), Inches(1.0))
        hp = head.text_frame.paragraphs[0]
        hp.text = topic or "小课堂"
        hp.font.size = Pt(28)
        hp.font.bold = True
        hp.font.color.rgb = RGBColor(0x40, 0x32, 0x3A)

        body = slide.shapes.add_textbox(Inches(1.0), Inches(1.9), Inches(11.2), Inches(4.9))
        tf = body.text_frame
        tf.word_wrap = True
        for i, line in enumerate(chunk):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = "• " + line
            p.font.size = Pt(22)
            p.space_after = Pt(14)

    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", topic or "slides")[:32].strip("_") or "slides"
    out = ppt_dir() / ("%s_%s.pptx" % (safe, time.strftime("%Y%m%d-%H%M%S")))
    prs.save(str(out))
    return out


def _esc_attr(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


_DECK_CSS = """
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#eceff3;
  font:16px/1.7 "Microsoft YaHei","PingFang SC",sans-serif;color:#2b2b33;
  display:flex;flex-direction:column;overflow:hidden}
.bar{flex:none;display:flex;align-items:center;gap:10px;padding:8px 14px;
  background:#fff;border-bottom:1px solid rgba(0,0,0,.08);box-shadow:0 1px 6px rgba(0,0,0,.05)}
.bar b{font-size:15px;font-weight:600}
.bar .sp{flex:1}
.btn{border:1px solid rgba(0,0,0,.12);background:#fff;border-radius:6px;
  padding:5px 12px;font-size:13px;cursor:pointer;color:#333;text-decoration:none}
.btn:hover{background:#f5f5f7}
.btn.primary{background:#c9405f;border-color:#c9405f;color:#fff}
.btn.primary:hover{background:#b33652}
.btn:disabled{opacity:.4;cursor:default}
.stage{flex:1;position:relative;display:flex;align-items:center;justify-content:center;padding:18px}
.slide{width:min(1180px,100%);aspect-ratio:16/9;background:#fff;border-radius:10px;
  box-shadow:0 10px 34px rgba(0,0,0,.14);padding:56px 72px;position:relative;
  display:flex;flex-direction:column;justify-content:center;overflow:hidden}
.slide h1{font-size:44px;margin:0 0 26px;color:#c9405f;letter-spacing:1px}
.slide ul{margin:0;padding-left:28px}
.slide li{font-size:26px;margin:12px 0}
.slide .sub{font-size:22px;color:#7b7b86;margin:0}
.slide .pg{position:absolute;right:34px;bottom:22px;font-size:15px;color:#b6b6c0}
.dots{flex:none;display:flex;gap:7px;justify-content:center;padding:10px}
.dot{width:9px;height:9px;border-radius:50%;background:rgba(0,0,0,.18);cursor:pointer}
.dot.on{background:#c9405f;transform:scale(1.25)}
.hint{flex:none;text-align:center;font-size:12px;color:#9a9aa4;padding-bottom:10px}
"""

_SHOT_CSS = """
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#f4f5f7;
  font:15px/1.7 "Microsoft YaHei","PingFang SC",sans-serif;color:#2b2b33;
  display:flex;flex-direction:column;overflow:hidden}
.bar{flex:none;display:flex;align-items:center;gap:10px;padding:8px 14px;
  background:#fff;border-bottom:1px solid rgba(0,0,0,.08)}
.bar b{font-size:15px;font-weight:600}
.bar .sp{flex:1}
.btn{border:1px solid rgba(0,0,0,.12);background:#fff;border-radius:6px;
  padding:5px 12px;font-size:13px;cursor:pointer;color:#333;text-decoration:none}
.btn:hover{background:#f5f5f7}
.btn.primary{background:#c9405f;border-color:#c9405f;color:#fff}
.wrap{flex:1;display:flex;align-items:center;justify-content:center;padding:16px;min-height:0}
.wrap img{max-width:100%;max-height:100%;border-radius:10px;
  box-shadow:0 10px 34px rgba(0,0,0,.16);background:#fff}
.foot{flex:none;padding:8px 16px 12px;text-align:center;font-size:13px;color:#6b6b76}
.foot .p{background:#fff;border:1px solid rgba(0,0,0,.08);border-radius:8px;
  display:inline-block;padding:5px 14px;max-width:92%;word-break:break-all}
"""


def standalone_deck_html(qs: dict[str, list[str]]) -> str:
    """独立窗口里的幻灯片页：键盘/点击翻页，可下载 .pptx。"""
    def _q(name: str, default: str = "") -> str:
        vals = qs.get(name) or []
        return str(vals[0]) if vals else default

    topic = _q("topic", "小课堂")[:80]
    pptx = _q("file", "")
    bullets: list[str] = []
    for raw in (qs.get("b") or []):
        for piece in re.split(r"[。！？!?；;\n]", raw):
            piece = piece.strip()
            if piece:
                bullets.append(piece[:90])
    bullets = bullets[:16] or ["这条回复里没有抓到要点"]
    slides = [{"h": topic, "sub": "礼貌小狗 · 自动生成", "bullets": []}]
    for i in range(0, len(bullets), 4):
        slides.append({"h": topic, "bullets": bullets[i:i + 4]})

    pages = []
    for i, s in enumerate(slides):
        body = ("<ul>" + "".join("<li>%s</li>" % html.escape(b) for b in s["bullets"]) + "</ul>"
                if s["bullets"] else "<p class='sub'>%s</p>" % html.escape(s.get("sub") or ""))
        pages.append(
            "<section class='slide' data-i='%d' style='display:%s'>"
            "<h1>%s</h1>%s<div class='pg'>%d / %d</div></section>"
            % (i, "flex" if i == 0 else "none", html.escape(s["h"]), body, i + 1, len(slides))
        )
    dots = "".join(
        "<span class='dot%s' data-i='%d'></span>" % (" on" if i == 0 else "", i)
        for i in range(len(slides))
    )
    dl = ("<a class='btn primary' href='/ppt/%s' download='%s'>下载 .pptx</a>"
          % (urllib.parse.quote(pptx), html.escape(pptx))) if pptx else ""

    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>%s</title><style>%s</style></head><body>"
        "<div class='bar'><b>%s</b><span class='sp'></span>"
        "<button class='btn' id='prev'>‹ 上一页</button>"
        "<button class='btn' id='next'>下一页 ›</button>%s"
        "<button class='btn' onclick='window.close()'>关闭</button></div>"
        "<div class='stage'>%s</div><div class='dots' id='dots'>%s</div>"
        "<div class='hint'>← → 翻页 · Esc 关闭</div>"
        "<script>(function(){var i=0,n=%d;"
        "var S=document.querySelectorAll('.slide'),D=document.querySelectorAll('.dot');"
        "function go(k){i=Math.max(0,Math.min(n-1,k));"
        "for(var j=0;j<n;j++){S[j].style.display=(j===i?'flex':'none');"
        "D[j].classList.toggle('on',j===i);}}"
        "document.getElementById('prev').onclick=function(){go(i-1)};"
        "document.getElementById('next').onclick=function(){go(i+1)};"
        "for(var j=0;j<D.length;j++)(function(k){D[k].onclick=function(){go(k)}})(j);"
        "document.addEventListener('keydown',function(e){"
        "if(e.key==='ArrowRight'||e.key===' '||e.key==='PageDown'){e.preventDefault();go(i+1);}"
        "else if(e.key==='ArrowLeft'||e.key==='PageUp'){e.preventDefault();go(i-1);}"
        "else if(e.key==='Escape'){window.close();}});"
        "window.__go=go;})();</script></body></html>"
        % (html.escape(topic), _DECK_CSS, html.escape(topic), dl,
           "".join(pages), dots, len(slides))
    )


def standalone_shot_html(qs: dict[str, list[str]]) -> str:
    """独立窗口里的图片查看页：看图 + 提示词 + 下载/再画一张。"""
    def _q(name: str, default: str = "") -> str:
        vals = qs.get(name) or []
        return str(vals[0]) if vals else default

    src = _q("src", "")
    prompt = _q("prompt", "")[:200]
    file = _q("file", "")
    if not src:
        return ("<!doctype html><html><head><meta charset='utf-8'><title>没有图</title>"
                "<style>body{margin:0;height:100vh;display:flex;align-items:center;"
                "justify-content:center;font:16px 'Microsoft YaHei';color:#888}</style>"
                "</head><body>这张图找不着了，回聊天里再点一次「打开」试试</body></html>")

    dl = ("<a class='btn primary' href='%s' download='%s'>保存这张图</a>"
          % (html.escape(src, quote=True), html.escape(file or "puppy.png")))
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>%s</title><style>%s</style></head><body>"
        "<div class='bar'><b>它画的图</b><span class='sp'></span>%s"
        "<button class='btn' onclick='window.close()'>关闭</button></div>"
        "<div class='wrap'><img alt='' src='%s'></div>"
        "<div class='foot'>提示词：<span class='p'>%s</span></div>"
        "<script>document.addEventListener('keydown',function(e){"
        "if(e.key==='Escape')window.close();});</script>"
        "</body></html>"
        % (html.escape(prompt or "图片"), _SHOT_CSS, dl,
           html.escape(src, quote=True), html.escape(prompt))
    )


_API_CSS = """
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#f4f5f8;
  font:14px/1.7 "Microsoft YaHei","PingFang SC",sans-serif;color:#2b2b33;
  display:flex;flex-direction:column;overflow:hidden}
.bar{flex:none;display:flex;align-items:center;gap:10px;padding:8px 14px;
  background:#fff;border-bottom:1px solid rgba(0,0,0,.08);box-shadow:0 1px 6px rgba(0,0,0,.05)}
.bar b{font-size:15px;font-weight:600}
.bar .sp{flex:1}
.btn{border:1px solid rgba(0,0,0,.12);background:#fff;border-radius:6px;
  padding:5px 12px;font-size:13px;cursor:pointer;color:#333}
.btn:hover{background:#f5f5f7}
.btn.primary{background:#c9405f;border-color:#c9405f;color:#fff}
.btn.primary:hover{background:#b33652}
.btn:disabled{opacity:.45;cursor:default}
.wrap{flex:1;display:flex;flex-direction:column;min-height:0;padding:12px 14px;gap:10px;overflow:auto}
.row{display:flex;gap:8px;align-items:center;flex:none}
.row label{flex:none;width:64px;font-size:13px;color:#666}
.row input,row textarea{flex:1}
input,textarea{width:100%;border:1px solid rgba(0,0,0,.14);border-radius:6px;
  padding:7px 10px;font-size:13px;background:#fff;color:#2b2b33;font-family:inherit}
textarea{resize:vertical}
.row input{flex:1}
.status{flex:none;display:flex;gap:8px;align-items:center}
.pill{font-size:12px;padding:2px 10px;border-radius:20px;background:#e9e9ee;color:#666}
.pill.ok{background:#e5f6ec;color:#1d8a4e}
.pill.bad{background:#fdeaea;color:#c0392b}
.log{flex:1;min-height:120px;border:1px solid rgba(0,0,0,.12);border-radius:8px;
  background:#fff;padding:10px 12px;overflow:auto;white-space:pre-wrap;
  font:12px/1.6 "Consolas","Microsoft YaHei",monospace;color:#3a3a44}
.log .q{color:#c9405f;font-weight:600}
.log .a{color:#1d6f3a}
.log .e{color:#c0392b}
.log .m{color:#9a9aa4}
.hint{flex:none;text-align:center;font-size:12px;color:#9a9aa4;padding-bottom:8px}
"""


def appdata_dir() -> Path:
    override = (os.environ.get("POLITE_PUPPY_DATA") or "").strip()
    if override:
        p = Path(override).expanduser()
    else:
        base = os.environ.get("APPDATA")
        p = (Path(base) / APP_ID) if base else (Path.home() / ("." + APP_ID))
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


ROOT = bundle_dir()


def _disk_or_bundle(name: str) -> Path:
    """优先用 exe 旁边的那份，找不到才回落到打包内置的那份。

    这样改了 index.html / dog_girl.png 不用重新打包就能生效
    （PyInstaller onefile 的 ROOT 是临时解包目录，改磁盘文件原本是无效的）。
    """
    beside = exe_dir() / name
    if beside.is_file():
        return beside
    return ROOT / name


INDEX_FILE = _disk_or_bundle("index.html")
PET_IMAGE = _disk_or_bundle("dog_girl.png")
# 冻结成 exe 时 ROOT 是 PyInstaller 的临时解包目录，退出即删；
# 记忆日记必须落在 exe 旁边，否则每次启动记忆都会丢。
DIARY_FILE = (exe_dir() if getattr(sys, "frozen", False) else ROOT) / "memory_diary.txt"
DATA_DIR = appdata_dir()
LOG_FILE = DATA_DIR / "pet.log"
CONFIG_FILE = DATA_DIR / "settings.json"

# ---------------------------------------------------------------------------
# 服务器模式（部署到服务器时用；桌面端默认完全不启用）
#
#   POLITE_PUPPY_SERVER=1 / main.py --server
#       只跑本地 HTTP 服务，不开窗口、不建托盘、不注册全局热键，
#       供 IIS / Nginx / 宝塔反向代理转发。适合"把这只狗放到云上"的场景。
#
#   POLITE_PUPPY_BIND=0.0.0.0
#       监听地址。默认 127.0.0.1（只给本机看）。改成 0.0.0.0 才能被反代访问。
#
#   POLITE_PUPPY_PORT=8765
#       固定端口。默认在 8765-8795 里挑第一个空闲端口。
#
#   POLITE_PUPPY_ALLOWED_HOSTS=dog.example.com
#       额外允许的 Host / Origin（反代到域名时必须配，否则 403 forbidden host）。
#       逗号或空格分隔，可写 host 或 host:port。
# ---------------------------------------------------------------------------
def server_mode_enabled() -> bool:
    flag = (os.environ.get("POLITE_PUPPY_SERVER") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    return "--server" in sys.argv[1:]


def bind_host() -> str:
    """监听地址：默认只给本机；POLITE_PUPPY_BIND=0.0.0.0 才对外。"""
    return (os.environ.get("POLITE_PUPPY_BIND") or "127.0.0.1").strip() or "127.0.0.1"


def fixed_port() -> int:
    raw = (os.environ.get("POLITE_PUPPY_PORT") or "").strip()
    return int(raw) if raw.isdigit() else 0


def allowed_hostnames() -> set[str]:
    """允许访问本服务的主机名集合（用于 Host / Origin 校验，防 DNS rebinding）。"""
    names = {"127.0.0.1", "localhost", "::1"}
    b = bind_host().lower()
    if b and b not in ("0.0.0.0", "*"):
        names.add(b)
    extra = os.environ.get("POLITE_PUPPY_ALLOWED_HOSTS") or ""
    for item in re.split(r"[,;\s]+", extra.strip()):
        if not item:
            continue
        item = item.strip().lower()
        if item.startswith("http://") or item.startswith("https://"):
            item = urllib.parse.urlsplit(item).hostname or ""
        if item.startswith("[") and "]" in item:
            item = item[1:item.index("]")]
        elif ":" in item:
            item = item.rsplit(":", 1)[0]
        if item:
            names.add(item)
    return names


DIARY_TEMPLATE = """# 千逐的日记。一行一件事，RAG 就从这里检索。
# 写法：2025-03-11 他说月考数学考砸了，我陪他聊到十二点
# 越具体越有用。带日期的那行会按时间算权重。
2025-09-01 开学第一天，他说暑假作业一个字没写，我笑他半天
2025-09-06 他说食堂的番茄炒蛋是甜的，我说这不正常
2025-09-12 他数学小测考了 58，心情很差，我说下次考个 60 也算进步
2025-09-18 他提过他妈老拿他跟表哥比，他烦这个
2025-09-25 他熬夜打王者，第二天上课睡着了，被班主任抓住
"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def load_dotenv(path: Path) -> None:
    """极简 .env 读取，不覆盖系统里已有的环境变量。"""
    try:
        text = path.read_text("utf-8-sig")
    except OSError:
        return

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


OLLAMA_DEFAULT_BASE = "http://127.0.0.1:11434/v1"


def env_key() -> str:
    return (os.environ.get("OLLAMA_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()


def env_base_url() -> str:
    return (
        os.environ.get("OLLAMA_BASE_URL")
        or os.environ.get("DEEPSEEK_BASE_URL")
        or OLLAMA_DEFAULT_BASE
    ).strip()


def env_model() -> str:
    return (os.environ.get("OLLAMA_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "").strip()


def is_loopback(url: str) -> bool:
    """本机地址不需要 API Key，Ollama 本来就裸奔在 127.0.0.1。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def ollama_root(base_url: str) -> str:
    """http://127.0.0.1:11434/v1 -> http://127.0.0.1:11434"""
    root = (base_url or OLLAMA_DEFAULT_BASE).strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root


def list_models(base_url: str) -> list[str]:
    """优先问 Ollama 的 /api/tags，其次退回 OpenAI 风格的 /v1/models。"""
    root = ollama_root(base_url)
    names: list[str] = []

    try:
        req = urllib.request.Request(root + "/api/tags", headers={"User-Agent": "PolitePuppy/2.0"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for item in data.get("models") or []:
            name = str(item.get("name") or item.get("model") or "").strip()
            if name:
                names.append(name)
    except Exception:
        pass

    if names:
        return names

    try:
        req = urllib.request.Request(
            (base_url or OLLAMA_DEFAULT_BASE).rstrip("/") + "/models",
            headers={"User-Agent": "PolitePuppy/2.0"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for item in data.get("data") or []:
            name = str(item.get("id") or "").strip()
            if name:
                names.append(name)
    except Exception:
        pass

    return names


def resolve_model(base_url: str, model: str, strict: bool = False) -> str:
    """本机模型名写错 / 没装时，自动换成真实装着的那个。

    常见坑：UI 里切到本地了但模型名还留着云端的（比如 deepseek-flash），
    或者 .env 里写的是说明文档的 qwen2.5:7b 而本机没拉过。

    strict=True 时不替换 —— 让接口把「模型不存在」原样报出来，
    总比悄悄换成另一个模型、让人以为切换成功了要强。
    """
    model = (model or "").strip()
    if not is_loopback(base_url):
        return model

    installed = list_models(base_url)
    if not installed:
        return model or "qwen2.5:7b"
    if not model:
        return installed[0]
    if model in installed:
        return model
    if strict:
        log.warning("strict 模式：本机没有模型 %s，原样发给 Ollama（不自动替换）", model)
        return model

    lower = {name.lower(): name for name in installed}
    for cand in (model.lower(), f"{model.lower()}:latest", model.split(":")[0].lower()):
        if cand in lower:
            log.warning("本机没有模型 %s，自动改用 %s", model, lower[cand])
            return lower[cand]

    family = model.split(":")[0].lower()
    for name in installed:
        if name.lower().startswith(family):
            log.warning("本机没有模型 %s，自动改用 %s", model, name)
            return name

    log.warning("本机没有模型 %s，自动改用 %s（本机共 %d 个模型）",
                model, installed[0], len(installed))
    return installed[0]


def _ollama_body(cfg: dict[str, Any], stream: bool) -> dict[str, Any]:
    """Ollama 原生 /api/chat 的请求体。

    think=False 是关键：qwen3 / gemma4 这类思考模型如果不关思考，
    token 会被推理过程吃光，OpenAI 兼容层返回的 content 永远是空字符串。
    """
    return {
        "model": cfg["model"],
        "messages": cfg["messages"],
        "stream": stream,
        "think": False,
        "options": {
            "temperature": cfg["temperature"],
            "num_predict": cfg["max_tokens"],
        },
    }


def _ollama_open(cfg: dict[str, Any], stream: bool, timeout: float = 300.0) -> Any:
    """打原生 Ollama；万一模型/版本不认 think 字段，去掉再试一次。"""
    url = ollama_root(cfg["base_url"]) + "/api/chat"
    headers = {"Content-Type": "application/json", "User-Agent": "PolitePuppy/2.0"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    body = _ollama_body(cfg, stream)
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = _read_error_body(exc)
        if exc.code in (400, 500) and "think" in detail.lower():
            body.pop("think", None)
            req = urllib.request.Request(
                url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers, method="POST",
            )
            return urllib.request.urlopen(req, timeout=timeout)
        raise


def _openai_open(cfg: dict[str, Any], timeout: float = 300.0) -> Any:
    """云端 / 第三方 OpenAI 兼容接口。"""
    body = {
        "model": cfg["model"],
        "messages": cfg["messages"],
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
        "stream": cfg["stream"],
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if cfg["stream"] else "application/json",
        "User-Agent": "PolitePuppy/2.0",
    }
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    req = urllib.request.Request(
        f"{cfg['base_url'].rstrip('/')}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def local_cfg(messages: list[dict[str, str]], model: str = "", want_stream: bool = False,
              temperature: float = 0.85, max_tokens: int = 220) -> dict[str, Any]:
    """本机 Ollama 的一份配置（走原生接口）。"""
    base = OLLAMA_DEFAULT_BASE
    return {
        "base_url": base,
        "api_key": "",
        "model": resolve_model(base, model or env_model()),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": want_stream,
        "native": True,
    }


def chat_candidates(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """主接口 + 备用接口，按顺序尝试，任何一路通了就用它。

    分层顺序：
      1. 主接口（UI / .env 里配的那个，云端本地都行）
      2. PUPPY_BACKUP_BASE_URL 显式配的备用
      3. 主是云端 -> 兜底本机 Ollama；主是本地 -> 兜底云端（有 Key 才加）
    """
    out: list[dict[str, Any]] = [cfg]
    seen = {(cfg["base_url"].rstrip("/").lower(), cfg["model"].lower())}

    if cfg.get("strict"):
        log.info("strict 模式：只用 %s / %s，不启用备用接口", cfg["base_url"], cfg["model"])
        return out

    def add(item: dict[str, Any]) -> None:
        mark = (item["base_url"].rstrip("/").lower(), item["model"].lower())
        if mark not in seen and item["model"]:
            seen.add(mark)
            out.append(item)

    backup_url = (os.environ.get("PUPPY_BACKUP_BASE_URL") or "").strip()
    if backup_url and re.match(r"^https?://", backup_url, re.I):
        native = is_loopback(backup_url)
        add({
            "base_url": backup_url.rstrip("/"),
            "api_key": (os.environ.get("PUPPY_BACKUP_API_KEY") or "").strip(),
            "model": resolve_model(backup_url, (os.environ.get("PUPPY_BACKUP_MODEL") or "").strip()),
            "messages": cfg["messages"],
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
            "stream": cfg["stream"],
            "native": native,
        })

    if is_loopback(cfg["base_url"]):
        cloud_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        if not cloud_key and str(cfg.get("api_key") or "").startswith("sk-"):
            cloud_key = str(cfg["api_key"]).strip()
        if cloud_key:
            add({
                "base_url": (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1").strip().rstrip("/"),
                "api_key": cloud_key,
                "model": (os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat").strip(),
                "messages": cfg["messages"],
                "temperature": cfg["temperature"],
                "max_tokens": cfg["max_tokens"],
                "stream": cfg["stream"],
                "native": False,
            })
    else:
        add(local_cfg(cfg["messages"], want_stream=cfg["stream"],
                      temperature=cfg["temperature"], max_tokens=cfg["max_tokens"]))

    return out


def message_box(text: str, title: str = APP_NAME, error: bool = False) -> None:
    if server_mode_enabled():
        # 服务器上没有交互桌面，弹窗会永远挂着；改成写日志。
        log.error("[%s] %s", title, text)
        return
    try:
        flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flags)
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)


def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    log.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    try:
        handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(fmt)
        log.addHandler(handler)
    except OSError:
        pass

    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        log.addHandler(stream)

    log.info("Python %s | frozen=%s | root=%s", sys.version.split()[0], getattr(sys, "frozen", False), ROOT)


def load_config() -> dict[str, Any]:
    try:
        obj = json.loads(CONFIG_FILE.read_text("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict[str, Any]) -> None:
    """写配置。用"各自独立的临时文件 + 重试替换"，避免两个实例/杀软抢同一个 .tmp。"""
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("保存配置失败（建目录）")
        return

    data = json.dumps(cfg, ensure_ascii=False, indent=2)
    tmp = CONFIG_FILE.with_name(
        f"{CONFIG_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        tmp.write_text(data, "utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, CONFIG_FILE)
                return
            except PermissionError:
                time.sleep(0.12 * (attempt + 1))
    except OSError:
        log.debug("写临时配置失败，改直接覆盖", exc_info=True)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    # 最后兜底：直接覆盖（偶尔被占用也比丢配置强）
    try:
        CONFIG_FILE.write_text(data, "utf-8")
    except OSError:
        log.exception("保存配置失败")


# ---------------------------------------------------------------------------
# Windows 坐标 / 单实例
# ---------------------------------------------------------------------------
def work_area() -> tuple[int, int, int, int]:
    """主屏工作区（不含任务栏），逻辑像素。"""
    if sys.platform != "win32":
        return 0, 0, 1280, 720
    try:
        from ctypes import wintypes

        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        log.exception("读取工作区失败")
    return 0, 0, 1280, 720


def virtual_screen() -> tuple[int, int, int, int]:
    if sys.platform != "win32":
        return 0, 0, 1280, 720
    try:
        user32 = ctypes.windll.user32
        return (
            int(user32.GetSystemMetrics(76)),  # SM_XVIRTUALSCREEN
            int(user32.GetSystemMetrics(77)),  # SM_YVIRTUALSCREEN
            int(user32.GetSystemMetrics(78)),  # SM_CXVIRTUALSCREEN
            int(user32.GetSystemMetrics(79)),  # SM_CYVIRTUALSCREEN
        )
    except Exception:
        return 0, 0, 1280, 720


def work_area_for_window(hwnd: int) -> tuple[int, int, int, int]:
    """
    窗口所在显示器的工作区，换算成 pywebview 使用的逻辑像素。

    进程启动后 pywebview 会 SetProcessDPIAware()，Win32 返回的是物理像素，
    而 window.x / window.move() 用的是逻辑像素。150% 缩放下如果直接混用，
    聊天窗口会被算到屏幕外。这里用窗口 DPI 做一次换算。
    """
    if sys.platform != "win32" or not hwnd:
        return work_area()
    try:
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            dpi = 96
            get_dpi = getattr(user32, "GetDpiForWindow", None)
            if get_dpi is not None:
                try:
                    dpi = int(get_dpi(wintypes.HWND(hwnd))) or 96
                except Exception:
                    dpi = 96
            scale = dpi / 96.0
            rect = info.rcWork
            return (
                int(rect.left / scale),
                int(rect.top / scale),
                int(rect.right / scale),
                int(rect.bottom / scale),
            )
    except Exception:
        log.debug("读取窗口所在显示器工作区失败", exc_info=True)
    return work_area()


def display_scale(hwnd: int) -> float:
    """窗口所在显示器的缩放倍率（96 DPI = 1.0）。"""
    if sys.platform != "win32" or not hwnd:
        return 1.0
    try:
        from ctypes import wintypes

        get_dpi = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
        if get_dpi is not None:
            return (int(get_dpi(wintypes.HWND(hwnd))) or 96) / 96.0
    except Exception:
        pass
    return 1.0


def set_window_topmost(hwnd: int, enabled: bool) -> bool:
    """用 Win32 改置顶状态，避免 pywebview 的跨线程 TopMost 卡死。"""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        insert_after = wintypes.HWND(-1 if enabled else -2)  # HWND_TOPMOST / HWND_NOTOPMOST
        flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
        return bool(user32.SetWindowPos(wintypes.HWND(hwnd), insert_after, 0, 0, 0, 0, flags))
    except Exception:
        log.debug("设置窗口置顶失败", exc_info=True)
        return False


AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "PolitePuppyPet"


def autostart_command() -> str:
    """写入注册表 Run 键的启动命令。"""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'

    script = Path(__file__).resolve()
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{script}"'


def is_autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    """开机自启（只写 HKCU，不需要管理员）。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        log.info("开机自启：%s", "开" if enabled else "关")
        return True
    except OSError:
        log.exception("设置开机自启失败")
        return False


_mutex_handle: Any = None


def another_instance_running() -> bool:
    """Windows 命名互斥体，防止重复启动。"""
    if sys.platform != "win32":
        return False
    global _mutex_handle
    try:
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, "Local\\PolitePuppyPet_SingleInstance")
        if not handle:
            return False
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return True
        _mutex_handle = handle
        return False
    except Exception:
        return False


# 运行中的 PetApp 实例（main() 里赋值），供 /show 路由唤回立绘用
_PET_APP: "PetApp | None" = None


def probe_running_pet() -> "dict[str, Any] | None":
    """探测已经在跑的实例：先把它的立绘唤回来，再问它的版本和位置。

    返回 {port, version, exePath, pid}；没有实例在跑就返回 None。
    分开成两个函数是为了不破坏 summon_running_pet() 原来的 bool 契约。
    """
    for port in range(8765, 8795):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/show", timeout=1.2
            ) as resp:
                if resp.status != 200:
                    continue
        except Exception:
            continue
        info: dict[str, Any] = {"port": port, "version": "", "exePath": "", "pid": 0}
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1.2
            ) as r2:
                j = json.loads(r2.read().decode("utf-8", "replace") or "{}")
                info["version"] = str(j.get("version") or "")
                info["exePath"] = str(j.get("exePath") or "")
                try:
                    info["pid"] = int(j.get("pid") or 0)
                except (TypeError, ValueError):
                    info["pid"] = 0
        except Exception:
            log.debug("探测运行中实例的版本失败", exc_info=True)
        return info
    return None


def summon_running_pet() -> bool:
    """已经有本尊在跑时，让运行中的实例把立绘窗口亮出来。

    旧版重复启动只会弹一句"去托盘里找它"——立绘一藏用户就找不着了，
    现在直接通知运行中的实例 show 立绘，用户一启动就能看到它回来。
    """
    return probe_running_pet() is not None


# ===========================================================================
# 记忆库：本地 SQLite（只用标准库 sqlite3，不装包、不联网、不上传）
#
# 四张表：episodes（情景）/ facts（事实）/ relations（关系）/ meta（元信息），
# episodes 上挂 FTS5 trigram 全文索引，靠 episodes_ai / _ad / _au 三个触发器同步。
#
# ⚠ 已知限制（必须带着）：trigram 按 3 字滑窗切词，1~2 个汉字的查询词
#   （"难受""错题"）FTS 会静默命中 0 条，所以检索一律按查询词长度分流：
#   >= 3 字走 MATCH，1~2 字走 LIKE '%词%'。两条路都过滤 deleted = 0。
# ===========================================================================
MEMORY_DIR = (exe_dir() if getattr(sys, "frozen", False) else ROOT) / "记忆"
MEMORY_DB = MEMORY_DIR / "memory.db"
# 建表脚本的磁盘位置：优先 exe 旁边那份，其次项目根；都不在就用下面的内嵌副本
MEMORY_SCHEMA_PATHS = (
    exe_dir() / "文档" / "memory_schema.sql",
    ROOT / "文档" / "memory_schema.sql",
)
FTS_MIN_LEN = 3       # 查询词少于这个长度就走 LIKE
MEMORY_KINDS = ("episode", "fact", "relation")
# 前端筛选框可能传中文，这里做一次别名映射
MEMORY_TYPE_ALIAS = {
    "": "all", "all": "all", "全部": "all",
    "episode": "episode", "episodes": "episode", "情景": "episode",
    "fact": "fact", "facts": "fact", "事实": "fact",
    "relation": "relation", "relations": "relation", "关系": "relation",
}

# 内嵌副本：与 文档\memory_schema.sql 完全一致，打包成 exe 后找不到脚本时兜底。
# 改动 schema 请两边一起改。
MEMORY_SCHEMA_FALLBACK = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  day TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  kind TEXT NOT NULL DEFAULT 'chat',
  text TEXT NOT NULL,
  mood TEXT,
  importance REAL NOT NULL DEFAULT 0.5,
  source TEXT NOT NULL DEFAULT 'desktop',
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
);
CREATE INDEX IF NOT EXISTS idx_episodes_day  ON episodes(day);
CREATE INDEX IF NOT EXISTS idx_episodes_ts   ON episodes(ts);
CREATE INDEX IF NOT EXISTS idx_episodes_live ON episodes(deleted, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
  text, content='episodes', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
  INSERT INTO episodes_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO episodes_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.6,
  evidence TEXT,
  updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
  deleted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(key, value)
);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
CREATE TABLE IF NOT EXISTS relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  title TEXT,
  detail TEXT,
  ts INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
  status TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_rel_kind ON relations(kind, status);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
INSERT INTO meta(k, v) VALUES ('schema_version', '1')
  ON CONFLICT(k) DO UPDATE SET v = excluded.v;
INSERT INTO meta(k, v) VALUES ('migrated_from_localstorage', '0')
  ON CONFLICT(k) DO NOTHING;
"""

MEMORY_BACKUP_DIR = MEMORY_DIR / "backup"
MEMORY_BACKUP_KEEP = 7          # 只留最近 7 天的备份

_MEM_INITED = False
_MEM_LOCK = threading.Lock()
_MEM_BACKUP_DAY = ""
_MEM_EXT: Any = None
_MEM_EXT_TRIED = False


def _mem_extractor():
    """延迟 import 抽取器（tools\\memory_extract.py）。

    找不到就返回 None——抽取只是锦上添花，没它聊天照常，
    所以这里失败只记一条日志，绝不影响主流程。
    """
    global _MEM_EXT, _MEM_EXT_TRIED
    if _MEM_EXT is not None:
        return _MEM_EXT
    if _MEM_EXT_TRIED:
        return None
    _MEM_EXT_TRIED = True
    try:
        tools = (exe_dir() if getattr(sys, "frozen", False) else ROOT) / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        import memory_extract  # type: ignore
        _MEM_EXT = memory_extract
        log.info("记忆抽取器已加载：%s", tools)
    except Exception as exc:
        log.warning("记忆抽取器没装上（不影响聊天）：%s", exc)
        _MEM_EXT = None
    return _MEM_EXT


def _mem_snapshot_to(dst: Path) -> bool:
    """把当前库完整复制一份。

    ⚠ 不能直接 copy memory.db：库是 WAL 模式，最近提交的改动可能还躺在
    memory.db-wal 里，只拷主文件会拷到一份旧数据。这里用 sqlite3 自带的
    backup 接口，它会先把 WAL 落盘再拷，拷出来才是完整的。
    """
    try:
        src = sqlite3.connect(str(MEMORY_DB), timeout=15)
    except sqlite3.Error as exc:
        log.warning("备份打不开源库：%s", exc)
        return False
    try:
        out = sqlite3.connect(str(dst), timeout=15)
        try:
            src.backup(out)
            return True
        finally:
            out.close()
    except sqlite3.Error as exc:
        log.warning("备份写失败：%s", exc)
        return False
    finally:
        src.close()


def _mem_daily_backup() -> str | None:
    """每天第一次打开库时，把 memory.db 复制一份到 记忆\\backup\\memory-YYYYMMDD.db。

    只在当天第一次调用时真的复制；超出 7 份的老备份自动删掉。
    """
    global _MEM_BACKUP_DAY
    today = time.strftime("%Y%m%d")
    if _MEM_BACKUP_DAY == today:
        return None
    _MEM_BACKUP_DAY = today
    try:
        if not MEMORY_DB.is_file():
            return None
        # 备份目录跟着 MEMORY_DIR 走（测试会把 MEMORY_DIR 指到临时目录）
        bdir = MEMORY_DIR / "backup"
        bdir.mkdir(parents=True, exist_ok=True)
        dst = bdir / ("memory-%s.db" % today)
        if not dst.exists():
            if not _mem_snapshot_to(dst):
                return None
            log.info("记忆库每日备份：%s（%d 字节）" % (dst.name, dst.stat().st_size))
        olds = sorted(bdir.glob("memory-*.db"))
        for f in olds[:-MEMORY_BACKUP_KEEP]:
            try:
                f.unlink()
            except OSError:
                pass
        return str(dst)
    except OSError as exc:
        log.warning("记忆库每日备份失败：%s", exc)
        return None


def _mem_schema_sql() -> str:
    """取建表脚本：优先磁盘那份（好改），找不到才用内嵌副本。"""
    for p in MEMORY_SCHEMA_PATHS:
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8")
        except OSError:
            continue
    return MEMORY_SCHEMA_FALLBACK


def _mem_open() -> sqlite3.Connection:
    """打开记忆库；首次调用自动建表（脚本幂等，可重复执行）。"""
    global _MEM_INITED
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEMORY_DB), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not _MEM_INITED:
        with _MEM_LOCK:
            if not _MEM_INITED:
                conn.executescript(_mem_schema_sql())
                conn.commit()
                _MEM_INITED = True
                try:
                    _mem_daily_backup()      # 每天首次打开库时顺手备份一份
                except Exception as exc:
                    log.warning("每日备份异常：%s", exc)
    return conn


def _mem_day_of(ts: int) -> str:
    """毫秒时间戳 -> YYYY-MM-DD（本地时区）。"""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts) / 1000))
    except (TypeError, ValueError, OSError):
        return time.strftime("%Y-%m-%d")


def _fts_phrase(q: str) -> str:
    """把用户输入包成 FTS5 短语。

    不包的话，括号、引号、冒号这些会被 FTS5 当语法，直接抛
    "fts5: syntax error"。实测 `考试 (数学)` 裸传报错，加引号后正常命中。
    """
    return '"' + str(q).replace('"', '""') + '"'


def _mem_item(row: dict[str, Any], kind: str) -> dict[str, Any]:
    """三张表的行统一成一个前端能直接渲染的形状。"""
    if kind == "episode":
        return {
            "id": row["id"], "type": "episode", "ts": row["ts"], "day": row["day"],
            "role": row["role"], "kind": row["kind"], "text": row["text"],
            "mood": row["mood"] or "", "importance": row["importance"],
            "source": row["source"],
        }
    if kind == "fact":
        return {
            "id": row["id"], "type": "fact", "ts": row["updated_at"], "day": _mem_day_of(row["updated_at"]),
            "role": "user", "kind": "fact",
            "text": f'{row["key"]}：{row["value"]}',
            "mood": "", "importance": row["confidence"], "source": "desktop",
            "confidence": row["confidence"],
        }
    return {
        "id": row["id"], "type": "relation", "ts": row["ts"], "day": _mem_day_of(row["ts"]),
        "role": "system", "kind": "relation",
        "text": (row["title"] or row["detail"] or ""),
        "mood": "", "importance": 0.5, "source": "desktop",
        "status": row["status"],
    }


# 单字兜底时跳过的字：太常见，留着会把整库都捞出来
MEM_STOP_CHARS = set("的了我在他是你就都也很吗啊吧呢不有个们那上下来到过还得着对说这")


def _mem_bigrams(q: str) -> list[str]:
    """把一句话切成 2 字滑窗，作为兜底检索的词。

    为什么需要它：trigram 要求查询串**整段连续出现**，用户说一整句
    （"我今天好难受啊"）时，库里根本没有这句话，FTS 直接 0 命中。
    切成 2 字片段再按"命中了几个片段"排序，就能做到沾边就捞出来。
    """
    t = re.sub(r"\s+", "", str(q or ""))
    if not t:
        return []
    if len(t) < 3:
        return [t]
    out, seen = [], set()
    for i in range(len(t) - 1):
        g = t[i:i + 2]
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out[:12]


def _mem_fuzzy_episodes(conn: sqlite3.Connection, q: str,
                        limit: int) -> list[dict[str, Any]]:
    """2 字片段兜底检索：命中片段越多的排越前。"""
    terms = _mem_bigrams(q)
    if not terms:
        return []
    where = " OR ".join(["text LIKE ?"] * len(terms))
    sql = ("SELECT * FROM episodes WHERE deleted = 0 AND (%s) "
           "ORDER BY ts DESC LIMIT ?" % where)
    rows = [dict(r) for r in conn.execute(
        sql, ["%" + t + "%" for t in terms] + [max(limit * 6, 60)])]

    def score(r: dict[str, Any]) -> int:
        txt = r.get("text") or ""
        return sum(1 for t in terms if t in txt)

    rows = [r for r in rows if score(r) > 0]
    rows.sort(key=lambda r: (score(r), r.get("ts") or 0), reverse=True)
    if rows:
        return rows[:limit]

    # 2 字片段一个都没沾上 → 再退一级：按单字找（去掉"的了我在他"这种没信息量的字），
    # 至少要撞上 2 个不同的字才认，避免一个"的"字把整库捞出来。
    uni = [c for c in re.sub(r"\s+", "", str(q or "")) if c not in MEM_STOP_CHARS]
    uni = list(dict.fromkeys(uni))[:12]
    if len(uni) < 2:
        return []
    where = " OR ".join(["text LIKE ?"] * len(uni))
    sql = ("SELECT * FROM episodes WHERE deleted = 0 AND (%s) "
           "ORDER BY ts DESC LIMIT ?" % where)
    cand = [dict(r) for r in conn.execute(
        sql, ["%" + c + "%" for c in uni] + [max(limit * 8, 80)])]

    def uscore(r: dict[str, Any]) -> int:
        txt = r.get("text") or ""
        return sum(1 for c in uni if c in txt)

    cand = [r for r in cand if uscore(r) >= 2]
    cand.sort(key=lambda r: (uscore(r), r.get("ts") or 0), reverse=True)
    return cand[:limit]


def _mem_count_fuzzy(conn: sqlite3.Connection, q: str) -> int:
    terms = _mem_bigrams(q)
    if not terms:
        return 0
    where = " OR ".join(["text LIKE ?"] * len(terms))
    return int(conn.execute(
        "SELECT count(*) FROM episodes WHERE deleted = 0 AND (%s)" % where,
        ["%" + t + "%" for t in terms]).fetchone()[0])


def _mem_rows_episodes(conn: sqlite3.Connection, q: str, limit: int) -> list[dict[str, Any]]:
    """情景检索：>=3 字先走 FTS；FTS 没命中（说的是整句）或报语法错，
    就退到 2 字片段的模糊检索。1~2 字直接 LIKE。"""
    if len(q) >= FTS_MIN_LEN:
        try:
            sql = ("SELECT e.* FROM episodes e JOIN episodes_fts f ON f.rowid = e.id "
                   "WHERE e.deleted = 0 AND episodes_fts MATCH ? "
                   "ORDER BY e.ts DESC LIMIT ?")
            rows = [dict(r) for r in conn.execute(sql, (_fts_phrase(q), limit))]
            if rows:
                return rows
        except sqlite3.OperationalError as exc:
            log.warning("记忆 FTS 检索失败，回落模糊检索：%s", exc)
        return _mem_fuzzy_episodes(conn, q, limit)
    sql = ("SELECT * FROM episodes WHERE deleted = 0 AND text LIKE ? "
           "ORDER BY ts DESC LIMIT ?")
    return [dict(r) for r in conn.execute(sql, ("%" + q + "%", limit))]


def _mem_count_episodes(conn: sqlite3.Connection, q: str) -> int:
    if len(q) >= FTS_MIN_LEN:
        try:
            sql = ("SELECT count(*) FROM episodes e JOIN episodes_fts f ON f.rowid = e.id "
                   "WHERE e.deleted = 0 AND episodes_fts MATCH ?")
            n = int(conn.execute(sql, (_fts_phrase(q),)).fetchone()[0])
            if n:
                return n
        except sqlite3.OperationalError:
            pass
        return _mem_count_fuzzy(conn, q)
    return int(conn.execute(
        "SELECT count(*) FROM episodes WHERE deleted = 0 AND text LIKE ?",
        ("%" + q + "%",)).fetchone()[0])


def _mem_rows_facts(conn: sqlite3.Connection, q: str, limit: int) -> list[dict[str, Any]]:
    like = "%" + q + "%"
    return [dict(r) for r in conn.execute(
        "SELECT * FROM facts WHERE deleted = 0 AND (key LIKE ? OR value LIKE ?) "
        "ORDER BY updated_at DESC LIMIT ?", (like, like, limit))]


def _mem_count_facts(conn: sqlite3.Connection, q: str) -> int:
    like = "%" + q + "%"
    return int(conn.execute(
        "SELECT count(*) FROM facts WHERE deleted = 0 AND (key LIKE ? OR value LIKE ?)",
        (like, like)).fetchone()[0])


def _mem_rows_relations(conn: sqlite3.Connection, q: str, limit: int) -> list[dict[str, Any]]:
    like = "%" + q + "%"
    return [dict(r) for r in conn.execute(
        "SELECT * FROM relations WHERE (title LIKE ? OR detail LIKE ?) "
        "ORDER BY ts DESC LIMIT ?", (like, like, limit))]


def _mem_count_relations(conn: sqlite3.Connection, q: str) -> int:
    like = "%" + q + "%"
    return int(conn.execute(
        "SELECT count(*) FROM relations WHERE (title LIKE ? OR detail LIKE ?)",
        (like, like)).fetchone()[0])


def _mem_search(q: str, mtype: str = "", limit: int = 50) -> dict[str, Any]:
    """统一检索入口，返回 {ok, total, items, q, type}。"""
    q = (q or "").strip()
    want = MEMORY_TYPE_ALIAS.get(str(mtype or "").strip().lower(), "all")
    kinds = MEMORY_KINDS if want == "all" else (want,)

    conn = _mem_open()
    try:
        items: list[dict[str, Any]] = []
        total = 0
        for k in kinds:
            if k == "episode":
                rows = _mem_rows_episodes(conn, q, limit)
                total += _mem_count_episodes(conn, q)
            elif k == "fact":
                rows = _mem_rows_facts(conn, q, limit)
                total += _mem_count_facts(conn, q)
            else:
                rows = _mem_rows_relations(conn, q, limit)
                total += _mem_count_relations(conn, q)
            items.extend(_mem_item(r, k) for r in rows)
        # 多表合并后按时间倒序；每类各取了 limit 条，合并截断不会漏掉真正在前面的
        items.sort(key=lambda it: it.get("ts") or 0, reverse=True)
        return {"ok": True, "total": total, "items": items[:limit],
                "q": q, "type": want}
    finally:
        conn.close()


def _mem_stats() -> dict[str, Any]:
    conn = _mem_open()
    try:
        ep = int(conn.execute("SELECT count(*) FROM episodes WHERE deleted=0").fetchone()[0])
        fa = int(conn.execute("SELECT count(*) FROM facts WHERE deleted=0").fetchone()[0])
        rel = int(conn.execute("SELECT count(*) FROM relations").fetchone()[0])
        last = conn.execute(
            "SELECT max(m) FROM ("
            "  SELECT max(created_at) AS m FROM episodes"
            "  UNION ALL SELECT max(updated_at) FROM facts"
            "  UNION ALL SELECT max(ts) FROM relations)").fetchone()[0]
        return {"ok": True, "episodes": ep, "facts": fa, "relations": rel,
                "last_write": int(last or 0), "db": str(MEMORY_DB)}
    finally:
        conn.close()


def _mem_add(payload: dict[str, Any]) -> dict[str, Any]:
    """单条写入：type = episode / fact / relation。"""
    mtype = MEMORY_TYPE_ALIAS.get(str(payload.get("type") or "episode").strip().lower(), "episode")
    conn = _mem_open()
    try:
        if mtype == "episode":
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "message": "缺 text"}
            ts = int(payload.get("ts") or 0) or int(time.time() * 1000)
            day = str(payload.get("day") or "").strip() or _mem_day_of(ts)
            try:
                imp = float(payload.get("importance", 0.5))
            except (TypeError, ValueError):
                imp = 0.5
            cur = conn.execute(
                "INSERT INTO episodes(ts, day, role, kind, text, mood, importance, source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ts, day,
                 str(payload.get("role") or "user")[:16],
                 str(payload.get("kind") or "chat")[:16],
                 text[:4000],
                 str(payload.get("mood") or "")[:32] or None,
                 max(0.0, min(1.0, imp)),
                 str(payload.get("source") or "desktop")[:16]))
            conn.commit()
            return {"ok": True, "id": cur.lastrowid, "type": "episode"}

        if mtype == "fact":
            key = str(payload.get("key") or "").strip()
            value = str(payload.get("value") or payload.get("text") or "").strip()
            if not key or not value:
                return {"ok": False, "message": "缺 key / value"}
            try:
                conf = float(payload.get("confidence", 1.0))
            except (TypeError, ValueError):
                conf = 1.0
            cur = conn.execute(
                "INSERT INTO facts(key, value, confidence, evidence, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(key, value) DO UPDATE SET "
                " confidence=excluded.confidence, evidence=excluded.evidence,"
                " updated_at=excluded.updated_at, deleted=0",
                (key[:64], value[:500], max(0.0, min(1.0, conf)),
                 str(payload.get("evidence") or "")[:500] or None,
                 int(payload.get("ts") or 0) or int(time.time() * 1000)))
            conn.commit()
            return {"ok": True, "id": cur.lastrowid, "type": "fact"}

        title = str(payload.get("title") or payload.get("text") or "").strip()
        if not title:
            return {"ok": False, "message": "缺 title"}
        cur = conn.execute(
            "INSERT INTO relations(kind, title, detail, ts, status) VALUES (?,?,?,?,?)",
            (str(payload.get("kind") or "shared_event")[:32], title[:200],
             str(payload.get("detail") or "")[:2000] or None,
             int(payload.get("ts") or 0) or int(time.time() * 1000),
             str(payload.get("status") or "open")[:16]))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid, "type": "relation"}
    finally:
        conn.close()


def _mem_import(payload: dict[str, Any]) -> dict[str, Any]:
    """批量导入（幂等：同 text+ts 的重复项直接跳过，不产生第二行）。

    mode：
      merge（默认）—— 库里已有的（含已软删的）一律跳过，删过的不复活；
      replace      —— 先把现有数据整批软删（不物理删），再把这一批原样写进去。
    """
    mode = str(payload.get("mode") or "merge").strip().lower()
    if mode not in ("merge", "replace"):
        mode = "merge"
    # replace 模式下旧数据已经整体软删了，去重只看还活着的；merge 模式要连
    # 软删的一起算，免得"删掉的又被导回来"
    live_only = (mode == "replace")
    raw = payload.get("items")
    if not isinstance(raw, list):
        raw = []
        for key, mtype in (("episodes", "episode"), ("facts", "fact"), ("relations", "relation")):
            part = payload.get(key)
            if isinstance(part, list):
                for it in part:
                    if isinstance(it, dict):
                        row = dict(it)
                        row.setdefault("type", mtype)
                        raw.append(row)

    conn = _mem_open()
    inserted = {"episode": 0, "fact": 0, "relation": 0}
    skipped = {"episode": 0, "fact": 0, "relation": 0}
    errors: list[str] = []
    cleared: dict[str, int] = {}
    try:
        conn.execute("BEGIN")
        if live_only:
            cleared["episodes"] = conn.execute(
                "UPDATE episodes SET deleted=1 WHERE deleted=0").rowcount
            cleared["facts"] = conn.execute(
                "UPDATE facts SET deleted=1 WHERE deleted=0").rowcount
            cleared["relations"] = conn.execute(
                "UPDATE relations SET status='dropped' WHERE status<>'dropped'").rowcount
        for it in raw:
            if not isinstance(it, dict):
                continue
            mtype = MEMORY_TYPE_ALIAS.get(str(it.get("type") or "episode").strip().lower(), "episode")
            try:
                if mtype == "episode":
                    text = str(it.get("text") or "").strip()
                    if not text:
                        skipped["episode"] += 1
                        continue
                    ts = int(it.get("ts") or 0) or int(time.time() * 1000)
                    day = str(it.get("day") or "").strip() or _mem_day_of(ts)
                    # 幂等键：text + ts（merge 模式连软删的一起算，删过的不复活）
                    if conn.execute(
                            "SELECT 1 FROM episodes WHERE ts=? AND text=?"
                            + ("" if live_only else " AND deleted=0")
                            + " LIMIT 1",
                            (ts, text[:4000])).fetchone():
                        skipped["episode"] += 1
                        continue
                    try:
                        imp = float(it.get("importance", 0.5))
                    except (TypeError, ValueError):
                        imp = 0.5
                    conn.execute(
                        "INSERT INTO episodes(ts, day, role, kind, text, mood, importance, source) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (ts, day, str(it.get("role") or "user")[:16],
                         str(it.get("kind") or "chat")[:16], text[:4000],
                         str(it.get("mood") or "")[:32] or None,
                         max(0.0, min(1.0, imp)),
                         str(it.get("source") or "desktop")[:16]))
                    inserted["episode"] += 1

                elif mtype == "fact":
                    key = str(it.get("key") or "").strip()
                    value = str(it.get("value") or it.get("text") or "").strip()
                    if not key or not value:
                        skipped["fact"] += 1
                        continue
                    if conn.execute("SELECT 1 FROM facts WHERE key=? AND value=? LIMIT 1",
                                    (key[:64], value[:500])).fetchone():
                        skipped["fact"] += 1
                        continue
                    try:
                        conf = float(it.get("confidence", 1.0))
                    except (TypeError, ValueError):
                        conf = 1.0
                    conn.execute(
                        "INSERT INTO facts(key, value, confidence, evidence, updated_at) "
                        "VALUES (?,?,?,?,?)",
                        (key[:64], value[:500], max(0.0, min(1.0, conf)),
                         str(it.get("evidence") or "")[:500] or None,
                         int(it.get("ts") or 0) or int(time.time() * 1000)))
                    inserted["fact"] += 1

                else:
                    title = str(it.get("title") or it.get("text") or "").strip()
                    if not title:
                        skipped["relation"] += 1
                        continue
                    kind = str(it.get("kind") or "shared_event")[:32]
                    detail = str(it.get("detail") or "")[:2000] or None
                    if conn.execute(
                            "SELECT 1 FROM relations WHERE kind=? AND title=? "
                            "AND IFNULL(detail,'')=IFNULL(?, '') LIMIT 1",
                            (kind, title[:200], detail)).fetchone():
                        skipped["relation"] += 1
                        continue
                    conn.execute(
                        "INSERT INTO relations(kind, title, detail, ts, status) VALUES (?,?,?,?,?)",
                        (kind, title[:200], detail,
                         int(it.get("ts") or 0) or int(time.time() * 1000),
                         str(it.get("status") or "open")[:16]))
                    inserted["relation"] += 1
            except Exception as exc:      # 单条出错不拖垮整批
                errors.append(str(exc))
                skipped[mtype] += 1
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"ok": False, "message": str(exc), "inserted": inserted,
                "skipped": skipped}
    finally:
        conn.close()

    return {
        "ok": True,
        "mode": mode,
        "cleared": cleared,
        "inserted": inserted,
        "skipped": skipped,
        "inserted_total": sum(inserted.values()),
        "skipped_total": sum(skipped.values()),
        "errors": errors[:5],
    }


def _mem_patch(mid: int, mtype: str, payload: dict[str, Any]) -> dict[str, Any]:
    """改一条记忆（报告里接口草案的 PATCH /api/memory/{id}）。

    - episode：改 text / mood / importance
    - fact：改 value / confidence
    - relation：改 title / detail / status
    改 text 会触发 episodes_au 触发器，全文索引跟着更新，旧词立刻查不到。
    """
    mtype = MEMORY_TYPE_ALIAS.get(str(mtype or "episode").strip().lower(), "episode")
    conn = _mem_open()
    try:
        if mtype == "episode":
            row = conn.execute(
                "SELECT text, mood, importance FROM episodes WHERE id=? AND deleted=0",
                (mid,)).fetchone()
            if row is None:
                return {"ok": False, "message": f"没找到 id={mid} 的情景（或已删除）", "id": mid}
            text = str(payload.get("text") or row["text"]).strip()[:4000]
            if not text:
                return {"ok": False, "message": "text 不能为空", "id": mid}
            mood = str(payload.get("mood", row["mood"] or ""))[:32] or None
            try:
                imp = float(payload.get("importance", row["importance"]))
            except (TypeError, ValueError):
                imp = float(row["importance"])
            conn.execute(
                "UPDATE episodes SET text=?, mood=?, importance=? WHERE id=?",
                (text, mood, max(0.0, min(1.0, imp)), mid))
            conn.commit()
            return {"ok": True, "id": mid, "type": "episode", "text": text}

        if mtype == "fact":
            row = conn.execute(
                "SELECT key, value, confidence FROM facts WHERE id=? AND deleted=0",
                (mid,)).fetchone()
            if row is None:
                return {"ok": False, "message": f"没找到 id={mid} 的事实（或已删除）", "id": mid}
            value = str(payload.get("value") or payload.get("text") or row["value"]).strip()[:500]
            if not value:
                return {"ok": False, "message": "value 不能为空", "id": mid}
            try:
                conf = float(payload.get("confidence", row["confidence"]))
            except (TypeError, ValueError):
                conf = float(row["confidence"])
            conn.execute(
                "UPDATE facts SET value=?, confidence=?, updated_at=? WHERE id=?",
                (value, max(0.0, min(1.0, conf)), int(time.time() * 1000), mid))
            conn.commit()
            return {"ok": True, "id": mid, "type": "fact",
                    "key": row["key"], "value": value}

        row = conn.execute(
            "SELECT title, detail, status FROM relations WHERE id=?", (mid,)).fetchone()
        if row is None:
            return {"ok": False, "message": f"没找到 id={mid} 的关系", "id": mid}
        title = str(payload.get("title") or payload.get("text") or row["title"] or "").strip()[:200]
        detail = str(payload.get("detail", row["detail"] or ""))[:2000] or None
        status = str(payload.get("status") or row["status"])[:16]
        conn.execute("UPDATE relations SET title=?, detail=?, status=? WHERE id=?",
                     (title, detail, status, mid))
        conn.commit()
        return {"ok": True, "id": mid, "type": "relation", "title": title}
    finally:
        conn.close()


def _mem_clear(mtype: str = "") -> dict[str, Any]:
    """按类型整类清空——只做软删除，任何一条都不物理删。"""
    want = MEMORY_TYPE_ALIAS.get(str(mtype or "all").strip().lower(), "all")
    conn = _mem_open()
    try:
        out: dict[str, Any] = {"ok": True, "type": want}
        if want in ("all", "episode"):
            cur = conn.execute("UPDATE episodes SET deleted=1 WHERE deleted=0")
            out["episodes"] = cur.rowcount
        if want in ("all", "fact"):
            cur = conn.execute("UPDATE facts SET deleted=1 WHERE deleted=0")
            out["facts"] = cur.rowcount
        if want in ("all", "relation"):
            # relations 表没有 deleted 列，用 status='dropped' 表示已作废
            cur = conn.execute("UPDATE relations SET status='dropped' WHERE status<>'dropped'")
            out["relations"] = cur.rowcount
        conn.commit()
        return out
    finally:
        conn.close()


def _mem_snapshot(raw: Any) -> dict[str, Any]:
    """迁移前把 localStorage 的原始 JSON 原样另存一份（报告 2.6 / 7.2 要求）。

    落盘到 记忆\\localStorage-snapshot-YYYYMMDD_HHMMSS.json，只写本地，
    不发任何外部请求。
    """
    text = str(raw if raw is not None else "").strip()
    if len(text) < 2:
        return {"ok": False, "message": "没给内容"}
    if not text.lstrip().startswith(("{", "[")):
        text = json.dumps(text, ensure_ascii=False)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    dst = MEMORY_DIR / ("localStorage-snapshot-%s.json"
                        % time.strftime("%Y%m%d_%H%M%S"))
    try:
        dst.write_text(text, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "message": f"写快照失败：{exc}"}
    return {"ok": True, "file": str(dst), "bytes": dst.stat().st_size}


def _mem_extract(payload: dict[str, Any]) -> dict[str, Any]:
    """一句话 → 原始情景（必存）+ 事实（能抽到就存）。

    抽取器超时/没装上都不影响：原始情景照样入库，绝不阻塞回复。
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "message": "缺 text"}

    # 1) 原始情景先落库（这条一定要存下来，抽取只是锦上添花）
    ts = int(payload.get("ts") or 0) or int(time.time() * 1000)
    ep = _mem_add({
        "type": "episode", "text": text, "ts": ts,
        "day": str(payload.get("day") or "").strip() or _mem_day_of(ts),
        "role": str(payload.get("role") or "user")[:16],
        "kind": str(payload.get("kind") or "chat")[:16],
        "mood": str(payload.get("mood") or "")[:32],
        "source": str(payload.get("source") or "desktop")[:16],
    })
    if not ep.get("ok"):
        return ep

    # 2) 抽事实（规则优先；规则没命中且开了 useModel 才问小模型，1.5s 超时）
    ext = _mem_extractor()
    items: list[dict[str, Any]] = []
    store: dict[str, Any] = {"inserted": 0, "merged": 0, "ids": []}
    if ext is not None:
        try:
            items = ext.extract(text, use_model=bool(payload.get("useModel")),
                                base=payload.get("baseUrl"), model=payload.get("model"))
            conn = _mem_open()
            try:
                store = ext.merge_facts(conn, items, evidence=str(ep.get("id") or ""))
            finally:
                conn.close()
        except Exception as exc:
            log.warning("记忆抽取失败（原始情景已存）：%s", exc)

    return {"ok": True, "episode_id": ep.get("id"), "items": items, "store": store}


def _mem_export(fmt: str = "json") -> dict[str, Any]:
    """导出全部记忆：format=json 给机器，format=md 给人看。"""
    fmt = (fmt or "json").strip().lower()
    conn = _mem_open()
    try:
        eps = [dict(r) for r in conn.execute(
            "SELECT id, ts, day, role, kind, text, mood, importance, source "
            "FROM episodes WHERE deleted=0 ORDER BY ts")]
        facts = [dict(r) for r in conn.execute(
            "SELECT id, key, value, confidence, evidence, updated_at "
            "FROM facts WHERE deleted=0 ORDER BY updated_at DESC")]
        rels = [dict(r) for r in conn.execute(
            "SELECT id, kind, title, detail, ts, status FROM relations ORDER BY ts DESC")]
    finally:
        conn.close()

    if fmt == "md":
        out = ["# 礼貌小狗 · 记忆导出",
               "",
               "导出时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
               "情景 %d 条 · 事实 %d 条 · 关系 %d 条" % (len(eps), len(facts), len(rels)),
               ""]
        if facts:
            out.append("## 关于他的事实")
            for f in facts:
                out.append("- %s：%s（置信度 %.1f）" % (f["key"], f["value"], f["confidence"]))
            out.append("")
        if rels:
            out.append("## 关系")
            for r in rels:
                out.append("- [%s] %s（%s）" % (r["kind"], r["title"] or "", r["status"]))
            out.append("")
        out.append("## 情景（按时间）")
        for e in eps:
            d = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"] / 1000))
            out.append("- %s [%s] %s" % (d, e["kind"], e["text"]))
        return {"ok": True, "format": "md", "text": "\n".join(out),
                "episodes": len(eps), "facts": len(facts), "relations": len(rels)}

    return {"ok": True, "format": "json",
            "exported_at": int(time.time() * 1000),
            "episodes": eps, "facts": facts, "relations": rels,
            "count": {"episodes": len(eps), "facts": len(facts), "relations": len(rels)}}


def _mem_delete(mid: int, mtype: str = "episode") -> dict[str, Any]:
    """软删除：episode / fact 置 deleted=1；relation 没有 deleted 列，置 status='dropped'。"""
    mtype = MEMORY_TYPE_ALIAS.get(str(mtype or "episode").strip().lower(), "episode")
    conn = _mem_open()
    try:
        if mtype == "episode":
            cur = conn.execute("UPDATE episodes SET deleted=1 WHERE id=? AND deleted=0", (mid,))
        elif mtype == "fact":
            cur = conn.execute("UPDATE facts SET deleted=1 WHERE id=? AND deleted=0", (mid,))
        else:
            cur = conn.execute("UPDATE relations SET status='dropped' WHERE id=?", (mid,))
        conn.commit()
        if cur.rowcount <= 0:
            return {"ok": False, "message": f"没找到 id={mid} 的{mtype}（或已删除）",
                    "id": mid, "type": mtype}
        return {"ok": True, "id": mid, "type": mtype, "deleted": 1}
    finally:
        conn.close()


# ===========================================================================
# Agent：可插拔工具 + 安全层 + 人格层
#
# 设计依据：《桌宠Agent_可插拔架构设计》
#   Persona（人格层）→ 只管"怎么说"：语气、口癖、表情、收尾
#   Safety （安全层）→ 只管"做不做"：风险分级、确认、审计、回滚
#   人格永远无权否决执行；它只影响话术。
#
# 这层是**懒加载**的：桌宠本体照常启动，第一次调 /api/agent/* 时才把
# puppy_agent 装起来。插件装不上也只是 Agent 功能不可用，不影响聊天/记忆。
# ===========================================================================
AGENT_STATE: dict[str, Any] = {
    "ready": False,
    "error": "",
    "runtime": None,
    "persona": {},
    "booted": False,
}
AGENT_LOCK = threading.Lock()


def _agent_persona_path() -> Path:
    """人格配置文件：优先 exe 旁边那份（可改），其次项目根。"""
    for p in (exe_dir() / "persona.json", ROOT / "persona.json"):
        if p.is_file():
            return p
    return ROOT / "persona.json"


def _agent_user_plugin_dir() -> Path:
    """用户自己放插件的目录：exe 旁边的 plugins\\（源码运行时就是项目根下那份）。"""
    d = exe_dir() / "plugins"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass          # 建不出来也不该让桌宠挂掉，扫描时跳过就行
    return d


def _agent_build() -> dict[str, Any]:
    """真正装配 Agent 运行时。返回 {ok, error, ...}。"""
    if puppy_agent_boot is None:
        return {"ok": False, "error": "引导模块没加载：%s" % (_AGENT_BOOT_ERROR or "未知")}
    info = puppy_agent_boot.boot()
    if not info.get("ok"):
        return {"ok": False, "error": info.get("error") or "引导失败"}

    from puppy_agent.core.runtime import AgentRuntime
    from puppy_agent.core.safety import SafetyGate
    from puppy_agent.core.personality import load_persona, persona_prompt

    # 人格：读 persona.json（读不到就用默认人设，不能因此炸掉）
    persona = load_persona(_agent_persona_path())

    # 安全层的数据目录：放在 exe 旁边的 agent_data\，审计日志和快照都在这
    agent_data = (exe_dir() if getattr(sys, "frozen", False) else ROOT) / "agent_data"
    agent_data.mkdir(parents=True, exist_ok=True)
    gate = SafetyGate(agent_data)

    # 插件目录：包内自带的 + 用户自己放的
    dirs = puppy_agent_boot.plugin_dirs([_agent_user_plugin_dir()])
    runtime = AgentRuntime(dirs[0], agent_data, gate, persona,
                            extra_dirs=dirs[1:],
                            # 把桌宠的真实记忆库告诉插件，否则它会自己找
                            # agent_data\记忆\memory.db 找不到而报错
                            config={"memory_db": str(MEMORY_DB),
                                    "memory_dir": str(MEMORY_DIR)})
    runtime.load_all()

    AGENT_STATE.update({
        "ready": True,
        "error": "",
        "runtime": runtime,
        "persona": persona,
        "persona_prompt": persona_prompt(persona),
        "booted": True,
        "plugin_dirs": [str(d) for d in dirs],
    })
    return {"ok": True, "error": "", "tools": len(runtime.registry.list())}


def agent_runtime() -> Any:
    """拿到 Agent 运行时（没有就现场装一次）。失败返回 None。"""
    if AGENT_STATE.get("ready") and AGENT_STATE.get("runtime") is not None:
        return AGENT_STATE["runtime"]
    with AGENT_LOCK:
        if AGENT_STATE.get("ready") and AGENT_STATE.get("runtime") is not None:
            return AGENT_STATE["runtime"]
        try:
            res = _agent_build()
        except Exception as exc:
            log.exception("Agent 运行时装配失败")
            AGENT_STATE.update({"ready": False, "error": str(exc),
                                "booted": True})
            return None
        if not res.get("ok"):
            log.warning("Agent 不可用：%s", res.get("error"))
            AGENT_STATE.update({"ready": False, "error": res.get("error", ""),
                                "booted": True})
            return None
        return AGENT_STATE.get("runtime")


def _agent_describe() -> dict[str, Any]:
    """Agent 总览：核心版本、插件、工具清单（带风险等级）。"""
    rt = agent_runtime()
    if rt is None:
        return {"ok": False, "message": AGENT_STATE.get("error") or "Agent 不可用",
                "tools": [], "plugins": [], "counts": {}}
    try:
        data = rt.describe()
    except Exception as exc:
        log.exception("Agent 状态读取失败")
        return {"ok": False, "message": str(exc), "tools": [], "plugins": []}
    data["ok"] = True
    data["persona_prompt"] = AGENT_STATE.get("persona_prompt", "")
    data["persona_name"] = (AGENT_STATE.get("persona", {}).get("identity", {}) or {}).get("name", "")
    data["plugin_dirs"] = AGENT_STATE.get("plugin_dirs", [])
    data["audit_file"] = str(rt.safety.audit_file)
    data["trash_dir"] = str(rt.safety.trash_dir)
    data["persona_path"] = str(_agent_persona_path())
    return data


def _agent_persona() -> dict[str, Any]:
    """人设卡：给设置页看，也给前端拼 systemPrompt 用。"""
    rt = agent_runtime()
    persona = AGENT_STATE.get("persona", {}) or {}
    out = {
        "ok": bool(persona),
        "persona": persona,
        "prompt": AGENT_STATE.get("persona_prompt", ""),
        "block": _persona_data_block(),
        "path": str(_agent_persona_path()),
        "error": AGENT_STATE.get("error", ""),
    }
    if rt is not None:
        try:
            out["style_hint"] = _agent_style_hint_from(rt, "")
        except Exception:
            pass
    return out


_PERSONA_CACHE: dict[str, Any] = {"path": "", "mtime": 0.0, "data": None}


def _persona_data_block() -> str:
    """
    读 persona.json，取"数据驱动"的那部分（口癖统计、情绪标签）。

    这一段是**纯个性化增强**：基础提示词已经写了身份和语气，这里补的是
    从 7695 条真实发言里统计出来的东西，让模型照她的真实习惯说话。
    读不到就返回空串，绝不影响聊天。
    """
    path = _agent_persona_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    if _PERSONA_CACHE["path"] != str(path) or _PERSONA_CACHE["mtime"] != mtime:
        try:
            from puppy_agent.core.personality import load_persona, persona_data_block
            data = load_persona(path)
            _PERSONA_CACHE.update({"path": str(path), "mtime": mtime,
                                   "data": (data, persona_data_block(data))})
        except Exception:
            return ""
    entry = _PERSONA_CACHE.get("data")
    if not entry:
        return ""
    return entry[1]


def _agent_style_hint_from(rt: Any, text: str) -> dict[str, Any]:
    from puppy_agent.core.personality import style_hint
    return style_hint(text, AGENT_STATE.get("persona", {}) or {})


def _agent_call(payload: dict[str, Any]) -> dict[str, Any]:
    """调用一个工具。确认令牌由前端在用户确认后带回来。"""
    rt = agent_runtime()
    if rt is None:
        return {"ok": False, "message": AGENT_STATE.get("error") or "Agent 不可用"}
    name = str(payload.get("tool") or payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "message": "缺 tool 名字"}
    args = payload.get("args")
    if not isinstance(args, dict):
        args = {}
    token = str(payload.get("confirm_token") or "")
    try:
        return rt.call(name, args, token)
    except Exception as exc:
        log.exception("Agent 工具调用失败：%s", name)
        return {"ok": False, "message": str(exc)}


def _agent_audit(limit: int = 50) -> dict[str, Any]:
    """最近若干条审计记录。"""
    rt = agent_runtime()
    if rt is None:
        return {"ok": False, "message": "Agent 不可用", "items": []}
    try:
        items = rt.safety.audit_tail(max(1, min(int(limit or 50), 500)))
    except Exception as exc:
        return {"ok": False, "message": str(exc), "items": []}
    return {"ok": True, "items": items, "count": len(items),
            "file": str(rt.safety.audit_file)}


def _agent_snapshots() -> dict[str, Any]:
    """快照列表（删过东西的，可以一键还原）。"""
    rt = agent_runtime()
    if rt is None:
        return {"ok": False, "message": "Agent 不可用", "items": []}
    try:
        items = rt.safety.snapshot_list()
    except Exception as exc:
        return {"ok": False, "message": str(exc), "items": []}
    return {"ok": True, "items": items, "count": len(items),
            "trash_dir": str(rt.safety.trash_dir)}


def _agent_restore(payload: dict[str, Any]) -> dict[str, Any]:
    """从快照还原一个文件/目录。"""
    rt = agent_runtime()
    if rt is None:
        return {"ok": False, "message": "Agent 不可用"}
    sid = str(payload.get("snapshot_id") or "").strip()
    if not sid:
        return {"ok": False, "message": "缺 snapshot_id"}
    try:
        res = rt.safety.restore(sid)
    except Exception as exc:
        log.exception("快照还原失败")
        return {"ok": False, "message": str(exc)}
    rt.safety.audit("restore", {"snapshot_id": sid}, "high",
                    bool(res.get("ok")), str(res.get("message") or res.get("restored") or ""))
    return res


def _agent_save_persona(payload: dict[str, Any]) -> dict[str, Any]:
    """保存人格配置（前端改完存回 persona.json）。写之前自动备份。"""
    persona = payload.get("persona")
    if not isinstance(persona, dict) or not persona:
        return {"ok": False, "message": "缺 persona 内容"}
    try:
        from puppy_agent.core.personality import (load_persona,
                                                  persona_data_block,
                                                  persona_prompt, save_persona)
    except Exception as exc:
        return {"ok": False, "message": "人格模块没加载：%s" % exc}
    path = _agent_persona_path()
    res = save_persona(path, persona)
    if not res.get("ok"):
        return res
    # 存完立刻刷新缓存，下一次对话就用新的人设
    try:
        AGENT_STATE["persona"] = load_persona(path)
        AGENT_STATE["persona_prompt"] = persona_prompt(AGENT_STATE["persona"])
        _PERSONA_CACHE.update({"path": "", "mtime": 0.0, "data": None})
    except Exception:
        pass
    return {"ok": True, "message": res.get("message", "已保存"),
            "path": res.get("path"), "backup": res.get("backup", "")}


def _agent_reload() -> dict[str, Any]:
    """重新扫描并加载插件（改了插件 .py 不用重启桌宠）。"""
    rt = agent_runtime()
    if rt is None:
        return {"ok": False, "message": "Agent 不可用"}
    try:
        # 先把旧工具卸掉，再重新扫一遍目录
        for pid in list(rt.registry.plugins.keys()):
            rt.unload_plugin(pid)
        rt.log.clear()
        stats = rt.load_all()
    except Exception as exc:
        log.exception("插件重载失败")
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "已重新加载", "stats": stats,
            "tools": len(rt.registry.list())}


# ---------------------------------------------------------------------------
# P10 · 桌宠侧接口接线：自主调用编排
#
# 目标：聊天时她能自己判断"这句话该不该动手"，不用人再去设置页点一下。
# 边界（这条是红线）：**自动规划只做只读与查询**——fs_read / fs_list /
#   mem_search / mem_stats。写、删、外发（fs_write / fs_delete / mem_forget /
#   msg_send）永不自动规划，必须人显式发起并过确认令牌。
#   理由：本机小模型判断不稳，工具一旦误触发是真的会删文件、真的会发消息。
# 依据：《AGENT_P5.md》§5「工具自主调用循环：还没做成她自己判断该用哪个工具」。
# ---------------------------------------------------------------------------
#: 允许由规则自动规划的工具（全部只读）
AGENT_AUTO_TOOLS = ("fs_read", "fs_list", "mem_search", "mem_stats")

_AG_FILE_EXT = r"(?:txt|md|json|jsonl|log|py|html?|css|js|csv|tsv|ini|cfg|bat|cmd|ps1|ya?ml|sql)"

#: "你还记得…吗" 里要剥掉的套话，剩下的才算搜索词
_AG_MEM_STRIP = re.compile(
    r"你还记得|你记不记得|还记得|帮我回忆|回忆一下|回想一下|搜一下记忆|"
    r"查一下记忆|记忆里有没有|记忆库里有没有|记得|吗|呢|吧|啊|呀")


def _agent_bump() -> int:
    """状态版本号 +1。前端拿它判断"要不要刷新"，不用一直拉整份 describe。"""
    AGENT_STATE["version"] = int(AGENT_STATE.get("version") or 0) + 1
    return AGENT_STATE["version"]


def _agent_plan(text: str) -> dict[str, Any]:
    """
    一句话 → 该不该动手（规则路由，确定性、可离线、可单测）。

    故意做得很保守：**只认明确的读文件 / 列目录 / 问记忆**。
    模棱两可的闲聊一律返回空工具，宁可不做，也不要在聊天里误触发。
    """
    t = str(text or "").strip()
    if not t:
        return {"tool": "", "args": {}, "reason": "空文本"}
    if len(t) > 200:
        return {"tool": "", "args": {}, "reason": "太长，按闲聊处理"}

    # 1) 读文件：必须出现带扩展名的文件名，才认为是要读文件
    m = re.search(
        r"(?:读一下|读读|读|打开|看看|看一下|瞅瞅|翻翻)\s*[「『\"']?"
        r"([^\s「」『』\"'，。]+?\.(?:" + _AG_FILE_EXT + r"))", t, re.I)
    if m:
        return {"tool": "fs_read", "args": {"path": m.group(1)},
                "reason": "要读文件"}

    # 2) 列目录：认"列一下 / 有哪些文件 / 目录里有什么"
    if re.search(r"列一下|列个|列出|有哪些文件|有什么文件|目录里有什么|"
                 r"文件夹里有什么|看看目录|列目录|目录下有什么", t):
        mp = re.search(r"([A-Za-z]:[\\/][^\s，。、]*|[^\s，。、]*\\[^\s，。、]+)", t)
        return {"tool": "fs_list", "args": {"path": mp.group(1) if mp else "."},
                "reason": "要看目录"}

    # 3) 记忆统计
    if re.search(r"记忆库里?有多|记忆库有多|多少条记忆|记住多少|记了多少|"
                 r"记忆库状态|记忆统计|存了多少条", t):
        return {"tool": "mem_stats", "args": {}, "reason": "问记忆库规模"}

    # 4) 记忆搜索（放最后：前面几条都可能带"记得"字样）
    if re.search(r"你还记得|你记不记得|还记得|帮我回忆|回忆一下|回想一下|"
                 r"搜一下记忆|查一下记忆|记忆里有没有|记忆库里有没有", t):
        kw = _AG_MEM_STRIP.sub(" ", t)
        kw = re.sub(r"[\s？?。！!，,、；;：:「」『』\"']+", " ", kw).strip()
        return {"tool": "mem_search", "args": {"q": kw[:30] or t[:20]},
                "reason": "要翻记忆库"}

    return {"tool": "", "args": {}, "reason": "不需要动手"}


def _ag_hint(tool: str, res: dict[str, Any]) -> str:
    """把工具结果压成一句人话（塞 prompt 或直接显示）。失败也给人话。"""
    if not isinstance(res, dict):
        return "工具返回格式不对"
    if not res.get("ok"):
        return "想用 %s 没成：%s" % (tool, res.get("message") or "原因不明")

    if tool == "fs_read":
        body = str(res.get("content") or "").replace("\r", "")
        head = " ".join(body.split())[:180]
        name = Path(str(res.get("path") or "")).name
        return "读到《%s》了（共 %s 字）%s" % (
            name, res.get("chars") or 0, ("，开头是：" + head) if head else "，是空的")

    if tool == "fs_list":
        items = res.get("items") or []
        names = [str((it or {}).get("name") or "") for it in items]
        names = [n for n in names if n][:20]
        p = res.get("path") or ""
        return "%s 下有 %s 项：%s" % (p, res.get("count") or len(items),
                                      "、".join(names) if names else "（空目录）")

    if tool == "mem_search":
        items = res.get("items") or []
        if not items:
            return "记忆库里没搜到「%s」" % (res.get("query") or "")
        texts = [" ".join(str((it or {}).get("text") or "").split())[:60]
                 for it in items[:3]]
        return "记忆库里搜到 %s 条（搜的是「%s」），例如：%s" % (
            res.get("count") or len(items), res.get("query") or "",
            " / ".join(x for x in texts if x))

    if tool == "mem_stats":
        st = res.get("stats") or {}
        return "记忆库现在有情景 %s 条、事实 %s 条、关系 %s 条" % (
            st.get("episodes", 0), st.get("facts", 0), st.get("relations", 0))

    return str(res.get("message") or res.get("result") or "做完了")[:180]


def _agent_act(payload: dict[str, Any]) -> dict[str, Any]:
    """
    桌宠侧自主调用入口：给一句话，判断要不要动手，动手就把结果压成一句人话。

    返回统一结构（前端只认 ok / used / hint / need_confirm）：
      ok           本次请求本身成不成（Agent 不可用 → False，前端静默降级）
      agent        Agent 运行时是否在线
      used         [{tool, risk, ok, args, summary, ms, need_confirm, confirm_token}]
      hint         可以拼进 prompt 或直接显示的一句结果
      need_confirm 有动作在等用户确认（高风险）
      version      状态版本，前端据此刷新 Agent 页
    """
    rt = agent_runtime()
    if rt is None:
        return {"ok": False, "agent": False, "used": [], "hint": "",
                "need_confirm": False, "route": "", "reason": "",
                "message": AGENT_STATE.get("error") or "Agent 不可用",
                "version": int(AGENT_STATE.get("version") or 0)}

    text = str(payload.get("text") or "").strip()
    tool = str(payload.get("tool") or "").strip()
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    token = str(payload.get("confirm_token") or "")

    route = "explicit" if tool else "rule"
    reason = "显式指定"
    if not tool:
        plan = _agent_plan(text)
        tool = str(plan.get("tool") or "")
        args = plan.get("args") or {}
        reason = str(plan.get("reason") or "")

    if not tool:
        return {"ok": True, "agent": True, "used": [], "hint": "",
                "need_confirm": False, "route": route, "reason": reason,
                "tool": "", "args": {}, "message": "",
                "version": int(AGENT_STATE.get("version") or 0)}

    spec = rt.registry.get(tool)
    risk = spec.risk.value if spec is not None else "?"

    try:
        res = rt.call(tool, args, token)
    except Exception as exc:
        log.exception("Agent 自主调用失败：%s", tool)
        res = {"ok": False, "message": str(exc)}
    if not isinstance(res, dict):
        res = {"ok": False, "message": "工具返回格式不对"}

    item: dict[str, Any] = {
        "tool": tool,
        "risk": str(res.get("risk") or risk),
        "ok": bool(res.get("ok")),
        "args": args,
        "summary": _ag_hint(tool, res),
        "ms": res.get("ms") or 0,
    }
    need = bool(res.get("need_confirm"))
    if need:
        item["need_confirm"] = True
        item["confirm_token"] = str(res.get("confirm_token") or "")
    if res.get("snapshot_id"):
        item["snapshot_id"] = res.get("snapshot_id")

    return {
        "ok": True, "agent": True, "used": [item],
        "hint": item["summary"],
        "need_confirm": need,
        "confirm_token": item.get("confirm_token", ""),
        "route": route, "reason": reason,
        "tool": tool, "args": args, "message": "",
        "version": _agent_bump(),
    }


def _agent_state() -> dict[str, Any]:
    """
    轻量状态：给前端做同步用。**不触发**运行时装配（没起来就报没起来）。
    前端拿 version 比对，变了才去拉整份 describe。
    """
    rt = AGENT_STATE.get("runtime")
    ready = bool(AGENT_STATE.get("ready")) and rt is not None
    out: dict[str, Any] = {
        "ok": True,
        "ready": ready,
        "booted": bool(AGENT_STATE.get("booted")),
        "error": "" if ready else str(AGENT_STATE.get("error") or ""),
        "version": int(AGENT_STATE.get("version") or 0),
        "tools": 0, "plugins": 0, "pending_confirm": 0,
    }
    if ready:
        try:
            out["tools"] = len(rt.registry.list())
            out["plugins"] = len(rt.registry.plugins)
            out["pending_confirm"] = int(rt.safety.pending_count())
            out["audit_lines"] = _agent_audit_line_count()
        except Exception:
            pass
    return out


def _agent_audit_line_count() -> int:
    """审计日志现有多少行（前端判断"有没有新动作"用，不读内容）。"""
    rt = AGENT_STATE.get("runtime")
    if rt is None:
        return 0
    try:
        return sum(1 for _ in open(rt.safety.audit_file, "r", encoding="utf-8"))
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# 本地 HTTP 服务：静态文件 + DeepSeek 流式代理
# ---------------------------------------------------------------------------
class _LocalHTTPServer(ThreadingHTTPServer):
    """前端页面偶尔会主动断开连接（关窗口/刷新），别把 ConnectionReset 打成异常栈。"""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            log.debug("客户端断开：%s", client_address)
            return
        super().handle_error(request, client_address)
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PolitePuppy/1.0"
    sys_version = ""
    timeout = 30

    # ---- 基础响应 ----
    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send_bytes(self, data: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404, "Not Found")
            return
        self._send_bytes(data, content_type)

    # ---- 防跨站 / DNS rebinding ----
    def _request_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = int(self.server.server_port)
        # 反向代理到域名时 Host 可能是 "dog.example.com"（不含端口），
        # 这里按"主机名"比对，避免 8765:PORT 的写法把域名请求全挡掉。
        try:
            hostname = (urllib.parse.urlsplit("//" + host).hostname or "").lower()
        except ValueError:
            hostname = ""
        allowed = allowed_hostnames()
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"} and hostname not in allowed:
            self._send_json(403, {"ok": False, "message": "forbidden host"})
            return False

        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            try:
                origin_host = urllib.parse.urlsplit(origin).hostname or ""
            except ValueError:
                origin_host = ""
            if origin_host.lower() != hostname:
                self._send_json(403, {"ok": False, "message": "forbidden origin"})
                return False

        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            self._send_json(403, {"ok": False, "message": "forbidden"})
            return False
        return True

    def _path_only(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    # ---- GET ----
    def do_GET(self) -> None:  # noqa: N802
        if not self._request_allowed():
            return

        path = self._path_only()
        if path == "/api/health":
            base = env_base_url()
            found = list_models(base)
            self._send_json(200, {
                "ok": bool(found),
                "provider": "ollama",
                "baseUrl": base,
                "model": env_model() or (found[0] if found else ""),
                "models": found,
                "hasKey": True,
                "diary": DIARY_FILE.exists(),
                # 版本 / 位置：重复启动时用来判断"跑的是不是旧版"，前端也显示版本号
                "version": APP_VERSION,
                "exePath": str(exe_dir()),
                "pid": os.getpid(),
                "frozen": bool(getattr(sys, "frozen", False)),
                "ts": int(time.time() * 1000),
            })
            return

        if path == "/api/models":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            base = (qs.get("baseUrl") or [env_base_url()])[0]
            found = list_models(base)
            self._send_json(200, {"ok": bool(found), "models": found, "baseUrl": base})
            return

        if path == "/api/ping":
            # 连通性测试：本地问 Ollama 的模型列表；云端带上 Key 请求 /models，
            # 把 HTTP 状态（401=Key 无效等）原样带回，页面才能给出准确提示。
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            base = str((qs.get("baseUrl") or [env_base_url()])[0]).strip()
            key = str((qs.get("key") or [""])[0]).strip() or env_key()
            found = list_models(base)
            status = 0
            if not found and not is_loopback(base):
                try:
                    headers = {"User-Agent": "PolitePuppy/2.0"}
                    if key:
                        headers["Authorization"] = f"Bearer {key}"
                    req = urllib.request.Request(base.rstrip("/") + "/models", headers=headers)
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8", "replace"))
                    for item in data.get("data") or []:
                        name = str(item.get("id") or "").strip()
                        if name:
                            found.append(name)
                except urllib.error.HTTPError as exc:
                    status = exc.code
                except Exception:
                    status = -1
            self._send_json(200, {
                "ok": bool(found), "models": found[:40], "baseUrl": base,
                "status": status,
                "message": "" if found else (f"HTTP {status}" if status else "没列出模型"),
            })
            return

        if path == "/api/diary":
            self._send_json(200, {"ok": True, "text": _read_diary(), "file": str(DIARY_FILE)})
            return

        # ---- 记忆库：检索 / 统计 ----
        if path == "/api/memory":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            q = str((qs.get("q") or [""])[0]).strip()[:200]
            mtype = str((qs.get("type") or [""])[0]).strip()[:16]
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            limit = max(1, min(limit, 500))
            try:
                self._send_json(200, _mem_search(q, mtype, limit))
            except Exception as exc:
                log.exception("记忆检索失败")
                self._send_json(200, {"ok": False, "message": str(exc),
                                      "total": 0, "items": []})
            return

        if path == "/api/memory/export":
            # 导出全部记忆：format=json（默认）给机器，format=md 给人看
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            fmt = str((qs.get("format") or ["json"])[0]).strip()[:8]
            try:
                self._send_json(200, _mem_export(fmt))
            except Exception as exc:
                log.exception("记忆导出失败")
                self._send_json(200, {"ok": False, "message": str(exc)})
            return

        if path == "/api/memory/stats":
            try:
                self._send_json(200, _mem_stats())
            except Exception as exc:
                log.exception("记忆统计失败")
                self._send_json(200, {"ok": False, "message": str(exc),
                                      "episodes": 0, "facts": 0, "relations": 0,
                                      "last_write": 0})
            return

        if path == "/api/stickers":
            self._send_json(200, {"ok": True, "dir": str(sticker_dir()),
                                  "stickers": self._list_builtin_stickers()})
            return

        if path == "/api/brain":
            try:
                import local_brain
                self._send_json(200, local_brain.status())
            except Exception as exc:
                self._send_json(200, {"ok": False, "error": str(exc)})
            return

        if path == "/api/voice/diag":
            try:
                data = voice_diag()
                data["ok"] = True
            except Exception as exc:
                log.exception("语音自检失败")
                data = {"ok": False, "message": str(exc)}
            self._send_json(200, data)
            return

        if path == "/api/voices":
            try:
                self._send_json(200, list_voice_profiles())
            except Exception as exc:
                self._send_json(200, {"ok": False, "message": str(exc), "voices": []})
            return

        # 声线身份证：小狗自己那条声线的持久化 ID + 音色指纹校验（实时同步，改档案即刻生效）
        if path == "/api/voice/id":
            try:
                import sys as _sys
                _tools = str(tools_dir())
                if _tools not in _sys.path:
                    _sys.path.insert(0, _tools)
                import voice_identity
                ident = voice_identity.sync_now()
                chk = voice_identity.verify()
                self._send_json(200, {
                    "ok": bool(ident.get("ok")),
                    "voice_id": ident.get("voice_id") or "",
                    "owner": ident.get("owner") or "",
                    "display": ident.get("display") or "",
                    "profile_id": ident.get("profile_id") or "",
                    "ref": ident.get("ref") or "",
                    "params": ident.get("params") or {},
                    "locked": bool(ident.get("locked")),
                    "updated_at": ident.get("updated_at") or 0,
                    "drift": bool(chk.get("drift")),
                    "message": chk.get("message") or ident.get("message") or "",
                })
            except Exception as exc:
                self._send_json(200, {"ok": False, "message": str(exc), "voice_id": ""})
            return

        if path == "/api/daylog":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            day = str((qs.get("date") or [""])[0])
            try:
                self._send_json(200, read_day_log(day))
            except Exception as exc:
                self._send_json(200, {"ok": False, "message": str(exc), "items": []})
            return

        if path == "/api/tools":
            # 电脑控制：权限配置 + 工具清单 + 最近审计（给设置面板看）
            self._send_json(200, tool_state())
            return

        if path == "/api/tool/schema":
            # 机器可读的工具契约：名字/风险级/参数 schema/调用方式，给后续脚本或 LLM 用
            self._send_json(200, tool_schema())
            return

        # ---- Agent：可插拔工具 / 人格 / 审计 / 快照 ----
        if path == "/api/agent/describe":
            try:
                self._send_json(200, _agent_describe())
            except Exception as exc:
                log.exception("Agent 状态读取失败")
                self._send_json(200, {"ok": False, "message": str(exc),
                                      "tools": [], "plugins": []})
            return

        if path == "/api/agent/persona":
            try:
                self._send_json(200, _agent_persona())
            except Exception as exc:
                log.exception("人设读取失败")
                self._send_json(200, {"ok": False, "message": str(exc),
                                      "persona": {}, "prompt": ""})
            return

        if path == "/api/agent/audit":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            try:
                self._send_json(200, _agent_audit(limit))
            except Exception as exc:
                log.exception("审计读取失败")
                self._send_json(200, {"ok": False, "message": str(exc), "items": []})
            return

        if path == "/api/agent/snapshots":
            try:
                self._send_json(200, _agent_snapshots())
            except Exception as exc:
                log.exception("快照列表读取失败")
                self._send_json(200, {"ok": False, "message": str(exc), "items": []})
            return

        if path == "/api/agent/persona/raw":
            # 人格配置原文（前端编辑用）
            try:
                text = _agent_persona_path().read_text(encoding="utf-8")
            except OSError as exc:
                self._send_json(200, {"ok": False, "message": str(exc), "text": ""})
                return
            self._send_json(200, {"ok": True, "text": text,
                                  "path": str(_agent_persona_path())})
            return

        if path == "/api/agent/state":
            # 轻量状态同步：不装配运行时，只看现状 + 版本号
            try:
                self._send_json(200, _agent_state())
            except Exception as exc:
                self._send_json(200, {"ok": False, "ready": False,
                                      "error": str(exc), "version": 0})
            return

        if path == "/show":
            # 单实例唤回：重复启动程序时，让运行中的实例把立绘亮出来
            app = globals().get("_PET_APP")
            if app is not None:
                self._send_json(200, app.restore_pet())
            else:
                self._send_json(503, {"ok": False, "message": "not ready"})
            return

        if path.startswith("/uploads/"):
            self._send_upload(urllib.parse.unquote(path[len("/uploads/"):]))
            return

        if path.startswith("/stickers/"):
            self._send_builtin_sticker(urllib.parse.unquote(path[len("/stickers/"):]))
            return

        if path.startswith("/ppt/"):
            self._send_ppt(urllib.parse.unquote(path[len("/ppt/"):]))
            return

        if path.startswith("/img/"):
            self._send_image(urllib.parse.unquote(path[len("/img/"):]))
            return

        if path == "/deck":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._send_bytes(standalone_deck_html(qs).encode("utf-8"),
                             "text/html; charset=utf-8")
            return

        if path == "/shot":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._send_bytes(standalone_shot_html(qs).encode("utf-8"),
                             "text/html; charset=utf-8")
            return

        if path in ("/", "/index.html"):
            self._send_file(INDEX_FILE, "text/html; charset=utf-8")
            return
        if path == "/memory_diary.txt":
            self._send_bytes(_read_diary().encode("utf-8"), "text/plain; charset=utf-8")
            return
        if path == "/pet":
            # 立绘页也从本地 HTTP 加载，保证它和 dog_girl.png 同源，
            # 不会被下面的 Sec-Fetch-Site 防护误伤。
            self._send_bytes(build_pet_html("").encode("utf-8"), "text/html; charset=utf-8")
            return
        if path in ("/dog_girl.png", "/favicon.ico"):
            self._send_file(PET_IMAGE, "image/png")
            return
        if path == "/voice/chat":
            # 对话音源（非系统 TTS）：页面里的 <audio> 和 Python 播的是同一份文件
            target = resolve_voice_file()
            if target is None:
                self.send_error(404, "Not Found")
                return
            ctype = VOICE_AUDIO_MIME.get(target.suffix.lower(), "application/octet-stream")
            self._send_file(target, ctype)
            return

        self.send_error(404, "Not Found")

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._request_allowed():
            return
        path = self._path_only()
        if path == "/pet":
            data = build_pet_html("").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        targets = {
            "/": (INDEX_FILE, "text/html; charset=utf-8"),
            "/index.html": (INDEX_FILE, "text/html; charset=utf-8"),
            "/dog_girl.png": (PET_IMAGE, "image/png"),
            "/favicon.ico": (PET_IMAGE, "image/png"),
        }
        if path not in targets:
            self.send_error(404, "Not Found")
            return
        file, ctype = targets[path]
        try:
            size = file.stat().st_size
        except OSError:
            self.send_error(404, "Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def do_DELETE(self) -> None:  # noqa: N802
        """记忆软删除：DELETE /api/memory/{id}?type=episode|fact|relation。"""
        if not self._request_allowed():
            return
        path = self._path_only()
        if not path.startswith("/api/memory/"):
            self.send_error(404, "Not Found")
            return
        tail = path[len("/api/memory/"):].strip("/")
        if not tail:
            self._send_json(400, {"ok": False, "message": "缺 id"})
            return
        try:
            mid = int(tail.split("/")[0])
        except ValueError:
            self._send_json(400, {"ok": False, "message": f"id 不是数字：{tail}"})
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        mtype = str((qs.get("type") or ["episode"])[0]).strip()[:16]
        try:
            self._send_json(200, _mem_delete(mid, mtype))
        except Exception as exc:
            log.exception("记忆删除失败")
            self._send_json(200, {"ok": False, "message": str(exc), "id": mid})

    def do_PATCH(self) -> None:  # noqa: N802
        """改一条记忆：PATCH /api/memory/{id}?type=episode|fact|relation。"""
        if not self._request_allowed():
            return
        path = self._path_only()
        if not path.startswith("/api/memory/"):
            self.send_error(404, "Not Found")
            return
        tail = path[len("/api/memory/"):].strip("/")
        try:
            mid = int(tail.split("/")[0])
        except ValueError:
            self._send_json(400, {"ok": False, "message": f"id 不是数字：{tail}"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4 * 1024 * 1024:
            self._send_json(400, {"ok": False, "message": "请求体为空或过大"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "message": "不是合法 JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "message": "请求格式不对"})
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        mtype = str((qs.get("type") or ["episode"])[0]).strip()[:16]
        try:
            self._send_json(200, _mem_patch(mid, mtype, payload))
        except Exception as exc:
            log.exception("记忆修改失败")
            self._send_json(200, {"ok": False, "message": str(exc), "id": mid})

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._request_allowed():
            return
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- POST ----
    def do_POST(self) -> None:  # noqa: N802
        if not self._request_allowed():
            return
        route = self._path_only()
        if route not in ("/api/chat", "/api/diary", "/api/ppt", "/api/draw",
                         "/api/daylog", "/api/brain", "/api/upload",
                         "/api/speak", "/api/voice/start", "/api/voice/stop",
                         "/api/voice/poll",
                         "/api/tool", "/api/tools/config", "/api/tools/stop",
                         "/api/tools/clear",
                         "/api/memory", "/api/memory/import", "/api/memory/extract",
                         "/api/memory/snapshot", "/api/memory/clear",
                         "/api/agent/call", "/api/agent/restore",
                         "/api/agent/act",
                         "/api/agent/reload", "/api/agent/persona/save"):
            self.send_error(404, "Not Found")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        # 图片/文件走 base64，体积天然比文本大得多，单独放宽到 24MB
        max_body = 24 * 1024 * 1024 if route == "/api/upload" else 4 * 1024 * 1024
        if length <= 0 or length > max_body:
            self._send_json(400, {"ok": False, "message": "请求体为空或过大"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "message": "不是合法 JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "message": "请求格式不对"})
            return

        if route == "/api/ppt":
            self._handle_ppt(payload)
            return

        if route == "/api/draw":
            self._handle_draw(payload)
            return

        if route == "/api/diary":
            self._handle_diary_write(payload)
            return

        if route == "/api/upload":
            self._handle_upload(payload)
            return

        if route == "/api/brain":
            self._handle_brain_cmd(payload)
            return

        if route == "/api/daylog":
            text = payload.get("text")
            if not isinstance(text, str):
                self._send_json(400, {"ok": False, "message": "缺 text"})
                return
            src = str(payload.get("source") or "manual")[:12]
            self._send_json(200, append_day_log(text, src))
            return

        # 语音输出 / 语音输入（HTTP 兜底：独立窗没有 pywebview 桥、或外部工具调用时用）
        if route in ("/api/speak", "/api/voice/start", "/api/voice/stop", "/api/voice/poll"):
            app = _PET_APP
            if app is None:
                self._send_json(503, {"ok": False, "message": "应用还没启动完"})
                return
            if route == "/api/speak":
                self._send_json(200, app.speech.speak(
                    str(payload.get("text") or ""),
                    str(payload.get("intent") or ""),
                    str(payload.get("mode") or ""),
                ))
            elif route == "/api/voice/start":
                self._send_json(200, app.voice.start(str(payload.get("lang") or "zh-CN")))
            elif route == "/api/voice/stop":
                self._send_json(200, app.voice.stop())
            else:
                self._send_json(200, app.voice.poll())
            return

        if route == "/api/tool":
            self._send_json(200, run_tool(
                str(payload.get("tool") or ""),
                payload.get("args") if isinstance(payload.get("args"), dict) else {},
                bool(payload.get("confirm")),
            ))
            return

        if route == "/api/tools/config":
            self._send_json(200, save_tool_config(payload))
            return

        if route == "/api/tools/stop":
            self._send_json(200, save_tool_config({"enabled": False, "reason": "emergency"}))
            return

        if route == "/api/tools/clear":
            try:
                TOOL_AUDIT_FILE.write_text("", encoding="utf-8")
            except OSError:
                pass
            self._send_json(200, {"ok": True})
            return

        # ---- Agent：调用工具 / 还原快照 / 重载插件 ----
        if route == "/api/agent/call":
            try:
                self._send_json(200, _agent_call(payload))
            except Exception as exc:
                log.exception("Agent 调用失败")
                self._send_json(200, {"ok": False, "message": str(exc)})
            return

        if route == "/api/agent/act":
            try:
                self._send_json(200, _agent_act(payload))
            except Exception as exc:
                log.exception("Agent 自主调用失败")
                self._send_json(200, {"ok": False, "agent": False, "used": [],
                                      "hint": "", "need_confirm": False,
                                      "message": str(exc)})
            return

        if route == "/api/agent/restore":
            try:
                self._send_json(200, _agent_restore(payload))
            except Exception as exc:
                log.exception("快照还原失败")
                self._send_json(200, {"ok": False, "message": str(exc)})
            return

        if route == "/api/agent/reload":
            try:
                self._send_json(200, _agent_reload())
            except Exception as exc:
                log.exception("插件重载失败")
                self._send_json(200, {"ok": False, "message": str(exc)})
            return

        if route == "/api/agent/persona/save":
            try:
                self._send_json(200, _agent_save_persona(payload))
            except Exception as exc:
                log.exception("人格保存失败")
                self._send_json(200, {"ok": False, "message": str(exc)})
            return

        # ---- /api/memory：单条写入 ----
        if route == "/api/memory":
            try:
                self._send_json(200, _mem_add(payload))
            except Exception as exc:
                log.exception("记忆写入失败")
                self._send_json(200, {"ok": False, "message": str(exc)})
            return

        # ---- /api/memory/extract：一句话 → 原始情景 + 事实 ----
        if route == "/api/memory/extract":
            try:
                self._send_json(200, _mem_extract(payload))
            except Exception as exc:
                log.exception("记忆抽取失败")
                self._send_json(200, {"ok": False, "message": str(exc)})
            return

        # ---- /api/memory/snapshot：迁移前另存原始 localStorage JSON ----
        if route == "/api/memory/snapshot":
            try:
                self._send_json(200, _mem_snapshot(payload.get("raw")))
            except Exception as exc:
                log.exception("记忆快照失败")
                self._send_json(200, {"ok": False, "message": str(exc)})
            return

        # ---- /api/memory/clear：按类型整类软删 ----
        if route == "/api/memory/clear":
            try:
                self._send_json(200, _mem_clear(payload.get("type")))
            except Exception as exc:
                log.exception("记忆清空失败")
                self._send_json(200, {"ok": False, "message": str(exc)})
            return

        # ---- /api/memory/import：批量导入（幂等） ----
        if route == "/api/memory/import":
            try:
                self._send_json(200, _mem_import(payload))
            except Exception as exc:
                log.exception("记忆导入失败")
                self._send_json(200, {"ok": False, "message": str(exc),
                                      "inserted_total": 0, "skipped_total": 0})
            return

        self._handle_chat(payload)

    # ---- /api/upload：照片/文件本地解析（不联网、不调模型） ----
    def _handle_upload(self, payload: dict[str, Any]) -> None:
        name = str(payload.get("name") or "file")
        b64 = str(payload.get("b64") or "")
        if not b64:
            self._send_json(200, {"ok": False, "message": "没有内容"})
            return
        try:
            raw = base64.b64decode(b64.split(",")[-1], validate=False)
        except Exception:
            self._send_json(200, {"ok": False, "message": "内容不是合法 base64"})
            return
        if len(raw) > 16 * 1024 * 1024:
            self._send_json(200, {"ok": False, "message": "太大了（上限 16MB）"})
            return

        up = appdata_dir() / "uploads"
        up.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-一-鿿]", "_", name)[:60] or "file"
        stem = f"{int(time.time() * 1000)}_{safe}"
        path = up / stem
        path.write_bytes(raw)

        info: dict[str, Any] = {
            "ok": True, "name": name, "size": len(raw), "file": stem,
            "url": "/uploads/" + urllib.parse.quote(stem),
        }
        if re.match(r"^image/", str(payload.get("type") or ""), re.I) or _looks_like_image(path):
            try:
                info.update(describe_image(path))
            except Exception as exc:
                log.warning("看图失败：%s", exc)
                info.update({"kind": "image", "caption": "（这张图没能解析）"})
        else:
            info.update({"kind": "file", "caption": f"一个文件：{name}，{len(raw) / 1024:.0f} KB"})
        self._send_json(200, info)

    # ---- 本机大脑（本地推理，零外部依赖） ----
    def _handle_brain_chat(self, payload: dict[str, Any]) -> None:
        try:
            import local_brain
        except Exception as exc:  # pragma: no cover
            self._send_json(200, {"ok": False, "message": f"本地大脑模块没装上：{exc}"})
            return
        st = local_brain.status()
        if not st.get("ok"):
            self._send_json(200, {"ok": False,
                                  "message": st.get("error") or "本地大脑没有可用数据"})
            return
        msgs = _sanitize_messages(payload.get("messages"))
        text = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                text = str(m.get("content") or "")
                break
        hist = [{"role": m.get("role"), "text": m.get("content") or ""}
                for m in msgs if m.get("role") != "system"]
        try:
            hour = int(payload.get("hour")) if payload.get("hour") is not None else None
        except (TypeError, ValueError):
            hour = None
        try:
            res = local_brain.brain.reply(text, hist, hour=hour)

            # 混合：本机大脑出计划（独立思考），措辞可以借小模型（允许跑在显卡上），
            # 但成品必须过人格闸门——过不了就整句丢掉，用语料原句兜底。
            # hybrid_reason 记录"为什么没用上小模型"，前端据此提示，别再静默降级。
            hybrid_reason = ""
            if not _brain_hybrid_on(payload):
                hybrid_reason = "off"
            else:
                try:
                    plan = local_brain.brain.deliberate(text, hist, hour=hour)
                    draft = _small_model_draft(plan, payload)
                    if not draft:
                        hybrid_reason = _draft_last_reason or "draft-empty"
                    else:
                        checked = local_brain.brain.enforce(draft, plan)
                        if checked.get("ok"):
                            res = checked
                            res["hybrid"] = True
                            hybrid_reason = ""
                        else:
                            hybrid_reason = "rejected:" + str(checked.get("reason") or "unknown")
                            res["thought"] = list(res.get("thought") or []) + [
                                f"拒绝小模型草稿（{checked.get('reason')}），用回自己的话"
                            ]
                except Exception as exc:
                    hybrid_reason = "draft-failed:" + type(exc).__name__
                    log.debug("混合措辞未启用", exc_info=True)
        except Exception as exc:
            log.exception("本地大脑推理失败")
            self._send_json(200, {"ok": False, "message": f"大脑卡住了：{exc}"})
            return
        used_hybrid = bool(res.get("hybrid"))
        self._send_json(200, {
            "ok": True,
            "reply": res.get("reply", ""),
            "model": "local-brain+hybrid" if used_hybrid else "local-brain",
            "provider": "local",
            "fallback": False,
            "thought": res.get("thought", []),
            "mood": res.get("mood", ""),
            "topic": res.get("topic", ""),
            "strategy": res.get("strategy", ""),
            "hybrid": used_hybrid,
            "hybridReason": "" if used_hybrid else hybrid_reason,
        })

    def _handle_brain_cmd(self, payload: dict[str, Any]) -> None:
        """/api/brain：greet（主动开场）/ idle（冷场搭话）/ think（只看它怎么想）。"""
        try:
            import local_brain
        except Exception as exc:  # pragma: no cover
            self._send_json(200, {"ok": False, "message": f"本地大脑模块没装上：{exc}"})
            return
        st = local_brain.status()
        if not st.get("ok"):
            self._send_json(200, {"ok": False, "message": st.get("error") or "本地大脑没有可用数据"})
            return

        kind = str(payload.get("kind") or "greet").strip()
        try:
            hour = int(payload.get("hour")) if payload.get("hour") is not None else None
        except (TypeError, ValueError):
            hour = None
        hist = [{"role": m.get("role"), "text": m.get("content") or m.get("text") or ""}
                for m in (payload.get("history") or [])
                if isinstance(m, dict) and m.get("role") != "system"]
        b = local_brain.brain
        try:
            if kind == "idle":
                res = b.idle(hist, hour=hour)
            elif kind == "think":
                text = str(payload.get("text") or "")
                res = b.reply(text, hist, hour=hour)
                res = dict(res, reply="")     # 只要思考过程，不出声
            else:
                avoid = [str(x) for x in (payload.get("avoid") or [])]
                res = b.greet(hour=hour, avoid=avoid)
        except Exception as exc:
            log.exception("本地大脑 %s 失败", kind)
            self._send_json(200, {"ok": False, "message": f"大脑卡住了：{exc}"})
            return
        self._send_json(200, {
            "ok": True,
            "reply": res.get("reply", ""),
            "thought": res.get("thought", []),
            "mood": res.get("mood", ""),
            "topic": res.get("topic", ""),
            "strategy": res.get("strategy", ""),
        })

    # ---- /api/diary ----
    def _handle_diary_write(self, payload: dict[str, Any]) -> None:
        append = payload.get("append")
        text = payload.get("text")
        try:
            if isinstance(append, str) and append.strip():
                _append_diary(append.strip())
            elif isinstance(text, str):
                _write_diary(text)
            else:
                self._send_json(200, {"ok": False, "message": "没给内容"})
                return
        except OSError as exc:
            self._send_json(200, {"ok": False, "message": f"写文件失败：{exc}"})
            return
        self._send_json(200, {"ok": True, "text": _read_diary(), "file": str(DIARY_FILE)})

    # ---- /api/stickers（内置表情包） ----
    def _list_builtin_stickers(self) -> list[dict[str, Any]]:
        """列出 表情包/ 里的图。

        文件名约定 `序号_英文_中文_其他.ext`，中文那段就是表情名，
        这样 `01_happy_开心_透明.png` 会变成名字「开心」。
        """
        folder = sticker_dir()
        ext = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        out: list[dict[str, Any]] = []
        try:
            files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ext)
        except OSError:
            return out
        for p in files:
            parts = [x for x in p.stem.split("_") if x]
            name = next((x for x in parts if re.search(r"[\u4e00-\u9fff]", x)), p.stem)
            cat = next((x for x in parts if x in ("情绪", "日常", "搞怪", "其他")), "情绪")
            out.append({
                "file": p.name,
                "name": name,
                "cat": cat,
                "url": "/stickers/" + urllib.parse.quote(p.name),
            })
        return out

    def _send_upload(self, name: str) -> None:
        folder = (appdata_dir() / "uploads").resolve()
        try:
            target = (folder / name).resolve()
            if folder not in target.parents or not target.is_file():
                raise FileNotFoundError(name)
        except Exception:
            self.send_error(404, "Not Found")
            return
        ctype = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
        }.get(target.suffix.lower(), "application/octet-stream")
        self._send_file(target, ctype)

    def _send_builtin_sticker(self, name: str) -> None:
        folder = sticker_dir().resolve()
        try:
            target = (folder / name).resolve()
            if folder not in target.parents or not target.is_file():
                raise FileNotFoundError(name)
        except Exception:
            self.send_error(404, "Not Found")
            return
        ctype = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif",
        }.get(target.suffix.lower(), "application/octet-stream")
        self._send_file(target, ctype)

    # ---- /api/ppt（真 .pptx）----
    def _handle_ppt(self, payload: dict[str, Any]) -> None:
        topic = str(payload.get("topic") or "小课堂").strip()[:60]
        raw = payload.get("bullets") or []
        if isinstance(raw, str):
            raw = re.split(r"[。！？!?；;\n]", raw)
        bullets = [str(b).strip()[:90] for b in raw if str(b).strip()][:12]
        try:
            path = build_pptx(topic, bullets)
        except Exception as exc:
            log.warning("生成 pptx 失败：%s", exc)
            self._send_json(200, {"ok": False, "message": str(exc)})
            return
        slides = 1 + max(1, (len(bullets) + 3) // 4)
        log.info("已生成 PPT：%s（%d 页）", path.name, slides)
        self._send_json(200, {
            "ok": True, "file": path.name, "slides": slides,
            "url": "/ppt/" + urllib.parse.quote(path.name),
        })

    def _send_ppt(self, name: str) -> None:
        folder = ppt_dir().resolve()
        try:
            target = (folder / name).resolve()
            if folder not in target.parents or not target.is_file():
                raise FileNotFoundError(name)
        except Exception:
            self.send_error(404, "Not Found")
            return
        self._send_file(
            target,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )

    def _send_image(self, name: str) -> None:
        folder = image_dir().resolve()
        try:
            target = (folder / name).resolve()
            if folder not in target.parents or not target.is_file():
                raise FileNotFoundError(name)
        except Exception:
            self.send_error(404, "Not Found")
            return
        self._send_file(target, "image/png")

    # ---- /api/draw：本地出图 ----
    def _handle_draw(self, payload: dict[str, Any]) -> None:
        prompt = str(payload.get("prompt") or "").strip()[:200]
        if not prompt:
            self._send_json(200, {"ok": False, "message": "没告诉我要画什么"})
            return
        try:
            width = int(payload.get("width") or 768)
            height = int(payload.get("height") or 768)
        except (TypeError, ValueError):
            width, height = 768, 768
        width = min(max(width, 256), 1280)
        height = min(max(height, 256), 1280)
        try:
            path = build_image(prompt, width, height)
        except Exception as exc:
            log.warning("本地出图失败：%s", exc)
            self._send_json(200, {"ok": False, "message": str(exc)})
            return
        log.info("已生成图片：%s", path.name)
        self._send_json(200, {
            "ok": True, "file": path.name, "prompt": prompt,
            "url": "/img/" + urllib.parse.quote(path.name),
            "engine": "local-pillow",
        })

    # ---- /api/chat ----
    def _handle_chat(self, payload: dict[str, Any]) -> None:
        # 本机大脑：不联网、不调模型、不碰显存，纯本地推理
        if str(payload.get("mode") or "").strip() == "brain":
            self._handle_brain_chat(payload)
            return
        cfg, err = _prepare_chat(payload)
        if err:
            self._send_json(200, err)
            return

        want_stream = cfg["stream"]
        candidates = chat_candidates(cfg)
        last_error = "模型没有返回内容"

        for index, cand in enumerate(candidates):
            tag = "主接口" if index == 0 else f"备用{index}"
            log.info("%s -> %s model=%s stream=%s native=%s",
                     tag, cand["base_url"], cand["model"], want_stream, cand["native"])
            try:
                resp = (_ollama_open(cand, want_stream) if cand["native"]
                        else _openai_open(cand))
            except urllib.error.HTTPError as exc:
                last_error = _extract_error(_read_error_body(exc)) or f"HTTP {exc.code}"
                log.warning("%s 失败：%s", tag, last_error)
                # 模型名被服务端明确否定（典型：把本地模型名填到了云端）→ 用它的名单重试一次
                fixed = _fixed_model(last_error, cand["model"])
                if not fixed:
                    continue
                retry = dict(cand, model=fixed)
                log.info("%s 的模型名不被支持，自动改用 %s 重试", tag, fixed)
                try:
                    resp = (_ollama_open(retry, want_stream)
                            if retry["native"] else _openai_open(retry))
                except urllib.error.HTTPError as exc2:
                    last_error = _extract_error(_read_error_body(exc2)) or f"HTTP {exc2.code}"
                    log.warning("%s 重试仍失败：%s", tag, last_error)
                    continue
                except (urllib.error.URLError, TimeoutError, OSError) as exc2:
                    last_error = f"连不上模型接口：{getattr(exc2, 'reason', exc2)}"
                    log.warning("%s 重试仍失败：%s", tag, last_error)
                    continue
                cand = retry
                cfg["model"] = fixed
                last_error = ""
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"连不上模型接口：{getattr(exc, 'reason', exc)}"
                log.warning("%s 失败：%s", tag, last_error)
                continue

            if index > 0:
                log.info("已自动切到%s：%s / %s", tag, cand["base_url"], cand["model"])

            with resp:
                if cand["native"]:
                    if want_stream:
                        self._stream_ollama_sse(resp)
                        return
                    try:
                        reply = _parse_ollama_reply(json.loads(resp.read().decode("utf-8", "replace")))
                    except (OSError, ValueError, UnicodeDecodeError) as exc:
                        last_error = f"模型返回的内容无法解析：{exc}"
                        log.warning("%s 解析失败：%s", tag, last_error)
                        continue
                    if not reply:
                        last_error = "模型没有返回内容"
                        log.warning("%s 返回空内容（可能没关思考）", tag)
                        continue
                    self._send_json(200, {"ok": True, "reply": reply, "model": cand["model"],
                                          "provider": cand["base_url"], "fallback": index > 0})
                    return

                content_type = (resp.headers.get("Content-Type") or "").lower()
                if want_stream and "text/event-stream" in content_type:
                    self._stream_sse(resp)
                    return

                try:
                    raw = resp.read()
                    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    data = json.loads(raw.decode("utf-8"))
                except (OSError, ValueError, UnicodeDecodeError) as exc:
                    last_error = f"模型返回的内容无法解析：{exc}"
                    log.warning("%s 解析失败：%s", tag, last_error)
                    continue

            if not isinstance(data, dict):
                last_error = "模型返回格式不对"
                continue
            if data.get("error"):
                last_error = _extract_error(json.dumps(data, ensure_ascii=False))
                log.warning("%s 返回错误：%s", tag, last_error)
                continue

            reply = ""
            try:
                reply = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            except (AttributeError, IndexError, TypeError):
                reply = ""
            if not reply:
                last_error = "模型没有返回内容"
                log.warning("%s 返回空内容", tag)
                continue

            self._send_json(200, {
                "ok": True,
                "reply": reply,
                "choices": data.get("choices"),
                "model": data.get("model") or cand["model"],
                "provider": cand["base_url"],
                "fallback": index > 0,
            })
            return

        tried = len(candidates)
        suffix = f"（已试 {tried} 个接口）" if tried > 1 else ""
        self._send_json(200, {"ok": False, "message": last_error + suffix})

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _stream_ollama_sse(self, resp: Any) -> None:
        """把 Ollama 原生 NDJSON 流转成前端认的 SSE 格式。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("error"):
                    log.warning("Ollama 流中断：%s", obj.get("error"))
                    break
                piece = str((obj.get("message") or {}).get("content") or "")
                if not piece:
                    continue
                chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
                self._write_chunk(
                    f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                )
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True
        finally:
            try:
                self._write_chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _stream_sse(self, resp: Any) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            while True:
                chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass


def _prepare_chat(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验并整理一次对话请求。返回 (配置, 错误)。错误非空表示不该发出去。"""
    page_mode = str(payload.get("mode") or "").strip()
    base_url = str(payload.get("baseUrl") or "").strip().rstrip("/")
    if not base_url:
        # 发布版默认配置全空：本地回落到 .env / Ollama 默认地址；
        # 云端没填就明确告诉用户去哪填，别偷偷发到本地模型上
        if page_mode == "cloud":
            return {}, {"ok": False,
                        "message": "云端还没配置：点输入框上方的模式面板，在 ☁️ 远程 API 卡里填地址和 API Key"}
        base_url = env_base_url()
    if not re.match(r"^https?://", base_url, re.I):
        return {}, {"ok": False, "message": "接口地址必须以 http:// 或 https:// 开头"}

    # 本机 Ollama 不需要 Key；云端接口才要
    api_key = env_key() or str(payload.get("apiKey") or "").strip()
    if not api_key and not is_loopback(base_url):
        return {}, {
            "ok": False,
            "message": "连的是云端接口但没填 API Key。补一下，或者把地址改成 "
                       "http://127.0.0.1:11434/v1 用本地 Ollama。",
        }

    model = resolve_model(base_url, str(payload.get("model") or env_model()).strip(),
                          strict=bool(payload.get("strict")))

    messages = _sanitize_messages(payload.get("messages"))
    if not messages:
        return {}, {"ok": False, "message": "没有要说的"}
    if not any(m["role"] == "user" for m in messages):
        return {}, {"ok": False, "message": "没有要说的"}
    if messages[0]["role"] != "system":
        messages.insert(0, {"role": "system", "content": FALLBACK_SYSTEM})

    # 人格画像注入：把 persona.json 里"数据驱动"的部分（口癖统计、情绪标签）
    # 追加到 system 消息后面。基础提示词已经写了身份和语气，这里只补画像；
    # 读不到文件就跳过，绝不因为人设文件坏了就不回话。
    if payload.get("personaOn") is not False:
        try:
            block = _persona_data_block()
        except Exception:
            block = ""
        if block:
            for m in messages:
                if m["role"] == "system":
                    if "她的说话习惯（从她真实发言里统计出来的）" not in m["content"]:
                        m["content"] = m["content"].rstrip() + "\n\n" + block
                    break

    try:
        temperature = float(payload.get("temperature", 0.85))
    except (TypeError, ValueError):
        temperature = 0.85
    temperature = min(max(temperature, 0.0), 1.5)

    try:
        max_tokens = int(payload.get("max_tokens", 220))
    except (TypeError, ValueError):
        max_tokens = 220
    if re.search(r"reason|r1|think|qwq", model, re.I):
        max_tokens = min(max(max_tokens, 900), 4000)
    else:
        max_tokens = min(max(max_tokens, 60), 1200)

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": bool(payload.get("stream")),
        "native": is_loopback(base_url),
        # strict=True：只用选中的这一路，不偷偷换模型/换服务商。
        # 切换模型后行为不一致，十次有八次是被静默 fallback 顶掉的。
        "strict": bool(payload.get("strict")),
    }, {}


def _parse_ollama_reply(data: dict[str, Any]) -> str:
    """Ollama 原生返回：正文在 message.content，思考过程在 message.thinking。"""
    msg = data.get("message") or {}
    text = str(msg.get("content") or "").strip()
    if text:
        return text
    try:
        return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    except (AttributeError, IndexError, TypeError):
        return ""


def chat_once(payload: dict[str, Any], timeout: float = 300.0) -> dict[str, Any]:
    """非流式跑一次，给 pywebview.api.chat 用（file:// 打开时的兜底通道）。

    同样走分层：主接口失败就试备用，本地走 Ollama 原生接口并关掉思考。
    """
    cfg, err = _prepare_chat(payload)
    if err:
        return err

    cfg = dict(cfg, stream=False)
    candidates = chat_candidates(cfg)
    last_error = "模型没有返回内容"

    for index, cand in enumerate(candidates):
        tag = "主接口" if index == 0 else f"备用{index}"
        log.info("%s -> %s model=%s stream=False native=%s",
                 tag, cand["base_url"], cand["model"], cand["native"])
        try:
            resp = (_ollama_open(dict(cand, stream=False), False, timeout) if cand["native"]
                    else _openai_open(dict(cand, stream=False), timeout))
        except urllib.error.HTTPError as exc:
            last_error = _extract_error(_read_error_body(exc)) or f"HTTP {exc.code}"
            log.warning("%s 失败：%s", tag, last_error)
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"连不上模型接口：{getattr(exc, 'reason', exc)}"
            log.warning("%s 失败：%s", tag, last_error)
            continue

        try:
            with resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            last_error = f"模型返回解析失败：{exc}"
            log.warning("%s 解析失败：%s", tag, last_error)
            continue

        if not isinstance(data, dict):
            last_error = "模型返回格式不对"
            continue
        if data.get("error"):
            last_error = _extract_error(json.dumps(data, ensure_ascii=False))
            log.warning("%s 返回错误：%s", tag, last_error)
            continue

        reply = (_parse_ollama_reply(data) if cand["native"] else
                 ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        if not reply:
            last_error = "模型没有返回内容"
            log.warning("%s 返回空内容", tag)
            continue

        if index > 0:
            log.info("已自动切到%s：%s / %s", tag, cand["base_url"], cand["model"])
        return {"ok": True, "reply": reply, "model": data.get("model") or cand["model"],
                "provider": cand["base_url"], "fallback": index > 0}

    tried = len(candidates)
    suffix = f"（已试 {tried} 个接口）" if tried > 1 else ""
    return {"ok": False, "message": last_error + suffix}


# ---------------------------------------------------------------------------
# 日记 memory_diary.txt
# ---------------------------------------------------------------------------
def _read_diary() -> str:
    try:
        return DIARY_FILE.read_text("utf-8-sig")
    except OSError:
        return ""


def _write_diary(text: str) -> None:
    DIARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DIARY_FILE.exists():
        try:
            DIARY_FILE.with_suffix(".txt.bak").write_text(_read_diary(), "utf-8")
        except OSError:
            pass
    tmp = DIARY_FILE.with_suffix(".txt.tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, DIARY_FILE)
    log.info("日记已重写：%d 字", len(text))


def _append_diary(line: str) -> None:
    DIARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DIARY_FILE.open("a", encoding="utf-8") as fh:
        if not _read_diary():
            fh.write("# 千逐的日记\n")
        fh.write(line.rstrip("\n") + "\n")
    log.info("日记追加：%s", line[:60])


# ---------------------------------------------------------------------------
# 当天对话日志：简要、有上限、可调用
# ---------------------------------------------------------------------------
DAYLOG_DIR = appdata_dir() / "daylog"
DAYLOG_MAX_ITEMS = 6        # 每天最多记 6 条
DAYLOG_ITEM_CHARS = 50      # 每条最多 50 字
DAYLOG_KEEP_DAYS = 14       # 只保留最近 14 天


def _looks_like_image(path: Path) -> bool:
    """不看扩展名，直接看文件头。"""
    try:
        head = path.open("rb").read(12)
    except OSError:
        return False
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if head[:3] == b"\xff\xd8\xff":
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return True
    return head[:2] == b"BM"


def _color_name(r: int, g: int, b: int) -> str:
    mx, mn = max(r, g, b), min(r, g, b)
    v, s = mx, (mx - mn)
    if v < 46:
        return "黑"
    if s < 26:
        return "白" if v > 200 else ("灰" if v > 110 else "深灰")
    if r >= g and r >= b:
        return "红" if b < 110 and g < 110 else ("粉" if b > g else "橙")
    if g >= r and g >= b:
        return "绿" if b < 140 else "青"
    return "蓝" if r < 120 else "紫"


def describe_image(path: Path) -> dict[str, Any]:
    """本地"看懂"一张图：尺寸、明暗、主色、朝向。

    只做像素统计，不加载任何模型；缩到 64px 后原图立即释放，
    所以 4K 大图也只占几十 KB 内存。
    """
    from PIL import Image  # Pillow 只在真的看图时才 import

    out: dict[str, Any] = {"kind": "image"}
    with Image.open(path) as im:
        out["w"], out["h"] = im.size
        out["format"] = im.format or ""
        try:
            ex = dict(im.getexif() or {})
            out["orientation"] = ex.get(274) or 1
        except Exception:
            pass
        rgb = im.convert("RGB")
        small = rgb.copy()
        small.thumbnail((64, 64))
        px = list(small.getdata())
        out["animated"] = getattr(im, "n_frames", 1) > 1

    n = max(len(px), 1)
    r = sum(p[0] for p in px) // n
    g = sum(p[1] for p in px) // n
    b = sum(p[2] for p in px) // n
    lum = 0.299 * r + 0.587 * g + 0.114 * b

    buckets: dict[tuple[int, int, int], int] = {}
    for p in px:
        k = (p[0] // 48, p[1] // 48, p[2] // 48)
        buckets[k] = buckets.get(k, 0) + 1
    top = sorted(buckets.items(), key=lambda kv: -kv[1])[:3]
    colors = [_color_name(k[0] * 48 + 24, k[1] * 48 + 24, k[2] * 48 + 24) for k, _ in top]
    seen: list[str] = []
    for c in colors:
        if c not in seen:
            seen.append(c)

    w, h = out["w"], out["h"]
    shape = "横构图" if w > h * 1.15 else ("竖构图" if h > w * 1.15 else "方构图")
    light = "偏暗" if lum < 70 else ("偏亮" if lum > 185 else "明暗适中")
    size_txt = f"{w}×{h}"
    out["brightness"] = round(lum, 1)
    out["main_colors"] = seen
    out["caption"] = (
        f"一张 {size_txt} 的{shape}图片，{light}，主要是{'、'.join(seen)}色调"
        + ("，像是夜里或室内拍的" if lum < 70 else "")
        + ("，动图" if out.get("animated") else "")
        + "。"
    )
    return out


def _brain_hybrid_on(payload: dict[str, Any] | None = None) -> bool:
    """是否让本机大脑借用外部小模型的措辞。

    ⚠ 人格 = 风格层，不是执行决策层：
      计划（意图/情绪/话题/策略）永远由本机大脑出；
      措辞可以借小模型（它可以跑在显卡上），但成品必须过 enforce() 人格闸门。
    """
    flag = (os.environ.get("BRAIN_HYBRID") or "1").strip().lower()
    base = flag not in ("0", "false", "off", "no")
    if not isinstance(payload, dict):
        return base
    v = payload.get("hybrid")
    if v is None:
        return base
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "on", "yes", "y")


# 上一次要草稿为什么没拿到。只给 /api/chat 的 hybridReason 用，不参与任何业务判断。
_draft_last_reason = ""


def _small_model_draft(plan: dict[str, Any], payload: dict[str, Any]) -> str:
    """向本地 / 云端小模型要一句措辞草稿。

    拿不到就返回空字符串——调用方会回落到语料原句，绝不硬凑。
    失败原因写进模块级 `_draft_last_reason`，供调用方回报给前端（不再静默）。
    """
    global _draft_last_reason
    _draft_last_reason = ""
    try:
        model = str(payload.get("draftModel") or "").strip() or env_model()
    except Exception as exc:
        log.debug("取草稿模型名失败：%s", exc)
        model = ""
    if not model:
        _draft_last_reason = "no-draft-model"
        return ""
    sys = (
        "你是千逐，郑郑养的一只电子小狗。说话极短，平均 4 个字，"
        "不解释、不客套、不复述对方的话、不用标点符号堆情绪。\n"
        f"这一次的任务：{plan.get('strategy') or '顺着他的话接一句'}。\n"
        f"当前话题：{plan.get('topic') or '（随便聊）'}。\n"
        f"硬要求：只输出你要说的那一句话，不超过 {plan.get('max_chars') or 8} 个字，"
        "不要引号、不要前缀、不要解释。"
    )
    body = {
        "model": model,
        "baseUrl": str(payload.get("draftBaseUrl") or "").strip() or env_base_url(),
        "apiKey": str(payload.get("draftKey") or "").strip() or env_key(),
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": str(plan.get("text") or "")},
        ],
        "temperature": 0.9, "max_tokens": 24, "stream": False,
        "mode": "local", "strict": False,
    }
    try:
        # 草稿只是"锦上添花"：最多等 1.6s，小模型没热起来就直接用语料原句，
        # 绝不让整段回复卡在模型冷启动上（实测冷启动要 8 秒+）。
        r = chat_once(body, timeout=1.6)
    except Exception as exc:
        log.debug("借小模型措辞失败：%s", exc)
        _draft_last_reason = "draft-failed:" + type(exc).__name__
        return ""
    if not isinstance(r, dict):
        _draft_last_reason = "draft-bad-response"
        return ""
    reply = str(r.get("reply") or "").strip()
    if not reply:
        try:
            ch = (r.get("choices") or [{}])[0]
            reply = str(((ch.get("message") or {}).get("content")) or "").strip()
        except (AttributeError, IndexError, TypeError):
            reply = ""
    if not reply:
        _draft_last_reason = "draft-empty"
    return reply


def _daylog_day(day: str = "") -> str:
    day = (day or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return day
    return time.strftime("%Y-%m-%d")


def _daylog_path(day: str) -> Path:
    return DAYLOG_DIR / f"{day}.json"


def read_day_log(day: str = "") -> dict[str, Any]:
    d = _daylog_day(day)
    items: list[Any] = []
    try:
        data = json.loads(_daylog_path(d).read_text("utf-8"))
        if isinstance(data.get("items"), list):
            items = data["items"]
    except (OSError, ValueError):
        items = []
    return {
        "ok": True, "date": d, "items": items, "count": len(items),
        "limit": DAYLOG_MAX_ITEMS, "max_chars": DAYLOG_ITEM_CHARS,
    }


# ---------------------------------------------------------------------------
# 电脑控制（工具总线）
#   分级：0 只读 / 1 常规 / 2 敏感（需确认）/ 3 高危（默认关闭）
#   原则：只做白名单里的具体动作，不开放"随便执行命令"的口子。
# ---------------------------------------------------------------------------
TOOL_CFG_FILE = DATA_DIR / "tools.json"
TOOL_AUDIT_FILE = DATA_DIR / "tools_audit.log"
TOOL_LEVEL_NAMES = {0: "只读", 1: "常规", 2: "敏感", 3: "高危"}

TOOL_DEFAULT_CFG: dict[str, Any] = {
    "enabled": True,
    "levels": {"0": True, "1": True, "2": True, "3": False},
    "confirm_level": 2,
    "allow_apps": ["notepad.exe", "calc.exe", "mspaint.exe",
                   "explorer.exe", "msedge.exe"],
}

TOOL_APPS: dict[str, str] = {
    "记事本": "notepad.exe", "notepad": "notepad.exe",
    "计算器": "calc.exe", "calc": "calc.exe",
    "画图": "mspaint.exe", "mspaint": "mspaint.exe", "paint": "mspaint.exe",
    "资源管理器": "explorer.exe", "explorer": "explorer.exe",
    "浏览器": "msedge.exe", "edge": "msedge.exe", "msedge": "msedge.exe",
}

TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "sys_info", "label": "看电脑状态", "level": 0,
     "desc": "内存 / 磁盘 / 显卡占用，只读不改"},
    {"name": "screenshot", "label": "截一张屏", "level": 1,
     "desc": "存到 生成/截图/，不发到任何地方"},
    {"name": "open_app", "label": "打开应用", "level": 1,
     "desc": "只限白名单：记事本 / 计算器 / 画图 / 资源管理器 / 浏览器"},
    {"name": "open_url", "label": "打开网页", "level": 1,
     "desc": "只接受 http / https 链接，用默认浏览器打开"},
    {"name": "volume", "label": "调音量", "level": 1,
     "desc": "加 / 减 / 静音"},
    {"name": "open_path", "label": "打开文件或文件夹", "level": 1,
     "desc": "只允许用户目录（桌面 / 文档 / 下载等）下的路径"},
    {"name": "clipboard_read", "label": "读剪贴板", "level": 2,
     "desc": "可能含隐私内容，每次都要你确认"},
    {"name": "lock_screen", "label": "锁屏", "level": 2,
     "desc": "立即锁定当前会话，需要密码解锁"},
]


def _tool_cfg() -> dict[str, Any]:
    """读权限配置；缺字段就用默认值补齐（老版本配置也能直接用）。"""
    cfg = json.loads(json.dumps(TOOL_DEFAULT_CFG))
    try:
        data = json.loads(TOOL_CFG_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    if isinstance(data.get("enabled"), bool):
        cfg["enabled"] = data["enabled"]
    lv = data.get("levels")
    if isinstance(lv, dict):
        for k in ("0", "1", "2", "3"):
            if isinstance(lv.get(k), bool):
                cfg["levels"][k] = lv[k]
    try:
        cfg["confirm_level"] = max(0, min(3, int(data.get("confirm_level", 2))))
    except (TypeError, ValueError):
        pass
    apps = data.get("allow_apps")
    if isinstance(apps, list):
        cfg["allow_apps"] = [str(a) for a in apps if str(a).strip()][:20]
    return cfg


def save_tool_config(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = _tool_cfg()
    before = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    if "enabled" in payload:
        cfg["enabled"] = bool(payload.get("enabled"))
    if isinstance(payload.get("levels"), dict):
        for k in ("0", "1", "2", "3"):
            if isinstance(payload["levels"].get(k), bool):
                cfg["levels"][k] = payload["levels"][k]
    if "confirm_level" in payload:
        try:
            cfg["confirm_level"] = max(0, min(3, int(payload.get("confirm_level"))))
        except (TypeError, ValueError):
            pass
    if isinstance(payload.get("allow_apps"), list):
        cfg["allow_apps"] = [str(a).strip().lower() for a in payload["allow_apps"] if str(a).strip()][:20]
    try:
        TOOL_CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    except OSError as exc:
        return {"ok": False, "message": f"保存失败：{exc}"}
    if json.dumps(cfg, ensure_ascii=False, sort_keys=True) != before:
        _tool_audit("config", {"enabled": cfg["enabled"], "levels": cfg["levels"]}, 0, True,
                    payload.get("reason") or "权限配置已更新", 0)
    return tool_state()


def _tool_audit(tool: str, args: dict[str, Any], level: int, ok: bool,
                detail: str, ms: int) -> None:
    """每次调用都记一行（谁、什么级别、结果），供个人随时回看。"""
    row = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool": tool, "level": level, "level_name": TOOL_LEVEL_NAMES.get(level, ""),
        "ok": bool(ok), "detail": str(detail)[:160], "ms": int(ms),
        "args": {k: str(v)[:80] for k, v in (args or {}).items() if k in ("app", "url", "action", "path")},
    }
    try:
        with TOOL_AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if TOOL_AUDIT_FILE.stat().st_size > 512 * 1024:
            lines = TOOL_AUDIT_FILE.read_text("utf-8").splitlines()[-300:]
            TOOL_AUDIT_FILE.write_text("\n".join(lines) + "\n", "utf-8")
    except OSError:
        pass


def _tool_audit_tail(n: int = 20) -> list[dict[str, Any]]:
    try:
        lines = TOOL_AUDIT_FILE.read_text("utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def tool_state() -> dict[str, Any]:
    cfg = _tool_cfg()
    return {
        "ok": True, "enabled": cfg["enabled"], "levels": cfg["levels"],
        "confirm_level": cfg["confirm_level"], "level_names": TOOL_LEVEL_NAMES,
        "allow_apps": cfg["allow_apps"], "tools": TOOL_SPECS,
        "audit": _tool_audit_tail(20), "audit_file": str(TOOL_AUDIT_FILE),
        "config_file": str(TOOL_CFG_FILE),
    }


# 风险级映射：内部 0~3 → 报告里的 LOW / MEDIUM / HIGH
TOOL_RISK = {0: "low", 1: "low", 2: "medium", 3: "high"}

# 每个工具的参数契约（只列真正会被读取的字段，避免"看起来能传很多"）
TOOL_ARGS_SCHEMA: dict[str, dict[str, Any]] = {
    "open_app": {"app": {"type": "string", "desc": "应用名或 exe 名",
                         "enum": ["记事本", "计算器", "画图", "资源管理器", "浏览器"]}},
    "open_url": {"url": {"type": "string", "desc": "只接受 http/https",
                         "pattern": "^https?://"}},
    "volume": {"action": {"type": "string", "enum": ["up", "down", "mute"]}},
    "open_path": {"path": {"type": "string", "desc": "用户目录下的绝对路径"}},
}


def tool_schema() -> dict[str, Any]:
    """工具契约：给后续脚本 / 模型调用用的稳定接口（字段不变，只增不改）。"""
    cfg = _tool_cfg()
    tools = []
    for spec in TOOL_SPECS:
        level = int(spec["level"])
        tools.append({
            "name": spec["name"], "label": spec["label"], "level": level,
            "risk": TOOL_RISK.get(level, "medium"),
            "desc": spec["desc"],
            "enabled": bool(cfg["levels"].get(str(level), False)) and cfg["enabled"],
            "need_confirm": level >= int(cfg["confirm_level"]),
            "args": TOOL_ARGS_SCHEMA.get(spec["name"], {}),
        })
    return {
        "ok": True, "version": 1, "enabled": cfg["enabled"],
        "levels": cfg["levels"], "confirm_level": cfg["confirm_level"],
        "risk_names": {"low": "低风险", "medium": "中风险", "high": "高风险"},
        "tools": tools,
        "call": {
            "method": "POST", "path": "/api/tool",
            "body": {"tool": "<name>", "args": {}, "confirm": False},
            "note": "need_confirm 为 true 的工具必须带 confirm=true 才会执行；否则返回 need_confirm",
        },
        "cli": "python tools/tool_client.py list | call <tool> [--args k=v ...] [--confirm]",
    }


# ---- 具体动作（每个都只做一件事，参数严格校验） ----
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _tool_sys_info(_args: dict[str, Any]) -> str:
    import shutil

    mem = _MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    used_gb = (mem.ullTotalPhys - mem.ullAvailPhys) / 1024 ** 3
    total_gb = mem.ullTotalPhys / 1024 ** 3
    try:
        du = shutil.disk_usage(str(Path.home()))
        disk = f"磁盘 {du.used / 1024 ** 3:.0f}/{du.total / 1024 ** 3:.0f}GB"
    except OSError:
        disk = "磁盘未知"
    gpu = ""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode == 0 and r.stdout.strip():
            u, t = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")][:2]
            gpu = f" · 显卡 {u}/{t}MB"
    except Exception:
        pass
    return f"内存 {used_gb:.1f}/{total_gb:.1f}GB（{mem.dwMemoryLoad}%）· {disk}{gpu}"


def _tool_screenshot(_args: dict[str, Any]) -> str:
    from PIL import ImageGrab

    d = image_dir() / "截图"
    d.mkdir(parents=True, exist_ok=True)
    p = d / time.strftime("shot_%Y%m%d_%H%M%S.png")
    ImageGrab.grab().save(str(p))
    return f"截好了：{p.name}"


def _tool_open_app(args: dict[str, Any]) -> str:
    cfg = _tool_cfg()
    raw = str(args.get("app") or "").strip().lower()
    exe = TOOL_APPS.get(raw) or TOOL_APPS.get(str(args.get("app") or "").strip())
    if not exe:
        raise ValueError(f"没有这个应用（可用：{'/'.join(sorted(set(TOOL_APPS.values())))}）")
    if exe not in cfg["allow_apps"]:
        raise ValueError(f"{exe} 不在白名单里，先在设置里加进去")
    subprocess.Popen([exe], close_fds=True,
                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return f"打开了 {exe}"


def _tool_open_url(args: dict[str, Any]) -> str:
    import webbrowser

    url = str(args.get("url") or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("只接受 http/https 链接")
    webbrowser.open(url, new=2)
    return f"用默认浏览器打开 {parsed.netloc}"


def _tool_volume(args: dict[str, Any]) -> str:
    action = str(args.get("action") or "up").lower()
    vk = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}.get(action)
    if not vk:
        raise ValueError("action 只能是 up / down / mute")
    user32 = ctypes.windll.user32
    for _ in range(1 if action == "mute" else 3):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
    return {"up": "音量加大", "down": "音量减小", "mute": "已静音切换"}[action]


def _tool_open_path(args: dict[str, Any]) -> str:
    raw = str(args.get("path") or "").strip().strip('"')
    if not raw:
        raise ValueError("缺 path")
    p = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    if not p.exists():
        raise ValueError("路径不存在")
    home = Path.home().resolve()
    if home not in p.parents and p != home:
        raise ValueError("只允许打开用户目录下的路径")
    os.startfile(str(p))  # noqa: S606 - 桌面应用，路径已限制在用户目录
    return f"打开了 {p.name or p}"


def _tool_clipboard_read(_args: dict[str, Any]) -> str:
    CF_UNICODETEXT = 13
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.OpenClipboard(None)
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return "剪贴板是空的（或不是文本）"
        ptr = kernel32.GlobalLock(handle)
        try:
            text = ctypes.c_wchar_p(ptr).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    text = text.strip()
    if not text:
        return "剪贴板是空的"
    return f"剪贴板（{len(text)} 字）：{text[:300]}"


def _tool_lock_screen(_args: dict[str, Any]) -> str:
    if not ctypes.windll.user32.LockWorkStation():
        raise RuntimeError("系统拒绝了锁屏请求")
    return "已锁屏"


TOOL_RUNNERS = {
    "sys_info": _tool_sys_info,
    "screenshot": _tool_screenshot,
    "open_app": _tool_open_app,
    "open_url": _tool_open_url,
    "volume": _tool_volume,
    "open_path": _tool_open_path,
    "clipboard_read": _tool_clipboard_read,
    "lock_screen": _tool_lock_screen,
}


def run_tool(name: str, args: dict[str, Any], confirm: bool = False) -> dict[str, Any]:
    """执行一个工具调用：先过权限（总开关 → 分级 → 确认），再执行，最后记账。"""
    spec = next((s for s in TOOL_SPECS if s["name"] == name), None)
    if not spec:
        return {"ok": False, "message": "没有这个工具"}
    cfg = _tool_cfg()
    level = int(spec["level"])
    if not cfg["enabled"]:
        return {"ok": False, "level": level, "message": "电脑控制已关闭（设置 → 电脑控制）"}
    if not cfg["levels"].get(str(level), False):
        return {"ok": False, "level": level,
                "message": f"「{TOOL_LEVEL_NAMES[level]}」这一级已被你关掉"}
    if level >= int(cfg["confirm_level"]) and not confirm:
        _tool_audit(name, args, level, False, "等待确认", 0)
        return {"ok": False, "level": level, "need_confirm": True,
                "message": f"「{spec['label']}」属于{TOOL_LEVEL_NAMES[level]}操作，需要你点确认"}
    t0 = time.monotonic()
    try:
        detail = TOOL_RUNNERS[name](args or {})
        ok = True
    except Exception as exc:
        detail, ok = str(exc), False
        log.warning("工具 %s 执行失败：%s", name, exc)
    ms = int((time.monotonic() - t0) * 1000)
    _tool_audit(name, args, level, ok, detail, ms)
    return {"ok": ok, "level": level, "level_name": TOOL_LEVEL_NAMES[level],
            "message": detail if ok else f"没做成：{detail}", "ms": ms}


def append_day_log(text: str, source: str = "auto", day: str = "") -> dict[str, Any]:
    """往当天日志加一条：自动去重 + 有上限，满了就不再写（不覆盖、不逐句堆）。"""
    line = re.sub(r"\s+", " ", str(text or "")).strip()
    line = re.sub(r"^[-*•·\d]+[.、)\s]*", "", line).strip()
    if not line or line in ("无", "没有", "-"):
        return {"ok": False, "message": "空内容"}
    if len(line) > DAYLOG_ITEM_CHARS:
        line = line[: DAYLOG_ITEM_CHARS - 1] + "…"

    d = _daylog_day(day)
    path = _daylog_path(d)
    try:
        DAYLOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"ok": False, "message": "写不进日志目录"}

    items: list[dict[str, Any]] = []
    try:
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data.get("items"), list):
            items = data["items"]
    except (OSError, ValueError):
        items = []

    norm = line.replace(" ", "")
    for it in items:
        old = str((it or {}).get("text") or "").replace(" ", "")
        # 完全一样，或开头 8 个字一致，就当同一条
        if old == norm or (len(old) >= 8 and norm and (old.startswith(norm[:8]) or norm.startswith(old[:8]))):
            return {"ok": True, "duplicate": True, **read_day_log(d)}
    if len(items) >= DAYLOG_MAX_ITEMS:
        return {"ok": True, "full": True, **read_day_log(d)}

    items.append({"t": time.strftime("%H:%M"), "text": line, "src": source})
    payload = {"date": d, "items": items, "updated": int(time.time())}
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, path)
    except OSError:
        return {"ok": False, "message": "写日志失败"}

    log.info("当天日志 +1（%s）：%s", source, line[:40])
    _rewrite_daylog_md()
    return {"ok": True, "added": line, **read_day_log(d)}


def _rewrite_daylog_md() -> None:
    """把最近几天合并成一份人看的 `对话日志.md`，同时清掉过期文件。"""
    try:
        files = sorted(DAYLOG_DIR.glob("*.json"))
    except OSError:
        return
    # 只留最近 KEEP_DAYS 天
    for old in files[:-DAYLOG_KEEP_DAYS]:
        try:
            old.unlink()
        except OSError:
            pass
    lines = ["# 对话日志（每天最多 %d 条，每条不超过 %d 字）"
             % (DAYLOG_MAX_ITEMS, DAYLOG_ITEM_CHARS), ""]
    for path in files[-DAYLOG_KEEP_DAYS:]:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        items = data.get("items") or []
        if not items:
            continue
        lines.append(f"## {data.get('date') or path.stem}")
        for it in items:
            lines.append(f"- {it.get('t', '')} {it.get('text', '')}".replace("  ", " "))
        lines.append("")
    try:
        tmp = DAYLOG_DIR / "对话日志.md.tmp"
        tmp.write_text("\n".join(lines), "utf-8")
        os.replace(tmp, DAYLOG_DIR / "对话日志.md")
    except OSError:
        log.debug("写 对话日志.md 失败", exc_info=True)


def _sanitize_messages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value[-32:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("system", "user", "assistant") or not isinstance(content, str):
            continue
        limit = 12000 if role == "system" else 4000
        content = content[:limit].strip()
        if content:
            out.append({"role": role, "content": content})
    return out


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read()
        if (exc.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")
    except Exception:
        return str(exc)


def _extract_error(text: str) -> str:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code")
                if msg:
                    return str(msg)
            if isinstance(err, str):
                return err
            for key in ("message", "detail"):
                if obj.get(key):
                    return str(obj[key])
    except (ValueError, TypeError):
        pass
    return text.strip()[:600]


def _fixed_model(message: str, current: str = "") -> str:
    """从服务端的 400 文案里捞出它真正支持的模型名。

    DeepSeek 这类网关会直接回「supported API model names are a, b」；
    把本地模型名（qwen-local-4b:latest）填到云端时，靠这个自动纠正一次，
    省得用户在"未选模型"和 400 之间来回猜。
    """
    m = re.search(r"supported API model names? (?:are|is)[:：]?\s*([^.\n]+)", message or "", re.I)
    if not m:
        return ""
    for raw in re.split(r"[,，、/]| or ", m.group(1)):
        name = raw.strip().strip("'\". ")
        if name and re.fullmatch(r"[A-Za-z0-9._\-:/]+", name) and name != current:
            return name
    return ""


class LocalServer:
    def __init__(self, root: Path, preferred_ports: range = range(8765, 8795)) -> None:
        self.root = root
        self.preferred_ports = preferred_ports
        self.httpd: _LocalHTTPServer | None = None
        self.port = 0
        self.thread: threading.Thread | None = None

    def start(self) -> int:
        last_error: Exception | None = None
        # 不用 +[0] 随机端口：端口一变，页面的 origin 就变，IndexedDB 里的表情包
        # 会按 origin 隔离而"集体消失"。固定从 8765 起找空闲端口，找不到就报错。
        host = bind_host()
        pinned = fixed_port()
        ports = [pinned] if pinned else list(self.preferred_ports)
        for port in ports:
            try:
                httpd = _LocalHTTPServer((host, port), _Handler)
                self.httpd = httpd
                break
            except OSError as exc:
                last_error = exc
        if self.httpd is None:
            raise RuntimeError(f"无法启动本地 HTTP 服务：{last_error}")

        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="local-http", daemon=True)
        self.thread.start()
        log.info("本地服务已启动：http://%s:%d", host, self.port)
        return self.port

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                log.exception("关闭本地服务失败")
            self.httpd = None


# ---------------------------------------------------------------------------
# 注入到聊天页的极少 JS：顶栏拖拽 + Esc 关闭
# ---------------------------------------------------------------------------
CHAT_INJECT_JS = r"""
(function () {
  if (window.__petDesktopInjected) return;
  window.__petDesktopInjected = true;

  var style = document.createElement('style');
  style.textContent = '.statusbar.pywebview-drag-region{cursor:move;user-select:none;-webkit-user-select:none;}';
  document.head.appendChild(style);

  var bar = document.querySelector('.statusbar');
  if (bar) bar.classList.add('pywebview-drag-region');

  window.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var sheet = document.getElementById('sheet');
    if (sheet && sheet.classList.contains('open')) {
      var close = document.getElementById('closeSheet');
      if (close) { close.click(); return; }
    }
    if (window.pywebview && pywebview.api) pywebview.api.close_chat();
  }, true);
})();
"""


# ---------------------------------------------------------------------------
# 立绘窗口 HTML（极简，完全透明；立绘本身来自 dog_girl.png）
# ---------------------------------------------------------------------------
PET_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<style>
  html, body {
    margin: 0; padding: 0; width: 100%; height: 100%;
    overflow: hidden; background: transparent;
  }
  body {
    -webkit-user-select: none; user-select: none;
    -webkit-tap-highlight-color: transparent;
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", "Segoe UI Emoji", sans-serif;
  }
  #stage { position: fixed; inset: 0; cursor: default; }

  /* 背景层：铺满整个视口，永远是 #stage 的第一个子节点、最底层（z-index:0）。
     要加背景就设 #bg 的 background（颜色/图片都行），它一定显示得出来：
     · 独立 #bg 选择器，不会被 `html,body{background:transparent}` 这类规则重置；
     · 排在最前 + z-index:0，不会被立绘/气泡/工具栏盖住；
     · 铺满不透明像素，顺带解决透明窗口"上一帧残留"的拖影问题。 */
  #bg {
    position: fixed; inset: 0; z-index: 0;
    background: transparent;
    pointer-events: none;
    will-change: transform;   /* 独立合成层：避免透明窗口重绘时留下上一帧 */
  }

  /* ---------------- 交互反馈：悬停 / 按下 / 状态过渡 ---------------- */
  #petBox { transition: transform .28s cubic-bezier(.34,1.56,.64,1); }
  #petBox.hover { transform: translateY(-4px); }
  #petBox.press { transform: translateY(2px) scale(.97); }
  #jolt { transition: transform .3s cubic-bezier(.34,1.56,.64,1); }
  /* 状态机只改表现层，不碰原有行为逻辑 */
  body[data-state="sleepy"] #rig { animation-duration: 6.6s; }
  body[data-state="sleepy"] #pet { filter: drop-shadow(0 6px 10px rgba(0,0,0,.2)) brightness(.92); }
  body[data-state="happy"] #tilt { animation-duration: 3.1s; }
  body[data-state="happy"] #pet { filter: drop-shadow(0 10px 18px rgba(0,0,0,.28)) brightness(1.06); }

  /* ---------------- 悬停提示气泡 ---------------- */
  #tip {
    position: absolute; left: 50%; top: 8px; transform: translate(-50%, 6px);
    padding: 6px 11px; border-radius: 12px; white-space: nowrap;
    background: rgba(255,252,248,.96); color: #6a4b46;
    font-size: 12px; line-height: 1.4;
    box-shadow: 0 4px 12px rgba(0,0,0,.16);
    opacity: 0; pointer-events: none;
    transition: opacity .22s ease, transform .22s ease; z-index: 40;
  }
  #tip.show { opacity: 1; transform: translate(-50%, 0); }

  /* ---------------- 右键菜单（暖色圆角，和立绘一个调子） ---------------- */
  #menu {
    position: absolute; min-width: 134px; padding: 5px;
    background: rgba(255,252,248,.98); border: 1px solid rgba(140,110,100,.16);
    border-radius: 12px; box-shadow: 0 10px 26px rgba(0,0,0,.2);
    opacity: 0; transform: scale(.94) translateY(-4px); pointer-events: none;
    transform-origin: top left; transition: opacity .16s ease, transform .16s ease;
    z-index: 50;
  }
  #menu.show { opacity: 1; transform: scale(1) translateY(0); pointer-events: auto; }
  #menu .mi {
    display: block; width: 100%; text-align: left;
    padding: 7px 10px; border: 0; border-radius: 8px; background: transparent;
    color: #5d4038; font: inherit; font-size: 13px; cursor: pointer;
    transition: background .15s ease, color .15s ease;
  }
  #menu .mi:hover { background: #ffe3e6; color: #b3394f; }
  #menu .sep { height: 1px; margin: 4px 6px; background: rgba(140,110,100,.14); }

  /* ---------------- 状态提示 toast ---------------- */
  #toast {
    position: absolute; left: 50%; bottom: 14px; transform: translate(-50%, 8px);
    padding: 5px 12px; border-radius: 999px; white-space: nowrap;
    background: rgba(93,64,56,.9); color: #fff6f2; font-size: 12px;
    opacity: 0; pointer-events: none;
    transition: opacity .2s ease, transform .2s ease; z-index: 45;
  }
  #toast.show { opacity: 1; transform: translate(-50%, 0); }

  /* ---------------- 立绘骨骼 ---------------- */
  #petBox {
    position: absolute; left: 0; bottom: 0;
    width: 100%; aspect-ratio: 1 / 1;
    cursor: pointer;
  }
  #tilt {
    width: 100%; height: 100%;
    transform-origin: 50% 96%;
    animation: sway 8.5s ease-in-out infinite;
  }
  #rig {
    width: 100%; height: 100%;
    transform-origin: 50% 96%;
    animation: breathe 4.2s ease-in-out infinite;
  }
  #jolt { position: relative; width: 100%; height: 100%; transform-origin: 50% 92%; }
  #pet {
    position: absolute; inset: 0; width: 100%; height: 100%;
    object-fit: contain; object-position: center bottom;
    pointer-events: none; opacity: 0;
    transition: opacity .3s ease, filter .4s ease;
    filter: drop-shadow(0 8px 14px rgba(0,0,0,.26));
    -webkit-user-drag: none; user-drag: none;
  }
  #pet.ready { opacity: 1; }
  #face { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }

  /* 眨眼 / 表情 */
  #lidL, #lidR { transform: translateY(var(--lidY, -170px)); transition: transform .13s ease-out; }
  #face.blink #lidL, #face.blink #lidR { transform: translateY(0); transition-duration: .075s; }
  #face.wink-l #lidL { transform: translateY(0); }
  #blush { opacity: var(--blush, 0); transition: opacity .55s ease; }
  #mouthGroup {
    opacity: 0;
    transform-box: fill-box; transform-origin: 50% 12%;
    transition: opacity .12s ease;
  }
  body.mouth-on #mouthGroup { opacity: 1; transform: scaleY(var(--mouth, .8)); }
  body.talking #mouthGroup { opacity: 1; animation: flap .46s infinite; }

  /* 呼吸 / 摇摆 */
  @keyframes breathe {
    0%, 100% { transform: translateY(0) scale(1, 1); }
    50% { transform: translateY(-1.6px) scale(1.006, 1.013); }
  }
  @keyframes sway {
    0%, 100% { transform: rotate(-.34deg); }
    50% { transform: rotate(.34deg); }
  }
  body.sleeping #rig { animation-duration: 7.2s; }
  body.talking #rig { animation-duration: 2.6s; }
  body.thinking #tilt { animation: think 2.6s ease-in-out infinite; }
  @keyframes think {
    0%, 100% { transform: rotate(0deg); }
    50% { transform: rotate(2.4deg); }
  }
  body.dizzy #tilt { animation: dizzy 1.5s ease-in-out 2; }
  @keyframes dizzy {
    0%, 100% { transform: rotate(0deg); }
    20% { transform: rotate(-5deg); }
    45% { transform: rotate(5deg); }
    70% { transform: rotate(-3.5deg); }
  }
  body.petting #pet { filter: drop-shadow(0 8px 16px rgba(0,0,0,.24)) brightness(1.03) saturate(1.06); }

  @keyframes flap {
    0%, 100% { transform: scaleY(.28); }
    18% { transform: scaleY(1); }
    36% { transform: scaleY(.42); }
    54% { transform: scaleY(.94); }
    72% { transform: scaleY(.32); }
    90% { transform: scaleY(.82); }
  }

  /* ---------------- 气泡 ---------------- */
  #bubble {
    position: absolute; left: 50%; top: 7px;
    transform: translateX(-50%) translateY(5px) scale(.9);
    transform-origin: 50% 100%;
    max-width: calc(100% - 18px); min-width: 34px;
    padding: 8px 13px; border-radius: 14px;
    background: rgba(255,255,255,.97);
    border: 1px solid rgba(120,90,80,.13);
    box-shadow: 0 6px 18px rgba(60,30,20,.16);
    font-size: 12.5px; line-height: 1.55; color: #3b2a26;
    opacity: 0; pointer-events: none;
    transition: opacity .2s ease, transform .28s cubic-bezier(.2,.9,.3,1.3);
    word-break: break-word; z-index: 6;
  }
  #bubble.show { opacity: 1; transform: translateX(-50%) translateY(0) scale(1); }
  #bubble::after {
    content: ""; position: absolute; left: 50%; bottom: -5.5px;
    width: 10px; height: 10px; background: inherit;
    border-right: 1px solid rgba(120,90,80,.13);
    border-bottom: 1px solid rgba(120,90,80,.13);
    transform: translateX(-50%) rotate(45deg);
    border-radius: 0 0 3px 0;
  }

  /* ---------------- 悬浮按钮 ---------------- */
  #toolbar {
    position: absolute; right: 7px; bottom: 7px; z-index: 7;
    display: flex; gap: 6px; opacity: 0; transform: translateY(7px);
    pointer-events: none;
    transition: opacity .2s ease, transform .25s ease;
  }
  body.hover #toolbar, body.listening #toolbar { opacity: 1; transform: none; pointer-events: auto; }
  #toolbar button {
    width: 28px; height: 28px; padding: 0; border: 0; border-radius: 50%;
    background: rgba(45,32,30,.52); color: #fff; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    backdrop-filter: blur(3px); transition: background .18s, transform .18s;
  }
  #toolbar button:hover { background: rgba(45,32,30,.78); transform: translateY(-1px); }
  #toolbar button svg { width: 15px; height: 15px; fill: currentColor; }
  #toolbar button.on { background: #ff5d73; animation: micPulse 1.2s infinite; }
  #toolbar button.disabled { opacity: .35; cursor: not-allowed; }
  @keyframes micPulse {
    0% { box-shadow: 0 0 0 0 rgba(255,93,115,.45); }
    70% { box-shadow: 0 0 0 9px rgba(255,93,115,0); }
    100% { box-shadow: 0 0 0 0 rgba(255,93,115,0); }
  }
  body.listening #pet { filter: drop-shadow(0 8px 16px rgba(255,93,115,.28)) brightness(1.02); }

  /* ---------------- 粒子 ---------------- */
  #fx { position: absolute; inset: 0; overflow: visible; pointer-events: none; z-index: 4; }
  #fx .particle {
    position: absolute; will-change: transform, opacity;
    font-weight: 700; text-shadow: 0 1px 2px rgba(0,0,0,.12);
  }

  @media (prefers-reduced-motion: reduce) {
    #tilt, #rig { animation: none; }
  }
</style>
</head>
<body>
<div id="stage" class="pywebview-drag-region">
  <!-- 背景层：必须放在最前面（最底层）。要显示背景就给 #bg 设 background -->
  <div id="bg"></div>

  <div id="bubble"><span id="bubbleText"></span></div>

  <!-- 悬停提示气泡 -->
  <div id="tip"></div>

  <!-- 右键菜单：文案用她自己的口气，动作走原有行为函数 -->
  <div id="menu">
    <button class="mi" data-act="poke">摸摸头</button>
    <button class="mi" data-act="say">说句话</button>
    <button class="mi" data-act="sleep">睡一会 / 起床</button>
    <button class="mi" data-act="top">置顶：开 / 关</button>
    <button class="mi" data-act="mute">语音：开 / 关</button>
    <div class="sep"></div>
    <button class="mi" data-act="chat">打开聊天</button>
    <button class="mi" data-act="sing">🎤 唱首歌</button>
    <button class="mi" data-act="sing_learn">👂 扒歌学习</button>
    <button class="mi" data-act="settings">设置</button>
    <button class="mi" data-act="more">更多（系统菜单）</button>
    <button class="mi" data-act="quit">退出</button>
  </div>

  <!-- 状态切换的轻提示 -->
  <div id="toast"></div>

  <div id="petBox">
    <div id="tilt">
      <div id="rig">
        <div id="jolt">
          <img id="pet" src="__IMG__" alt="礼貌小狗">
          <svg id="face" viewBox="0 0 839 839" aria-hidden="true">
            <defs>
              <linearGradient id="lidGrad" gradientUnits="userSpaceOnUse"
                              x1="0" y1="356" x2="0" y2="492">
                <stop offset="0" stop-color="#f7dbcf"/>
                <stop offset="0.5" stop-color="#f1cab8"/>
                <stop offset="1" stop-color="#e7b3a1"/>
              </linearGradient>
              <radialGradient id="blushGrad">
                <stop offset="0" stop-color="#ff8f9a" stop-opacity=".72"/>
                <stop offset=".55" stop-color="#ff8f9a" stop-opacity=".34"/>
                <stop offset="1" stop-color="#ff8f9a" stop-opacity="0"/>
              </radialGradient>
              <radialGradient id="mouthGrad" cx=".5" cy=".25" r=".85">
                <stop offset="0" stop-color="#8d3540"/>
                <stop offset="1" stop-color="#5b1f28"/>
              </radialGradient>
              <filter id="lidSoft" x="-30%" y="-30%" width="160%" height="160%">
                <feGaussianBlur stdDeviation="2.2"/>
              </filter>
            </defs>

            <g id="blush">
              <ellipse cx="266" cy="509" rx="48" ry="24" fill="url(#blushGrad)"/>
              <ellipse cx="474" cy="509" rx="48" ry="24" fill="url(#blushGrad)"/>
            </g>

            <!-- 粗粒度硬裁剪：只在遮罩位图没渲染出来时兜底，正常时被遮罩收在里面 -->
            <clipPath id="clipHardL"><ellipse cx="266.5" cy="429" rx="62" ry="52"/></clipPath>
            <clipPath id="clipHardR"><ellipse cx="470" cy="431" rx="60" ry="50"/></clipPath>

            <mask id="eyeMask" maskUnits="userSpaceOnUse" x="0" y="0" width="839" height="839">
              <image href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAUIAAABkCAAAAADWRGbfAAANKElEQVR42u1d+18axxafmZ3ZBRZwkWdcsDXXR6O32jTG3N4+0v7l95M2ty/b2CTVNMbEqJDwDCAssjvLzP0BiKCAILtL7HV+9SMz+51zzsz5nnPmAHA9rsf1uB7XA/4/f3rz4zng1xBeAjcAEJYwBABwUzcZGANMaM16ALBkQ22Hr40bABBPRaYwBNwsZ8smB+/BHPkb4Bjona6njSDVm4v5ELGEiEiENHEDAEAxMhcRIeBGdj9rcACaYFJu6pRx2yGEABEJw9P1tBA03qWrp/v5QcEIEfEGo4onOhcRYW8p5EZ2P6vTcqag0VGEEV4OP7cSmSJSpL2eFoTVvZ2Kq70sSnXKPgwUIUCiHJxdnI95lPe7DrGEEQCAmXpbj8vZMtUze7vJEuVUp2y4X8eXUAe3Eg2rcxHprBSykk8iM56mcmRqpWzp5INAERGXOxBfvLUYVwhp4tZhyZEgtVbIAjdMTksru3sZnZZSuRM2lEmCo50ciMjBWFidU8PRKdLex1MI/3wVXZxCgJvlbKmW2U/lMqVa3WQTVmFPWI2qC4uzQZkgOOiLOeCAUa2QKVM99fhpjnLA6YXrH1IKEXYRCADESnxxQQ1HFbeE0bkTGQmiL6AgAFjgBqXlTC61n0wdFvSJiqAoq6ufzUZiQS9BcIgzUhC9MZMbuVgsZXBuFC5c/5AQkuBsUIQAitH5xXjALRHUezcRRAg1lYMFbpyUMqnH31com6QIRuZW1j+NuqULAWz/i4AkDpjP91HJ5Ly6/cNF68dDbuXs1yteCCCeigZlsQ9+ABGRoPdXMEHyKrEbxg6ZpBX0qqvrK3MRtzDCwQkBBEgWQ5QDVgJ/iTULpBBCKbzyhYIAgHjQbmJ/xI87ViIgYvgImhyEJDS/urGqyiK6hAEQOQAMTQf8NZ2NCSFExBWYnp5WUC+HpGvWqM/TtVoIEYSTtIOzX/77k4gHwUu7XlJoKY8yNTYWhEiUQzF1OSyhC7cS+71I6DriOOOTvNOQ4NJyTELjbYJX2UrWBjgsF0KIPNG5pYW4OjuEMiDEu/abmSfH1QkeJgAR2Sui8UyBK+AXSUoz2GUhRHL89sZyPOBx4dGdncZx+vV+sQHhxCQRwjENCRIVkYhT2/v9lRlfhGDi7v07M/0P4UGDs1rm1aHu4ZO7XXM+riGBgpwQI3O/9ldm4aJ/v/vdxkd+cqlTAfIG1Q1x2mPqjcnIoKQuqG405oEGsRwMesw6bfTeDmGgEPtn7357NyELl1wEJB4lcvOmv1rWJ6LKEPtC0z5JGPdnBMnr9xCg1xujQiiGb937Zj0ho3HmDs3EXNWiZk7oVuMLh30CHJ8p8ygBclw2+Gi2UPDNf7uxGPWgcfYPSVxmZQ0kBxxp9kmh6PZ6XciCmynyzLhAsQJ6OXv9pRB5Z+/dX4240bhHoiDJbiI0DNNpZUZy/M6Xq1ERWmITREmATNcZhLDbvxD6455Y/3ot6kIWzO5WIgFYqzuMIZITd7+7G/cgiwgLya8I1Tpxu90u3HnQ436ujRS9ff+2NTuIPKoS8RN4pDFHOZrE3e/ujmHJz36FewaflKcAgmcYMNyHG0TyJ/duW7WDSCIiRoAf1ZiTHM2d+xYiCACSAnP0MzeBZxgw3IcbRPLiqmrZ/FCQE4CZekp3kKNZu3c7LltJEyFXwi+4MGRF/owMOpGb3CASgwmfYKlpp8eZ40aDO8bRfLkU9VhLtGHFhxACDHQxeL2kMLT8rwCCxIWtNe7qyusicIrCJsGlW2NxNL1plGa0rdtX62ULiS8wjQYyg5cZgu/mF9y7l9edCpmMydEMoFG6HW/c6yxDCNlANZPoHa9sHDsihohIomAT2csbRleMuYctxCK2hatH7hmUfypqTgghViJT2J6fZrTyNlkyB0Fo3+xI9CleETkghYIcX4yJ9gRtaH7vj99SHeEU7OTsUFQiStF04kqzuBC0J3LIjKP//vhXlnZIxvmjbMGu2QESY4txWXBACGdvzdo1j3l8sJc+Yf0htPcr7dyf7mkWQ3ZNI3im3F34nJNCPBVR8FW0UQ6JAZJm1v4ZJoMuNbYdyHaflE6JAQl/mk+XzUZ/CCFCNgbP7d0gR2ZBnpmV/ayu9aUZ7P5G6Eh2g62zCL659YKW1PtB6IymXe1BIquFDlU+A5fgTdhl7zngALDJJohYqMo5Q2vFlXG3HZTVW4v23DqYWaccsKIjVA3njAHLiZJOxmSjApKtXBfczW7E72zc9NpyHaCFw4LBeXUnT21HkNFKESBIXHaZdRL5HHgft1gnfCYhdOPziD1CaBz+sF3l3Cgc2h8Npfkd6EVicDYo2aXKcaJ4W6wT7igrmFldX5mzmuk9lcJnvxQZ57Ruu4/MjMMfdkTkW/7GtvxQ5JlB+SekwxYi7PIE4svrl0sIHdZAUc1ggHNge6IXzVeJgEOuNdNGD8grt/YHtyNOanxhae6SCaFDmY/phbxUNIcqQxjfFjJREj1TdjIap2l3uBWs+WZNjYVsFEEkzn4V2M0MV4ZgAesfikU+vjMjOZHn3ZLC0K31iIvY6dqRkCu6Uh6uDGFsejw2t7QQj6gRR4oNcDvipPgQtFcyFDlmcnAmCGvTleOLlUTA7XKm2AA7lprfLIk5G4S1hY5S17/62HupxNwxIBw/oXboMgT7yyiwEp9RsGPFGqh1hGlVvfE3cF9tj4D2lUJaeD5dH1Bad8WGE4zaqd7i1nX+YXFJDUcVp0zwlR/MqGqte0VLCvP1zE5YnVOjasRzjeEQCNYyz58XaAeEzChpabcSjXx02+KEsr8pglpy65fHLcKkfSI3mF4tvXW/KFCeuMbwQgSPNh/8nqrSbsqVgwbTNc1gAFxjeJEWJzf/s3lU68Fac2BqRwAAmLi2h4MGzT56sHmaOI7PiShAWFKla6D6jkbl1eajZP8gKNOSxB/1Y8HOC9UVN4Sp7e2UNiCzi2mp7bmw6LVLlZlpTPjVlbEP40ebryqNQfmFjcqrTT9J2EW+mqVs2byyAHJWO9p88KgzN65XojDNPsLAtCkE0NCSu2njqooho9XU7w82k131M7jnmQ3o8Yo9UQCa331RoFdVBo383pNftpLdVVy4983RyO7bEotqaIfPDrXGFZVB/d2zh5u7Z58YwL3vjqlStlCwISJK87u7+SsohBxwwE6yu5s/7bw7+2oN7oM3NbT0/obVDrNDQmj5vYmZdcqN3NNfn+4VzhX44361FVqqlD2mPOFGFqamOCOEzDRMhqxVnqOCrqceP0lVKR+6Kp7p1DAZMyKSdakpDgmhWc4UFSvjC43q3sNdzSilcr1eCcEDiny0I04Lqi+UCErQoqSQF04IoZF+Pi8rbutykmjh2c/bWqPPw5qDHDlunhTfvnxZdnlFS643rPby4VaWOsDJI0GnomRVqik72Xv4W+q4rvcuY8WDvzmVF30ZDj4JS5YsJZNyonCnUd0zXq7d91oWwzDLmeJlH5xihgE1k5nYNT7vwFktfZA5ccIxoTktB+c/tuxGNtivxxfeh/RcgwSnpXFvN8zQko8eperMET+sApKv/+GxjCsZeEsShqgdpRT5p2U8phK/fbb5cOutQ88mcQ4Ej99NLDkG+cnB04MTPsYTkNzIPo3NhsaxLJwbmd9/2n6Vcex9C5rdIsiSEAa/KMceDyVBuTclc5w1UL30148PD6rOUTSsloQAgIT7oqc/h3BM9ME59ng426JfniZlZr1WyrzZ+eOg1OCORtkAYEaEQACJNLoKtR+zpIXD3LuBOfbYdqeTFg5Tyf3km8OK6XSkktOCKkKIFTU8InHHGg1BQO00+3e5QTn2eIRCjkvpA6sffP8klSnV6qbz0V79SMEQiura6oxMRnmxwyxXpCkPalYP/PxOH7R4PHwhhzCis9wsdzJKB09+zdcn8dY/q6XyBAJIwunCclw586D+IDyZnnopzatuBJipV4rFxqDF4+ELOVzBeFAaxjY3OwswUzc5N4uv/nydqzLH8Wt7BgAAVKmmX8xHu9s6nG1Q0A0szW7VC2sRAlmxopuDV4+HLuRwyQsbC/KAmqJ2T4Zm94tmHxFmZHa3D+qNCTKlADAtVTp62t1cpN3u5Ny7n82Th9WS+5mcKsIhyrXw8IUcclpPy+9ris41sWj3DWn1YGl2s2FmOZOv0okz9lRLd7e4aTfdOftOoKKG3Qgw1tAO3xwoGA5RrjWsLaQQVOjxloi8y1/LAjpttHJK67S617Q6AbWRHLkPkD2v8zAdAlR826HHvaQQiurap2ECWbFSr2pZAgG4uFwLD68RevaYQDRF46Ep1NF06tSHafZQav+p1dnrA2lfxAEHrKGfYtaJZ4ceh9Pplvoaeh0O1cYKj+K8UwiQvv+YeuFp06lzUvi+Kxr/4Fpoda6oC89TYCuVtvrqw94hRr7pScHZoAjPS2FHP7kPu5UbuCjVva2+zK72Wa3+Ox0N+K5GEzwwWoemET4GOjDH9bge1+N6XI+//fgf1NJ1F9lFuW8AAAAASUVORK5CYII=" x="207" y="380" width="322" height="100" preserveAspectRatio="none"/>
            </mask>
            <g mask="url(#eyeMask)">
              <g id="lidL" clip-path="url(#clipHardL)">
                <path d="M 190 42 L 345 42 L 345 459.4 L 321 459.4 L 311 458.3 L 308 459.5 L 305 460.4 L 302 462.0 L 299 463.2 L 296 464.2 L 293 464.8 L 290 465.2 L 287 465.6 L 284 466.0 L 281 466.4 L 278 466.6 L 275 466.8 L 272 467.0 L 269 467.0 L 266 467.2 L 263 466.8 L 260 466.4 L 257 465.6 L 254 464.8 L 251 461.8 L 248 459.0 L 245 456.0 L 242 452.8 L 239 449.2 L 236 447.2 L 233 444.8 L 230 442.0 L 227 440.8 L 224 439.3 L 214 440.7 Z" fill="url(#lidGrad)"/>
                <path d="M 214 442.7 L 224 441.3 L 227 442.8 L 230 444.0 L 233 446.8 L 236 449.2 L 239 451.2 L 242 454.8 L 245 458.0 L 248 461.0 L 251 463.8 L 254 466.8 L 257 467.6 L 260 468.4 L 263 468.8 L 266 469.2 L 269 469.0 L 272 469.0 L 275 468.8 L 278 468.6 L 281 468.4 L 284 468.0 L 287 467.6 L 290 467.2 L 293 466.8 L 296 466.2 L 299 465.2 L 302 464.0 L 305 462.4 L 308 461.5 L 311 460.3 L 321 461.4" fill="none" stroke="#b07a6c" stroke-opacity=".34" stroke-width="7" filter="url(#lidSoft)"/>
                <path d="M 214 440.7 L 224 439.3 L 227 440.8 L 230 442.0 L 233 444.8 L 236 447.2 L 239 449.2 L 242 452.8 L 245 456.0 L 248 459.0 L 251 461.8 L 254 464.8 L 257 465.6 L 260 466.4 L 263 466.8 L 266 467.2 L 269 467.0 L 272 467.0 L 275 466.8 L 278 466.6 L 281 466.4 L 284 466.0 L 287 465.6 L 290 465.2 L 293 464.8 L 296 464.2 L 299 463.2 L 302 462.0 L 305 460.4 L 308 459.5 L 311 458.3 L 321 459.4" fill="none" stroke="#3a2420" stroke-width="5" stroke-linecap="round"/>
              </g>
              <g id="lidR" clip-path="url(#clipHardR)">
                <path d="M 396 42 L 545 42 L 545 446.2 L 521 446.2 L 511 445.3 L 508 446.2 L 505 447.2 L 502 450.4 L 499 454.2 L 496 457.0 L 493 459.4 L 490 462.6 L 487 464.0 L 484 464.2 L 481 464.8 L 478 465.6 L 475 465.2 L 472 465.0 L 469 465.0 L 466 465.0 L 463 464.8 L 460 464.6 L 457 464.4 L 454 464.2 L 451 463.8 L 448 463.6 L 445 463.2 L 442 462.4 L 439 461.6 L 436 460.8 L 433 460.2 L 430 459.7 L 420 460.2 Z" fill="url(#lidGrad)"/>
                <path d="M 420 462.2 L 430 461.7 L 433 462.2 L 436 462.8 L 439 463.6 L 442 464.4 L 445 465.2 L 448 465.6 L 451 465.8 L 454 466.2 L 457 466.4 L 460 466.6 L 463 466.8 L 466 467.0 L 469 467.0 L 472 467.0 L 475 467.2 L 478 467.6 L 481 466.8 L 484 466.2 L 487 466.0 L 490 464.6 L 493 461.4 L 496 459.0 L 499 456.2 L 502 452.4 L 505 449.2 L 508 448.2 L 511 447.3 L 521 448.2" fill="none" stroke="#b07a6c" stroke-opacity=".34" stroke-width="7" filter="url(#lidSoft)"/>
                <path d="M 420 460.2 L 430 459.7 L 433 460.2 L 436 460.8 L 439 461.6 L 442 462.4 L 445 463.2 L 448 463.6 L 451 463.8 L 454 464.2 L 457 464.4 L 460 464.6 L 463 464.8 L 466 465.0 L 469 465.0 L 472 465.0 L 475 465.2 L 478 465.6 L 481 464.8 L 484 464.2 L 487 464.0 L 490 462.6 L 493 459.4 L 496 457.0 L 499 454.2 L 502 450.4 L 505 447.2 L 508 446.2 L 511 445.3 L 521 446.2" fill="none" stroke="#3a2420" stroke-width="5" stroke-linecap="round"/>
              </g>
            </g>

            <g id="mouthGroup">
              <ellipse cx="370" cy="533" rx="16" ry="14" fill="url(#mouthGrad)"/>
              <ellipse cx="370" cy="540" rx="10" ry="5" fill="#e88d95" opacity=".88"/>
            </g>
          </svg>
          <div id="fx"></div>
        </div>
      </div>
    </div>
  </div>

  <div id="toolbar">
    <button id="btnChat" type="button" title="聊天（双击我也可以）">
      <svg viewBox="0 0 24 24"><path d="M4 4h16a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9l-5 4v-4H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm3 5.2a1.2 1.2 0 1 0 0 2.4 1.2 1.2 0 0 0 0-2.4Zm5 0a1.2 1.2 0 1 0 0 2.4 1.2 1.2 0 0 0 0-2.4Zm5 0a1.2 1.2 0 1 0 0 2.4 1.2 1.2 0 0 0 0-2.4Z"/></svg>
    </button>
    <button id="btnMic" type="button" title="点一下说话，再点一下结束">
      <svg viewBox="0 0 24 24"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5.3-3a.7.7 0 1 0-1.4 0 3.9 3.9 0 0 1-7.8 0 .7.7 0 0 0-1.4 0 5.3 5.3 0 0 0 4.6 5.23V20H9.7a.7.7 0 0 0 0 1.4h4.6a.7.7 0 0 0 0-1.4h-1.6v-2.77A5.3 5.3 0 0 0 17.3 12Z"/></svg>
    </button>
  </div>
</div>

<script>
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var stage = $('stage'), box = $('petBox'), jolt = $('jolt');
  var face = $('face'), img = $('pet'), bubble = $('bubble'), bubbleText = $('bubbleText');
  var fx = $('fx'), toolbar = $('toolbar'), btnChat = $('btnChat'), btnMic = $('btnMic');

  var api = function () {
    return (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;
  };
  var clamp = function (v, a, b) { return v < a ? a : (v > b ? b : v); };
  var rand = function (a, b) { return a + Math.random() * (b - a); };
  var pick = function (arr) { return arr[Math.floor(Math.random() * arr.length)]; };
  var trim = function (s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); };

  var LID_OPEN = -170, LID_SQUINT = -40, LID_DROWSY = -58, LID_CLOSED = 0;

  var state = {
    mood: 68,
    lidBase: LID_OPEN,
    sleeping: false,
    drowsy: false,
    petting: false,
    dragging: false,
    pressed: false,
    justDragged: false,
    dizzy: false,
    thinking: false,
    micOn: false,
    micDead: false,
    voiceOn: true,
    soundOn: true,
    packOn: '',
    packStrict: false,
    voiceUrl: '',
    lastInteract: Date.now(),
    lastSay: 0,
    lastClick: 0,
    lastPetLine: 0,
    lastPetMood: 0,
    lastHoverSay: 0,
    thinkBubble: false,
    pokes: [],
    idleTimer: null,
    petEnd: null,
    petFx: null,
    zzz: null,
    sayTimer: null,
    pressTimer: null,
    dragAnim: null,
    micTimer: null,
    mouthTimer: null,
    talkTimer: null,
    micBuf: '',
    micLast: 0,
    micStart: 0
  };

  /* ============================================================
   * 1. 表情：眼睑 / 腮红 / 嘴
   * ============================================================ */
  function setLidBase(v) {
    state.lidBase = v;
    face.style.setProperty('--lidY', v + 'px');
  }

  function blink(times) {
    if (state.sleeping || state.lidBase === LID_CLOSED) return;
    times = times || 1;
    var n = 0;
    (function once() {
      face.classList.add('blink');
      setTimeout(function () {
        face.classList.remove('blink');
        n += 1;
        if (n < times) setTimeout(once, 135);
      }, 82);
    })();
  }

  function baseBlush() {
    return 0.1 + clamp(state.mood / 100, 0, 1) * 0.26;
  }

  function setBlush(v) {
    face.style.setProperty('--blush', clamp(v, 0, 1).toFixed(2));
  }

  function setMouth(v, ms) {
    document.body.classList.add('mouth-on');
    face.style.setProperty('--mouth', String(v));
    clearTimeout(state.mouthTimer);
    if (ms) {
      state.mouthTimer = setTimeout(function () {
        document.body.classList.remove('mouth-on');
      }, ms);
    }
  }

  function closeMouthSoon() {
    setTimeout(function () { document.body.classList.remove('mouth-on'); }, 120);
  }

  function startTalking(ms) {
    document.body.classList.add('talking');
    if (typeof setState === 'function') setState('talking');   // 状态机：说话中
    clearTimeout(state.talkTimer);
    state.talkTimer = setTimeout(stopTalking, clamp(ms || 1600, 500, 15000));
  }

  function stopTalking() {
    document.body.classList.remove('talking');
    // 说完回到上一个状态（困了就继续困，开心就继续开心）
    if (typeof setState === 'function' && state.mode === 'talking') {
      setState(state.sleeping ? 'sleepy' : 'idle');
    }
  }

  /* ============================================================
   * 2. 气泡
   * ============================================================ */
  var typer = null;

  function cleanBubble(text) {
    return trim(String(text == null ? '' : text)
      .replace(/\[STICKER:[^\]]*\]/gi, '')
      .replace(/\[DRAW:[^\]]*\]/gi, '')
      .replace(/\|\|/g, '，'));
  }

  function showBubble(text, silent) {
    var t = cleanBubble(text);
    if (!t) return;
    if (t.length > 70) t = t.slice(0, 68) + '…';
    clearInterval(typer);
    bubble.classList.add('show');
    bubbleText.textContent = '';
    var i = 0, step = t.length > 34 ? 2 : 1;
    typer = setInterval(function () {
      i += step;
      bubbleText.textContent = t.slice(0, i);
      if (i >= t.length) clearInterval(typer);
    }, 26);
    return t;
  }

  function hideBubble() {
    clearInterval(typer);
    bubble.classList.remove('show');
    bubbleText.textContent = '';
  }

  /* ============================================================
   * 3. 台词库
   * ============================================================ */
  var CLICK_LINES = [
    '汪！', '嗯？', '干嘛呀～', '在的在的', '戳我干嘛', '嘿！', '别闹啦',
    '要抱抱吗', '我在听呢', '再摸一下', '你今天好闲哦', '我刚刚在发呆',
    '叫我做什么呀', '汪呜～', '诶嘿'
  ];
  var PET_LINES = [
    '呼噜噜…', '好舒服～', '再摸摸', '嘿嘿', '喜欢你', '别停嘛', '头这里，对',
    '汪…好痒'
  ];
  var DRAG_LINES = ['呀！', '轻点轻点', '要飞起来了', '放我下来～', '别把我摔了'];
  var DROP_LINES = ['咚', '站稳啦', '吓我一跳', '呼…落地了'];
  var DIZZY_LINES = ['晕了晕了…', '别戳啦～', '眼冒金星', '让我缓缓'];
  var WAKE_LINES = ['唔…醒了', '我刚梦到小饼干', '天亮了吗', '再睡五分钟…'];
  var IDLE_LINES = [
    '今天也要好好的呀', '我一直在这儿', '要不要摸摸头？', '嘿，休息一下眼睛吧',
    '你在忙什么呀', '汪…我是说，加油', '有点想吃小饼干', '窗外好像有鸟',
    '陪我待一会儿嘛', '我刚打了个小盹', '安静的时候也很好', '你听，风扇在转'
  ];
  var IDLE_LOW = ['理理我嘛…', '有点无聊', '你是不是把我忘了', '我一个人也行…吧'];
  var THINK_LINES = ['…', '我想想', '嗯…'];

  function idleLine() {
    if (state.mood < 34) return pick(IDLE_LOW);
    if (state.drowsy) return pick(['困…', '眼皮好重', '哈欠…']);
    return pick(IDLE_LINES);
  }

  /* ============================================================
   * 4. 说话（气泡 + SAPI 语音）
   * ============================================================ */
  var sayQueue = [], saying = false, sayGen = 0;

  /* 台词 -> 意图：语音包按这个 key 找你的录音 */
  var LINE_GROUPS = [
    ['click', CLICK_LINES], ['pet', PET_LINES], ['drag', DRAG_LINES],
    ['drop', DROP_LINES], ['dizzy', DIZZY_LINES], ['wake', WAKE_LINES],
    ['idle', IDLE_LINES], ['idle', IDLE_LOW], ['think', THINK_LINES]
  ];

  function intentOf(text) {
    var t = trim(text);
    for (var i = 0; i < LINE_GROUPS.length; i++) {
      var arr = LINE_GROUPS[i][1];
      for (var j = 0; j < arr.length; j++) {
        if (arr[j] === t) return LINE_GROUPS[i][0];
      }
    }
    return '';
  }

  function speakText(text, opts) {
    var clean = trim(text).replace(/\[[^\]]*\]/g, '').slice(0, 80);
    if (!clean) return 0;
    var est = clamp(clean.length * 210, 1200, 14000);
    var a = api();
    if (a && a.speak) {
      try {
        var intent = (opts && opts.intent) || intentOf(text);
        voiceIdle = false;          /* 这句开始了：队列要等它结束 */
        a.speak(clean, intent).then(function (r) {
          if (r && r.silent) {
            /* 没有能念对这句的引擎：不动嘴、不放任何对不上的声音 */
            stopTalking();
            voiceFireEnd();
            return;
          }
          if (r && r.ok) {
            if (r.audio && !r.played) {
              /* Python 播不了（非 wav / 没设备）时，页面用同一份文件补上 */
              playVoiceFile(r.audio, clean);
            } else {
              /* Python 自己播：按它报的时长等，到了就放行下一句 */
              var d = clamp(Number(r.duration || 1.4) * 1000, 800, 16000);
              startTalking(d);
              setTimeout(voiceFireEnd, d + 150);
            }
          } else if (!opts || opts.fallback !== false) {
            speechFallback(clean);
          } else {
            voiceFireEnd();
          }
        })['catch'](function () { speechFallback(clean); });
        return est;
      } catch (e) { /* fallthrough */ }
    }
    speechFallback(clean);
    return est;
  }

  /* 对话音源：只播本地音频文件，不再调用 speechSynthesis（那是系统嗓子） */
  var voiceAudio = null;
  var voiceIdle = true;             /* 现在没有正在播的语音 */
  var voiceWaiters = [];            /* 等「这句真的播完」的回调 */
  var voiceUnlockPending = null;    /* 被自动播放策略拦下的那一句 */
  var voiceUnlockArmed = false;

  function voiceFireEnd() {
    voiceIdle = true;
    var ws = voiceWaiters; voiceWaiters = [];
    for (var i = 0; i < ws.length; i++) { try { ws[i](); } catch (e) { } }
  }
  /* 等这一句播完再走下一步；带兜底超时，绝不会把队列卡死 */
  function waitVoiceEnd(done, maxMs) {
    if (voiceIdle) { done(); return; }
    var fired = false, tmr = 0;
    function fire() { if (fired) return; fired = true; clearTimeout(tmr); done(); }
    tmr = setTimeout(fire, maxMs || 9000);
    voiceWaiters.push(fire);
  }
  /* 浏览器自动播放限制：被拦下就等一次用户手势补播，不静默放弃 */
  function armVoiceUnlock() {
    if (voiceUnlockArmed) return;
    voiceUnlockArmed = true;
    ['click', 'keydown', 'touchstart', 'pointerdown'].forEach(function (ev) {
      document.addEventListener(ev, function once() {
        document.removeEventListener(ev, once);
        var pend = voiceUnlockPending;
        if (!pend) return;
        voiceUnlockPending = null;
        voiceIdle = false;
        var p2 = null;
        try { p2 = pend.audio.play(); } catch (e) { }
        if (p2 && p2['catch']) p2['catch'](function () { voiceFireEnd(); });
      });
    });
  }

  function stopVoiceAudio() {
    if (!voiceAudio) return;
    var a = voiceAudio;
    voiceAudio = null;
    try { a.pause(); } catch (e) { }
    voiceFireEnd();                 /* 主动掐断也要放行等待者，别挂着 */
  }

  function playVoiceFile(url, text) {
    if (!url || !state.voiceOn) { voiceFireEnd(); return; }
    stopVoiceAudio();
    try {
      var a = new Audio(url);
      voiceAudio = a;
      voiceIdle = false;            /* 开始播了：后面的队列要等它结束 */
      a.volume = 0.95;
      var ms = clamp(String(text || '').length * 210, 1200, 14000);
      a.onplay = function () { startTalking(ms); };
      a.onended = function () { if (voiceAudio === a) voiceAudio = null; stopTalking(); voiceFireEnd(); };
      a.onerror = function () { if (voiceAudio === a) voiceAudio = null; stopTalking(); voiceFireEnd(); };
      var p = a.play();
      if (p && p['catch']) p['catch'](function () {
        /* 被自动播放策略拦下：记住这句，等用户点一下就补播 */
        voiceUnlockPending = { audio: a, text: text };
        armVoiceUnlock();
      });
    } catch (e) { voiceFireEnd(); }
  }

  function speechFallback(text) {
    /* 兜底也绝不放"对不上这句话"的音频：
       以前这里无脑播 state.voiceUrl（固定的 chat.wav），
       于是任何一句台词都会配同一段跟内容无关的声音。没引擎就安静。 */
    stopTalking();
    voiceFireEnd();                 /* 没声音也得放行队列，别让下一句等空 */
  }

  function say(text, opts) {
    opts = opts || {};
    var t = cleanBubble(text);
    if (!t) return;
    if (opts.fromChat) {
      state.thinkBubble = false;
      if (state.sleeping) wake(false);
    }
    if (opts.interrupt) {
      sayGen += 1;
      sayQueue = [];
      clearTimeout(state.sayTimer);
      saying = false;
      stopVoiceAudio();
    }
    sayQueue.push({ text: t, opts: opts });
    /* 别再丢句子了：以前超过 3 句就把最旧的扔掉，于是后面的台词直接消失 */
    if (sayQueue.length > 12) sayQueue.splice(0, sayQueue.length - 12);
    if (!saying) nextSay();
  }

  function nextSay() {
    if (!sayQueue.length) { saying = false; return; }
    saying = true;
    state.thinkBubble = false;
    var item = sayQueue.shift(), text = item.text, opts = item.opts;
    var gen = sayGen;

    state.lastSay = Date.now();
    if (opts.fromChat) addMood(1);
    if (opts.happy) { setBlush(0.82); spawnHearts(3); }
    else if (!state.petting) setBlush(baseBlush());

    showBubble(text);
    var intent = intentOf(text);
    // 有语音包时，台词优先用你自己的录音，即使原本设计成「只出气泡不出声」
    var speak = (opts.speak !== false || (state.packOn && intent)) && state.voiceOn && !state.micOn;
    // 聊天页自己管播报（去重 / 抢占 / 时长同步）时，这里只出气泡，免得两个声音叠一起
    if (opts.mute) speak = false;
    var dur = speak ? speakText(text, { intent: intent, fallback: opts.fallback }) : 0;
    var hold = opts.hold || clamp(text.length * 150, 1600, 5200);
    var wait = Math.max(hold, dur + 420);

    clearTimeout(state.sayTimer);
    state.sayTimer = setTimeout(function () {
      if (gen !== sayGen) return;
      /* 关键：等这句真的播完，再收气泡、放下一句。
         以前纯按估算时长推进，音频比估算长就会被下一句掐断（听起来就是"播几秒就没了"）。 */
      waitVoiceEnd(function () {
        if (gen !== sayGen) return;
        hideBubble();
        setTimeout(function () {
          if (gen !== sayGen) return;
          saying = false;
          nextSay();
        }, 220);
      }, 9000);
    }, wait);
  }

  /* ============================================================
   * 5. 粒子 / 音效
   * ============================================================ */
  function spawnParticle(glyph, o) {
    o = o || {};
    var el = document.createElement('span');
    el.className = 'particle';
    el.textContent = glyph;
    el.style.left = (o.x == null ? 150 : o.x) + 'px';
    el.style.top = (o.y == null ? 150 : o.y) + 'px';
    el.style.color = o.color || '#ff7d9c';
    el.style.fontSize = (o.size || 15) + 'px';
    el.style.opacity = '0';
    fx.appendChild(el);
    var dx = o.dx == null ? rand(-24, 24) : o.dx;
    var dy = o.dy == null ? -rand(55, 95) : o.dy;
    var dur = o.dur || rand(1100, 1700);
    var anim = el.animate([
      { transform: 'translate(-50%,-50%) scale(.35) rotate(0deg)', opacity: 0 },
      { transform: 'translate(-50%,-50%) scale(1) rotate(6deg)', opacity: 1, offset: .18 },
      { transform: 'translate(calc(-50% + ' + dx + 'px), calc(-50% + ' + dy + 'px)) scale(.85) rotate(-8deg)', opacity: 0 }
    ], { duration: dur, easing: 'cubic-bezier(.2,.7,.3,1)' });
    anim.onfinish = function () { el.remove(); };
  }

  function spawnHearts(n) {
    for (var i = 0; i < (n || 1); i++) {
      setTimeout(function () {
        spawnParticle(pick(['♥', '♥', '♡']), {
          x: rand(105, 195), y: rand(115, 175),
          color: pick(['#ff7d9c', '#ff9fbb', '#ff5d83']),
          size: rand(13, 21)
        });
      }, i * 90);
    }
  }

  function spawnSparkle(n) {
    for (var i = 0; i < (n || 1); i++) {
      spawnParticle(pick(['✦', '✧', '·']), {
        x: rand(60, 240), y: rand(70, 190),
        color: pick(['#ffd56b', '#ffe9a6', '#fff3c4']),
        size: rand(10, 17), dur: rand(900, 1500)
      });
    }
  }

  var actx = null;
  function unlockAudio() {
    if (actx) return;
    try {
      var C = window.AudioContext || window.webkitAudioContext;
      if (C) actx = new C();
    } catch (e) { actx = null; }
  }
  document.addEventListener('pointerdown', unlockAudio, true);

  function tone(freq, dur, vol, type) {
    if (!actx || !state.soundOn) return;
    try {
      if (actx.state === 'suspended') actx.resume();
      var o = actx.createOscillator(), g = actx.createGain();
      o.type = type || 'sine';
      o.frequency.value = freq;
      var t = actx.currentTime;
      g.gain.setValueAtTime(0, t);
      g.gain.linearRampToValueAtTime(vol || .025, t + .012);
      g.gain.exponentialRampToValueAtTime(.0001, t + dur);
      o.connect(g); g.connect(actx.destination);
      o.start(t); o.stop(t + dur + .03);
    } catch (e) { /* ignore */ }
  }

  /* ============================================================
   * 6. 心情
   * ============================================================ */
  function addMood(v) {
    state.mood = clamp(state.mood + v, 0, 100);
    if (!state.petting) setBlush(baseBlush());
    saveMoodSoon();
  }

  var moodSaveTimer = null;
  function saveMoodSoon() {
    if (moodSaveTimer) return;
    moodSaveTimer = setTimeout(function () { moodSaveTimer = null; saveMood(); }, 800);
  }

  function saveMood() {
    try {
      localStorage.setItem('pp_mood_v2', JSON.stringify({ mood: state.mood, ts: Date.now() }));
    } catch (e) { /* ignore */ }
  }

  function loadMood() {
    try {
      var d = JSON.parse(localStorage.getItem('pp_mood_v2') || '{}');
      if (typeof d.mood === 'number') {
        var hours = Math.max(0, (Date.now() - (d.ts || Date.now())) / 3600000);
        state.mood = clamp(d.mood - hours * 1.5, 8, 100);
      }
    } catch (e) { /* ignore */ }
  }

  /* ============================================================
   * 7. 动作
   * ============================================================ */
  function jump() {
    jolt.animate([
      { transform: 'translateY(0) scale(1,1)' },
      { transform: 'translateY(-13px) scale(.985,1.025)', offset: .34 },
      { transform: 'translateY(0) scale(1.045,.945)', offset: .72 },
      { transform: 'translateY(0) scale(1,1)' }
    ], { duration: 520, easing: 'ease-out' });
  }

  function tiltTo(deg, ms) {
    jolt.animate([
      { transform: 'rotate(0deg)' },
      { transform: 'rotate(' + deg + 'deg)', offset: .35 },
      { transform: 'rotate(' + (deg * .55) + 'deg)', offset: .7 },
      { transform: 'rotate(0deg)' }
    ], { duration: ms || 1500, easing: 'ease-in-out' });
  }

  function tiltRandom() {
    tiltTo(Math.random() < .5 ? -2.6 : 2.6, rand(1300, 1900));
  }

  function lookAround() {
    var dx = Math.random() < .5 ? -3.5 : 3.5;
    jolt.animate([
      { transform: 'translateX(0) rotate(0deg)' },
      { transform: 'translateX(' + dx + 'px) rotate(' + (dx > 0 ? .8 : -.8) + 'deg)', offset: .45 },
      { transform: 'translateX(0) rotate(0deg)' }
    ], { duration: rand(1700, 2400), easing: 'ease-in-out' });
  }

  function breatheDeep() {
    jolt.animate([
      { transform: 'scale(1,1)' },
      { transform: 'scale(1.012,1.026)', offset: .5 },
      { transform: 'scale(1,1)' }
    ], { duration: 2600, easing: 'ease-in-out' });
  }

  function stretch() {
    setLidBase(LID_SQUINT);
    setMouth(.65, 700);
    jolt.animate([
      { transform: 'scale(1,1)' },
      { transform: 'translateY(-4px) scale(.97,1.05)', offset: .4 },
      { transform: 'scale(1,1)' }
    ], { duration: 900, easing: 'cubic-bezier(.2,.8,.3,1.1)' });
    setTimeout(function () {
      if (!state.petting && !state.sleeping) setLidBase(LID_OPEN);
    }, 850);
  }

  function yawn() {
    if (state.sleeping) return;
    setLidBase(LID_CLOSED);
    setMouth(.92, 900);
    setTimeout(function () {
      if (!state.petting && !state.sleeping) setLidBase(state.drowsy ? LID_DROWSY : LID_OPEN);
    }, 780);
    say('哈啊～', { speak: false, hold: 1200 });
  }

  function hum() {
    spawnParticle('♪', { x: rand(175, 205), y: rand(95, 125), color: '#8ec9ff', size: 16, dy: -70 });
    if (Math.random() < .3) say('哼哼～', { speak: false, hold: 1200 });
  }

  function dizzy() {
    if (state.dizzy) return;
    state.dizzy = true;
    document.body.classList.add('dizzy');
    setLidBase(LID_SQUINT);
    setBlush(.2);
    say(pick(DIZZY_LINES), { interrupt: true, speak: false });
    tone(300, .18, .03, 'triangle');
    setTimeout(function () {
      state.dizzy = false;
      document.body.classList.remove('dizzy');
      setLidBase(LID_OPEN);
      setBlush(baseBlush());
    }, 2400);
  }

  function sleep() {
    if (state.sleeping) return;
    state.sleeping = true;
    state.drowsy = false;
    document.body.classList.add('sleeping');
    document.body.classList.remove('drowsy');
    setLidBase(LID_CLOSED);
    setBlush(baseBlush() * .8);
    hideBubble();
    sayGen += 1;
    sayQueue = [];
    saying = false;
    clearTimeout(state.sayTimer);
    clearInterval(state.zzz);
    state.zzz = setInterval(function () {
      if (!state.sleeping) return;
      spawnParticle('Z', {
        x: rand(175, 205), y: rand(85, 110), color: '#9db7ff',
        size: rand(12, 19), dx: rand(8, 22), dy: -rand(55, 80), dur: 2600
      });
    }, 3600);
  }

  function wake(animate) {
    if (!state.sleeping) return;
    state.sleeping = false;
    document.body.classList.remove('sleeping');
    clearInterval(state.zzz);
    state.zzz = null;
    setLidBase(LID_OPEN);
    if (animate !== false) {
      stretch();
      say(pick(WAKE_LINES), { speak: false, hold: 2200 });
    }
  }

  function touch() {
    state.lastInteract = Date.now();
    if (state.sleeping) wake(true);
    else if (state.drowsy) {
      state.drowsy = false;
      document.body.classList.remove('drowsy');
      setLidBase(LID_OPEN);
    }
  }

  /* ============================================================
   * 8. 点击 / 抚摸 / 拖拽
   * ============================================================ */
  function poke() {
    touch();
    if (state.sleeping) return;
    if (state.dizzy) return;

    var now = Date.now();
    state.pokes = state.pokes.filter(function (t) { return now - t < 3600; });
    state.pokes.push(now);
    if (state.pokes.length >= 5) { dizzy(); return; }

    addMood(1);
    tone(760, .07, .03, 'sine');
    var r = Math.random();
    if (r < .5) jump();
    else if (r < .72) tiltRandom();
    else if (r < .86) {
      face.classList.add('wink-l');
      setTimeout(function () { face.classList.remove('wink-l'); }, 230);
    } else {
      setMouth(.55, 380);
    }
    if (Math.random() < .28) spawnHearts(2);
    if (Math.random() < .16) spawnSparkle(1);
    say(pick(CLICK_LINES), {
      interrupt: true,
      speak: Math.random() < .42,
      hold: rand(1300, 2200)
    });
  }

  function startPetting() {
    if (state.petting) return;
    state.petting = true;
    document.body.classList.add('petting');
    setLidBase(LID_SQUINT);
    setBlush(.8);
    addMood(2);
    spawnHearts(2);
    tone(520, .16, .022, 'sine');
    clearInterval(state.petFx);
    state.petFx = setInterval(function () {
      if (!state.petting) return;
      spawnHearts(1);
    }, 480);
    if (Date.now() - state.lastPetLine > 9000) {
      state.lastPetLine = Date.now();
      say(pick(PET_LINES), { interrupt: true, speak: Math.random() < .5, happy: true });
    }
    clearTimeout(state.petEnd);
    state.petEnd = setTimeout(endPetting, 1150);
  }

  function endPetting() {
    if (!state.petting) return;
    state.petting = false;
    document.body.classList.remove('petting');
    clearInterval(state.petFx);
    state.petEnd = null;
    setLidBase(state.sleeping ? LID_CLOSED : (state.drowsy ? LID_DROWSY : LID_OPEN));
    setBlush(baseBlush());
  }

  var strokes = [];
  function trackStroke(e) {
    if (state.dragging || state.pressed) return;
    if (!box.contains(e.target)) return;
    var now = Date.now();
    strokes.push({ x: e.clientX, y: e.clientY, t: now });
    while (strokes.length && now - strokes[0].t > 1500) strokes.shift();
    if (strokes.length < 6) return;

    var len = 0, turns = 0, lastSign = 0;
    for (var i = 1; i < strokes.length; i++) {
      var dx = strokes[i].x - strokes[i - 1].x;
      len += Math.abs(dx) + Math.abs(strokes[i].y - strokes[i - 1].y);
      var sign = dx > 2 ? 1 : (dx < -2 ? -1 : 0);
      if (sign && lastSign && sign !== lastSign) turns++;
      if (sign) lastSign = sign;
    }
    if (len < 230 || turns < 3) return;

    if (state.petting) {
      clearTimeout(state.petEnd);
      state.petEnd = setTimeout(endPetting, 1150);
      if (now - state.lastPetMood > 900) {
        state.lastPetMood = now;
        addMood(1);
      }
      return;
    }
    startPetting();
  }

  function drop() {
    jolt.animate([
      { transform: 'scale(1.05,.92)' },
      { transform: 'scale(.975,1.035)', offset: .55 },
      { transform: 'scale(1,1)' }
    ], { duration: 460, easing: 'cubic-bezier(.2,.8,.3,1.2)' });
    setLidBase(LID_OPEN);
    setBlush(baseBlush());
    tone(220, .09, .028, 'sine');
    if (Math.random() < .4) say(pick(DROP_LINES), { speak: false, hold: 1400 });
  }

  /* ============================================================
   * 9. 鼠标事件
   * ============================================================ */
  window.addEventListener('pywebviewready', function () { /* bridge ready */ });
  document.addEventListener('pointerdown', unlockAudio, true);

  stage.addEventListener('mousedown', function (e) {
    if (e.button !== 0) return;
    if (e.target.closest && e.target.closest('#toolbar')) return;
    touch();
    state.pressed = true;
    state.dragging = false;
    state.justDragged = false;
    state.down = { x: e.screenX, y: e.screenY };
    state.lastScreen = { x: e.screenX, t: Date.now() };
    clearTimeout(state.pressTimer);
    state.pressTimer = setTimeout(function () {
      if (state.pressed && !state.dragging) {
        document.body.classList.add('picked');
        setLidBase(LID_OPEN);
      }
    }, 150);
  });

  window.addEventListener('mousemove', function (e) {
    trackStroke(e);
    if (!state.pressed) return;
    if (e.buttons === 0) {
      // 鼠标在窗口外松开时收不到 mouseup，兜底复位
      state.pressed = false;
      state.dragging = false;
      clearTimeout(state.pressTimer);
      document.body.classList.remove('picked');
      document.body.classList.remove('dragging');
      return;
    }

    var dx = e.screenX - state.down.x;
    var dy = e.screenY - state.down.y;
    if (!state.dragging && (Math.abs(dx) > 5 || Math.abs(dy) > 5)) {
      state.dragging = true;
      document.body.classList.add('dragging');
      endPetting();
      setLidBase(LID_OPEN);
      setMouth(.45, 500);
      say(pick(DRAG_LINES), { speak: false, interrupt: true, hold: 1200 });
    }
    if (!state.dragging) return;

    var now = Date.now();
    var dt = now - state.lastScreen.t;
    if (dt < 80) return;
    var vx = (e.screenX - state.lastScreen.x) / dt;
    state.lastScreen = { x: e.screenX, t: now };
    var deg = clamp(vx * 9, -12, 12);
    state.dragAnim = jolt.animate([
      { transform: 'rotate(' + deg + 'deg)' },
      { transform: 'rotate(' + (deg * .35) + 'deg)' }
    ], { duration: 420, easing: 'ease-out' });
  });

  window.addEventListener('mouseup', function (e) {
    if (e.button !== 0 || !state.pressed) return;
    state.pressed = false;
    clearTimeout(state.pressTimer);
    document.body.classList.remove('picked');
    if (state.dragging) {
      state.dragging = false;
      state.justDragged = true;
      document.body.classList.remove('dragging');
      if (state.dragAnim) { try { state.dragAnim.cancel(); } catch (err) { } state.dragAnim = null; }
      drop();
      setTimeout(function () { state.justDragged = false; }, 280);
    }
  });

  stage.addEventListener('mouseleave', function () {
    document.body.classList.remove('hover');
    endPetting();
    strokes = [];
  });

  stage.addEventListener('mouseenter', function () {
    document.body.classList.add('hover');
  });

  stage.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('#toolbar')) return;
    if (state.justDragged) return;
    var now = Date.now();
    if (now - state.lastClick < 370) {
      state.lastClick = 0;
      openChat();
      say('来啦～', { speak: false, interrupt: true, hold: 1200 });
      return;
    }
    state.lastClick = now;
    poke();
  });

  /* ============================================================
   * 9b. 交互增强：悬停提示 / 按下反馈 / 页内右键菜单 / 键盘快捷键 / 状态切换
   *     只加"表现层 + 输入入口"，动作全部复用原有的 poke / openChat / say 等行为
   *     函数，不改原有逻辑；配色与文案沿用她的调子（暖色、短句、嘴硬心软）。
   * ============================================================ */
  var elTip = document.getElementById('tip');
  var elMenu = document.getElementById('menu');
  var elToast = document.getElementById('toast');
  var elBox = document.getElementById('petBox');
  var stageDrag = stage.classList.contains('pywebview-drag-region');
  var toastTimer = 0, menuOpen = false;

  function toast(msg) {
    if (!elToast || !msg) return;
    elToast.textContent = msg;
    elToast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { elToast.classList.remove('show'); }, 1600);
  }

  /* 状态机：idle / happy / sleepy / talking / menu —— 只改表现层 */
  function setState(name) {
    state.mode = name || 'idle';
    document.body.dataset.state = state.mode;
    var tips = { idle: '', happy: '心情不错', sleepy: '有点困了', talking: '', menu: '' };
    toast(tips[state.mode] || '');
  }

  /* ---------- 悬停提示气泡（说话方式照人设来） ---------- */
  var TIPS = ['咋，想摸我？', '别盯着我看了', '昂？', '手伸过来干啥', '再摸就收费了昂', '包行的，摸吧'];
  function showTip(t) {
    if (!elTip) return;
    elTip.textContent = t || TIPS[Math.floor(Math.random() * TIPS.length)];
    elTip.classList.add('show');
  }
  function hideTip() { if (elTip) elTip.classList.remove('show'); }

  /* ---------- 页内右键菜单 ---------- */
  function showMenu(x, y) {
    if (!elMenu) return false;
    var w = elMenu.offsetWidth || 140, h = elMenu.offsetHeight || 210;
    var vw = window.innerWidth, vh = window.innerHeight;
    elMenu.style.left = Math.max(4, Math.min(x, vw - w - 4)) + 'px';
    elMenu.style.top = Math.max(4, Math.min(y, vh - h - 4)) + 'px';
    elMenu.classList.add('show');
    menuOpen = true;
    setState('menu');
    // 菜单开着时先摘掉拖拽区，免得点菜单变成拖窗口
    stage.classList.remove('pywebview-drag-region');
    return true;
  }
  function hideMenu() {
    if (!elMenu) return;
    elMenu.classList.remove('show');
    menuOpen = false;
    if (stageDrag) stage.classList.add('pywebview-drag-region');
    if (state.mode === 'menu') setState('idle');
  }

  window.addEventListener('contextmenu', function (e) {
    e.preventDefault();
    var a = api();
    if (showMenu(e.clientX, e.clientY)) return;   // 优先页内菜单（更好看、更好控）
    if (a && a.show_menu) { try { a.show_menu(); } catch (err) { } }  // 兜底：原来的系统菜单
  });

  if (elMenu) {
    elMenu.addEventListener('mousedown', function (e) { e.stopPropagation(); });
    elMenu.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.mi') : null;
      if (!btn) return;
      e.stopPropagation();
      var act = btn.getAttribute('data-act') || '', a = api();
      hideMenu();
      if (act === 'poke') { poke(); return; }
      if (act === 'say') { say('嗯？咋了', { speak: true, hold: 1500 }); return; }
      if (act === 'sleep') {
        if (a && a.toggle_sleep) { try { a.toggle_sleep(); return; } catch (err) { } }
        toast('睡觉开关不可用'); return;
      }
      if (act === 'top') {
        if (a && a.toggle_top) { try { a.toggle_top(); return; } catch (err) { } }
        toast('置顶开关不可用'); return;
      }
      if (act === 'mute') {
        state.voiceOn = !state.voiceOn;
        toast(state.voiceOn ? '语音：开' : '语音：关'); return;
      }
      if (act === 'chat') { openChat(); return; }
      if (act === 'sing') {
        if (a && a.open_singer) { try { a.open_singer(); return; } catch (err) { } }
        toast('唱歌台不可用'); return;
      }
      if (act === 'sing_learn') {
        if (a && a.open_singer_learn) { try { a.open_singer_learn(); return; } catch (err) { } }
        toast('扒歌学习不可用'); return;
      }
      if (act === 'settings') {
        if (a && a.open_settings) { try { a.open_settings(); return; } catch (err) { } }
        toast('设置不可用'); return;
      }
      if (act === 'more') {
        if (a && a.show_menu) { try { a.show_menu(); return; } catch (err) { } }
        toast('系统菜单不可用'); return;
      }
      if (act === 'quit') {
        if (a && a.quit) { try { a.quit(); return; } catch (err) { } }
        toast('退出不可用'); return;
      }
    });
  }

  /* 点别处收菜单 */
  stage.addEventListener('mousedown', function () { if (menuOpen) hideMenu(); });

  /* ---------- 悬停 / 按下反馈 ---------- */
  if (elBox) {
    elBox.addEventListener('mouseenter', function () { elBox.classList.add('hover'); showTip(); });
    elBox.addEventListener('mouseleave', function () { elBox.classList.remove('hover'); hideTip(); });
    elBox.addEventListener('mousedown', function () { elBox.classList.add('press'); });
  }
  window.addEventListener('mouseup', function () { if (elBox) elBox.classList.remove('press'); });

  /* ---------- 键盘快捷键 ---------- */
  window.addEventListener('keydown', function (e) {
    var k = (e.key || '').toLowerCase();
    if (k === 'escape') { if (menuOpen) hideMenu(); return; }
    if (menuOpen) return;                          // 菜单开着时不抢键
    if (k === ' ' || k === 'spacebar') { e.preventDefault(); poke(); return; }
    if (k === 'enter' || k === 's') { openChat(); return; }
    if (k === 'm') {
      state.voiceOn = !state.voiceOn;
      toast(state.voiceOn ? '语音：开' : '语音：关'); return;
    }
    if (k === 'h') {
      setState('happy');
      if (typeof setBlush === 'function') setBlush(.82);
      if (typeof spawnHearts === 'function') spawnHearts(3);
      return;
    }
    if (k === 'l') { if (typeof lookAround === 'function') lookAround(); return; }
    if (k === 'z') {
      var a = api();
      if (a && a.toggle_sleep) { try { a.toggle_sleep(); return; } catch (err) { } }
      setState(state.mode === 'sleepy' ? 'idle' : 'sleepy');
      return;
    }
  });

  /* ============================================================
   * 10. 待机 / 睡觉 / 问候
   * ============================================================ */
  function idleAction() {
    if (state.sleeping || state.dragging || state.petting || state.micOn || state.thinking) return;
    var r = Math.random();
    if (r < .26) lookAround();
    else if (r < .44) tiltRandom();
    else if (r < .58) breatheDeep();
    else if (r < .68) hum();
    else if (r < .74) yawn();
    else if (r < .8) spawnSparkle(1);
  }

  function scheduleIdle() {
    clearTimeout(state.idleTimer);
    state.idleTimer = setTimeout(function () {
      idleAction();
      scheduleIdle();
    }, rand(4300, 8600));
  }

  function scheduleBlink() {
    if (!state.sleeping) {
      var r = Math.random();
      blink(r < .16 ? 2 : 1);
      if (r > .93) setTimeout(function () { blink(1); }, 260);
    }
    setTimeout(scheduleBlink, rand(2700, 6400));
  }

  function checkSleep() {
    if (state.micOn || state.dragging || state.petting) return;
    var idle = Date.now() - state.lastInteract;
    if (idle > 9 * 60000) {
      sleep();
    } else if (idle > 4 * 60000) {
      if (!state.drowsy && !state.sleeping) {
        state.drowsy = true;
        document.body.classList.add('drowsy');
        setLidBase(LID_DROWSY);
      }
    } else if (state.drowsy && !state.sleeping) {
      state.drowsy = false;
      document.body.classList.remove('drowsy');
      setLidBase(state.petting ? LID_SQUINT : LID_OPEN);
    }
  }

  function greet() {
    var h = new Date().getHours();
    var line;
    if (h < 5) line = '这么晚还不睡…我陪你';
    else if (h < 11) line = '早上好～';
    else if (h < 14) line = '中午好，吃饭了吗';
    else if (h < 18) line = '下午好呀';
    else if (h < 23) line = '晚上好～';
    else line = '该睡啦，别熬夜';
    say(line, { speak: false, hold: 3200 });
  }

  function maybeHint() {
    try {
      var key = 'pp_hint_' + new Date().toDateString();
      if (localStorage.getItem(key)) return;
      localStorage.setItem(key, '1');
    } catch (e) { /* ignore */ }
    say('摸摸我会开心，双击我聊天～', { speak: false, hold: 4200 });
  }

  /* ============================================================
   * 11. 语音输入
   * ============================================================ */
  function openChat() {
    var a = api();
    if (a && a.open_chat) {
      try { a.open_chat(); return; } catch (e) { /* ignore */ }
    }
    say('双击我就能打开聊天啦', { speak: false });
  }

  function micTip(text) {
    showBubble('🎤 ' + text.slice(-46));
  }

  function startMic() {
    if (state.micOn) return;
    if (state.micDead) {
      say('语音输入这台机器用不了，双击我打字聊吧', { speak: false, hold: 3600 });
      return;
    }
    var a = api();
    if (!a || !a.voice_start) {
      say('语音要在桌宠窗口里用哦', { speak: false });
      return;
    }
    touch();
    state.micBuf = '';
    state.micStart = Date.now();
    state.micLast = Date.now();
    if (a.stop_speak) { try { a.stop_speak(); } catch (e) { } }
    stopVoiceAudio();
    stopTalking();
    showBubble('听着呢，说吧～');
    a.voice_start('zh-CN').then(function (r) {
      if (!r || !r.ok) {
        state.micDead = true;
        btnMic.classList.add('disabled');
        hideBubble();
        say((r && r.message) || '麦克风没打开', { speak: false, hold: 4600 });
        return;
      }
      state.micOn = true;
      btnMic.classList.add('on');
      document.body.classList.add('listening');
      setLidBase(LID_OPEN);
      clearTimeout(state.micTimer);
      pollMic();
    })['catch'](function () {
      state.micDead = true;
      btnMic.classList.add('disabled');
      say('麦克风没打开，检查一下系统权限', { speak: false });
    });
  }

  function pollMic() {
    if (!state.micOn) return;
    var a = api();
    if (!a || !a.voice_poll) { stopMic(); return; }
    a.voice_poll().then(function (r) {
      if (!state.micOn) return;
      if (r && r.error) { stopMic(r.error); return; }
      if (r && r.text) {
        state.micBuf += r.text;
        state.micLast = Date.now();
        micTip(state.micBuf);
      }
      var now = Date.now();
      if (state.micBuf && now - state.micLast > 2800) { stopMic(); return; }
      if (!state.micBuf && now - state.micStart > 9000) { stopMic('没听到声音…'); return; }
      if (now - state.micStart > 15000) { stopMic(); return; }
      state.micTimer = setTimeout(pollMic, 360);
    })['catch'](function () {
      state.micTimer = setTimeout(pollMic, 500);
    });
  }

  function stopMic(note) {
    if (!state.micOn) return;
    state.micOn = false;
    clearTimeout(state.micTimer);
    btnMic.classList.remove('on');
    document.body.classList.remove('listening');
    var a = api();
    if (a && a.voice_stop) { try { a.voice_stop(); } catch (e) { } }
    var text = trim(state.micBuf);
    state.micBuf = '';
    hideBubble();
    if (text) {
      say('听到啦：' + text.slice(0, 24), { speak: false, hold: 1200 });
      if (a && a.chat_from_pet) {
        try { a.chat_from_pet(text); } catch (e) { }
      }
    } else if (note) {
      say(note, { speak: false, hold: 2200 });
    }
  }

  btnMic.addEventListener('click', function (e) {
    e.stopPropagation();
    if (state.micOn) stopMic(); else startMic();
  });

  btnChat.addEventListener('click', function (e) {
    e.stopPropagation();
    openChat();
  });

  toolbar.addEventListener('mousedown', function (e) { e.stopPropagation(); });
  toolbar.addEventListener('click', function (e) { e.stopPropagation(); });

  /* ============================================================
   * 12. 给聊天窗口 / Python 的桥
   * ============================================================ */
  function setPetState(s) {
    if (s === 'thinking') {
      if (state.sleeping) wake(false);
      state.thinking = true;
      document.body.classList.add('thinking');
      if (!bubble.classList.contains('show')) {
        showBubble('…');
        state.thinkBubble = true;
      }
    } else {
      state.thinking = false;
      document.body.classList.remove('thinking');
      if (state.thinkBubble) {
        hideBubble();
        state.thinkBubble = false;
      }
    }
  }

  window.PetBridge = {
    say: function (text, opts) {
      opts = opts || {};
      opts.fromChat = true;
      say(text, opts);
    },
    setState: setPetState,
    setFlags: function (flags) {
      if (!flags) return;
      if (flags.voice != null) state.voiceOn = !!flags.voice;
      if (flags.sound != null) state.soundOn = !!flags.sound;
      if (flags.voiceUrl != null) state.voiceUrl = String(flags.voiceUrl || '');
      if (flags.pack != null) {
        state.packOn = String(flags.pack || '');
        state.packStrict = !!flags.strict;
      }
      if (!state.voiceOn) {
        stopVoiceAudio();
        stopTalking();
        var a = api();
        if (a && a.stop_speak) { try { a.stop_speak(); } catch (e) { } }
      }
    },
    poke: poke,
    blink: blink,
    model: function () { return { mood: state.mood, sleeping: state.sleeping, drowsy: state.drowsy }; }
  };

  window.__pet = {
    say: say, poke: poke, blink: blink, sleep: sleep, wake: wake,
    setState: setPetState, state: state, setMouth: setMouth,
    setLidBase: setLidBase, spawnHearts: spawnHearts
  };

  /* ============================================================
   * 13. 启动
   * ============================================================ */
  (function init() {
    loadMood();
    setLidBase(LID_OPEN);
    setBlush(baseBlush());

    var ready = function () {
      if (img.classList.contains('ready')) return;
      img.classList.add('ready');
      setTimeout(function () { blink(1); }, 800);
    };
    if (img.complete && img.naturalWidth) ready();
    img.addEventListener('load', ready);
    img.addEventListener('error', function () {
      img.style.display = 'none';
      var miss = document.createElement('div');
      miss.textContent = '找不到 dog_girl.png';
      miss.style.cssText = 'position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);' +
        'background:rgba(20,20,20,.78);color:#fff;font:12px/1.5 sans-serif;padding:8px 12px;border-radius:8px;';
      box.appendChild(miss);
    });

    scheduleBlink();
    scheduleIdle();
    setTimeout(greet, 1700);
    setTimeout(maybeHint, 5600);
    setInterval(checkSleep, 15000);
    setInterval(function () { addMood(-0.4); }, 90000);
    setInterval(saveMood, 30000);

    // 长时间没人理，冒一句
    setInterval(function () {
      if (state.sleeping || state.micOn || saying || document.body.classList.contains('talking')) return;
      if (Date.now() - state.lastInteract < 110000) return;
      if (Date.now() - state.lastSay < 70000) return;
      if (Math.random() > .34) return;
      say(idleLine(), { speak: false, hold: 3600 });
    }, 45000);
  })();
})();
</script>
</body>
</html>
"""


def build_pet_html(base_url: str = "") -> str:
    try:
        version = int(PET_IMAGE.stat().st_mtime)
    except OSError:
        version = 0
    prefix = base_url.rstrip("/")
    src = f"{prefix}/dog_girl.png?v={version}" if prefix else f"dog_girl.png?v={version}"
    return PET_HTML.replace("__IMG__", html.escape(src, quote=True))


# ---------------------------------------------------------------------------
# 语音识别 / 合成兜底（System.Speech）
# ---------------------------------------------------------------------------
SYS_SPEECH_DLL = (
    r"C:\Windows\Microsoft.NET\assembly\GAC_MSIL\System.Speech"
    r"\v4.0_4.0.0.0__31bf3856ad364e35\System.Speech.dll"
)


def load_system_speech() -> bool:
    """加载 System.Speech。

    这台机器上 clr.AddReference("System.Speech") 会抛 FileNotFoundException，
    但 dll 确实躺在 GAC_MSIL 下；按完整路径再试一次就能用，语音识别和合成都是。
    """
    try:
        import clr  # type: ignore
        if not hasattr(clr, "AddReference"):
            # pythonnet 没被真正初始化（例如被别的线程抢先 import 过）：
            # 让它自己把运行时装好再重试一次，避免拿半成品模块去 AddReference
            import importlib

            import pythonnet  # type: ignore

            pythonnet.load("netfx")
            importlib.reload(clr)
    except Exception:
        return False

    try:
        clr.AddReference("System.Speech")
        return True
    except Exception:
        pass

    candidates = [SYS_SPEECH_DLL]
    try:
        import glob
        candidates += glob.glob(
            r"C:\Windows\Microsoft.NET\assembly\GAC_MSIL\System.Speech\*\System.Speech.dll"
        )
    except Exception:
        pass

    for path in candidates:
        try:
            if os.path.exists(path):
                clr.AddReference(path)
                return True
        except Exception:
            continue
    return False


_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200D]+"
)


def voice_diag() -> dict[str, Any]:
    """语音运行时自检：一次性把"能不能听、能不能说"的依赖全查出来。

    GET /api/voice/diag 直接返回这个结构，用来区分是 pythonnet 没加载、
    系统没装中文语音包、还是识别引擎不可用——不用靠猜。
    """
    info: dict[str, Any] = {
        "platform": sys.platform,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "flags": {
            "VOICE_ALLOW_SYSTEM": os.environ.get("VOICE_ALLOW_SYSTEM", ""),
            "VOICE_READ_MODE": os.environ.get("VOICE_READ_MODE", ""),
            "VOICE_CLONE_MAX_CHARS": os.environ.get("VOICE_CLONE_MAX_CHARS", ""),
            "VOICE_STT_MIN_CONF": os.environ.get("VOICE_STT_MIN_CONF", ""),
            "VOICE_STRICT_CLONE": os.environ.get("VOICE_STRICT_CLONE", "1(默认开)"),
            "VOICE_TTS_URL": os.environ.get("VOICE_TTS_URL", ""),
            "VOICE_TTS_MODEL": os.environ.get("VOICE_TTS_MODEL", ""),
            "VOICE_TTS_VOICE": os.environ.get("VOICE_TTS_VOICE", ""),
        },
    }

    # 克隆音色是否在用、用的是哪个音色/模型/参考音频：一眼可查
    try:
        import voice_identity
        ident = voice_identity.sync_now()
        info["clone_voice"] = {
            "voice_id": ident.get("voice_id") or "",
            "profile_id": ident.get("profile_id") or "",
            "ref": ident.get("ref") or "",
            "params": ident.get("params") or {},
            "drift": voice_identity.verify().get("drift"),
        }
    except Exception as exc:
        info["clone_voice"] = {"error": str(exc)}

    try:
        import clr  # type: ignore  # noqa: F401

        info["pythonnet"] = True
    except Exception as exc:
        info["pythonnet"] = False
        info["pythonnet_error"] = str(exc)

    info["system_speech"] = bool(load_system_speech())

    try:
        from System.Speech.Synthesis import SpeechSynthesizer  # type: ignore

        synth = SpeechSynthesizer()
        try:
            synth.SetOutputToNull()
        except Exception:
            pass
        voices = []
        for item in synth.GetInstalledVoices():
            try:
                if not item.Enabled:
                    continue
                v = item.VoiceInfo
                voices.append(f"{v.Name}|{v.Culture.Name}")
            except Exception:
                continue
        synth.Dispose()
        info["tts_voices"] = voices
        info["tts_zh"] = any("zh" in v.lower() or "chinese" in v.lower() for v in voices)
    except Exception as exc:
        info["tts_voices"] = []
        info["tts_zh"] = False
        info["tts_error"] = str(exc)

    try:
        from System.Speech.Recognition import (  # type: ignore
            SpeechRecognitionEngine,
        )

        recs = []
        for r in SpeechRecognitionEngine.InstalledRecognizers():
            try:
                recs.append(f"{r.Culture.Name}|{r.Name}")
            except Exception:
                continue
        info["stt_recognizers"] = recs
        info["stt_zh"] = any(r.lower().startswith("zh") for r in recs)
    except Exception as exc:
        info["stt_recognizers"] = []
        info["stt_zh"] = False
        info["stt_error"] = str(exc)

    try:
        vf = resolve_voice_file()
        info["chat_file"] = vf.name if vf else ""
        info["chat_text"] = (os.environ.get("VOICE_CHAT_TEXT") or "").strip()
    except Exception:
        pass

    # 克隆服务在不在（语音"不出声"十有八九是这里挂了）
    try:
        url = (os.environ.get("VOICE_TTS_URL") or "").strip()
        info["clone_url"] = url
        if url:
            host, port = _split_tts_url(url)
            info["clone_up"] = _port_listening(host, port)
    except Exception as exc:
        info["clone_up"] = False
        info["clone_error"] = str(exc)

    # 麦克风设备数：0 表示系统里根本没有录音设备（或在隐私设置里被禁用）
    try:
        import ctypes

        info["mic_devices"] = int(ctypes.windll.winmm.waveInGetNumDevs())
    except Exception:
        info["mic_devices"] = -1
    return info


def clean_speech_text(text: str) -> str:
    """把聊天里的标记、表情、链接去掉，免得 SAPI 念出“左中括号”。"""
    text = re.sub(r"[\[【](?:STICKER|DRAW|PPT)\s*[:：][^\]】]*[\]】]", "", text, flags=re.I)
    text = re.sub(r"https?://\S+", "", text)
    text = text.replace("||", "，").replace("｜｜", "，")
    text = re.sub(r"[*_`#>~]+", "", text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*([，。！？、；：,.!?;:])\s*", r"\1", text)
    return text[:160]


def estimate_speech_seconds(text: str) -> float:
    return round(min(40.0, max(1.0, len(text) * 0.22 + 0.4)), 2)


def short_speech(text: str, max_clauses: int = 2, max_chars: int = 30) -> str:
    """聊天回复只播 1~2 个短句（≤30 字）。

    一次把整段回复念完既费时、又必然跟下一句打架；先切句再按字数收口，
    保证单次播报短、可控，前端按返回的 duration 收起指示条。
    """
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return ""
    parts = [p.strip() for p in re.split(r"[。！？!?；;\n，,]", clean) if p.strip()]
    if not parts:
        return clean[:max_chars]
    out = ""
    for seg in parts[:max_clauses]:
        nxt = (out + "，" + seg) if out else seg
        if len(nxt) > max_chars:
            if not out:
                out = seg[:24]
            break
        out = nxt
    return out or clean[:max_chars]


def _make_sapi_engine(lang: str) -> Any:
    """建一个 Windows 自带的中文听写引擎。失败抛 RuntimeError，文案直接给用户看。"""
    try:
        import clr  # type: ignore  # noqa: F401  # pythonnet，pywebview 在 Windows 上已经带进来了
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("这台机器没装 pythonnet，语音用不了，打字吧") from exc

    if not load_system_speech():
        raise RuntimeError("系统里没有语音识别组件（System.Speech）")

    try:
        from System.Speech.Recognition import DictationGrammar, SpeechRecognitionEngine  # type: ignore
        from System.Globalization import CultureInfo  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("系统里没有语音识别组件（System.Speech）") from exc

    culture = None
    for name in (lang, "zh-CN", "zh-Hans-CN", "zh"):
        try:
            culture = CultureInfo(name)
            break
        except Exception:
            culture = None

    try:
        engine = SpeechRecognitionEngine(culture) if culture is not None else SpeechRecognitionEngine()
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "系统里没装中文语音识别。设置 → 时间和语言 → 语言 → 中文 → 语音，安装“语音识别”组件"
        ) from exc

    # 分开报错：麦克风打不开和"没装中文识别"是两回事，混在一起只会让人瞎猜
    try:
        engine.SetInputToDefaultAudioDevice()
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "打不开麦克风：检查 设置→隐私和安全性→麦克风 里「允许桌面应用访问麦克风」，"
            "或麦克风是否被别的程序占用"
        ) from exc

    try:
        engine.LoadGrammar(DictationGrammar())
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("语音识别加载听写语法失败：" + str(exc)) from exc

    return engine


class VoiceEngine:
    """后台听写线程。前端每 350ms poll 一次拿增量文字。

    WebView2 里 Web Speech API 通常没有可用后端，所以必须有这一层。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listening = False
        self._buf = ""
        self._error = ""
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        # 识别质量相关：
        #   _last_at   最后一次听到有效内容的时刻（前端靠它判断"说完了"）
        #   _conf      最近一句的置信度（太低就别自动发出去，免得答非所问）
        #   _last_frag 上一段文字（SAPI 偶尔会把同一句重复吐出来）
        #   _heard     这次对话到底听到过没有
        self._last_at = 0.0
        self._conf = 0.0
        self._last_frag = ""
        self._heard = False
        try:
            self.min_conf = float(os.environ.get("VOICE_STT_MIN_CONF", "0.08") or "0.08")
        except ValueError:
            self.min_conf = 0.08

    # 英文/数字之间需要空格，中文之间用逗号：模型拿到的是"人话"，不是一串粘连字符
    _FILLERS = {"嗯", "啊", "呃", "额", "哦", "噢", "唉", "诶", "哎", "那个", "这个"}

    @classmethod
    def _clean_frag(cls, text: str) -> str:
        t = re.sub(r"\s+", " ", str(text or "")).strip()
        t = re.sub(r"^[，。！？、；：,.!?;:]+|[，。！？、；：,.!?;:]+$", "", t).strip()
        if not t or t in cls._FILLERS:
            return ""
        return t

    def _push(self, text: str, conf: float) -> None:
        text = self._clean_frag(text)
        if not text:
            return
        with self._lock:
            if text == self._last_frag:
                return                      # 同一句被重复吐出来，丢掉
            if self._buf:
                tail = self._buf[-1]
                if tail.isascii() and tail.isalnum() and text[:1].isascii() and text[:1].isalnum():
                    self._buf += " "
                elif tail not in "，。！？、；：,.!?;:":
                    self._buf += "，"        # 中文分句，模型更好懂
            self._buf += text
            self._last_frag = text
            self._conf = conf
            self._last_at = time.monotonic()
            self._heard = True

    def start(self, lang: str = "zh-CN") -> dict[str, Any]:
        if sys.platform != "win32":
            return {"ok": False, "message": "语音识别只在 Windows 上用"}
        with self._lock:
            if self._listening:
                return {"ok": True, "listening": True}
            self._buf = ""
            self._error = ""
            self._last_at = 0.0
            self._conf = 0.0
            self._last_frag = ""
            self._heard = False
            self._stop_evt.clear()
            self._listening = True

        # 引擎在识别线程里创建、也在那里用：SAPI 的 COM 对象不能跨线程乱传
        ready = threading.Event()
        box: dict[str, Any] = {}
        self._thread = threading.Thread(
            target=self._loop, args=(lang, ready, box), name="voice", daemon=True
        )
        self._thread.start()
        ready.wait(8.0)
        if not box.get("ok"):
            with self._lock:
                self._listening = False
            log.warning("语音识别启动失败：%s", box.get("message"))
            return {"ok": False, "message": box.get("message") or "语音没起来"}
        log.info("语音识别已开始，lang=%s", lang)
        return {"ok": True, "listening": True}

    def _loop(self, lang: str, ready: threading.Event, box: dict[str, Any]) -> None:
        try:
            engine = _make_sapi_engine(lang)
        except Exception as exc:
            box["ok"] = False
            box["message"] = str(exc)
            ready.set()
            with self._lock:
                self._listening = False
            return

        box["ok"] = True
        ready.set()
        try:
            from System import TimeSpan  # type: ignore
            while not self._stop_evt.is_set():
                # 单次最多等 0.5 秒：既够收集一句话，又能在点"停"时迅速放开麦克风
                result = engine.Recognize(TimeSpan.FromSeconds(0.5))
                if result is None:
                    continue
                text = self._clean_frag(str(result.Text or ""))
                if not text:
                    continue
                try:
                    conf = float(result.Confidence)
                except Exception:
                    conf = 0.0
                # 噪声/幻觉：置信度低且只有一两个字，直接丢，别把垃圾塞进输入框
                if conf < self.min_conf and len(text) <= 2:
                    log.debug("丢掉低置信度识别：%r(%.2f)", text, conf)
                    continue
                self._push(text, conf)
        except Exception as exc:
            log.warning("语音识别循环结束：%s", exc)
            with self._lock:
                self._error = "语音识别断了，再点一次麦克风"
        finally:
            try:
                engine.Dispose()
            except Exception:
                pass
            with self._lock:
                self._listening = False

    def poll(self) -> dict[str, Any]:
        with self._lock:
            text, self._buf = self._buf, ""
            silent = (time.monotonic() - self._last_at) if self._heard else 0.0
            return {
                "listening": self._listening,
                "text": text,
                "error": self._error,
                "conf": round(self._conf, 3),
                "silent": round(silent, 2),
                "heard": self._heard,
            }

    def stop(self) -> dict[str, Any]:
        self._stop_evt.set()
        with self._lock:
            self._listening = False
        # 等识别线程真正放开麦克风：马上重开时旧线程还占着设备会直接失败
        th = self._thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(0.8)
        return {"ok": True}


# ---------------------------------------------------------------------------
# 语音素材包：用自己的录音说话
# ---------------------------------------------------------------------------
def _voices_root() -> Path:
    """台词库根目录：优先 voices/profiles/<音色>/（持久化音色档案）。

    一个音色档案里既有参考音频（ref.wav），又有它自己的台词库（manifest.json），
    所以 VoicePack 直接把档案当"语音包"用：命中台词 0 延迟播，没命中才去合成。
    """
    base = exe_dir() if getattr(sys, "frozen", False) else ROOT
    prof = base / "voices" / "profiles"
    return prof if prof.is_dir() else (base / "voices")


def active_voice_profile() -> dict[str, Any]:
    """当前启用的音色档案（voices/active.json → profiles/<id>/profile.json）。"""
    base = exe_dir() if getattr(sys, "frozen", False) else ROOT
    try:
        data = json.loads((base / "voices" / "active.json").read_text("utf-8"))
        pid = str(data.get("id") or "")
    except (OSError, ValueError):
        pid = ""
    if not pid:
        return {}
    try:
        prof = json.loads((base / "voices" / "profiles" / pid / "profile.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    prof["id"] = pid
    prof["dir"] = str(base / "voices" / "profiles" / pid)
    return prof


def list_voice_profiles() -> dict[str, Any]:
    """列出所有音色档案（给 /api/voices 和前端"音色"面板用）。"""
    base = exe_dir() if getattr(sys, "frozen", False) else ROOT
    root = base / "voices" / "profiles"
    active = active_voice_profile().get("id") or ""
    items = []
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        dirs = []
    for d in dirs:
        try:
            prof = json.loads((d / "profile.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        items.append({
            "id": d.name,
            "name": prof.get("name") or d.name,
            "bank": prof.get("bank") or 0,
            "ref_seconds": (prof.get("source") or {}).get("seconds"),
            "engine": prof.get("engine") or "f5-tts",
            "created_at": prof.get("created_at") or 0,
            "active": d.name == active,
        })
    return {"ok": True, "active": active, "voices": items}


VOICE_ROOT = _voices_root()
VOICE_INTENTS = (
    "greet", "click", "pet", "drag", "drop", "dizzy",
    "wake", "sleep", "idle", "think", "laugh", "bye",
)
AUDIO_EXT = (".wav", ".mp3", ".ogg", ".m4a")


def wav_seconds(path: Path) -> float:
    """读 wav 头算时长；非 wav 按码率粗估。"""
    try:
        import wave

        with wave.open(str(path), "rb") as w:
            rate = w.getframerate() or 1
            return round(w.getnframes() / float(rate), 2)
    except Exception:
        try:
            return round(max(1.0, path.stat().st_size / 48000.0), 2)
        except OSError:
            return 1.6


class VoicePack:
    """voices/<包名>/ 下的录音片段。

    目录结构（manifest.json 可省）::

        voices/我的声音/manifest.json
        voices/我的声音/greet_01.wav
        voices/我的声音/pet_01.wav

    manifest.json::

        {"name": "我的声音", "strict": false,
         "lines": {"greet": ["greet_01.wav"], "pet": ["pet_01.wav", "pet_02.wav"]}}

    没写 manifest 时按文件名前缀归类（click_02.wav -> click）。
    strict=true 表示没录音的意图保持安静，不退回系统 TTS。
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else VOICE_ROOT
        self.name = ""
        self.strict = False
        self.packs: dict[str, dict[str, Any]] = {}
        self._dur: dict[str, float] = {}
        self._last: dict[str, str] = {}
        self._lock = threading.Lock()
        self.reload()

    # ---------- 扫描 ----------
    def _read(self, folder: Path) -> dict[str, Any]:
        lines: dict[str, list[Path]] = {}
        texts: dict[str, Path] = {}
        strict = False
        voice = ""
        manifest = folder / "manifest.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                strict = bool(data.get("strict"))
                voice = str(data.get("voice") or "")
                for intent, files in (data.get("lines") or {}).items():
                    if isinstance(files, str):
                        files = [files]
                    for name in files:
                        p = folder / str(name)
                        if p.exists() and p.suffix.lower() in AUDIO_EXT:
                            lines.setdefault(str(intent).lower(), []).append(p)
                # texts：原句 -> 文件。录的就是这句时优先播它，避免"嘴型对不上台词"
                for line, name in (data.get("texts") or {}).items():
                    p = folder / str(name)
                    if p.exists() and p.suffix.lower() in AUDIO_EXT:
                        texts[str(line).strip()] = p
            except Exception:
                log.debug("manifest 读取失败：%s", manifest, exc_info=True)
        for p in sorted(folder.glob("*")):
            if p.suffix.lower() not in AUDIO_EXT:
                continue
            key = re.split(r"[_\-\s.]", p.stem)[0].lower()
            if key in VOICE_INTENTS and p not in lines.get(key, []):
                lines.setdefault(key, []).append(p)
        return {"lines": lines, "texts": texts, "strict": strict,
                "folder": folder, "voice": voice}

    def reload(self) -> list[str]:
        packs: dict[str, dict[str, Any]] = {}
        try:
            folders = sorted([p for p in self.root.iterdir() if p.is_dir()])
        except OSError:
            folders = []
        for folder in folders:
            info = self._read(folder)
            if info["lines"] or info["texts"]:
                packs[folder.name] = info
        self.packs = packs
        if self.name and self.name not in packs:
            self.name = ""
        if self.name:
            self.strict = bool(packs[self.name]["strict"])
        if packs:
            log.info("发现语音包：%s", "、".join(packs))
        return list(packs)

    # ---------- 选择 ----------
    def set(self, name: str) -> bool:
        if name not in self.packs:
            return False
        self.name = name
        self.strict = bool(self.packs[name]["strict"])
        log.info("语音包已启用：%s（%d 类台词）", name, len(self.packs[name]["lines"]))
        return True

    def auto_pick(self, prefer: str = "") -> str:
        if prefer and self.set(prefer):
            return prefer
        names = list(self.packs)
        if len(names) == 1 and self.set(names[0]):
            return names[0]
        return self.name

    def intents(self) -> list[str]:
        return sorted((self.packs.get(self.name) or {}).get("lines", {}))

    def texts(self) -> dict[str, Any]:
        """当前音色档案里"原句 → wav"的台词表（命中就 0 延迟播）。"""
        return dict((self.packs.get(self.name) or {}).get("texts") or {})

    # ---------- 播放 ----------
    def duration(self, path: Path) -> float:
        key = str(path)
        with self._lock:
            if key in self._dur:
                return self._dur[key]
        seconds = wav_seconds(path)
        with self._lock:
            self._dur[key] = seconds
        return seconds

    def pick(self, intent: str) -> Path | None:
        files = (self.packs.get(self.name) or {}).get("lines", {}).get(intent) or []
        if not files:
            return None
        if len(files) == 1:
            return files[0]
        pool = [f for f in files if f.name != self._last.get(intent)] or files
        return random.choice(pool)

    def play(self, intent: str, text: str = "") -> tuple[bool, float]:
        """先按原句精确匹配（texts），再退回按意图随机（lines）。"""
        if not self.name:
            return False, 0.0
        pack = self.packs.get(self.name) or {}
        with self._lock:
            path = None
            if text:
                path = (pack.get("texts") or {}).get(text.strip())
            if path is None and intent:
                path = self.pick(intent)
            if path is None:
                return False, 0.0
            self._last[intent or "_"] = path.name
            self._dur.setdefault(str(path), wav_seconds(path))
        return self._play_path(path)

    # ---------- 原句精确匹配 ----------
    @staticmethod
    def _norm_key(s: str) -> str:
        """标点/空白归一：short_speech 会把「。」换成「，」，不归一就永远匹配不上。"""
        s = re.sub(r"[\s，。！？、；：,.!?;:~～]+", "", str(s or ""))
        return s.lower()

    def match_text(self, text: str) -> Path | None:
        if not self.name or not text:
            return None
        key = self._norm_key(text)
        if not key:
            return None
        pack = self.packs.get(self.name) or {}
        texts = pack.get("texts") or {}
        if text.strip() in texts:
            return texts[text.strip()]
        for line, path in texts.items():
            if self._norm_key(line) == key:
                return path
        # 长回复只播前两句：允许"整句是录音的前缀"，但**长度必须几乎一致**。
        # 以前这里是 key.startswith(lk) 就返回，于是任何以"好/嗯/不"开头的长句
        # 都会命中一条 0.3 秒的单字录音 —— 用户听到的就是一句跟回复无关的声音。
        if len(key) >= 2:
            for line, path in texts.items():
                lk = self._norm_key(line)
                if not lk or len(lk) < 2:
                    continue
                if abs(len(key) - len(lk)) > 3:
                    continue
                if key.startswith(lk) or lk.startswith(key):
                    return path
        return None

    def play_text(self, path: Path) -> tuple[bool, float]:
        with self._lock:
            self._dur.setdefault(str(path), wav_seconds(path))
        return self._play_path(path)

    def _play_path(self, path: Path) -> tuple[bool, float]:
        if path.suffix.lower() != ".wav":
            log.warning("语音包只支持 wav：%s（用 tools/voice_pack.py 转一下）", path.name)
            return False, 0.0
        try:
            import winsound

            winsound.PlaySound(
                str(path),
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        except Exception:
            log.warning("播放录音失败：%s", path, exc_info=True)
            return False, 0.0
        duration = self.duration(path)
        self._play_until = time.monotonic() + duration
        log.info("播放录音 %s（%.2fs）", path.name, duration)
        return True, duration

    def stop(self) -> None:
        # 只有自己真在播才 PURGE：SND_PURGE 是全局的，会把克隆 TTS 正在播的句子一起掐掉
        if getattr(self, "_play_until", 0.0) <= time.monotonic():
            return
        self._play_until = 0.0
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass


def env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off", "none")


VOICE_AUDIO_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}


def bypass_proxy_for_loopback() -> None:
    """把本机地址加进 no_proxy。

    这台机器配了系统代理（http_proxy=127.0.0.1:65396），urllib 连
    http://127.0.0.1:9880 这种本机服务也会绕代理走一圈：平时多一跳延迟，
    服务没起时还会拿到 502 而不是"连接被拒"，重试/自愈逻辑全被带偏。
    """
    hosts = ("127.0.0.1", "localhost", "::1")
    cur = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    parts = [p.strip() for p in re.split(r"[,\s]+", cur) if p.strip()]
    for h in hosts:
        if h not in parts:
            parts.append(h)
    merged = ",".join(parts)
    os.environ["no_proxy"] = merged
    os.environ["NO_PROXY"] = merged


def _loopback_opener():
    """直连本机用：显式空 ProxyHandler，彻底绕开系统代理。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _split_tts_url(url: str) -> tuple[str, int]:
    """解析 VOICE_TTS_URL，返回 (host, port)。"""
    parts = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    host = (parts.hostname or "").strip().lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        raise ValueError("无效的 TTS 地址")
    return host, port


def _port_listening(host: str, port: int) -> bool:
    """探测 host:port 是否已有服务在监听。"""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def launch_clone_service(force: bool = False) -> bool:
    """本机 F5 克隆服务没在跑就拉起来。

    克隆服务是独立进程（voice-clone-env），warmup 要十几秒；用户点开就说时它
    可能还没起来，所以合成失败时也要能再拉一次，而不是整条语音链路沉默。
    """
    url = (os.environ.get("VOICE_TTS_URL") or "").strip()
    if not url:
        return False
    try:
        host, port = _split_tts_url(url)
    except ValueError:
        return False
    if host not in ("127.0.0.1", "localhost"):
        return False
    if not force and _port_listening(host, port):
        return True

    env_py = Path(r"C:\Users\zheng'zheng\WorkBuddy\voice-clone-env\Scripts\python.exe")
    # frozen 时 __file__ 在 _MEIPASS（临时解包目录），服务脚本要用 exe_dir() 定位
    server = exe_dir() / "tools" / "f5_server.py"
    if not env_py.is_file() or not server.is_file():
        log.info("找不到 F5 服务环境/脚本（%s / %s），跳过自动拉起", env_py.name, server.name)
        return False
    try:
        import subprocess

        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [str(env_py), str(server), "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        log.info("已拉起克隆 TTS 服务：%s", url)
        return True
    except Exception:
        log.warning("拉起克隆 TTS 服务失败（可手动启动 f5_server.py）", exc_info=True)
        return False


def resolve_voice_file() -> Path | None:
    """找「对话音源」：VOICE_CHAT_FILE > 语音包里的 chat.* > 克隆音色。

    优先级：
      1. 环境变量 VOICE_CHAT_FILE 显式指定（绝对路径或相对 exe 目录）；
      2. voices/<包名>/chat.*（含 chat.wav，winsound 可播的标准 PCM）；
      3. clone_resynth.wav —— 声音克隆产出的「小狗」音色本体，和 chat.wav
         保持同一来源（改 clone 就两边一致）。
    """
    base = exe_dir()
    found: list[Path] = []

    raw = (os.environ.get("VOICE_CHAT_FILE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        found.append(p if p.is_absolute() else base / p)
        if p.is_absolute():
            found.append(ROOT / p.name)

    try:
        folders = sorted(p for p in VOICE_ROOT.iterdir() if p.is_dir())
    except OSError:
        folders = []
    for folder in folders:
        for name in ("chat.wav", "chat.mp3", "chat.m4a", "chat.ogg"):
            found.append(folder / name)

    # 克隆音色本体：voice_clone 产出的 clone_resynth.wav，作为「小狗」的单一音色源。
    # 它和 voices/<包名>/chat.wav 是同一段声音，这里也纳入候选，
    # 保证就算 voices 目录被清空，朗读仍能落回这份克隆音色（数据同步）。
    clone_refs = [
        base / "clone_resynth.wav",
        base / "voices" / "clone_resynth.wav",
        base / "试听" / "clone_resynth.wav",
    ]
    if ROOT != base:
        clone_refs.append(ROOT / "clone_resynth.wav")
    found.extend(clone_refs)

    for path in found:
        try:
            if path.is_file() and path.suffix.lower() in AUDIO_EXT:
                return path
        except OSError:
            continue
    return None


class VoiceFile:
    """一句"备用的真嗓子"：一个音频文件 + 它对应原句。

    固定台词走 voices/<包名>/ 里的录音；这里的 chat.wav 只是**它自己那句话**的
    音频（句子写在 .env 的 VOICE_CHAT_TEXT 或 manifest 的 chat_text）。
    只有要念的内容和它一致时才播，绝不拿来顶替任意回复。
    wav 由 Python 直接播，mp3/m4a/ogg 交给页面里的 <audio>。
    """

    def __init__(self) -> None:
        self.path = resolve_voice_file()
        # 有文件音源就不再用系统嗓子；想留后路写 VOICE_ALLOW_SYSTEM=1
        self.allow_system = env_flag("VOICE_ALLOW_SYSTEM", not self.path)
        # 这个音源文件录的是哪句话：只有要念的正好是它，才允许出声。
        # 以前 VOICE_FILE_FALLBACK=1 表示"任意句子都用它顶"，结果每条回复都播
        # 同一段跟内容无关的声音（重复朗读 + 播放不属于当前回复的音频）。
        self.text = self._read_text()
        self.fallback = env_flag("VOICE_FILE_FALLBACK", True)
        self._dur: dict[str, float] = {}

    def _read_text(self) -> str:
        """对话音源对应的原句：优先 .env，其次同目录 manifest.json 的 chat_text。"""
        raw = (os.environ.get("VOICE_CHAT_TEXT") or "").strip()
        if raw:
            return raw
        if self.path is None:
            return ""
        try:
            man = self.path.parent / "manifest.json"
            if man.is_file():
                data = json.loads(man.read_text(encoding="utf-8"))
                for key in ("chat_text", "chatText"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        except Exception:
            log.debug("读 manifest 里的 chat_text 失败", exc_info=True)
        return ""

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"[\s，。！？、；：,.!?;:~～…\-—_\"'“”‘’（）()]+", "", str(text or "")).lower()

    def matches(self, text: str) -> bool:
        """要念的这句话，是不是就是这个文件录的那句（忽略标点/空白）。"""
        if self.path is None or not self.text:
            return False
        want = self._norm(text)
        mine = self._norm(self.text)
        if not want or not mine:
            return False
        return want == mine

    def ready(self) -> bool:
        return self.path is not None

    def url(self) -> str:
        if self.path is None:
            return ""
        try:
            stamp = int(self.path.stat().st_mtime)
        except OSError:
            stamp = 0
        return f"/voice/chat?v={stamp}"

    def duration(self) -> float:
        if self.path is None:
            return 0.0
        key = str(self.path)
        if key not in self._dur:
            self._dur[key] = wav_seconds(self.path)
        return self._dur[key]

    def play(self) -> tuple[bool, float]:
        """返回 (Python 是否已经播出去, 时长)。"""
        if self.path is None:
            return False, 0.0
        seconds = self.duration()
        if self.path.suffix.lower() != ".wav":
            # winsound 只认 wav，其它格式让页面用 <audio> 播同一份文件
            return False, seconds
        try:
            import winsound

            winsound.PlaySound(
                str(self.path),
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        except Exception:
            log.warning("对话音源播放失败：%s", self.path, exc_info=True)
            return False, seconds
        log.info("播放对话音源 %s（%.2fs）", self.path.name, seconds)
        self._play_until = time.monotonic() + (seconds or 0.0)
        return True, seconds

    def stop(self) -> None:
        # 同样：只在自己真在播时才 PURGE，别把克隆 TTS 的声音一起掐了
        if getattr(self, "_play_until", 0.0) <= time.monotonic():
            return
        self._play_until = 0.0
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass


class CloneTTS:
    """可选：把文字交给「声音克隆」接口合成，用你的音色念任意句子。

    只要服务端兼容 OpenAI 的 /v1/audio/speech 就通用：
    GPT-SoVITS、CosyVoice、Fish-Speech、IndexTTS、MiniMax 网关等。

    .env 里配置（openai 模式，通用）：
        VOICE_TTS_MODE=openai
        VOICE_TTS_URL=http://127.0.0.1:9880/v1/audio/speech
        VOICE_TTS_KEY=（可空）
        VOICE_TTS_MODEL=tts-1
        VOICE_TTS_VOICE=我的声音

    或者直接接 GPT-SoVITS 自带的 api_v2.py（gsv 模式）：
        VOICE_TTS_MODE=gsv
        VOICE_TTS_URL=http://127.0.0.1:9880/tts
        VOICE_GSV_REF=D:\\voices\\ref.wav
        VOICE_GSV_PROMPT=参考音频里说的那句话
    """

    def __init__(self) -> None:
        self.url = (os.environ.get("VOICE_TTS_URL") or "").strip()
        self.key = (os.environ.get("VOICE_TTS_KEY") or "").strip()
        self.model = (os.environ.get("VOICE_TTS_MODEL") or "tts-1").strip()
        self.voice = (os.environ.get("VOICE_TTS_VOICE") or "").strip()
        self.mode = (os.environ.get("VOICE_TTS_MODE") or "openai").strip().lower()
        self.ref_audio = (os.environ.get("VOICE_GSV_REF") or "").strip()
        self.ref_text = (os.environ.get("VOICE_GSV_PROMPT") or "").strip()
        self.text_lang = (os.environ.get("VOICE_GSV_TEXT_LANG") or "zh").strip()
        self.prompt_lang = (os.environ.get("VOICE_GSV_PROMPT_LANG") or "zh").strip()
        self.tmp = Path(tempfile.gettempdir()) / "polite_puppy_clone.wav"
        self._busy = threading.Lock()
        self._lock = threading.Lock()
        # 最新句令牌：合成完成时若已不是最新一句（期间有新回复/被打断），直接丢弃不播
        self._latest = ""
        # 正在合成时又来了新句子：只记这一句，等当前合成结束立刻接上（不排队、不浪费算力）
        self._pending = ""
        self._seq = 0
        # 最近一次合成失败的原因（给 /api/voice/diag 看）
        self._last_error = ""
        # 已合成过的整句落盘缓存：同一句话第二次说 0ms 出声（不占显存、不走网络）
        self._cache_dir = exe_dir() / ".voice_cache" / "tts_cache"
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._cache_dir = Path(tempfile.gettempdir()) / "polite_puppy_tts_cache"
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        # 克隆合成允许的整段字数：整段一次合成 => 连贯，不再一句一合成
        try:
            self._speak_chars = int(os.environ.get("VOICE_CLONE_SPEAK_CHARS", "110") or "110")
        except ValueError:
            self._speak_chars = 110

    def invalidate(self) -> None:
        self._latest = ""

    def ready(self) -> bool:
        return bool(self.url)

    def _payload(self, text: str) -> dict[str, Any]:
        if self.mode in ("gsv", "gpt-sovits", "sovits"):
            # GPT-SoVITS api_v2.py 的 /tts 接口
            return {
                "text": text,
                "text_lang": self.text_lang,
                "ref_audio_path": self.ref_audio,
                "prompt_text": self.ref_text,
                "prompt_lang": self.prompt_lang,
                "media_type": "wav",
                "streaming_mode": False,
            }
        voice = self.voice
        if not voice:
            # 没配音色就向「音色身份证」要小狗的档案名，绝不用服务端默认音色
            try:
                if str(tools_dir()) not in sys.path:
                    sys.path.insert(0, str(tools_dir()))
                import voice_identity
                voice = voice_identity.current_profile_id() or "default"
            except Exception:
                voice = "default"
            log.warning("VOICE_TTS_VOICE 未配置，已改用音色身份证的档案名：%s", voice)
        # 关键日志：每次请求都能对上"实际用的是哪个音色和模型"
        log.info("克隆请求 → url=%s model=%s voice=%s", self.url, self.model, voice)
        return {
            "model": self.model,
            "input": text,
            "voice": voice,
            "response_format": "wav",
        }

    def _cache_path(self, text: str) -> Path:
        """整句 → 缓存文件名（同一音色 + 同一句文本 = 同一个 wav）。"""
        key = hashlib.sha1(
            ("v1|" + (self.voice or "") + "|" + text).encode("utf-8")
        ).hexdigest()[:20]
        return self._cache_dir / f"{key}.wav"

    def _play_file(self, path: Path) -> None:
        import winsound

        winsound.PlaySound(
            str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        )

    def _save_cache(self, data: bytes, hit: Path) -> None:
        """落盘缓存，最多留 240 句（按修改时间淘汰），避免无限长胖。"""
        try:
            hit.write_bytes(data)
            olds = sorted(self._cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
            for old in olds[:-240]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    def _fetch(self, text: str) -> None:
        # ① 磁盘缓存命中：0ms 出声。重复的台词/同一句夸奖不再走 GPU。
        hit = self._cache_path(text)
        if hit.is_file():
            if self._latest != text:
                log.info("克隆音频已过时（期间有新回复），不播放")
                return
            self._last_error = ""
            self._play_file(hit)
            log.info("克隆播报「%s」命中缓存 0ms", text[:16])
            return
        payload = json.dumps(self._payload(text), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")
        t0 = time.monotonic()
        # 正常情况下 GPU 一句 1 秒内；留足余量防止服务刚起还在预热。
        # 连不上时先拉起服务再试——服务可能是它自己崩了/刚被重启，
        # 以前这里直接放弃，表现就是"小狗突然不说话了"。
        data = b""
        last_exc: Exception | None = None
        opener = _loopback_opener()      # 克隆服务永远在本机，不绕代理
        for attempt in range(3):
            try:
                with opener.open(req, timeout=300) as resp:
                    data = resp.read()
                last_exc = None
                break
            except urllib.error.HTTPError as exc:
                # 502/503/504 多半是代理或服务刚重启，值得再试；其它 4xx 直接失败
                if exc.code in (502, 503, 504) and attempt < 2:
                    last_exc = exc
                    log.warning("克隆服务暂时不可用（HTTP %s），第 %d 次重试", exc.code, attempt + 1)
                    launch_clone_service()
                    time.sleep(2.5 if attempt == 0 else 6.0)
                    continue
                raise
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt >= 2:
                    break
                log.warning("克隆服务没连上（第 %d 次）：%s，尝试重新拉起", attempt + 1, exc)
                launch_clone_service()
                time.sleep(2.5 if attempt == 0 else 6.0)
        if last_exc is not None:
            self._last_error = str(last_exc)
            raise last_exc
        self._last_error = ""
        spend = time.monotonic() - t0
        if not data:
            raise RuntimeError("接口返回空音频")
        # 合成期间来了新回复 / 被打断：这句已经过时，丢掉，别几分钟后突然冒出来
        if self._latest != text:
            log.info("克隆音频已过时（期间有新回复），不播放")
            return
        # 每次用独立文件名：上一句还在播时被覆盖会截断/爆音
        with self._lock:
            self._seq += 1
            seq = self._seq
        tmp = self.tmp.with_name(f"{self.tmp.stem}.{os.getpid()}.{seq}.wav")
        tmp.write_bytes(data)
        self._save_cache(data, hit)
        # 清掉更早的临时文件（最多留最近 3 个）
        try:
            olds = sorted(self.tmp.parent.glob(f"{self.tmp.stem}.{os.getpid()}.*.wav"))
            for old in olds[:-3]:
                old.unlink(missing_ok=True)
        except OSError:
            pass
        self._play_file(tmp)
        log.info("克隆播报「%s」合成 %.2fs", text[:16], spend)

    def speak(self, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not self.url:
            return {"ok": False, "message": "没有配置 VOICE_TTS_URL"}
        if not text:
            return {"ok": False, "message": "空文本"}
        with self._lock:
            self._latest = text
            if not self._busy.acquire(blocking=False):
                # 上一句还在合成：只留最新这句，合成线程一结束就接着做
                self._pending = text
                log.debug("克隆忙，已排队最新句：%s", text[:20])
                return {"ok": True, "duration": estimate_speech_seconds(text),
                        "voice": "clone", "async": True, "queued": True}

        def run() -> None:
            try:
                self._fetch(text)
            except Exception as exc:
                log.warning("克隆音色合成失败：%s", exc)
            finally:
                self._busy.release()
                nxt = ""
                with self._lock:
                    if self._pending and self._pending == self._latest:
                        nxt = self._pending
                        self._pending = ""
                if nxt and nxt == self._latest:
                    self.speak(nxt)      # 接上最新那句

        threading.Thread(target=run, name="clone-tts", daemon=True).start()
        return {"ok": True, "duration": estimate_speech_seconds(text),
                "voice": "clone", "async": True}


class SpeechEngine:
    """系统 SAPI 朗读，优先中文女声。

    单独开一条线程：SAPI 的 COM 对象对线程敏感，创建和使用必须在同一条线程里。
    WebView2 里的 speechSynthesis 不保证有声音，所以默认走这边。
    """

    def __init__(self) -> None:
        # 配置时机兜底：不管本引擎在什么时刻被构造，都先确保 .env 已读进来。
        # 以前若 .env 晚于这里加载，VOICE_TTS_URL 为空 → 克隆永远不可用 →
        # 于是静默落到系统嗓子（这就是"声音变成系统合成音"的根源之一）。
        try:
            load_dotenv(exe_dir() / ".env")
        except Exception:
            pass
        self._queue: queue.Queue[Any] = queue.Queue()
        self._ready = threading.Event()
        self._voice = ""
        self._zh_voice = False
        self._error = ""
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._last_text = ""
        self._last_at = 0.0
        # 朗读策略：read=读原文（默认，内容对得上）/ clone=优先克隆录音 /
        #           intent=只按情绪播录音（老行为）
        self.read_mode = (os.environ.get("VOICE_READ_MODE") or "read").strip().lower()
        if self.read_mode not in ("read", "clone", "intent"):
            self.read_mode = "read"
        # 系统 SAPI 当"读原文"的嗓子。有中文语音包才有意义，
        # 没装中文嗓子时 _sapi_speak 会自己返回 False。
        self.sapi_enabled = env_flag("VOICE_ALLOW_SYSTEM", True)
        # 系统嗓子的音色贴近：Huihui 基频约 190Hz，参考录音约 242Hz，
        # 用 SSML 的 prosody 抬一点音高、稍快一点，听起来更像克隆音色。
        self._pitch = os.environ.get("VOICE_SAPI_PITCH", "+16")
        self._rate = os.environ.get("VOICE_SAPI_RATE", "+4")
        self._ssml_failed = False
        self.pack = VoicePack()
        self.pack.auto_pick()
        self.file = VoiceFile()
        self.clone = CloneTTS()
        # 克隆合成策略：
        #   VOICE_CLONE_ALWAYS=1 → 所有自由文本都交给克隆（GPU 上 1 秒内，用户要求全程克隆音色）
        #   否则退回旧闸门 VOICE_CLONE_MAX_CHARS（0=不用克隆）
        self.clone_always = env_flag("VOICE_CLONE_ALWAYS", False)
        try:
            self.clone_max = int(os.environ.get("VOICE_CLONE_MAX_CHARS", "0") or "0")
        except ValueError:
            self.clone_max = 0
        # 整段克隆播报的字数上限（一次合成整段，听感连贯；超长才按标点收口）
        try:
            self.clone_chars = max(20, int(os.environ.get("VOICE_CLONE_SPEAK_CHARS", "110") or "110"))
        except ValueError:
            self.clone_chars = 110
        # 严格克隆（默认开）：克隆不可用时宁可安静并明确报错，
        # 绝不静默回落到系统合成音。想临时允许系统音就设 VOICE_STRICT_CLONE=0。
        self.strict_clone = env_flag("VOICE_STRICT_CLONE", True)
        self.clone_error = ""
        self.clone_info: dict[str, Any] = {}

    def _verify_clone(self) -> dict[str, Any]:
        """初始化阶段校验克隆音色是否真的可用：地址 → 服务健康 → 音色身份证。

        任何一步不过都记下明确原因，供日志与 /api/voice/diag 查看；
        严格模式下这些原因会直接导致"不出声"，而不是悄悄换成系统嗓子。
        """
        info: dict[str, Any] = {
            "ok": False, "engine": "clone",
            "voice": self.clone.voice, "model": self.clone.model,
            "url": self.clone.url, "reason": "",
        }
        if not self.clone.ready():
            info["reason"] = "未配置 VOICE_TTS_URL（克隆接口地址为空）"
            self.clone_error = info["reason"]
            log.error("克隆音色不可用：%s", info["reason"])
            self.clone_info = info
            return info

        # ① 服务健康
        try:
            base = self.clone.url.split("/v1/")[0].split("/tts")[0]
            with urllib.request.urlopen(base + "/health", timeout=3) as resp:
                info["server"] = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as exc:
            info["reason"] = f"克隆服务不可达（{self.clone.url}）：{exc}"
            self.clone_error = info["reason"]
            log.error("克隆音色不可用：%s", info["reason"])
            self.clone_info = info
            return info

        # ② 音色身份证：确认用的是小狗那条声线（参考音频 + 参数 + 指纹）
        try:
            if str(tools_dir()) not in sys.path:
                sys.path.insert(0, str(tools_dir()))
            import voice_identity
            ident = voice_identity.sync_now()
            chk = voice_identity.verify()
            info.update(voice_id=ident.get("voice_id"), ref=ident.get("ref"),
                        drift=chk.get("drift"), params=ident.get("params"))
            if not ident.get("ok"):
                info["reason"] = f"音色身份证不可用：{ident.get('message')}"
                self.clone_error = info["reason"]
                log.error("克隆音色不可用：%s", info["reason"])
                self.clone_info = info
                return info
        except Exception as exc:
            info["reason"] = f"音色身份证校验失败：{exc}"
            self.clone_error = info["reason"]
            log.error("克隆音色不可用：%s", info["reason"])
            self.clone_info = info
            return info

        info["ok"] = True
        log.info("克隆音色就绪：voice_id=%s model=%s voice=%s ref=%s drift=%s",
                 info.get("voice_id"), info.get("model"), info.get("voice"),
                 info.get("ref"), info.get("drift"))
        self.clone_info = info
        return info

    def clone_allows(self, text: str) -> bool:
        """这句要不要交给克隆接口。"""
        n = len(text)
        if n <= 0:
            return False
        # 严格克隆（默认开）：克隆可用就一律走克隆。
        # 以前这里的闸门是 clone_max>0，而 VOICE_CLONE_MAX_CHARS 默认 0，
        # 于是克隆分支永远进不去、每句都掉到系统嗓子。
        if self.strict_clone and self.clone.ready():
            return n <= 120
        if self.clone_always:
            return n <= 120          # 单次播报已经收口到 ≤30 字，这里只防异常长文本
        return self.clone_max > 0 and n <= self.clone_max

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="speech", daemon=True)
                self._thread.start()

    def warm_up(self) -> None:
        """提前初始化合成器：日志里能立刻看到用的是哪个嗓子，或者为什么用不了。"""
        self._ensure_thread()
        self._ready.wait(4.0)
        # 校验克隆音色是否真的可用（地址 / 服务 / 音色身份证），结果写进日志
        info = self._verify_clone()
        if not info.get("ok"):
            log.error("启动校验：克隆音色不可用 → %s（严格克隆=%s，已禁止回退系统音）",
                      info.get("reason"), self.strict_clone)
        if self._error:
            log.warning("语音合成不可用：%s", self._error)
        else:
            log.info("语音合成已就绪：%s（read_mode=%s，系统语音=%s，严格克隆=%s）",
                     self._voice or "默认嗓子", self.read_mode, self.sapi_enabled,
                     self.strict_clone)

    def _build(self) -> Any:
        if not load_system_speech():
            raise RuntimeError("系统里没装语音合成组件（System.Speech）")
        from System.Speech.Synthesis import SpeechSynthesizer  # type: ignore

        synth = SpeechSynthesizer()
        try:
            synth.SetOutputToDefaultAudioDevice()
        except Exception:
            log.debug("设置默认音频输出失败", exc_info=True)

        for item in synth.GetInstalledVoices():
            try:
                info = item.VoiceInfo
                culture = str(info.Culture).lower()
                name = str(info.Name)
                if item.Enabled and ("zh" in culture or "chinese" in name.lower()):
                    synth.SelectVoice(name)
                    self._voice = name
                    self._zh_voice = True
                    break
            except Exception:
                continue

        if not self._zh_voice:
            log.warning("系统里没有中文语音包，读原文会不可用（去 设置-时间和语言-语音 装一个）")
        try:
            synth.Rate = 1
            synth.Volume = int(os.environ.get("POLITE_PUPPY_TTS_VOLUME", "85"))
        except Exception:
            pass
        log.info("语音合成就绪：%s", self._voice or "系统默认嗓子")
        return synth

    def _loop(self) -> None:
        try:
            synth = self._build()
        except Exception as exc:
            self._error = str(exc)
            self._ready.set()
            log.warning("语音合成不可用：%s", exc)
            return

        self._ready.set()
        while True:
            task = self._queue.get()
            if task is None:
                break
            kind, text = task
            try:
                if kind == "speak":
                    synth.SpeakAsyncCancelAll()
                    if not self._ssml_failed and self._speak_ssml(synth, text):
                        continue
                    synth.SpeakAsync(text)
                elif kind == "stop":
                    synth.SpeakAsyncCancelAll()
            except Exception as exc:
                self._error = str(exc)
                log.warning("语音合成失败：%s", exc)

    @staticmethod
    def _rel(raw: str, default: str) -> str:
        """把 16 / +16 / 16% 统一成 SSML 要求的相对值 +16%。"""
        val = (raw or default).strip()
        if re.match(r"^[+-]?\d+(\.\d+)?%?$", val):
            if not val.startswith(("+", "-")):
                val = "+" + val
            if not val.endswith("%"):
                val += "%"
        return val

    def _speak_ssml(self, synth: Any, text: str) -> bool:
        """用 SSML 抬音高/调语速念一句；引擎不支持时返回 False 让它退回纯文本。"""
        try:
            from xml.sax.saxutils import escape  # type: ignore

            ssml = (
                '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
                'xml:lang="zh-CN"><prosody pitch="%s" rate="%s">%s</prosody></speak>'
                % (self._rel(self._pitch, "+16"), self._rel(self._rate, "+4"), escape(text))
            )
            synth.SpeakSsmlAsync(ssml)
            return True
        except Exception:
            self._ssml_failed = True
            log.debug("这个中文语音包不吃 SSML，退回纯文本朗读", exc_info=True)
            return False

    def available(self) -> dict[str, Any]:
        info: dict[str, Any] = {}
        if self.pack.name:
            info["pack"] = self.pack.name
            info["intents"] = self.pack.intents()
        if self.clone.ready():
            info["clone"] = True
        if self.file.ready():
            info["file"] = self.file.path.name
            info["voiceUrl"] = self.file.url()
        if info:
            info.setdefault("ok", True)
            info.setdefault(
                "voice", self.pack.name or ("file" if self.file.ready() else "clone")
            )
            return info
        self._ensure_thread()
        self._ready.wait(4.0)
        if self._error:
            return {"ok": False, "message": self._error}
        return {"ok": True, "voice": self._voice}

    def speak(self, text: Any, intent: str = "", mode: str = "") -> dict[str, Any]:
        """把一句话念出来。优先级（核心原则：**播出来的必须是这句话本身**）：

        1. 原句精确命中语音包录音（texts）—— 内容对得上，还是自己的音色；
        2. 对话音源 chat.wav —— 同样要求 VOICE_CHAT_TEXT 与这句一致才播；
        3. 读原文（read 模式）：克隆 TTS（仅当 VOICE_CLONE_MAX_CHARS 放行）>
           系统中文 SAPI（零 GPU、即时，SSML 抬音高贴近克隆音色）；
        4. 情绪录音（intent 模式 + 短句）；
        5. clone / intent 模式下仍可用克隆合成这句话；
        6. 都不行返回 ``silent``：宁可安静，也不播一段跟当前回复无关的声音。

        ``mode`` 由前端传：
          - ``""`` / ``"read"``：读原文（默认，内容对得上）
          - ``"clone"``：优先用克隆录音，读不了就安静
          - ``"intent"``：只按情绪播一条录音（老行为）
        """
        clean = clean_speech_text(str(text or ""))
        intent = str(intent or "").strip().lower()
        mode = (str(mode or "").strip().lower() or self.read_mode)

        # 去重 + 抢占：新的一句来了先掐掉还在播的，别两句叠在一起
        now = time.monotonic()
        if clean and clean != self._last_text:
            self.pack.stop()
            self.file.stop()
            self.clone.invalidate()          # 旧句合成完也不许再播
            try:                              # 连"已经在播的克隆音"也掐掉（stop() 只管自己那路）
                import winsound

                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            self._queue.put(("stop", ""))
        elif clean and clean == self._last_text and (now - self._last_at) < 12.0:
            return {"ok": True, "skipped": True, "reason": "duplicate",
                    "duration": 0.0, "voice": "dedupe"}
        self._last_text, self._last_at = clean, now

        # 1) 原句命中录音
        if self.pack.name:
            hit = self.pack.match_text(clean)
            if hit is not None:
                played, seconds = self.pack.play_text(hit)
                if played:
                    return {
                        "ok": True,
                        "duration": seconds,
                        "voice": f"pack:{self.pack.name}",
                        "engine": "pack-text",
                    }

        # 2) 对话音源：只有"要念的就是它录的那句话"才播。
        #    内容对得上 + 是克隆音色，比系统嗓子更优先；但绝不拿来顶替别的回复
        #    （以前对任意文本都播同一个 chat.wav，这就是"重复朗读/放错音频"的根源）。
        if self.file.ready() and self.file.matches(clean):
            played, seconds = self.file.play()
            return {
                "ok": True,
                "duration": seconds or estimate_speech_seconds(clean),
                "voice": f"file:{self.file.path.name}",
                "engine": "file-match",
                "audio": self.file.url(),
                "played": played,
            }

        # 读原文前先收口成 1~2 个短句（前端已经收过，这里再兜一次）
        read_text = short_speech(clean) if len(clean) > 30 else clean
        canned = len(read_text) <= 10      # 短句才配用情绪录音
        # 克隆不走"只念第一句"：整段一次合成，听感才连贯。超长才按标点收口。
        clone_text = clean
        if len(clone_text) > self.clone_chars:
            clone_text = short_speech(clean, max_clauses=99, max_chars=self.clone_chars)

        # 3) intent 模式：短句先用情绪录音（更像真人反应）。
        #    但开了 VOICE_CLONE_ALWAYS 就跳过——用户要求"念的就是当前这句"，
        #    不能再拿一条固定情绪录音顶替。
        if canned and self.pack.name and mode == "intent" and not self.clone_always:
            played, seconds = self.pack.play(intent, "")
            if played:
                return {
                    "ok": True,
                    "duration": seconds,
                    "voice": f"pack:{self.pack.name}",
                    "engine": "pack-intent",
                }

        # 4) 克隆音色合成"这句话本身"（整段一次合成，连贯不逐句）
        if self.strict_clone and not self.clone.ready():
            # 严格模式：克隆压根不可用 → 明确报错 + 保持安静，绝不换系统音
            return {
                "ok": False, "silent": True, "duration": 0.0,
                "voice": "clone", "engine": "clone", "reason": "clone_unavailable",
                "message": self.clone_error or "克隆音色不可用（已禁止回退系统音）",
            }

        if clone_text and self.clone.ready() and self.clone_allows(clone_text):
            got = self.clone.speak(clone_text)
            if got.get("ok"):
                log.info("播报引擎=clone model=%s voice=%s 文本=%r",
                         self.clone.model, self.clone.voice, clone_text[:20])
                return got
            log.warning("克隆合成失败：%s", got.get("message"))
            if self.strict_clone:
                return {
                    "ok": False, "silent": True, "duration": 0.0,
                    "voice": "clone", "engine": "clone", "reason": "clone_failed",
                    "message": got.get("message") or "克隆合成失败（已禁止回退系统音）",
                }
            log.debug("克隆接口没成，继续往下走：%s", got.get("message"))

        # 5) 系统中文嗓子：严格克隆模式下**永远不启用**（默认严格）。
        #    只有显式设 VOICE_STRICT_CLONE=0 且未开全程克隆时，才可能走到这里。
        allow_sapi = (self.sapi_enabled and not self.strict_clone
                      and not (self.clone_always and self.clone.ready()))
        if read_text and allow_sapi and self._sapi_speak(read_text):
            log.warning("播报引擎=sapi（系统合成音）—— 严格克隆已关闭，且克隆不可用")
            return {
                "ok": True,
                "duration": estimate_speech_seconds(read_text),
                "voice": self._voice or "系统中文",
                "engine": "sapi",
            }

        # 6) 真的没有任何引擎：宁可安静，也不要放一段对不上的声音
        log.debug("这句没有能念对的引擎，保持安静：%r", clean[:30])
        return {
            "ok": True,
            "silent": True,
            "duration": 0.0,
            "voice": "silent",
            "reason": "no_content_matched_engine",
        }

    def _sapi_speak(self, clean: str) -> bool:
        """系统 SAPI 逐字朗读。没装组件 / 没有中文嗓子时返回 False。"""
        if not clean:
            return False
        if not self.sapi_enabled:
            return False
        self._ensure_thread()
        if not self._ready.wait(2.0):
            return False
        if self._error or not self._zh_voice:
            return False
        try:
            self._queue.put(("speak", clean))
        except Exception:
            return False
        return True

    def stop(self) -> dict[str, Any]:
        self.pack.stop()
        self.clone.invalidate()
        self.file.stop()
        try:                              # 用户按"停止播报"：正在播的克隆音也要立刻停
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        self._queue.put(("stop", ""))
        return {"ok": True}

    def shutdown(self, timeout: float = 1.0) -> None:
        """退出时让语音线程自己收尾，不然 pythonnet 在解释器关闭时会吐异常。"""
        thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put(None)
            thread.join(timeout)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# JS API
# ---------------------------------------------------------------------------
class _PetApi:
    def __init__(self, app: "PetApp") -> None:
        # 必须用下划线属性：pywebview 会递归遍历 js_api 的公开属性，
        # 如果叫 self.app，它会把整个窗口 / WinForms 对象也翻一遍，导致递归爆栈。
        self._app = app

    def toggle_chat(self) -> None:
        self._app.toggle_chat()

    def open_chat(self) -> None:
        self._app.show_chat()

    def close_chat(self) -> None:
        self._app.hide_chat()

    def quit(self) -> None:
        self._app.quit()

    def show_menu(self) -> None:
        self._app.show_pet_menu()

    def open_singer(self) -> dict[str, Any]:
        """打开小狗唱歌台（puppy-sing）。"""
        return self._app.launch_singer(open_learn=False)

    def open_singer_learn(self) -> dict[str, Any]:
        """打开唱歌台并直达「扒歌学习」。"""
        return self._app.launch_singer(open_learn=True)

    def open_settings(self) -> dict[str, Any]:
        """打开设置窗口（独立面板窗）。"""
        return self._app.open_settings_window()

    # ---- 语音合成（桌宠说话）----
    def speak(self, text: str = "", intent: str = "", mode: str = "") -> dict[str, Any]:
        return self._app.speech.speak(text, intent, mode)

    def get_read_mode(self) -> dict[str, Any]:
        return {"ok": True, "mode": self._app.speech.read_mode,
                "modes": ["read", "clone", "intent"],
                "sapi": bool(self._app.speech._zh_voice) and self._app.speech.sapi_enabled,
                "voice": self._app.speech._voice}

    def set_read_mode(self, mode: str = "") -> dict[str, Any]:
        return self._app.set_read_mode(mode)

    def open_window(self, kind: str = "", payload: Any = None) -> dict[str, Any]:
        return self._app.open_asset_window(kind, payload or {})

    def draw_image(self, prompt: str = "", width: int = 768, height: int = 768) -> dict[str, Any]:
        return self._app.draw_image(prompt, width, height)

    def make_deck(self, topic: str = "", bullets: Any = None) -> dict[str, Any]:
        return self._app.make_deck(topic, bullets or [])

    def voice_packs(self) -> dict[str, Any]:
        pack = self._app.speech.pack
        return {
            "packs": list(pack.packs),
            "current": pack.name,
            "intents": pack.intents(),
            "strict": pack.strict,
            "clone": self._app.speech.clone.ready(),
        }

    def use_voice_pack(self, name: str = "") -> dict[str, Any]:
        pack = self._app.speech.pack
        pack.reload()
        ok = pack.set(name) if name else bool(pack.auto_pick())
        if ok:
            self._app.config["voice_pack"] = pack.name
            save_config(self._app.config)
        return {"ok": ok, "current": pack.name, "intents": pack.intents()}

    def stop_speak(self) -> dict[str, Any]:
        return self._app.speech.stop()

    def speak_available(self) -> dict[str, Any]:
        return self._app.speech.available()

    # ---- 语音输入（桌宠麦克风）----
    def voice_start(self, lang: str = "zh-CN") -> dict[str, Any]:
        return self._app.voice.start(lang or "zh-CN")

    def voice_poll(self) -> dict[str, Any]:
        return self._app.voice.poll()

    def voice_stop(self) -> dict[str, Any]:
        return self._app.voice.stop()

    def chat_from_pet(self, text: str = "") -> None:
        self._app.chat_from_pet(text)

    def get_flags(self) -> dict[str, Any]:
        return {
            "voice": self._app.voice_on,
            "sound": self._app.sound_on,
            "pack": self._app.speech.pack.name,
            "strict": self._app.speech.pack.strict,
            "voiceUrl": (
                self._app.speech.file.url()
                if self._app.speech.file.ready() and self._app.speech.file.fallback
                else ""
            ),
        }


class _ChatApi:
    def __init__(self, app: "PetApp") -> None:
        self._app = app

    def close_chat(self) -> None:
        self._app.hide_chat()

    def toggle_chat(self) -> None:
        self._app.toggle_chat()

    # ---- 跨域兜底：前端 fetch 不通时直接走这个 ----
    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._app.bridge_chat(payload)

    # ---- 语音 ----
    def voice_start(self, lang: str = "zh-CN") -> dict[str, Any]:
        return self._app.voice.start(lang or "zh-CN")

    def voice_poll(self) -> dict[str, Any]:
        return self._app.voice.poll()

    def voice_stop(self) -> dict[str, Any]:
        return self._app.voice.stop()

    # ---- 语音输出：聊天页的播报队列靠这两个出声 ----
    def speak(self, text: str = "", intent: str = "", mode: str = "") -> dict[str, Any]:
        return self._app.speech.speak(text, intent, mode)

    def stop_speak(self) -> dict[str, Any]:
        return self._app.speech.stop()

    # ---- 朗读策略：read=读原文 / clone=克隆录音 / intent=只播情绪录音 ----
    def get_read_mode(self) -> dict[str, Any]:
        return {"ok": True, "mode": self._app.speech.read_mode,
                "modes": ["read", "clone", "intent"],
                "sapi": bool(self._app.speech._zh_voice) and self._app.speech.sapi_enabled,
                "voice": self._app.speech._voice}

    def set_read_mode(self, mode: str = "") -> dict[str, Any]:
        return self._app.set_read_mode(mode)

    # ---- 音色：列出 / 切换（持久化克隆音源）----
    def list_voices(self) -> dict[str, Any]:
        return list_voice_profiles()

    def set_voice(self, pid: str = "") -> dict[str, Any]:
        return self._app.set_voice(pid)

    # ---- 独立窗口：PPT / 图片 ----
    def open_window(self, kind: str = "", payload: Any = None) -> dict[str, Any]:
        return self._app.open_asset_window(kind, payload or {})

    # ---- 聊天窗口尺寸：预设按钮 + 自由拖拽都会记住 ----
    def resize_chat(self, w: int = 0, h: int = 0) -> dict[str, Any]:
        return self._app.resize_chat_window(int(w or 0), int(h or 0))

    def get_chat_size(self) -> dict[str, Any]:
        return {"ok": True, "w": self._app.chat_w, "h": self._app.chat_h}

    def draw_image(self, prompt: str = "", width: int = 768, height: int = 768) -> dict[str, Any]:
        return self._app.draw_image(prompt, width, height)

    def make_deck(self, topic: str = "", bullets: Any = None) -> dict[str, Any]:
        return self._app.make_deck(topic, bullets or [])

    # ---- 把回复同步给桌宠：气泡 + 口型 + 朗读 ----
    def pet_say(self, text: str = "") -> None:
        self._app.pet_say(text)

    def pet_state(self, state: str = "") -> None:
        self._app.pet_state(state)

    # ---- 快捷键 ----
    def get_hotkeys(self) -> dict[str, Any]:
        return self._app.get_hotkeys()

    def set_hotkeys(self, bindings: Any) -> dict[str, Any]:
        return self._app.set_hotkeys(bindings)


HOTKEY_ACTIONS = {
    "voice_out": "语音输出 开 / 关",
    "voice_pause": "播报 暂停 / 继续",
    "voice_stop": "播报 立即停止",
    "mic": "语音输入 开 / 关",
    "settings": "打开设置窗口",
    "sing": "小狗唱歌（打开唱歌台）",
    "sing_learn": "扒歌学习（听原唱自学）",
}

DEFAULT_HOTKEYS = {
    # 对话窗口不再占用全局快捷键：点立绘、托盘、或组合热键都能开，
    # 列表里只留和"说话"直接相关的四个，且都可以在设置里改。
    "voice_out": "Ctrl+Alt+V",
    "voice_pause": "Ctrl+Alt+Space",
    "voice_stop": "Ctrl+Alt+X",
    "mic": "Ctrl+Alt+I",          # Ctrl+Alt+M 常被输入法/截图工具占掉
    # 设置 + 唱歌台（puppy-sing）入口：同样可以在 设置 → 键位 里改
    "settings": "Ctrl+Alt+S",
    "sing": "Ctrl+Alt+G",
    "sing_learn": "Ctrl+Alt+L",
}

# 只认常用键，够用且不会和输入法抢
_VK_NAMES = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D, "ESC": 0x1B,
    "ESCAPE": 0x1B, "SPACE": 0x20, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "END": 0x23, "HOME": 0x24, "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27,
    "DOWN": 0x28, "INSERT": 0x2D, "DELETE": 0x2E, "`": 0xC0, "-": 0xBD,
    "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA, "'": 0xDE,
    ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
for _i in range(1, 13):
    _VK_NAMES[f"F{_i}"] = 0x6F + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    _VK_NAMES[_c] = ord(_c)


def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """'Ctrl+Alt+V' -> (modifiers, vk)。解析不了返回 None。"""
    raw = str(spec or "").strip()
    if not raw:
        return None
    mods = 0
    parts = [p.strip() for p in raw.replace("＋", "+").split("+") if p.strip()]
    if not parts:
        return None
    for token in parts[:-1]:
        low = token.lower()
        if low in ("ctrl", "control"):
            mods |= 0x0002
        elif low in ("alt", "menu"):
            mods |= 0x0001
        elif low == "shift":
            mods |= 0x0004
        elif low in ("win", "super", "cmd"):
            mods |= 0x0008
        else:
            return None
    key = parts[-1].upper()
    vk = _VK_NAMES.get(key)
    if vk is None and len(key) == 1:
        vk = ord(key)
    if vk is None:
        return None
    return mods | 0x4000, vk        # 0x4000 = MOD_NOREPEAT


def load_hotkeys(cfg: dict[str, Any] | None = None) -> dict[str, str]:
    """配置里没有或写坏了就退回默认值。"""
    raw = (cfg or {}).get("hotkeys")
    out = dict(DEFAULT_HOTKEYS)
    if isinstance(raw, dict):
        for action in DEFAULT_HOTKEYS:
            val = raw.get(action)
            if isinstance(val, str):
                out[action] = val.strip()
    return out


class GlobalHotkey(threading.Thread):
    """全局快捷键：聊天开关 + 语音输出/暂停/停止 + 语音输入。

    注册/注销必须在有消息循环的那条线程里做，所以改键位走 PostThreadMessage
    回到本线程重注册，主线程只负责改配置。
    """

    HOTKEY_ID_BASE = 100
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    WM_APPLY = 0x8000 + 1          # 自定义：重载键位

    def __init__(self, app: "PetApp") -> None:
        super().__init__(name="global-hotkey", daemon=True)
        self._app = app
        self._bindings: dict[str, str] = dict(DEFAULT_HOTKEYS)
        self._pending: dict[str, str] | None = None
        self._live: dict[int, str] = {}      # hotkey id -> action
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self.last_errors: dict[str, str] = {}

    # ---- 主线程调用 ----
    def apply(self, bindings: dict[str, str]) -> None:
        with self._lock:
            self._bindings = dict(bindings or {})
        self._ready.wait(3.0)
        try:
            ctypes.windll.user32.PostThreadMessageW(self.ident, self.WM_APPLY, 0, 0)
        except Exception:
            log.debug("热键重载消息发送失败", exc_info=True)

    # ---- 热键线程 ----
    def _unregister_all(self, user32: Any) -> None:
        for hid in list(self._live):
            try:
                user32.UnregisterHotKey(None, hid)
            except Exception:
                pass
        self._live.clear()

    def _register_all(self, user32: Any, wintypes: Any) -> None:
        self._unregister_all(user32)
        self.last_errors = {}
        with self._lock:
            bindings = dict(self._bindings)
        for index, (action, spec) in enumerate(bindings.items()):
            if not spec:
                continue                      # 留空 = 不绑定
            parsed = parse_hotkey(spec)
            if parsed is None:
                self.last_errors[action] = f"键位写法不认：{spec}"
                continue
            mods, vk = parsed
            hid = self.HOTKEY_ID_BASE + index
            if user32.RegisterHotKey(None, hid, mods, vk):
                self._live[hid] = action
                log.info("快捷键 %s -> %s", spec, action)
            else:
                self.last_errors[action] = f"{spec} 注册失败（被别的程序占了？）"
                log.warning("快捷键注册失败：%s (%s)", spec, action)
        if self.last_errors:
            log.warning("部分快捷键不可用：%s", self.last_errors)

    def run(self) -> None:
        if sys.platform != "win32":
            self._ready.set()
            return
        user32 = None
        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            user32.RegisterHotKey.restype = wintypes.BOOL
            self._register_all(user32, wintypes)
            self._ready.set()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_APPLY:
                    self._register_all(user32, wintypes)
                    continue
                if msg.message != self.WM_HOTKEY:
                    continue
                action = self._live.get(int(msg.wParam))
                if action:
                    try:
                        self._app.hotkey_action(action)
                    except Exception:
                        log.exception("快捷键处理失败：%s", action)
        except Exception:
            log.exception("全局快捷键线程异常")
        finally:
            self._ready.set()
            if user32 is not None:
                self._unregister_all(user32)

    def stop(self) -> None:
        try:
            if self.ident is not None:
                ctypes.windll.user32.PostThreadMessageW(self.ident, self.WM_QUIT, 0, 0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------
class PetApp:
    def __init__(self, no_tray: bool = False, debug: bool = False) -> None:
        self.no_tray = no_tray
        self.debug = debug
        self.quitting = False
        self.pet: Any = None
        self.chat: Any = None
        self.icon: Any = None
        self.chat_visible = False
        self.chat_shown_once = False
        self.server = LocalServer(ROOT)
        self.config = load_config()
        self._move_timer: threading.Timer | None = None
        self.hotkey: GlobalHotkey | None = None
        self.voice = VoiceEngine()

        try:
            scale = float(self.config.get("pet_scale", 1.0))
        except (TypeError, ValueError):
            scale = 1.0
        self.pet_scale = min(max(scale, 0.7), 1.6)
        self.on_top = bool(self.config.get("on_top", True))
        self.voice_on = bool(self.config.get("voice", True))
        self.sound_on = bool(self.config.get("sound", True))
        # 聊天窗口尺寸：配置里存过就用存的，否则用默认（已放大到 560x900）
        try:
            self.chat_w = int(self.config.get("chat_w") or CHAT_W)
            self.chat_h = int(self.config.get("chat_h") or CHAT_H)
        except (TypeError, ValueError):
            self.chat_w, self.chat_h = CHAT_W, CHAT_H
        self.chat_w = min(max(self.chat_w, CHAT_MIN_W), 1600)
        self.chat_h = min(max(self.chat_h, CHAT_MIN_H), 1600)
        self.speech = SpeechEngine()
        # 注意：这里**不能**提前开线程初始化 SAPI。
        # pywebview 在 webview.start() 里才加载 pythonnet，如果我们在它之前
        # 从另一个线程 import clr，winforms 那边会拿到一个没有 AddReference 的
        # 半成品 clr，窗口直接建不出来（实测过一次，整只狗消失）。
        # 语音按需初始化：第一次要出声时再建，日志里同样能看到结果。
        # 朗读策略从配置里恢复（read=读原文 / clone=克隆录音 / intent=情绪录音）
        saved_mode = str(self.config.get("read_mode") or "").strip().lower()
        if saved_mode == "intent":
            # 老版"只听情绪"长句完全不出声，表现就是"发消息不朗读"；一次性迁回读原文
            saved_mode = "read"
            self.config["read_mode"] = "read"
            save_config(self.config)
        if saved_mode in ("read", "clone", "intent"):
            self.speech.read_mode = saved_mode
        # 台词库=当前音色档案（voices/profiles/<id>）：命中就播已生成好的 wav，
        # 没命中才交给克隆服务合成。老配置里的 voices_dir 不再优先，避免指回旧目录。
        prof = active_voice_profile()
        pack_dir = Path(str(self.config.get("voices_dir") or VOICE_ROOT))
        if not pack_dir.is_dir():
            pack_dir = VOICE_ROOT
        if pack_dir != self.speech.pack.root:
            self.speech.pack.root = pack_dir
            self.speech.pack.reload()
        self.speech.pack.auto_pick(str(prof.get("name") or self.config.get("voice_pack") or ""))
        if self.speech.pack.name:
            log.info("音色档案：%s（台词 %d 条）", self.speech.pack.name, len(self.speech.pack.texts()))
        self._migrate_layout()

    def _migrate_layout(self) -> None:
        """窗口从 300x300 加高到 300x392 给气泡留位置。

        旧配置里存的是窗口左上角；把 y 往上挪 92px，立绘在屏幕上看起来不动。
        """
        try:
            version = int(self.config.get("layout_v", 1))
        except (TypeError, ValueError):
            version = 1
        if version >= 2:
            return
        y = self.config.get("pet_y")
        if isinstance(y, int):
            self.config["pet_y"] = y - (PET_H - 300)
        self.config["layout_v"] = 2
        try:
            save_config(self.config)
        except Exception:
            log.debug("布局迁移保存失败", exc_info=True)

    # ---- 位置 ----
    def _initial_position(self) -> tuple[int, int]:
        left, top, right, bottom = work_area()
        width = int(PET_W * self.pet_scale)
        height = int(PET_H * self.pet_scale)
        default_x = max(left, right - width - 24)
        default_y = max(top, bottom - height - 16)

        x = self.config.get("pet_x")
        y = self.config.get("pet_y")
        if isinstance(x, int) and isinstance(y, int):
            vx, vy, vw, vh = virtual_screen()
            if vx <= x < vx + vw and vy <= y < vy + vh:
                return x, y
        return default_x, default_y

    def _remember_position(self) -> None:
        if self.pet is None:
            return
        try:
            self.config["pet_x"] = int(self.pet.x)
            self.config["pet_y"] = int(self.pet.y)
            save_config(self.config)
        except Exception:
            log.debug("保存窗口位置失败", exc_info=True)

    def _on_pet_moved(self) -> None:
        if self._move_timer is not None:
            self._move_timer.cancel()
        self._move_timer = threading.Timer(0.6, self._remember_position)
        self._move_timer.daemon = True
        self._move_timer.start()

    # ---- 句柄 / 置顶 / 缩放 / 自启 ----
    def _pet_hwnd(self) -> int:
        try:
            return int(self.pet.native.Handle.ToInt64())
        except Exception:
            return 0

    def _chat_hwnd(self) -> int:
        try:
            return int(self.chat.native.Handle.ToInt64())
        except Exception:
            return 0

    def _set_topmost(self, enabled: bool) -> None:
        self.on_top = bool(enabled)
        for hwnd in (self._pet_hwnd(), self._chat_hwnd()):
            if hwnd:
                set_window_topmost(hwnd, self.on_top)
        self.config["on_top"] = self.on_top
        save_config(self.config)
        self._refresh_tray_menu()

    def _set_pet_scale(self, scale: float) -> None:
        try:
            scale = min(max(float(scale), 0.7), 1.6)
        except (TypeError, ValueError):
            return
        hwnd = self._pet_hwnd()
        if not hwnd:
            return
        try:
            px, py = int(self.pet.x), int(self.pet.y)
        except Exception:
            return

        new_w = int(PET_W * scale)
        new_h = int(PET_H * scale)
        left, top, right, bottom = work_area_for_window(hwnd)
        px = min(max(px, left + 8), max(left + 8, right - new_w - 8))
        py = min(max(py, top + 8), max(top + 8, bottom - new_h - 8))
        dpi = display_scale(hwnd)

        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.SetWindowPos(
                wintypes.HWND(hwnd), None,
                int(px * dpi), int(py * dpi), int(new_w * dpi), int(new_h * dpi),
                0x0004 | 0x0010,  # NOZORDER | NOACTIVATE
            )
        except Exception:
            log.exception("调整桌宠大小失败")
            return

        self.pet_scale = scale
        self.config["pet_scale"] = scale
        save_config(self.config)
        if self.chat_visible:
            self._position_chat()
        self._refresh_tray_menu()

    def _set_autostart(self, enabled: bool) -> None:
        ok = set_autostart(enabled)
        if self.icon is not None:
            try:
                self.icon.notify(
                    "已开启开机自启" if enabled else "已关闭开机自启", APP_NAME
                )
            except Exception:
                pass
        self._refresh_tray_menu()
        if not ok:
            log.warning("开机自启设置没有成功")

    def _refresh_tray_menu(self) -> None:
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    # ---- 右键菜单 ----
    def show_pet_menu(self) -> None:
        if sys.platform != "win32":
            return
        hwnd = self._pet_hwnd()
        if not hwnd:
            return

        MF_STRING = 0x0000
        MF_POPUP = 0x0010
        MF_SEPARATOR = 0x0800
        MF_CHECKED = 0x0008
        MF_GRAYED = 0x0001
        TPM_RETURNCMD = 0x0100
        TPM_RIGHTBUTTON = 0x0002

        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.CreatePopupMenu.restype = wintypes.HANDLE
            user32.AppendMenuW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
            user32.AppendMenuW.restype = wintypes.BOOL
            user32.TrackPopupMenu.argtypes = [
                wintypes.HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
            ]
            user32.TrackPopupMenu.restype = ctypes.c_int
            user32.DestroyMenu.argtypes = [wintypes.HANDLE]
            user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]

            hmenu = user32.CreatePopupMenu()
            submenu = user32.CreatePopupMenu()
            if not hmenu:
                return
            cmd = 0
            try:
                def add(flags: int, ident: int, text: str | None) -> None:
                    user32.AppendMenuW(hmenu, flags, ident, text)

                add(MF_STRING, 1001, "收起聊天" if self.chat_visible else "打开聊天")
                if self.icon is not None:
                    add(MF_STRING, 1002, "隐藏立绘")
                add(MF_SEPARATOR, 0, None)
                add(MF_STRING | (MF_CHECKED if self.on_top else 0), 1003, "窗口置顶")

                sizes = (
                    (1011, "小（80%）", 0.8),
                    (1012, "标准（100%）", 1.0),
                    (1013, "大（125%）", 1.25),
                    (1014, "特大（150%）", 1.5),
                )
                for ident, label, value in sizes:
                    flags = MF_STRING | (MF_CHECKED if abs(self.pet_scale - value) < 0.01 else 0)
                    user32.AppendMenuW(submenu, flags, ident, label)
                user32.AppendMenuW(hmenu, MF_POPUP, int(submenu), "桌宠大小")

                add(MF_SEPARATOR, 0, None)
                add(MF_STRING | (MF_CHECKED if self.voice_on else 0), 1006, "语音朗读")
                add(MF_STRING | (MF_CHECKED if self.sound_on else 0), 1007, "互动音效")

                add(MF_STRING | (MF_CHECKED if is_autostart_enabled() else 0), 1004, "开机自启")
                add(MF_SEPARATOR, 0, None)
                add(MF_STRING, 1015, "小狗唱歌")
                add(MF_STRING, 1016, "扒歌学习")
                add(MF_STRING, 1017, "设置")
                add(MF_SEPARATOR, 0, None)
                hk = load_hotkeys(self.config)
                add(MF_STRING | MF_GRAYED, 0,
                    "%s 设置 · %s 唱歌 · %s 扒歌" % (
                        hk.get("settings") or "Ctrl+Alt+S",
                        hk.get("sing") or "Ctrl+Alt+G",
                        hk.get("sing_learn") or "Ctrl+Alt+L",
                    ))
                add(MF_STRING, 1005, "退出程序")

                point = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(point))
                user32.SetForegroundWindow(wintypes.HWND(hwnd))
                cmd = int(user32.TrackPopupMenu(
                    hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                    point.x, point.y, 0, wintypes.HWND(hwnd), None,
                ))
                user32.PostMessageW(wintypes.HWND(hwnd), 0, 0, 0)
            finally:
                user32.DestroyMenu(hmenu)
                user32.DestroyMenu(submenu)
        except Exception:
            log.exception("显示右键菜单失败")
            return

        self._dispatch_menu(cmd)

    def _dispatch_menu(self, cmd: int) -> None:
        if cmd == 1001:
            self.toggle_chat()
        elif cmd == 1002:
            try:
                self.pet.hide()
            except Exception:
                log.exception("隐藏立绘失败")
            self._notify_pet_hidden()
        elif cmd == 1003:
            self._set_topmost(not self.on_top)
        elif cmd == 1004:
            self._set_autostart(not is_autostart_enabled())
        elif cmd == 1005:
            self.quit()
        elif cmd == 1006:
            self.voice_on = not self.voice_on
            self.config["voice"] = self.voice_on
            save_config(self.config)
            if not self.voice_on:
                self.speech.stop()
            self._pet_eval(
                "window.PetBridge&&PetBridge.setFlags(%s);"
                % json.dumps({"voice": self.voice_on, "sound": self.sound_on})
            )
        elif cmd == 1007:
            self.sound_on = not self.sound_on
            self.config["sound"] = self.sound_on
            save_config(self.config)
            self._pet_eval(
                "window.PetBridge&&PetBridge.setFlags(%s);"
                % json.dumps({"voice": self.voice_on, "sound": self.sound_on})
            )
        elif cmd == 1011:
            self._set_pet_scale(0.8)
        elif cmd == 1012:
            self._set_pet_scale(1.0)
        elif cmd == 1013:
            self._set_pet_scale(1.25)
        elif cmd == 1014:
            self._set_pet_scale(1.5)
        elif cmd == 1015:
            threading.Thread(target=self.launch_singer, args=(False,),
                             name="launch-singer", daemon=True).start()
        elif cmd == 1016:
            threading.Thread(target=self.launch_singer, args=(True,),
                             name="launch-singer-learn", daemon=True).start()
        elif cmd == 1017:
            try:
                self.open_settings_window()
            except Exception:
                log.exception("菜单打开设置失败")

    # ---- 聊天窗口 ----
    def show_chat(self) -> None:
        if self.chat is None:
            return
        try:
            if not self.chat_shown_once:
                self.chat.show()
                self.chat_visible = True
                self.chat_shown_once = True
                self._position_chat()
            else:
                self._position_chat()
                self.chat.show()
                self.chat_visible = True
            # 窗口创建时已经是 on_top=True。这里不要再赋值：
            # pywebview 的 set_on_top 会在当前线程直接改 WinForms.TopMost，
            # 从 JS API / 托盘线程调用会卡死。
            t = threading.Timer(0.18, self._focus_chat_input)
            t.daemon = True
            t.start()
        except Exception:
            log.exception("显示聊天窗口失败")

    def _position_chat(self) -> None:
        if self.pet is None or self.chat is None:
            return
        try:
            px, py = int(self.pet.x), int(self.pet.y)
            pw = int(self.pet.width) or PET_W
            ph = int(self.pet.height) or PET_H
            cw = int(self.chat.width) or CHAT_W
            ch = int(self.chat.height) or CHAT_H
        except Exception:
            return

        try:
            hwnd = int(self.pet.native.Handle.ToInt64())
        except Exception:
            hwnd = 0
        left, top, right, bottom = work_area_for_window(hwnd)
        sx, sy, sw, sh = left, top, right - left, bottom - top

        gap = 10
        margin = 8
        x = px - cw - gap
        if x < sx + margin:
            x = px + pw + gap
        if x + cw > sx + sw - margin:
            x = sx + sw - cw - margin

        y = py + ph - ch
        if y < sy + margin:
            y = sy + margin
        if y + ch > sy + sh - margin:
            y = sy + sh - ch - margin

        x = max(sx + margin, min(x, sx + sw - cw - margin))
        y = max(sy + margin, min(y, sy + sh - ch - margin))
        try:
            self.chat.move(int(x), int(y))
        except Exception:
            log.debug("移动聊天窗口失败", exc_info=True)

    def hide_chat(self) -> None:
        if self.chat is None or not self.chat_visible:
            return
        try:
            self.chat.hide()
            self.chat_visible = False
        except Exception:
            log.exception("隐藏聊天窗口失败")

    def toggle_chat(self) -> None:
        if self.chat_visible:
            self.hide_chat()
        else:
            self.show_chat()

    def _focus_chat_input(self) -> None:
        try:
            self.chat.evaluate_js("var i=document.getElementById('input'); if(i){i.focus();}")
        except Exception:
            pass

    # ---- 前端 fetch 挂了的时候，走 pywebview.api 直接转发 ----
    def bridge_chat(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"ok": False, "message": "参数不对"}
        try:
            log.info("bridge chat -> %s", payload.get("baseUrl") or env_base_url())
            return chat_once(payload)
        except Exception as exc:
            log.exception("bridge chat 失败")
            return {"ok": False, "message": f"代理出错：{exc}"}

    # ---- 朗读策略 ----
    def set_read_mode(self, mode: str = "") -> dict[str, Any]:
        mode = str(mode or "").strip().lower()
        if mode not in ("read", "clone", "intent"):
            return {"ok": False, "message": "模式只能是 read / clone / intent"}
        self.speech.read_mode = mode
        self.config["read_mode"] = mode
        try:
            save_config(self.config)
        except Exception:
            log.debug("保存朗读模式失败", exc_info=True)
        return {"ok": True, "mode": mode}

    # ---- 音色（持久化克隆音源）----
    def set_voice(self, pid: str = "") -> dict[str, Any]:
        """切换当前音色：改 active.json + 重载台词库 + 清掉正在合成的旧句。"""
        pid = str(pid or "").strip()
        base = exe_dir() if getattr(sys, "frozen", False) else ROOT
        pdir = base / "voices" / "profiles" / pid
        prof_path = pdir / "profile.json"
        if not pid or not prof_path.is_file():
            return {"ok": False, "message": "没有这个音色", "voices": list_voice_profiles()}

        try:
            payload = {"id": pid, "at": int(time.time())}
            (base / "voices" / "active.json").write_text(
                json.dumps(payload, ensure_ascii=False), "utf-8")
        except OSError as exc:
            return {"ok": False, "message": f"写不进 active.json：{exc}"}

        try:
            prof = json.loads(prof_path.read_text("utf-8"))
        except (OSError, ValueError):
            prof = {}

        self.speech.clone.invalidate()          # 旧音色正在合成的句子作废
        self.speech.pack.stop()
        self.speech.pack.reload()
        self.speech.pack.auto_pick(str(prof.get("name") or pid))
        self.config["voice_pack"] = self.speech.pack.name
        try:
            save_config(self.config)
        except Exception:
            log.debug("保存音色选择失败", exc_info=True)
        log.info("已切换音色：%s（台词 %d 条）", self.speech.pack.name,
                 len(self.speech.pack.texts()))
        return {
            "ok": True, "active": pid, "name": self.speech.pack.name,
            "bank": len(self.speech.pack.texts()),
            "voices": list_voice_profiles(),
        }

    # ---- 独立窗口（PPT / 图片）----
    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    def make_deck(self, topic: str = "", bullets: Any = None) -> dict[str, Any]:
        """生成真 .pptx，返回文件名（由 open_asset_window 开独立窗口）。"""
        topic = str(topic or "小课堂").strip()[:60]
        raw = bullets if isinstance(bullets, list) else (
            re.split(r"[。！？!?；;\n]", str(bullets or "")) if bullets else [])
        clean = [str(b).strip()[:90] for b in raw if str(b).strip()][:16]
        try:
            path = build_pptx(topic, clean)
        except Exception as exc:
            log.warning("生成 pptx 失败：%s", exc)
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "file": path.name, "topic": topic, "bullets": clean,
                "slides": 1 + max(1, (len(clean) + 3) // 4)}

    def draw_image(self, prompt: str = "", width: int = 768, height: int = 768) -> dict[str, Any]:
        prompt = str(prompt or "").strip()[:200]
        if not prompt:
            return {"ok": False, "message": "没告诉我要画什么"}
        try:
            path = build_image(prompt, int(width or 768), int(height or 768))
        except Exception as exc:
            log.warning("本地出图失败：%s", exc)
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "file": path.name, "prompt": prompt,
                "url": "/img/" + urllib.parse.quote(path.name)}

    def open_asset_window(self, kind: str = "", payload: Any = None) -> dict[str, Any]:
        """开一个**原生独立窗口**看 PPT / 图片。

        以前用 window.open()，在 WebView2 里要么被拦、要么跳到系统浏览器，
        用户根本看不到 —— 所以改成后端 create_window 真开一个窗口。
        """
        payload = payload if isinstance(payload, dict) else {}
        kind = str(kind or "").strip().lower()
        base = self._base_url()

        if kind == "ppt":
            topic = str(payload.get("topic") or "小课堂")[:60]
            bullets = payload.get("bullets") or []
            if isinstance(bullets, str):
                bullets = re.split(r"[。！？!?；;\n]", bullets)
            bullets = [str(b).strip()[:90] for b in bullets if str(b).strip()][:16]
            made = self.make_deck(topic, bullets)
            if not made.get("ok"):
                # python-pptx 不在也照样能看：退回纯 HTML 幻灯片
                qs = {"topic": [topic]}
                for i in range(0, len(bullets), 1):
                    qs.setdefault("b", []).append(bullets[i])
                url = base + "/deck?" + urllib.parse.urlencode(qs, doseq=True)
                return self._spawn_window(url, f"{topic} · 幻灯片", 1100, 760,
                                          note="没装 python-pptx，先用网页版")
            qs = {"topic": [topic], "file": [made["file"]]}
            for b in made["bullets"]:
                qs.setdefault("b", []).append(b)
            url = base + "/deck?" + urllib.parse.urlencode(qs, doseq=True)
            return self._spawn_window(url, f"{topic} · PPT", 1100, 760,
                                      note=made["file"])

        if kind == "img":
            made = self.draw_image(payload.get("prompt") or "",
                                   int(payload.get("width") or 768),
                                   int(payload.get("height") or 768))
            if not made.get("ok"):
                return made
            qs = {"src": [made["url"]], "prompt": [made["prompt"]],
                  "file": [made["file"]]}
            url = base + "/shot?" + urllib.parse.urlencode(qs)
            return self._spawn_window(url, "它画的图", 900, 820, note=made["file"])

        if kind == "panel":
            # 把某个面板单独弹成一个窗口：同一个页面 + ?panel=xxx，
            # 页面自己会把聊天区藏掉，只留这一块设置。逻辑复用，不用维护第二份 UI。
            which = str(payload.get("panel") or "setting").strip()
            if which not in ("chat", "voice", "album", "setting"):
                which = "setting"
            title = {"chat": "聊天设置", "voice": "语音", "album": "照片 / 文件",
                     "setting": "设置"}[which]
            url = base + "/?panel=" + urllib.parse.quote(which)
            # 设置窗要给左导航 + 右内容两栏留位置，比其它面板宽一些
            w, h = (860, 780) if which == "setting" else (560, 760)
            return self._spawn_window(url, title, w, h,
                                      note=f"面板：{title}")

        return {"ok": False, "message": "不知道要开什么窗口（ppt / img / panel）"}

    def _spawn_window(self, url: str, title: str, w: int, h: int,
                      note: str = "") -> dict[str, Any]:
        """在 GUI 线程之外直接建窗口。pywebview 6 支持运行时创建。"""
        try:
            import webview

            win = webview.create_window(
                f"{APP_NAME} · {title}",
                url,
                width=w,
                height=h,
                resizable=True,
                min_size=(520, 420),
                frameless=False,
                on_top=False,
                background_color="#eceff3",
                text_select=True,
                js_api=_ChatApi(self),   # 试听/音色/朗读模式/麦克风都要桥；缺了就"设置窗里语音不出声"
            )
            if win is None:
                return {"ok": False, "message": "窗口没建起来"}
            log.info("已开独立窗口：%s -> %s", title, url)
            return {"ok": True, "url": url, "note": note}
        except Exception as exc:
            log.exception("开独立窗口失败")
            return {"ok": False, "message": f"窗口没开成：{exc}", "url": url}

    # ---- 桌宠页面桥接 ----
    def _pet_eval(self, js: str) -> None:
        if self.pet is None:
            return
        try:
            self.pet.evaluate_js(js)
        except Exception:
            log.debug("桌宠页面脚本执行失败", exc_info=True)

    def pet_state(self, state: str) -> None:
        self._pet_eval(
            "window.PetBridge&&PetBridge.setState(%s);" % json.dumps(str(state or "idle"))
        )

    def pet_say(self, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        # mute：铺气泡 + 口型，但不在这里出声。
        # 声音由聊天页的播报队列负责（去重、抢占、与气泡同一句），避免两条音轨叠加。
        self._pet_eval(
            "window.PetBridge&&PetBridge.say(%s,{speak:false,mute:true});"
            % json.dumps(text, ensure_ascii=False)
        )

    def chat_from_pet(self, text: str) -> None:
        """桌宠那边用麦克风说完，把文字丢进聊天窗口发出去。"""
        text = str(text or "").strip()
        if not text or self.chat is None:
            return
        self.show_chat()
        js = (
            "window.PetBridge&&PetBridge.sendFromPet(%s);"
            % json.dumps(text, ensure_ascii=False)
        )
        # 聊天页可能刚好还在加载，晚一点再补一发；前端有去重
        for delay in (0.4, 1.0):
            timer = threading.Timer(delay, self._send_to_chat, args=(js,))
            timer.daemon = True
            timer.start()

    def _send_to_chat(self, js: str) -> None:
        try:
            if self.chat is not None:
                self.chat.evaluate_js(js)
        except Exception:
            log.debug("转发桌宠消息失败", exc_info=True)

    # ---- 窗口事件 ----
    def _notify_pet_hidden(self) -> None:
        """立绘被藏起来时给个提示，别让用户以为它消失了。"""
        try:
            if self.icon is not None:
                self.icon.notify("立绘躲起来了：双击托盘图标可以唤回", APP_NAME)
        except Exception:
            pass

    def restore_pet(self) -> dict[str, Any]:
        """把立绘窗口重新亮出来（托盘 / 单实例唤回共用）。"""
        try:
            if self.pet is not None:
                self.pet.show()
        except Exception:
            log.exception("显示立绘失败")
            return {"ok": False}
        return {"ok": True}

    def _on_pet_closing(self) -> bool:
        if self.quitting or self.icon is None:
            return True
        try:
            self.pet.hide()
        except Exception:
            log.exception("隐藏立绘失败")
        self._notify_pet_hidden()
        return False

    def _on_chat_closing(self) -> bool:
        if self.quitting:
            return True
        self.chat_visible = False
        try:
            self.chat.hide()
        except Exception:
            log.exception("隐藏聊天失败")
        return False

    def _on_pet_loaded(self) -> None:
        # 把“语音朗读 / 互动音效 / 语音包”的开关同步给页面，不然每次启动都按默认开
        self._pet_eval(
            "window.PetBridge&&PetBridge.setFlags(%s);"
            % json.dumps({
                "voice": self.voice_on,
                "sound": self.sound_on,
                "pack": self.speech.pack.name,
                "strict": self.speech.pack.strict,
                "voiceUrl": (
                    self.speech.file.url()
                    if self.speech.file.ready() and self.speech.file.fallback
                    else ""
                ),
            })
        )

    def _on_chat_loaded(self) -> None:
        try:
            self.chat.evaluate_js(CHAT_INJECT_JS)
        except Exception:
            log.exception("注入聊天窗口 JS 失败")

    # ---- 聊天窗口尺寸 ----
    def resize_chat_window(self, w: int, h: int) -> dict[str, Any]:
        """按预设调整聊天窗口大小，并记住（下次启动还是这个尺寸）。"""
        try:
            if self.chat is None:
                return {"ok": False, "message": "聊天窗口还没开"}
            w = min(max(int(w), CHAT_MIN_W), 1600)
            h = min(max(int(h), CHAT_MIN_H), 1600)
            self.chat.resize(w, h)
            self.chat_w, self.chat_h = w, h
            self.config["chat_w"], self.config["chat_h"] = w, h
            save_config(self.config)
            return {"ok": True, "w": w, "h": h}
        except Exception:
            log.exception("调整聊天窗口大小失败")
            return {"ok": False, "message": "调整失败"}

    def _on_chat_resized(self, w: int, h: int) -> None:
        """用户拖拽边缘调整大小：节流 0.6s 落盘一次。"""
        try:
            self.chat_w, self.chat_h = int(w), int(h)
        except (TypeError, ValueError):
            return
        t = getattr(self, "_chat_resize_timer", None)
        if t is not None:
            t.cancel()
        t = threading.Timer(0.6, self._save_chat_size)
        t.daemon = True
        self._chat_resize_timer = t
        t.start()

    def _save_chat_size(self) -> None:
        try:
            self.config["chat_w"] = int(self.chat_w)
            self.config["chat_h"] = int(self.chat_h)
            save_config(self.config)
        except Exception:
            log.debug("保存聊天窗口尺寸失败", exc_info=True)

    # ---- 托盘 ----
    def _make_tray_image(self) -> Any:
        """托盘图标：和程序封面（dog_girl.ico）用同一套构图逻辑。

        托盘实际显示只有 16-24px，所以这里也取"脸部特写"而不是全身立绘，
        保证小尺寸下还能认出是那个戴皇冠的狗耳女孩。
        """
        from PIL import Image

        with Image.open(PET_IMAGE) as src:
            im = src.convert("RGBA")
            w, h = im.size
            # 脸 + 皇冠：横向 20%-80%，纵向 16%-66%
            head = im.crop((int(w * 0.20), int(h * 0.16), int(w * 0.80), int(h * 0.66)))
            bbox = head.getchannel("A").getbbox()
            if bbox:
                head = head.crop(bbox)
            resample = getattr(Image, "Resampling", Image).LANCZOS
            # 托盘会缩到 16/24/32，用 128 作源，放大时更稳
            head.thumbnail((128, 128), resample)
            canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
            canvas.alpha_composite(head, ((128 - head.width) // 2, (128 - head.height) // 2))
            return canvas

    def _build_tray_menu(self, pystray: Any) -> Any:
        Menu = pystray.Menu
        Item = pystray.MenuItem

        def size_item(label: str, value: float) -> Any:
            return Item(
                label,
                lambda icon, item: self._set_pet_scale(value),
                checked=lambda item: abs(self.pet_scale - value) < 0.01,
                radio=True,
            )

        return Menu(
            Item("显示立绘", self._tray_show_pet, default=True),
            Item(
                lambda item: "收起聊天" if self.chat_visible else "打开聊天",
                self._tray_toggle_chat,
            ),
            Menu.SEPARATOR,
            Item("窗口置顶", self._tray_toggle_topmost, checked=lambda item: self.on_top),
            Item(
                "桌宠大小",
                Menu(
                    size_item("小（80%）", 0.8),
                    size_item("标准（100%）", 1.0),
                    size_item("大（125%）", 1.25),
                    size_item("特大（150%）", 1.5),
                ),
            ),
            Item(
                "开机自启",
                self._tray_toggle_autostart,
                checked=lambda item: is_autostart_enabled(),
            ),
            Menu.SEPARATOR,
            Item("小狗唱歌", self._tray_open_singer),
            Item("扒歌学习", self._tray_open_singer_learn),
            Item("设置", self._tray_open_settings),
            Menu.SEPARATOR,
            Item("Ctrl+Alt+P 打开 / 收起聊天", None, enabled=False),
            Item("退出程序", self._tray_quit),
        )

    def _start_tray(self) -> None:
        if self.no_tray:
            return
        try:
            import pystray
        except Exception:
            log.exception("pystray 不可用，跳过托盘")
            return

        # pystray 在部分 Windows 环境里会因 ChangeWindowMessageFilterEx
        # 返回 Access Denied 而起不来。这个调用只是为了让“资源管理器重启”
        # 的消息能进到托盘窗口，非管理员进程本来就不需要，失败可以安全忽略。
        try:
            from pystray._util import win32 as _pystray_win32

            _orig_filter = _pystray_win32.ChangeWindowMessageFilterEx

            def _safe_filter(*args: Any, **kwargs: Any) -> Any:
                try:
                    return _orig_filter(*args, **kwargs)
                except OSError:
                    return True

            _pystray_win32.ChangeWindowMessageFilterEx = _safe_filter
        except Exception:
            log.debug("pystray 消息过滤器打补丁失败", exc_info=True)

        try:
            image = self._make_tray_image()
        except Exception:
            log.exception("读取托盘图标失败，跳过托盘")
            return

        try:
            menu = self._build_tray_menu(pystray)
            self.icon = pystray.Icon("polite-puppy", image, APP_NAME, menu)
            threading.Thread(target=self._tray_loop, name="tray", daemon=True).start()
            log.info("托盘已启动")
        except Exception:
            log.exception("启动托盘失败")
            self.icon = None

    def _tray_loop(self) -> None:
        try:
            self.icon.run()
        except Exception:
            log.exception("托盘循环异常")
            self.icon = None

    def _tray_toggle_chat(self, icon: Any = None, item: Any = None) -> None:
        self.toggle_chat()

    def _tray_toggle_topmost(self, icon: Any = None, item: Any = None) -> None:
        self._set_topmost(not self.on_top)

    def _tray_toggle_autostart(self, icon: Any = None, item: Any = None) -> None:
        self._set_autostart(not is_autostart_enabled())

    def _tray_open_singer(self, icon: Any = None, item: Any = None) -> None:
        threading.Thread(target=self.launch_singer, args=(False,),
                         name="launch-singer", daemon=True).start()

    def _tray_open_singer_learn(self, icon: Any = None, item: Any = None) -> None:
        threading.Thread(target=self.launch_singer, args=(True,),
                         name="launch-singer-learn", daemon=True).start()

    def _tray_open_settings(self, icon: Any = None, item: Any = None) -> None:
        try:
            self.open_settings_window()
        except Exception:
            log.exception("托盘打开设置失败")

    def _start_extras(self) -> None:
        self._start_hotkey()
        if not self.no_tray:
            self._start_tray()
        self._maybe_start_clone_tts()
        t = threading.Timer(1.5, self._maybe_first_run_hint)
        t.daemon = True
        t.start()

    def _maybe_start_clone_tts(self) -> None:
        """若配置了 VOICE_TTS_URL 指向本地 F5 服务，就自动把它拉起来（后台常驻）。

        克隆音色 TTS 跑在独立的 voice-clone-env 环境里（torch/F5-TTS 太重，
        不打进桌宠 exe），桌宠只负责在启动时用 subprocess 拉起它、退出时带它一起走。
        服务已经在跑（端口占着）就跳过，不重复启动。
        """
        url = (os.environ.get("VOICE_TTS_URL") or "").strip()
        if not url:
            return
        try:
            host, port = _split_tts_url(url)
        except ValueError:
            return
        if host not in ("127.0.0.1", "localhost"):
            return
        if _port_listening(host, port):
            log.info("克隆 TTS 服务已在运行：%s", url)
            return
        launch_clone_service()

    # ---- 唱歌台（puppy-sing）----
    def _singer_command(self) -> tuple[list[str], Path] | None:
        """拼出启动 puppy-sing 的命令行。找不到组件返回 None。

        源码运行：直接用当前解释器跑 puppy-sing/main.py；
        打包 exe：exe 本身不是 python，找系统 python，实在没有就退回 run.bat。
        """
        sing_dir = ROOT / "puppy-sing"
        entry = sing_dir / "main.py"
        if not entry.is_file():
            return None
        if not getattr(sys, "frozen", False):
            return [sys.executable, str(entry)], sing_dir
        py = shutil.which("python")
        if py:
            return [py, str(entry)], sing_dir
        py = shutil.which("py")
        if py:
            return [py, "-3", str(entry)], sing_dir
        bat = sing_dir / "run.bat"
        if bat.is_file():
            return ["cmd", "/c", str(bat)], sing_dir
        return None

    def launch_singer(self, open_learn: bool = False) -> dict[str, Any]:
        """拉起小狗唱歌台（puppy-sing 窗口）。已经在跑就不重复开。

        open_learn=True 时给唱歌台传 --learn，窗口起来后自动弹「扒歌学习」。
        """
        proc = getattr(self, "_singer_proc", None)
        if proc is not None and proc.poll() is None:
            self._hint("唱歌台已经开着啦，去任务栏找找它～")
            return {"ok": True, "message": "already-running"}
        cmd = self._singer_command()
        if cmd is None:
            self._hint("没找到唱歌组件（puppy-sing），换最新的安装包试试")
            return {"ok": False, "message": "puppy-sing not found"}
        args, cwd = cmd
        if open_learn:
            args = list(args) + ["--learn"]
        try:
            flags = 0
            if sys.platform == "win32":
                # run.bat 兜底时会带控制台，正常 python 路径不弹黑窗
                flags = subprocess.CREATE_NO_WINDOW if not args[0].lower().endswith("cmd.exe") else 0
            self._singer_proc = subprocess.Popen(
                args, cwd=str(cwd),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            log.info("唱歌台已启动：%s", args)
            self._hint("小狗唱歌台开好了，汪！" if not open_learn else "耳朵竖起来了，去学首歌吧～")
            return {"ok": True, "pid": self._singer_proc.pid}
        except Exception:
            log.exception("启动唱歌台失败：%s", args)
            self._hint("唱歌台启动失败，看看日志")
            return {"ok": False, "message": "launch failed"}

    def open_settings_window(self) -> dict[str, Any]:
        """打开设置窗口（独立面板窗，菜单 / 快捷键共用）。"""
        return self.open_asset_window("panel", {"panel": "setting"})

    def _start_hotkey(self) -> None:
        if sys.platform != "win32":
            return
        try:
            self.hotkey = GlobalHotkey(self)
            self.hotkey._bindings = load_hotkeys(self.config)
            self.hotkey.start()
        except Exception:
            log.exception("启动全局快捷键失败")

    # ---- 快捷键动作分发 ----
    def hotkey_action(self, action: str) -> None:
        """全局快捷键统一入口（跑在热键线程里，只做分发，别干重活）。"""
        log.info("快捷键触发：%s", action)
        if action == "chat":
            self.toggle_chat()
            return

        if action == "voice_out":
            self.voice_on = not self.voice_on
            self.config["voice"] = self.voice_on
            save_config(self.config)
            self._push_flags()
            self._chat_eval(
                "window.HotkeyBridge&&HotkeyBridge.voiceSet(%s);"
                % ("true" if self.voice_on else "false")
            )
            self._hint("语音输出：%s" % ("开" if self.voice_on else "关"))
            return

        if action in ("voice_pause", "voice_stop"):
            if not self.chat_visible:
                # 播报可能还在响，先停掉再开窗，别让用户以为按键没反应
                self.speech.stop()
            self._chat_eval(
                "window.HotkeyBridge&&HotkeyBridge.%s();"
                % ("voicePause" if action == "voice_pause" else "voiceStop")
            )
            return

        if action == "mic":
            if not self.chat_visible:
                self.show_chat()
                threading.Timer(0.6, lambda: self._chat_eval(
                    "window.HotkeyBridge&&HotkeyBridge.micToggle();")).start()
            else:
                self._chat_eval("window.HotkeyBridge&&HotkeyBridge.micToggle();")
            return

        if action == "settings":
            # 设置独立成窗：复用面板弹窗逻辑（/?panel=setting）
            try:
                made = self.open_asset_window("panel", {"panel": "setting"})
                if not made.get("ok"):
                    self._hint(str(made.get("message") or "设置窗口没开出来"))
            except Exception:
                log.exception("快捷键打开设置失败")
                self._hint("设置窗口没开出来，看看日志")
            return

        if action in ("sing", "sing_learn"):
            threading.Thread(
                target=self.launch_singer,
                args=(action == "sing_learn",),
                name="launch-singer",
                daemon=True,
            ).start()
            return

    def _chat_eval(self, js: str) -> None:
        if self.chat is None:
            return
        try:
            self.chat.evaluate_js(js)
        except Exception:
            log.debug("聊天页脚本执行失败", exc_info=True)

    def _hint(self, text: str) -> None:
        """快捷键的即时反馈：托盘气泡 + 桌宠气泡（不出声）。"""
        try:
            if self.icon is not None:
                self.icon.notify(text, APP_NAME)
                return
        except Exception:
            pass
        self.pet_say(text)

    def _push_flags(self) -> None:
        flags = {
            "voice": self.voice_on,
            "sound": self.sound_on,
            "pack": self.speech.pack.name,
            "strict": self.speech.pack.strict,
            "voiceUrl": (
                self.speech.file.url()
                if self.speech.file.ready() and self.speech.file.fallback
                else ""
            ),
        }
        self._pet_eval(
            "window.PetBridge&&PetBridge.setFlags(%s);" % json.dumps(flags, ensure_ascii=False)
        )

    def get_hotkeys(self) -> dict[str, Any]:
        bindings = load_hotkeys(self.config)
        errors = dict(self.hotkey.last_errors) if self.hotkey is not None else {}
        return {"ok": True, "bindings": bindings, "actions": HOTKEY_ACTIONS,
                "defaults": DEFAULT_HOTKEYS, "errors": errors}

    def set_hotkeys(self, bindings: Any) -> dict[str, Any]:
        if not isinstance(bindings, dict):
            return {"ok": False, "message": "参数不对"}
        clean: dict[str, str] = {}
        for action in DEFAULT_HOTKEYS:
            spec = bindings.get(action, "")
            spec = str(spec or "").strip()
            if spec and parse_hotkey(spec) is None:
                return {"ok": False, "message": "「%s」这个键位认不出来：%s" % (action, spec)}
            clean[action] = spec
        self.config["hotkeys"] = clean
        save_config(self.config)
        if self.hotkey is not None:
            self.hotkey.apply(clean)
        return {"ok": True, "bindings": clean,
                "errors": dict(self.hotkey.last_errors) if self.hotkey is not None else {}}

    def _maybe_first_run_hint(self) -> None:
        if self.icon is None or self.config.get("hint_shown"):
            return
        try:
            self.icon.notify("点一下立绘聊天；右键立绘或托盘里有更多功能", APP_NAME)
        except Exception:
            pass
        self.config["hint_shown"] = True
        save_config(self.config)

    def _tray_show_pet(self, icon: Any = None, item: Any = None) -> None:
        try:
            self.pet.show()
        except Exception:
            log.exception("显示立绘失败")

    def _tray_quit(self, icon: Any = None, item: Any = None) -> None:
        self.quit()

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        log.info("退出程序")
        try:
            self.speech.shutdown()
        except Exception:
            pass
        try:
            if self.hotkey is not None:
                self.hotkey.stop()
        except Exception:
            pass
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        for win in (self.chat, self.pet):
            if win is None:
                continue
            try:
                win.destroy()
            except Exception:
                log.exception("销毁窗口失败")
        # 兜底：万一 pywebview 的事件循环没退出
        t = threading.Timer(3.0, lambda: os._exit(0))
        t.daemon = True
        t.start()

    # ---- 启动 ----
    def run(self) -> None:
        try:
            import webview
        except Exception as exc:  # pragma: no cover - 依赖 / WebView2 环境问题
            raise RuntimeError(
                "pywebview 加载失败。请先执行：\n"
                "  pip install -r requirements.txt\n\n"
                f"原始错误：{exc}"
            ) from exc

        self.server.start()
        base_url = f"http://127.0.0.1:{self.server.port}"
        x, y = self._initial_position()

        self.pet = webview.create_window(
            APP_NAME,
            f"{base_url}/pet",
            width=int(PET_W * self.pet_scale),
            height=int(PET_H * self.pet_scale),
            x=x,
            y=y,
            frameless=True,
            transparent=True,
            on_top=self.on_top,
            resizable=False,
            easy_drag=False,
            shadow=False,
            background_color="#000000",
            text_select=False,
            zoomable=False,
            js_api=_PetApi(self),
        )
        self.chat = webview.create_window(
            f"{APP_NAME} · 聊天",
            f"{base_url}/index.html",
            width=self.chat_w,
            height=self.chat_h,
            frameless=True,
            on_top=self.on_top,
            hidden=True,
            resizable=True,
            easy_drag=False,
            shadow=True,
            background_color="#ededed",
            text_select=True,
            zoomable=False,
            min_size=(CHAT_MIN_W, CHAT_MIN_H),
            js_api=_ChatApi(self),
        )

        self.pet.events.closing += self._on_pet_closing
        self.pet.events.moved += self._on_pet_moved
        self.pet.events.loaded += self._on_pet_loaded
        self.chat.events.closing += self._on_chat_closing
        self.chat.events.loaded += self._on_chat_loaded
        self.chat.events.resized += self._on_chat_resized

        # 托盘 / 热键 / 首次提示放到 GUI 线程起来之后再启动
        t = threading.Timer(0.5, self._start_extras)
        t.daemon = True
        t.start()

        try:
            webview.start(
                debug=self.debug,
                gui="edgechromium",
                private_mode=False,
                storage_path=str(DATA_DIR / "webview2"),
            )
        finally:
            self.server.stop()
            if self.icon is not None:
                try:
                    self.icon.stop()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="礼貌小狗 Windows 桌面宠物")
    parser.add_argument("--debug", action="store_true", help="打开 devtools 并输出调试日志")
    parser.add_argument("--no-tray", action="store_true", help="不创建系统托盘（调试用）")
    parser.add_argument("--allow-multi", action="store_true", help="允许重复启动（调试用）")
    parser.add_argument("--server", action="store_true",
                        help="服务器模式：只跑本地 HTTP 服务，不开窗口/托盘（供反向代理）")
    return parser.parse_args(argv)


def run_server(args: argparse.Namespace) -> int:
    """服务器模式：只起本地 HTTP 服务，交给 IIS / Nginx / 宝塔反代。

    没有 GUI 依赖：不开窗口、不建托盘、不注册全局热键，崩溃后由守护进程拉起即可。
    """
    app = PetApp(no_tray=True, debug=args.debug)
    port = app.server.start()
    log.info("服务器模式已启动：http://%s:%d  允许的 Host: %s",
             bind_host(), port, ", ".join(sorted(allowed_hostnames())))
    # 给进程守护（宝塔/PM2/NSSM）一个明确的就绪信号
    try:
        print("SERVER_READY http://%s:%d" % (bind_host(), port), flush=True)
    except Exception:
        pass

    stop = threading.Event()

    def _on_signal(*_args: Any) -> None:
        stop.set()

    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        try:
            signal.signal(getattr(signal, sig), _on_signal)
        except Exception:
            pass

    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        app.server.stop()
        log.info("服务器模式已停止")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.debug)
    load_dotenv(exe_dir() / ".env")
    # 本机服务（Ollama / 克隆 TTS / 自己的 HTTP 端口）一律不走系统代理
    bypass_proxy_for_loopback()

    if sys.platform != "win32":
        message_box("这个程序是为 Windows 写的。", APP_NAME, True)
        return 1
    if not INDEX_FILE.exists():
        message_box(f"缺少 index.html：\n{INDEX_FILE}", APP_NAME, True)
        return 1
    if not PET_IMAGE.exists():
        message_box(f"缺少 dog_girl.png：\n{PET_IMAGE}\n\n请把透明背景的立绘放在同一目录。", APP_NAME, True)
        return 1
    if not DIARY_FILE.exists():
        try:
            DIARY_FILE.write_text(DIARY_TEMPLATE, "utf-8")
            log.info("已生成记忆日记模板：%s", DIARY_FILE)
        except OSError:
            log.warning("记忆日记创建失败，RAG 会暂时为空")

    # ---- 服务器模式：走这里就不进 GUI 分支 ----
    if args.server or server_mode_enabled():
        try:
            return run_server(args)
        except Exception as exc:
            log.exception("服务器模式启动失败")
            message_box(f"服务器模式启动失败：\n{exc}\n\n日志：{LOG_FILE}", APP_NAME, True)
            return 1

    if not args.allow_multi and another_instance_running():
        info = probe_running_pet()
        if info:
            run_ver = info.get("version") or "（旧版，未上报版本号）"
            run_path = info.get("exePath") or ""
            lines = [
                "礼貌小狗已经在运行啦，桌面上把立绘给你叫回来了。",
                "",
                f"正在运行：{run_ver}",
            ]
            if run_path:
                lines.append(f"它的位置：{run_path}")
            lines.append(f"你刚双击：{APP_VERSION}")
            if info.get("version") and info["version"] != APP_VERSION:
                lines += [
                    "",
                    "版本不一样。要换成刚双击的这个版本，",
                    "请先到托盘右键 → 退出，然后再启动一次。",
                ]
            else:
                lines += ["", "以后找不到它，双击托盘图标就能唤回。"]
            message_box("\n".join(lines), APP_NAME)
        else:
            message_box("礼貌小狗已经在运行了。\n请到系统托盘里找它。", APP_NAME)
        return 0

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PolitePuppy.Pet")
    except Exception:
        pass

    app = PetApp(no_tray=args.no_tray, debug=args.debug)
    global _PET_APP
    _PET_APP = app
    try:
        app.run()
    except Exception as exc:
        log.exception("启动失败")
        message_box(
            "启动失败：\n"
            f"{exc}\n\n"
            "常见原因：\n"
            "1. 没有安装 WebView2 Runtime（Windows 10/11 一般自带）\n"
            "2. 没有执行 pip install -r requirements.txt\n"
            "3. 项目放在 OneDrive / 网络盘，.NET 加载被拦（本程序已自动兼容，仍有问题请换到 C:\\）\n\n"
            f"日志：{LOG_FILE}",
            APP_NAME,
            True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
