# -*- coding: utf-8 -*-
"""
礼貌小狗·唱歌 — 图形界面
================================
tkinter 单文件界面：小狗头像 + 曲库列表 + 🎤唱 + 热歌刷新 + 引擎切换 +
自动更新 + 延迟显示。低延迟来自「本地实时合成 + 缓存预渲染」。
"""
import os
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import core

HERE = os.path.dirname(os.path.abspath(__file__))


class PuppySingApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("🐶 礼貌小狗·唱歌")
        self.root.geometry("720x520")
        self.root.resizable(True, True)

        self.lib = core.load_library()
        self.engine = tk.StringVar(value="local")  # local | edge
        self.auto_var = tk.BooleanVar(value=True)
        self.playing = False
        self.updater = None

        self._build_ui()
        self._refresh_list()
        self._start_updater()

    # -------------------------------------------------------------- UI
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="🐶 礼貌小狗·唱歌", font=("Microsoft YaHei", 16, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(top, textvariable=self.status_var, foreground="#666").pack(side="right")

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        # 左：曲库
        left = ttk.LabelFrame(body, text="曲库 / 想唱的热歌", padding=6)
        left.pack(side="left", fill="both", expand=True)
        self.listbox = tk.Listbox(left, font=("Microsoft YaHei", 11), activestyle="none")
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<Double-1>", lambda e: self.sing())
        self._add_scroll(left, self.listbox)

        # 右：小狗 + 控制
        right = ttk.Frame(body)
        right.pack(side="right", fill="y", padx=(8, 0))

        self.canvas = tk.Canvas(right, width=180, height=160, bg="#fff7f0",
                                highlightthickness=0)
        self.canvas.pack()
        self._draw_puppy()

        ctl = ttk.LabelFrame(right, text="控制", padding=6)
        ctl.pack(fill="x", pady=6)

        ttk.Button(ctl, text="🔄 刷新热歌榜", command=self.refresh_now).pack(fill="x", pady=2)
        ttk.Button(ctl, text="🎤 唱！", command=self.sing).pack(fill="x", pady=2)
        ttk.Button(ctl, text="📝 学习这首歌(填授权歌词)", command=self.learn).pack(fill="x", pady=2)
        ttk.Button(ctl, text="👂 扒歌学习(听原唱自学)", command=self.ear_learn).pack(fill="x", pady=2)

        ttk.Label(ctl, text="合成引擎:").pack(anchor="w", pady=(4, 0))
        for val, txt in (
            ("local", "本地旋律(零依赖/低延迟)"),
            ("edge", "edge-tts 真实语音"),
            ("f5", "🐶 小狗原声(F5克隆)"),
        ):
            ttk.Radiobutton(ctl, text=txt, value=val, variable=self.engine).pack(anchor="w")

        ttk.Checkbutton(ctl, text="自动更新曲库(10分钟)", variable=self.auto_var,
                        command=self._toggle_auto).pack(anchor="w", pady=(4, 0))

        self.lat_var = tk.StringVar(value="上次延迟: —")
        ttk.Label(ctl, textvariable=self.lat_var, foreground="#2a7").pack(anchor="w", pady=(4, 0))

        self.trend_var = tk.StringVar(value="热歌来源: —")
        ttk.Label(ctl, textvariable=self.trend_var, foreground="#888").pack(anchor="w")

        ttk.Label(right, text="双击歌曲或点「唱！」即可点唱", foreground="#999",
                  font=("Microsoft YaHei", 9)).pack(pady=(6, 0))

    def _add_scroll(self, parent, widget):
        sb = ttk.Scrollbar(parent, orient="vertical", command=widget.yview)
        widget.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

    def _draw_puppy(self, sing=False):
        c = self.canvas
        c.delete("all")
        oy = 8 if sing else 0  # 唱歌时轻微上跳
        c.create_oval(40, 30 + oy, 140, 130 + oy, fill="#f6c177", outline="#e0a85a", width=2)
        # 耳朵
        c.create_oval(35, 20 + oy, 65, 55 + oy, fill="#e0a85a")
        c.create_oval(115, 20 + oy, 145, 55 + oy, fill="#e0a85a")
        # 眼睛
        c.create_oval(68, 60 + oy, 80, 72 + oy, fill="#3a2a1a")
        c.create_oval(100, 60 + oy, 112, 72 + oy, fill="#3a2a1a")
        # 鼻
        c.create_oval(86, 80 + oy, 94, 90 + oy, fill="#3a2a1a")
        # 嘴
        if sing:
            c.create_oval(80, 98 + oy, 100, 112 + oy, fill="#b5524e", outline="#7a2e2b")
        else:
            c.create_line(90, 90 + oy, 90, 98 + oy, fill="#7a2e2b", width=2)
            c.create_line(90, 98 + oy, 80, 104 + oy, fill="#7a2e2b", width=2)
            c.create_line(90, 98 + oy, 100, 104 + oy, fill="#7a2e2b", width=2)

    # -------------------------------------------------------------- 列表
    def _refresh_list(self):
        self.listbox.delete(0, tk.END)
        for s in self.lib.values():
            tag = {"ready": "✅", "learned": "🎵", "待学习": "🔧", "pending": "⏳"}.get(s.status, "•")
            lic = "★" if s.licensed else " "
            label = f"{tag}{lic} {s.title} — {s.artist}" if s.artist else f"{tag}{lic} {s.title}"
            self.listbox.insert(tk.END, label)

    def _selected_song(self):
        sel = self.listbox.curselection()
        if not sel:
            return None
        idx = sel[0]
        return list(self.lib.values())[idx]

    # -------------------------------------------------------------- 热歌
    def _start_updater(self):
        self.updater = core.TrendingUpdater(self.lib, interval_sec=600,
                                            on_update=self._on_trending)
        self.updater.start()
        # 首拉（异步，不阻塞界面）
        threading.Thread(target=self._first_pull, daemon=True).start()

    def _first_pull(self):
        self.root.after(0, lambda: self.status_var.set("🔄 正在拉取热歌榜…"))
        songs, meta = core.fetch_trending()
        added = core.merge_trending(self.lib, songs)
        core.save_library(self.lib)
        self.root.after(0, self._apply_trending, meta, added)

    def _toggle_auto(self):
        if self.auto_var.get():
            if self.updater is None or not self.updater.running:
                self._start_updater()
        else:
            if self.updater:
                self.updater.stop()

    def refresh_now(self):
        self.status_var.set("🔄 刷新中…")
        threading.Thread(target=self._first_pull, daemon=True).start()

    def _on_trending(self, meta, added):
        self.root.after(0, self._apply_trending, meta, added)

    def _apply_trending(self, meta, added):
        self._refresh_list()
        self.trend_var.set(f"热歌来源: {meta.get('used','—')} ({meta.get('count',0)}首)")
        if meta.get("ok"):
            self.status_var.set(f"已更新热歌榜，新增 {len(added)} 首")
        else:
            self.status_var.set(f"热歌榜离线回退：{meta.get('used','—')}")

    # -------------------------------------------------------------- 学习
    def learn(self):
        song = self._selected_song()
        if not song:
            messagebox.showinfo("提示", "先在左侧选一首歌～")
            return
        win = tk.Toplevel(self.root)
        win.title(f"📝 学习《{song.title}》— 填入授权歌词")
        win.geometry("420x320")
        win.transient(self.root)

        ttk.Label(win, text="粘贴该歌曲的【授权歌词】（每句自动成旋律；填了才能唱词）",
                  wraplength=380, foreground="#666").pack(padx=10, pady=(10, 2))
        txt = tk.Text(win, height=10, font=("Microsoft YaHei", 11), wrap="word")
        txt.pack(fill="both", expand=True, padx=10, pady=4)
        txt.insert("1.0", song.lyric or "")

        frm = ttk.Frame(win)
        frm.pack(fill="x", padx=10, pady=4)
        ttk.Label(frm, text="旋律风格:").pack(side="left")
        style_var = tk.StringVar(value="smooth")
        ttk.Combobox(frm, textvariable=style_var, width=12, state="readonly",
                     values=["smooth", "happy", "sad", "random"]).pack(side="left", padx=4)
        ttk.Label(frm, text="歌词来源须为已授权内容").pack(side="right")

        def _save():
            lyric = txt.get("1.0", "end").strip()
            if not lyric:
                messagebox.showinfo("提示", "先粘贴歌词～")
                return
            core.learn_song(song, lyric, style_var.get())
            core.save_library(self.lib)
            self._refresh_list()
            self.status_var.set(f"已学习《{song.title}》，可点「唱！」了")
            win.destroy()

        ttk.Button(win, text="保存并学习", command=_save).pack(fill="x", padx=10, pady=(2, 10))

    def ear_learn(self):
        """扒歌学习：从音频里分离人声、提取旋律，小狗自己学会唱。"""
        song = self._selected_song()
        if not song and self.lib:
            # 没选中就默认选第一首（桌宠 --learn 直达时体验更顺）
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(0)
            song = self._selected_song()
        if not song:
            messagebox.showinfo("提示", "曲库还是空的，先学一首歌吧～")
            return
        win = tk.Toplevel(self.root)
        win.title(f"👂 扒歌学习《{song.title}》")
        win.geometry("460x250")
        win.transient(self.root)

        ttk.Label(win, text="音频来源（热歌榜链接 / 本地文件；本地文件最稳）",
                  wraplength=420, foreground="#666").pack(padx=10, pady=(10, 2))
        src_var = tk.StringVar(value=getattr(song, "url", "") or "")
        row = ttk.Frame(win)
        row.pack(fill="x", padx=10, pady=2)
        ttk.Entry(row, textvariable=src_var).pack(side="left", fill="x", expand=True)

        def pick():
            from tkinter import filedialog
            p = filedialog.askopenfilename(
                title="选择音频文件", parent=win,
                filetypes=[("音频", "*.mp3 *.wav *.flac *.m4a *.ogg"), ("所有文件", "*.*")])
            if p:
                src_var.set(p)
        ttk.Button(row, text="…", width=3, command=pick).pack(side="left", padx=4)

        opt = ttk.Frame(win)
        opt.pack(fill="x", padx=10, pady=2)
        asr_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="本地 ASR 认歌词(首次自动下载小模型)",
                        variable=asr_var).pack(side="left")
        ttk.Label(opt, text="默认用「啦」哼唱旋律", foreground="#999").pack(side="right")
        ttk.Label(win, text="可选：附加授权歌词，会逐字对齐到旋律上唱出字来",
                  wraplength=420, foreground="#666").pack(padx=10, pady=(4, 0))
        lyr_var = tk.StringVar()
        ttk.Entry(win, textvariable=lyr_var).pack(fill="x", padx=10, pady=2)

        def _go():
            src = src_var.get().strip()
            if not src:
                messagebox.showinfo("提示", "先填音频来源或点「…」选本地文件～", parent=win)
                return
            win.destroy()
            self.status_var.set(f"👂 正在扒《{song.title}》：分离→提旋律→学习…")
            t = threading.Thread(
                target=self._do_ear_learn,
                args=(song, src, lyr_var.get().strip(), asr_var.get()), daemon=True)
            t.start()

        ttk.Button(win, text="开扒，小狗自己学！", command=_go).pack(fill="x", padx=10, pady=(6, 10))

    def _do_ear_learn(self, song, src, lyric, transcribe):
        def progress(step, msg):
            self.root.after(0, self.status_var.set, f"👂 [{step}] {msg}")
        try:
            import ear_train
            res = ear_train.learn_by_ear(song.title, song.artist, src, lyric,
                                         transcribe, progress=progress)
        except Exception as exc:
            res = {"ok": False, "message": str(exc)}
        def done():
            if res.get("ok"):
                self.status_var.set(
                    f"✅ 学会《{song.title}》！{res['notes_count']} 个音符"
                    f"({'ASR认得%d字' % res['lyric_chars'] if res['lyric_chars'] else '啦哼唱'})，点「唱！」试试")
                self._refresh_list()
            else:
                self.status_var.set(f"❌ 扒歌失败：{res.get('message','未知原因')}")
        self.root.after(0, done)

    # -------------------------------------------------------------- 演唱
    def sing(self):
        if self.playing:
            return
        song = self._selected_song()
        if not song:
            messagebox.showinfo("提示", "先在左侧选一首歌～")
            return
        self.playing = True
        self.status_var.set(f"🎤 演唱中：{song.title}")
        self._draw_puppy(sing=True)
        t = threading.Thread(target=self._do_sing, args=(song,), daemon=True)
        t.start()

    def _do_sing(self, song):
        try:
            t0 = time.perf_counter()
            kind, payload = core.ensure_cache(song, self.engine.get())
            dt = time.perf_counter() - t0
            if kind == "wav":
                core.play_wav(payload)
            else:
                core.play_sequence(payload)
            self.root.after(0, lambda: self.lat_var.set(f"上次延迟: {dt*1000:.0f}ms (缓存命中)" if dt < 0.2
                                                         else f"上次合成+播放: {dt:.1f}s"))
            # 标记已学习
            if not song.licensed:
                song.status = "待学习"
            else:
                song.status = "learned"
                song.learned_at = time.strftime("%Y-%m-%d %H:%M")
            core.save_library(self.lib)
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("演唱失败", str(e)))
        finally:
            self.playing = False
            self.root.after(0, lambda: self.status_var.set("就绪"))
            self.root.after(0, lambda: self._draw_puppy(sing=False))
            self.root.after(0, self._refresh_list)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="礼貌小狗·唱歌台")
    ap.add_argument("--learn", action="store_true",
                    help="窗口起来后自动打开「扒歌学习」弹窗（给桌宠快捷键用）")
    args = ap.parse_args()

    root = tk.Tk()
    try:
        from ttkthemes import ThemedStyle  # 可选美化
        style = ThemedStyle(root)
        style.set_theme("arc")
    except Exception:
        pass
    app = PuppySingApp(root)
    if args.learn:
        # 等界面画完再弹窗，不然 tk 变量还没就绪
        root.after(600, app.ear_learn)
    root.mainloop()


if __name__ == "__main__":
    main()
