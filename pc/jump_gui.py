# -*- coding: utf-8 -*-
"""
手机画面 + 手动画线（电脑版）

电脑用 adb 抓手机截图显示在视窗里，用滑鼠从小人拉到目标，放开就用蓝牙叫 ESP32 按。
不用相机、不用画边框，任何「跳一跳」类的游戏都能用（不管方块长什么样）。

快捷键：
  空白键 = 按       R = 重新截图       右键 = 清除线
  1 = 太久          2 = 刚好           3 = 太短         Esc = 停止 AI 自动玩

学习：你每画一条线按一次，都会记下来（pc/data/）。AI 会学小人的样子、跳跃角度和 k/b，
     并在你玩的时候偷偷预测（紫色虚线），跟你画的比较；够准了再打开「AI 自动玩」。
"""
import asyncio
import math
import os
import subprocess
import sys
import queue
import random
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

import jump_bot as jb
import learn

FONT = ('Microsoft YaHei UI', 11)
FONT_B = ('Microsoft YaHei UI', 11, 'bold')
BG, PANEL, TEXT, SUB = '#0e0f12', '#1a1c21', '#f3f4f6', '#c0c5cd'
CARD = '#1d2026'


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = jb.load_config()
        for k, v in {'gui_auto': False, 'gui_proj': True, 'gui_live': False}.items():
            self.cfg.setdefault(k, v)
        self.q = queue.Queue()                      # 背景执行绪 → 介面
        self.loop = asyncio.new_event_loop()        # 蓝牙用的 asyncio 迴圈（在背景执行绪跑）
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.esp = None
        self.adb = None
        self.img = None            # 原始截图 (PIL)
        self.photo = None
        self.scale = 1.0           # 画面像素 / 截图像素
        self.line = None           # {'a':(x,y), 'b':(x,y)} 截图像素座标
        self.drag = False
        self.units = 0.0
        self.raw = 0.0
        self.ms = 0
        self.proj_end = None
        self.last = None           # 上一次画线按的 {'ms', 'units'}
        self.last_dir = 0
        self.busy = False          # 正在按 / 等动画
        self.grabbing = False
        self.store = learn.Store()
        self.tpls = learn.build_templates(self.store)
        self.pred = None           # AI 对目前画面的预测
        self.cur_rec = None        # 上一次按压的学习纪录（等你按 1/2/3）
        self.auto_fail = 0
        self.pending = None        # 刚按完、等着量落点的那一跳 (jump, rec)
        self.cfg.setdefault('auto_calib', True)
        self.cfg.setdefault('miss_pct', 10)        # AI 自动玩时，故意不跳正中的机率（%）
        self.cfg.setdefault('miss_amount', 0.6)    # 故意偏离最多到「中心→边缘」距离的几成
        self.build()
        self.root.after(50, self.poll)
        self.root.after(100, self.start)

    # ------------------------------------------------------------ 介面
    def build(self):
        r = self.root
        r.title('Servo Press · 手机画面画线')
        r.configure(bg=BG)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        self.wide = sw >= 1150                     # 够宽就三栏；太窄就两栏（右边捲动）
        self.max_h = max(400, sh - 140)
        try:
            r.state('zoomed')                      # Windows：一打开就最大化
        except tk.TclError:
            r.geometry(f'{sw}x{max(500, sh - 60)}+0+0')
        r.minsize(900, 560)
        r.grid_rowconfigure(0, weight=1)

        style = ttk.Style(r)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('Treeview', background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=24,
                        font=('Microsoft YaHei UI', 10), borderwidth=0)
        style.configure('Treeview.Heading', background='#30343c', foreground=TEXT, font=('Microsoft YaHei UI', 10, 'bold'),
                        relief='flat')
        style.map('Treeview', background=[('selected', '#3b4250')])

        # ---- 第 1 栏：手机画面（跟着视窗高度放大缩小）
        left = tk.Frame(r, bg=BG)
        left.grid(row=0, column=0, sticky='ns', padx=(8, 4), pady=8)
        self.cv = tk.Canvas(left, bg='#000', highlightthickness=0, width=int(self.max_h / 2.2), height=self.max_h,
                            cursor='crosshair')
        self.cv.pack()
        self.cv.bind('<ButtonPress-1>', self.on_down)
        self.cv.bind('<B1-Motion>', self.on_move)
        self.cv.bind('<ButtonRelease-1>', self.on_up)
        self.cv.bind('<Button-3>', lambda e: self.clear_line())
        self._resize_job = None
        r.bind('<Configure>', self.on_resize)

        # ---- 第 2 栏：操作；第 3 栏：学习 + 设定
        colA = self.scroll_col(r, 1, 380)
        colB = self.scroll_col(r, 2, 440, expand=True) if self.wide else colA
        if not self.wide:
            r.grid_columnconfigure(1, weight=1)

        def btn(parent, text, cmd, bg='#262a31', fg=TEXT, **kw):
            return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                             relief='flat', font=kw.pop('font', FONT_B), padx=10, pady=6, cursor='hand2', **kw)

        def card(parent, title=None):
            f = tk.Frame(parent, bg=CARD, padx=12, pady=10)
            f.pack(fill='x', pady=(0, 10))
            if title:
                tk.Label(f, text=title, font=('Microsoft YaHei UI', 12, 'bold'), bg=CARD, fg=TEXT).pack(anchor='w', pady=(0, 6))
            return f

        # 操作卡
        c = card(colA)
        row = tk.Frame(c, bg=CARD); row.pack(fill='x')
        self.bt = btn(row, '连接蓝牙', self.connect, bg='#3b7cff', fg='#fff')
        self.bt.pack(side='left', fill='x', expand=True)
        btn(row, '⟳ 截图 (R)', self.refresh).pack(side='left', fill='x', expand=True, padx=(6, 0))
        self.ms_lbl = tk.Label(c, text='--', font=('Microsoft YaHei UI', 44, 'bold'), bg=CARD, fg=TEXT)
        self.ms_lbl.pack(anchor='w', pady=(10, 0))
        self.dist_lbl = tk.Label(c, text='用滑鼠从小人拉到目标', font=FONT, bg=CARD, fg=SUB, justify='left')
        self.dist_lbl.pack(anchor='w')
        self.press_btn = btn(c, '按！(空白键)', self.press_line, bg='#3b7cff', fg='#fff',
                             font=('Microsoft YaHei UI', 18, 'bold'))
        self.press_btn.pack(fill='x', pady=(12, 8))
        fb = tk.Frame(c, bg=CARD); fb.pack(fill='x')
        btn(fb, '− 太久 (1)', lambda: self.feedback(-1), bg='#d9472f', fg='#fff').pack(side='left', fill='x', expand=True)
        btn(fb, '✓ 刚好 (2)', self.ok, bg='#1fa955', fg='#fff').pack(side='left', fill='x', expand=True, padx=6)
        btn(fb, '＋ 太短 (3)', lambda: self.feedback(1), bg='#d58f12', fg='#fff').pack(side='left', fill='x', expand=True)
        self.v_play = tk.BooleanVar(value=False)
        self.play_btn = btn(c, '🤖 AI 自动玩 (A)', self.toggle_play_btn, bg='#7b3fe4', fg='#fff',
                            font=('Microsoft YaHei UI', 15, 'bold'))
        self.play_btn.pack(fill='x', pady=(10, 0))
        self.v_play.trace_add('write', lambda *a: self.sync_play_btn())

        # 状态卡
        c = card(colA, '状态')
        self.st = tk.Label(c, text='', font=FONT, bg=CARD, fg=SUB, wraplength=340, justify='left', height=4,
                           anchor='nw')
        self.st.pack(fill='x')

        # 选项卡
        c = card(colA, '选项')

        def check(parent, text, key, cmd=None):
            var = tk.BooleanVar(value=bool(self.cfg.get(key)))

            def on():
                self.cfg[key] = var.get(); jb.save_config(self.cfg)
                if cmd: cmd()
            tk.Checkbutton(parent, text=text, variable=var, command=on, font=FONT, bg=CARD, fg=TEXT,
                           selectcolor='#262a31', activebackground=CARD, activeforeground=TEXT,
                           anchor='w').pack(fill='x')
            return var
        self.v_auto = check(c, '画完自动按', 'gui_auto')
        self.v_proj = check(c, '只算沿跳跃方向的距离', 'gui_proj', self.recalc)
        self.v_live = check(c, '自动刷新画面（每 1.5 秒）', 'gui_live')
        self.v_calib = check(c, '跳完自动量落点、校准 k/b', 'auto_calib')

        # 学习卡
        c = card(colB, '🧠 学习')
        self.learn_lbl = tk.Label(c, text='', font=('Microsoft YaHei UI', 10), bg=CARD, fg=SUB, justify='left',
                                  wraplength=400)
        self.learn_lbl.pack(anchor='w')
        lr = tk.Frame(c, bg=CARD); lr.pack(fill='x', pady=(8, 0))
        self.fit_btn = btn(lr, '套用拟合 k/b', self.apply_fit, font=FONT)
        self.fit_btn.pack(side='left')
        btn(lr, '清除学习', self.clear_learn, font=FONT).pack(side='left', padx=6)
        self.export_btn = btn(lr, '📦 导出数据', self.export_data, bg='#2f6f9f', fg='#fff', font=FONT)
        self.export_btn.pack(side='left')

        # 落点误差图
        c = card(colB, '🎯 落点误差（最近 30 跳，绿 = ±3% 内，黄框 = 故意偏离）')
        self.chart = tk.Canvas(c, bg='#15171b', highlightthickness=0, height=130, width=400)
        self.chart.pack(fill='x')
        self.chart.bind('<Configure>', lambda e: self.draw_chart())

        # 最近跳跃纪录
        c = card(colB, '📋 最近跳跃')
        cols = ('n', 'who', 'dist', 'ms', 'land')
        self.hist = ttk.Treeview(c, columns=cols, show='headings', height=8)
        for col, title, w in zip(cols, ('#', '谁', '距离', 'ms', '落点'), (44, 50, 80, 70, 110)):
            self.hist.heading(col, text=title)
            self.hist.column(col, width=w, anchor='center', stretch=True)
        self.hist.tag_configure('good', foreground='#5fe08f')
        self.hist.tag_configure('bad', foreground='#ff7b66')
        self.hist.pack(fill='x')

        # 设定卡
        c = card(colB, '⚙ 设定')

        def entry(label, key, width=8):
            f = tk.Frame(c, bg=CARD); f.pack(fill='x', pady=2)
            tk.Label(f, text=label, font=FONT, bg=CARD, fg=TEXT, width=12, anchor='w').pack(side='left')
            e = tk.Entry(f, width=width, font=FONT, bg='#0b0c0f', fg=TEXT, insertbackground=TEXT, relief='flat')
            e.insert(0, str(self.cfg[key])); e.pack(side='left')

            def save(_=None):
                try:
                    self.cfg[key] = float(e.get()); jb.save_config(self.cfg); self.recalc()
                    self.status(f'{label} = {self.cfg[key]}')
                except ValueError:
                    e.delete(0, 'end'); e.insert(0, str(self.cfg[key]))
                self.cv.focus_set()
            e.bind('<Return>', save); e.bind('<FocusOut>', save)
            return e
        self.e_k = entry('k', 'k')
        self.e_b = entry('b (ms)', 'b')
        self.e_ang = entry('跳跃角度 (°)', 'jump_angle_deg')
        self.e_wait = entry('按完等几秒', 'wait_after')
        self.e_miss = entry('故意偏离机率 %', 'miss_pct')
        self.e_miss_amt = entry('偏离幅度 0~0.7', 'miss_amount')
        tk.Label(c, text='AI 自动玩时，大约这个比例的跳会故意不跳正中（只往跳跃方向偏远或偏近，\n'
                         '最多偏到中心→边缘距离的这几成，一定还在平台上）。小平台（宽 < 屏宽 25%）一律瞄正中。',
                 font=('Microsoft YaHei UI', 9), bg=CARD, fg=SUB, justify='left').pack(anchor='w')
        self.info_lbl = tk.Label(c, text='', font=('Microsoft YaHei UI', 10), bg=CARD, fg=SUB, justify='left')
        self.info_lbl.pack(anchor='w', pady=(6, 0))
        tk.Label(c, text='按压时间 = 距离 × k + b（距离：手机屏幕宽 = 1000）\n\n'
                         '快捷键：空白键 按 · R 截图 · 右键 清除线\n'
                         '1 太久 · 2 刚好 · 3 太短 · A AI 自动玩 · Esc 停止',
                 font=('Microsoft YaHei UI', 10), bg=CARD, fg=SUB, justify='left').pack(anchor='w', pady=(8, 0))

        self.show_info()
        self.update_learn()
        r.bind('<KeyPress>', self.on_key)

    def scroll_col(self, r, column, width, expand=False):
        """一栏可以捲动的区域（滑鼠移到上面时，滚轮捲这一栏）。回传里面放东西的 Frame。"""
        outer = tk.Frame(r, bg=BG)
        outer.grid(row=0, column=column, sticky='nsew', padx=4, pady=8)
        if expand:
            r.grid_columnconfigure(column, weight=1)
        cv = tk.Canvas(outer, bg=BG, highlightthickness=0, width=width)
        sb = tk.Scrollbar(outer, orient='vertical', command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        cv.pack(side='left', fill='both', expand=True)
        inner = tk.Frame(cv, bg=BG)
        win = cv.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>', lambda e: cv.configure(scrollregion=cv.bbox('all')))
        cv.bind('<Configure>', lambda e: cv.itemconfigure(win, width=e.width))

        def wheel(e):
            d = -1 if (getattr(e, 'num', 0) == 4 or getattr(e, 'delta', 0) > 0) else 1
            if cv.bbox('all') and cv.bbox('all')[3] > cv.winfo_height():
                cv.yview_scroll(d * 2, 'units')
        outer.bind('<Enter>', lambda e: (cv.bind_all('<MouseWheel>', wheel), cv.bind_all('<Button-4>', wheel),
                                         cv.bind_all('<Button-5>', wheel)))
        outer.bind('<Leave>', lambda e: (cv.unbind_all('<MouseWheel>'), cv.unbind_all('<Button-4>'),
                                         cv.unbind_all('<Button-5>')))
        return inner

    def on_resize(self, e):
        if e.widget is not self.root:
            return
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(150, self.after_resize)

    def after_resize(self):
        self._resize_job = None
        h = max(300, self.root.winfo_height() - 16)
        if abs(h - self.max_h) > 4:
            self.max_h = h
            if self.img:
                self.render(); self.draw()
            else:
                self.cv.config(height=h, width=int(h / 2.2))

    def draw_chart(self):
        c = self.chart
        c.delete('all')
        W, H = max(50, c.winfo_width()), int(c.cget('height'))
        mid = H / 2
        lim = 12.0                                              # ±12% 画满
        band = 3 / lim * (H / 2 - 8)
        c.create_rectangle(0, mid - band, W, mid + band, fill='#1d3326', outline='')
        c.create_line(0, mid, W, mid, fill='#4a4f58')
        c.create_text(4, 8, text='跳过头 +', anchor='w', fill=SUB, font=('Microsoft YaHei UI', 8))
        c.create_text(4, H - 8, text='不够远 −', anchor='w', fill=SUB, font=('Microsoft YaHei UI', 8))
        rs = [r for r in self.store.recs if r.get('land')][-30:]
        if not rs:
            c.create_text(W / 2, mid, text='还没有落点资料（跳完会自动量）', fill=SUB, font=FONT)
            return
        bw = (W - 70) / 30
        for i, r in enumerate(rs):
            units = max(1.0, r['land']['traveled'] - r['land']['err'])
            pct = (r['land']['err'] - (r.get('aim') or 0)) / units * 100   # 跟「想跳的地方」比
            h = max(-1.0, min(1.0, pct / lim)) * (H / 2 - 8)
            col = '#2fd07a' if abs(pct) < 3 else '#e5a12a' if abs(pct) < 6 else '#ff6b55'
            x = 64 + i * bw
            c.create_rectangle(x, mid, x + bw * 0.7, mid - h, fill=col,
                               outline='#ffd83a' if r.get('aim') else ('#e9a0ff' if r.get('auto') else ''))

    def update_history(self):
        t = self.hist
        t.delete(*t.get_children())
        n = len(self.store.recs)
        for i, r in enumerate(reversed(self.store.recs[-40:])):
            land, tag = '—', ''
            if r.get('land'):
                units = max(1.0, r['land']['traveled'] - r['land']['err'])
                pct = (r['land']['err'] - (r.get('aim') or 0)) / units * 100
                land = f'{pct:+.1f}%' + (' 🎯' if abs(pct) < 2 else '')
                if r.get('aim'):
                    land = f'故意{r["aim"]:+.0f} · ' + land
                tag = 'good' if abs(pct) < 3 else 'bad'
            t.insert('', 'end', values=(n - i, 'AI' if r.get('auto') else '你', f'{r["units"]:.0f}', r['ms'], land),
                     tags=(tag,))

    def status(self, t, kind=''):
        self.st.config(text=t, fg={'ok': '#5fe08f', 'warn': '#ffc14d', 'err': '#ff7b66'}.get(kind, SUB))

    def show_info(self):
        self.info_lbl.config(text=f'调整幅度 ±{self.cfg["step"] * 100:.1f}%')

    def on_key(self, e):
        if isinstance(self.root.focus_get(), tk.Entry):
            return
        k = e.keysym.lower()
        if k == 'space': self.press_line()
        elif k == 'r': self.refresh()
        elif k in ('1', 'kp_1'): self.feedback(-1)
        elif k in ('2', 'kp_2'): self.ok()
        elif k in ('3', 'kp_3'): self.feedback(1)
        elif k == 'a':
            self.toggle_play_btn()
        elif k == 'escape' and self.v_play.get():
            self.v_play.set(False); self.status('⏸ AI 自动玩已停止', 'warn')

    # ------------------------------------------------------------ 背景工作
    def bg(self, fn, done):
        def run():
            try:
                res, err = fn(), None
            except Exception as ex:  # noqa: BLE001
                res, err = None, ex
            self.q.put(lambda: done(res, err))
        threading.Thread(target=run, daemon=True).start()

    def bg_async(self, coro, done):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def cb(f):
            err = f.exception()
            self.q.put(lambda: done(None if err else f.result(), err))
        fut.add_done_callback(cb)

    def poll(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()()
            except Exception as ex:  # noqa: BLE001
                self.status(f'错误：{ex}', 'err')
        self.root.after(40, self.poll)

    # ------------------------------------------------------------ adb / 截图
    def start(self):
        self.adb = jb.find_adb(self.cfg)
        if not self.adb:
            messagebox.showerror('找不到 adb', '找不到 adb.exe。\n请下载 SDK Platform-Tools 解压到 C:\\platform-tools，'
                                 '或在 jump_config.json 的 "adb" 写完整路径。')
            return
        def check():
            err = jb.check_device(self.adb)
            if not err:
                jb.pick_screencap_mode(self.adb)       # 测一下哪种截图方式比较快
            return err
        self.bg(check, self.after_check)
        self.live_tick()

    def after_check(self, err_msg, err):
        if err or err_msg:
            self.status(str(err or err_msg), 'err'); return
        self.refresh()
        self.status(f'截图方式：{jb.SCREENCAP_INFO}', 'ok')

    def grab_stable(self, tries=8):
        """等画面停下来（镜头还在移动时，跳跃方向会是歪的）：连拍两张差不多才算。"""
        def small(im):
            return np.asarray(im.convert('L').resize((60, 132)), dtype=np.float32)
        a = jb.screenshot(self.adb)
        for _ in range(tries):
            time.sleep(0.25)
            b = jb.screenshot(self.adb)
            if float(np.abs(small(a) - small(b)).mean()) < 3.0:   # 雪花在飘只会差一点点
                return b
            a = b
        return a

    def refresh(self, stable=False):
        if stable and self.adb and not self.grabbing:
            self.grabbing = True
            self.bg(self.grab_stable, self.after_shot)
            return
        if not self.adb or self.grabbing:
            return
        self.grabbing = True
        self.bg(lambda: jb.screenshot(self.adb), self.after_shot)

    def after_shot(self, im, err):
        self.grabbing = False
        if err:
            self.status(f'截图失败：{err}', 'err'); return
        self.img = im
        self.render()
        self.pred = None
        self.draw()
        self.predict_async()

    def render(self):
        im = self.img
        w, h = im.size
        self.scale = min(self.max_h / h, 1.0)
        disp = im.resize((max(1, int(w * self.scale)), max(1, int(h * self.scale))), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(disp)
        self.cv.config(width=disp.width, height=disp.height)
        self.cv.delete('img')
        self.cv.create_image(0, 0, anchor='nw', image=self.photo, tags='img')
        self.cv.tag_lower('img')

    def live_tick(self):
        if self.v_live.get() and not self.drag and not self.busy:
            self.refresh()
        self.root.after(1500, self.live_tick)

    # ------------------------------------------------------------ 画线
    def to_img(self, e):
        return (e.x / self.scale, e.y / self.scale)

    def on_down(self, e):
        if not self.img:
            return
        self.drag = True
        self.prev = self.line
        p = self.to_img(e)
        self.line = {'a': p, 'b': p}
        self.recalc()

    def on_move(self, e):
        if self.drag:
            self.line['b'] = self.to_img(e)
            self.recalc()

    def on_up(self, e):
        if not self.drag:
            return
        self.drag = False
        self.line['b'] = self.to_img(e)
        self.recalc()
        if self.units < 5:                     # 只是点一下：保留原来的线
            self.line = self.prev
            self.recalc()
            return
        if self.v_auto.get():
            self.press_line()

    def clear_line(self):
        self.line = None
        self.recalc()

    def recalc(self):
        self.proj_end = None
        if not self.line or not self.img:
            self.units = self.raw = 0; self.ms = 0
        else:
            W = self.img.size[0]
            (ax, ay), (bx, by) = self.line['a'], self.line['b']
            dx, dy = bx - ax, by - ay
            self.raw = math.hypot(dx, dy) * 1000 / W
            self.units = self.raw
            if self.v_proj.get() and self.raw > 0:
                t = math.radians(self.cfg['jump_angle_deg'])
                best = None
                for ux, uy, name in ((math.cos(t), -math.sin(t), '右上'), (-math.cos(t), -math.sin(t), '左上')):
                    dot = dx * ux + dy * uy
                    if best is None or abs(dot) > abs(best[0]):
                        best = (dot, ux, uy, name)
                dot, ux, uy, name = best
                self.units = abs(dot) * 1000 / W
                self.proj_end = (ax + ux * dot, ay + uy * dot, name)
            ms = self.units * self.cfg['k'] + self.cfg['b']
            self.ms = int(round(max(self.cfg['min_ms'], min(self.cfg['max_ms'], ms))))
        if self.units:
            self.ms_lbl.config(text=f'{self.ms} ms')
            if self.proj_end:
                self.dist_lbl.config(text=f'沿{self.proj_end[2]} {self.units:.0f}（原 {self.raw:.0f}，屏宽 = 1000）')
            else:
                self.dist_lbl.config(text=f'距离 {self.units:.0f}（屏宽 = 1000）')
        else:
            self.ms_lbl.config(text='--')
            self.dist_lbl.config(text='用滑鼠从小人拉到目标')
        self.draw()

    def draw(self):
        c, s = self.cv, self.scale
        c.delete('ov')
        p = self.pred
        if p and 'center' in p and self.img:
            W = self.img.size[0]
            F = (p['foot'][0] * W * s, p['foot'][1] * W * s)
            C = (p['center'][0] * W * s, p['center'][1] * W * s)
            c.create_line(*F, *C, fill='#e9a0ff', width=3, dash=(6, 4), tags='ov')
            for (x, y) in (F, C):
                c.create_oval(x - 6, y - 6, x + 6, y + 6, outline='#e9a0ff', width=3, tags='ov')
            c.create_text(C[0], C[1] - 18, text=f'AI {p["ms"]} ms', fill='#e9a0ff',
                          font=('Microsoft YaHei UI', 11, 'bold'), tags='ov')
        if not self.line:
            return
        (ax, ay), (bx, by) = self.line['a'], self.line['b']
        A, B = (ax * s, ay * s), (bx * s, by * s)
        c.create_line(*A, *B, fill='#000', width=7, capstyle='round', tags='ov')
        c.create_line(*A, *B, fill='#ffd83a', width=4, capstyle='round', tags='ov')
        if self.proj_end:
            P = (self.proj_end[0] * s, self.proj_end[1] * s)
            c.create_line(*A, *P, fill='#33d6ff', width=3, dash=(8, 6), tags='ov')
            c.create_oval(P[0] - 5, P[1] - 5, P[0] + 5, P[1] + 5, fill='#33d6ff', outline='#000', tags='ov')
        for (x, y), col in ((A, '#2fd07a'), (B, '#ff5a4a')):
            c.create_oval(x - 7, y - 7, x + 7, y + 7, fill=col, outline='#fff', width=2, tags='ov')
        if self.units:
            mx, my = (A[0] + B[0]) / 2, (A[1] + B[1]) / 2 - 18
            t = c.create_text(mx, my, text=f'{self.ms} ms', fill='#ffd83a', font=('Microsoft YaHei UI', 13, 'bold'),
                              tags='ov')
            x0, y0, x1, y1 = c.bbox(t)
            r = c.create_rectangle(x0 - 6, y0 - 2, x1 + 6, y1 + 2, fill='#000', outline='', tags='ov')
            c.tag_lower(r, t)

    # ------------------------------------------------------------ 蓝牙 / 按
    def connect(self):
        if self.esp:
            return
        self.bt.config(text='搜索中…', state='disabled')
        self.status('搜索 ESP32 中…（请先关掉手机网页上的蓝牙连接）')
        esp = jb.Esp(self.cfg)
        self.bg_async(esp.connect(), lambda _, err: self.after_connect(esp, err))

    def after_connect(self, esp, err):
        if err:
            self.bt.config(text='连接蓝牙', state='normal')
            self.status(f'连接失败：{err}', 'err'); return
        self.esp = esp
        self.bt.config(text='🔗 已连接', bg='#1fa955', state='normal')
        self.status('已连接 ESP32 ✓', 'ok')

    def press_line(self):
        if not self.units:
            self.status('先用滑鼠从小人拉到目标', 'warn'); return
        if self.press(self.ms):
            self.last = {'ms': self.ms, 'units': self.units}
            p = self.pred if self.pred and 'units' in self.pred else None
            self.cur_rec = self.store.add(self.img, self.line['a'], self.line['b'], self.units, self.ms, pred=p,
                                          extra=self.press_extra())
            self.pending = (self.make_jump(self.line['a'], self.line['b'], self.units), self.cur_rec)
            self.tpls = learn.build_templates(self.store)     # 学新的小人样子
            self.update_learn()

    def press_extra(self):
        """每一跳额外记下来的东西（导出后用来重新校准）。"""
        return {'k_used': self.cfg['k'], 'b_used': self.cfg['b'], 't_press': round(time.time(), 3),
                'proj': bool(self.cfg.get('gui_proj')), 'angle': self.cfg.get('jump_angle_deg')}

    def make_jump(self, a, b, units):
        """这一跳原本要跳的方向和距离。AI 有量到目标、而且跟你画的终点差不多时，用 AI 量的（比较精确）。"""
        W = self.img.size[0]
        p = self.pred
        if p and 'center' in p:
            pc = (p['center'][0] * W, p['center'][1] * W)
            if math.hypot(pc[0] - b[0], pc[1] - b[1]) < 0.06 * W:
                a, b, units = (p['foot'][0] * W, p['foot'][1] * W), pc, p['raw_units']
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy) or 1.0
        return {'dir': (dx / n, dy / n), 'units': units}

    def press(self, ms):
        if self.busy:
            self.status('上一次还在按 / 等动画，稍等', 'warn'); return False
        if not self.esp:
            self.status('还没连接蓝牙（按「连接蓝牙」）', 'warn'); return False
        self.busy = True
        self.press_btn.config(state='disabled')
        self.status(f'按 {ms} ms…')
        self.bg_async(self.esp.press(ms), lambda _, err: self.after_press(ms, err))
        return True

    def after_press(self, ms, err):
        if err:
            self.busy = False; self.press_btn.config(state='normal')
            self.status(f'按压失败：{err}（蓝牙可能断了，重新连接）', 'err')
            self.esp = None; self.bt.config(text='连接蓝牙', bg='#3b7cff')
            return
        if self.cur_rec is not None and self.cur_rec.get('ms') == ms:
            self.cur_rec['t_done'] = round(time.time(), 3)      # ESP 回报按完的时间
        self.status(f'已按 {ms} ms ✓ 等画面停下来量落点…（也可以按 1 太久 / 2 刚好 / 3 太短）', 'ok')

        def done():
            self.busy = False; self.press_btn.config(state='normal')
            self.line = None; self.recalc(); self.refresh(stable=True)   # 等镜头停下来再截图（才能量落点）
        self.root.after(int(self.cfg['wait_after'] * 1000), done)

    # ------------------------------------------------------------ 校准
    def feedback(self, d):
        if not self.last:
            self.status('先画线按一次，再告诉我太久还是太短', 'warn'); return
        if self.last_dir and d != self.last_dir:
            self.cfg['step'] = max(0.005, self.cfg['step'] / 2)
        self.last_dir = d
        new_ms = self.last['ms'] * (1 + d * self.cfg['step'])
        self.cfg['k'] = round(max(0.01, (new_ms - self.cfg['b']) / self.last['units']), 4)
        jb.save_config(self.cfg)
        self.e_k.delete(0, 'end'); self.e_k.insert(0, str(self.cfg['k']))
        self.store.set_result(self.cur_rec, d, new_ms); self.update_learn()
        self.store.event('feedback', id=self.cur_rec and self.cur_rec['id'], result=d, k=self.cfg['k'])
        self.status(f'{"加长" if d > 0 else "缩短"} {self.cfg["step"] * 100:.1f}%：刚才那条线下次按 {round(new_ms)} ms'
                    f'（k = {self.cfg["k"]}）', 'ok')
        self.show_info(); self.recalc()

    def ok(self):
        if self.last:
            self.store.set_result(self.cur_rec, 0, self.last['ms']); self.update_learn()
            self.store.event('feedback', id=self.cur_rec and self.cur_rec['id'], result=0)
        self.cfg['step'] = max(0.005, self.cfg['step'] / 2); self.last_dir = 0
        jb.save_config(self.cfg); self.show_info()
        self.status(f'👍 调整幅度收窄为 ±{self.cfg["step"] * 100:.1f}%', 'ok')


    # ------------------------------------------------------------ 学习 / AI
    def predict_async(self):
        if not self.img or not self.tpls:
            self.update_learn(); return
        img, tpls = self.img, self.tpls
        pend, self.pending = self.pending, None

        def work():
            ch = learn.find_char(img, tpls)            # 小人只找一次，量落点和预测共用
            land = learn.landing(img, tpls, pend[0], ch=ch) if pend else None
            return learn.predict(img, tpls, self.cfg, self.store, ch=ch), land
        self.bg(work, lambda res, err: self.after_pred(img, res, err, pend))

    def after_pred(self, img, res, err, pend=None):
        p, land = res if res else ({'err': str(err)}, None)
        if pend:
            self.handle_landing(land, pend, img)
        if img is not self.img:
            return                                   # 画面已经换了
        if 'units' in p:                             # k/b 可能刚被校准过：用最新的重算 ms
            ms = p['units'] * self.cfg['k'] + self.cfg['b']
            p['ms'] = int(round(max(self.cfg['min_ms'], min(self.cfg['max_ms'], ms))))
        self.pred = p
        self.draw(); self.update_learn()
        if self.v_play.get():
            self.root.after(150, self.auto_step)

    def handle_landing(self, land, pend, img=None):
        jump, rec = pend
        self.store.save_after(rec, img)                 # 跳完的画面也存起来（导出用）
        self.store.event('landing', id=rec['id'], result=land)
        if land is None:
            self.status('量不到这一跳的落点（这块没有奶油色顶面），可以按 1/2/3 告诉我结果', 'warn'); return
        if land.get('lost'):
            self.status('⚠ 跳完找不到小人：可能掉下去了（游戏结束？）', 'err')
            if self.v_play.get():
                self.v_play.set(False)
            return
        e, units = land['err'], jump['units']
        aim = rec.get('aim', 0) or 0
        e_rel = e - aim                                # 扣掉故意偏离的量
        rec['land'] = {'err': round(e, 1), 'traveled': round(land['traveled'], 1),
                       'foot': land.get('foot'), 'face': land.get('face'), 'score': land.get('score')}
        if rec.get('r') is None:                     # 自动填结果（你按 1/2/3 会覆盖）
            pct = e_rel / max(1.0, units)
            rec['r'] = 0 if abs(pct) < 0.03 else (-1 if pct > 0 else 1)
            rec['est'] = int(round(rec['ms'] * units / max(1.0, land['traveled'])))
        self.store.save()
        pct = e_rel / max(1.0, units) * 100
        if aim:
            msg = (f'故意{"偏远" if aim > 0 else "偏近"} {abs(aim):.0f}，实际 {e:+.0f}（跟瞄的点差 {pct:+.1f}%）')
        else:
            msg = ('🎯 几乎正中' if abs(pct) < 2 else ('跳过头' if e > 0 else '不够远')) + f' {abs(e):.0f}（{pct:+.1f}%）'
        if self.v_calib.get():
            msg += self.calibrate(rec, jump)
        self.status('落点：' + msg, 'ok' if abs(pct) < 3 else 'warn')

    def calibrate(self, rec, jump):
        f = learn.fit_land(self.store)
        if f and f['rms'] < 30:
            k, b = round(f['k'], 4), round(f['b'])
            how = f'（{f["n"]} 跳拟合，误差 ±{f["rms"]:.0f} ms）'
        else:
            # 资料还不够：用这一跳单独修 k（只修一部分，避免一次量错就乱掉）
            traveled = rec['land']['traveled']
            if traveled < 50:
                return ''
            k_true = (rec['ms'] - self.cfg['b']) / traveled
            k = round(max(0.01, self.cfg['k'] + 0.6 * (k_true - self.cfg['k'])), 4)
            b = self.cfg['b']
            how = '（单跳修正）'
        if k == self.cfg['k'] and b == self.cfg['b']:
            return ''
        self.store.event('calibrate', k_old=self.cfg['k'], b_old=self.cfg['b'], k=k, b=b, how=how)
        self.cfg['k'], self.cfg['b'] = k, b
        jb.save_config(self.cfg)
        for e, key in ((self.e_k, 'k'), (self.e_b, 'b')):
            e.delete(0, 'end'); e.insert(0, str(self.cfg[key]))
        self.recalc()
        return f' → k = {k}, b = {b}{how}'

    def update_learn(self):
        recs = self.store.recs
        mine = sum(1 for r in recs if not r.get('auto'))
        lines = [f'已记录 {len(recs)} 跳（你画的 {mine}）· 小人模板 {len(self.tpls) // 2} 个']
        ang = learn.learned_angle(self.store)
        if ang:
            lines.append(f'学到的跳跃角度：{ang:.1f}°')
        sh = learn.shadow_stats(self.store)
        if sh['n']:
            lines.append(f'AI vs 你（最近 {sh["n"]} 跳）：中位误差 {sh["med"] * 100:.1f}%'
                         + ('  ✓ 够准了，可以试 AI 自动玩' if sh['ok'] else
                            f'（要 {learn.MIN_SHADOW} 跳、≤{learn.GOOD_ERR * 100:.0f}% 才算够准）'))
        p = self.pred
        if p:
            lines.append(f'这张：AI 预测 {p["ms"]} ms（小人信心 {p["score"]:.2f}）' if 'ms' in p else f'这张：{p["err"]}')
        elif not self.tpls:
            lines.append('先自己画线玩几跳，AI 就会开始学小人的样子')
        ls = learn.land_stats(self.store)
        if ls:
            lines.append(f'落点（最近 {ls["n"]} 跳）：平均偏 {ls["mean"] * 100:.1f}%，上一跳 {ls["last"]:+.0f}'
                         f'（共量到 {learn.land_count(self.store)} 跳）')
        f = learn.fit_kb(self.store)
        self.fit = f if 'k' in f else None
        if self.fit:
            lines.append(f'拟合 k = {f["k"]:.4f}，b = {f["b"]:.0f} ms（{f["n"]} 笔，误差 ±{f["rms"]:.0f} ms）')
        elif f.get('bad'):
            lines.append(f'有结果的 {f["n"]} 笔，但远近太接近，多跳几种距离才能拟合 k/b')
        else:
            lines.append(f'有结果的 {f["n"]} 笔（按 1/2/3），满 8 笔就能拟合 k/b')
        self.learn_lbl.config(text='\n'.join(lines))
        self.fit_btn.config(state='normal' if self.fit else 'disabled')
        self.draw_chart()
        self.update_history()

    def apply_fit(self):
        if not self.fit:
            return
        self.cfg['k'] = round(self.fit['k'], 4); self.cfg['b'] = round(self.fit['b'])
        jb.save_config(self.cfg)
        for e, key in ((self.e_k, 'k'), (self.e_b, 'b')):
            e.delete(0, 'end'); e.insert(0, str(self.cfg[key]))
        self.recalc(); self.predict_async()
        self.status(f'已套用拟合：k = {self.cfg["k"]}，b = {self.cfg["b"]} ms', 'ok')

    def export_data(self):
        if not self.store.recs:
            self.status('还没有资料可以导出（先玩几跳）', 'warn'); return
        self.export_btn.config(state='disabled', text='导出中…')
        info = {'截图方式': jb.SCREENCAP_INFO}
        if self.img:
            info['手机截图大小'] = f'{self.img.size[0]}x{self.img.size[1]}'
        self.bg(lambda: learn.export(self.store, dict(self.cfg), info), self.after_export)

    def after_export(self, path, err):
        self.export_btn.config(state='normal', text='📦 导出数据')
        if err:
            self.status(f'导出失败：{err}', 'err'); return
        size = os.path.getsize(path) / 1e6
        self.status(f'已导出（{size:.1f} MB）：{path}\n把这个 zip 和录屏一起传给我', 'ok')
        try:                                           # 打开资料夹、选中这个档案
            if sys.platform.startswith('win'):
                subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', '-R', path])
        except Exception:  # noqa: BLE001
            pass

    def clear_learn(self):
        if not messagebox.askyesno('清除学习', '清除所有学习纪录和截图？（k、b 不会变）'):
            return
        self.store.clear(); self.tpls = []; self.pred = None
        self.draw(); self.update_learn(); self.status('学习纪录已清除', 'ok')

    def toggle_play_btn(self):
        self.v_play.set(not self.v_play.get())
        self.toggle_play()

    def sync_play_btn(self):
        on = self.v_play.get()
        self.store.event('ai_auto', on=on)
        self.play_btn.config(text='⏸ 停止 AI（A / Esc）' if on else '🤖 AI 自动玩 (A)',
                             bg='#d9472f' if on else '#7b3fe4', activebackground='#d9472f' if on else '#7b3fe4')

    def toggle_play(self):
        if not self.v_play.get():
            self.status('⏸ AI 自动玩已停止', 'warn'); return
        if not self.esp:
            self.v_play.set(False); self.status('先连接蓝牙', 'warn'); return
        if not self.tpls:
            self.v_play.set(False); self.status('AI 还没学过：先自己画线玩几跳', 'warn'); return
        sh = learn.shadow_stats(self.store)
        ready = sh.get('ok') or (learn.fit_land(self.store) is not None)
        if not ready and not messagebox.askyesno(
                'AI 还不够准',
                f'AI 跟你的比较：{sh["n"]} 跳，中位误差 {sh.get("med", 0) * 100:.1f}%。\n'
                f'（建议 ≥{learn.MIN_SHADOW} 跳、≤{learn.GOOD_ERR * 100:.0f}%）\n\n还是要让它自己玩吗？'):
            self.v_play.set(False); return
        self.status('🤖 AI 自动玩中（Esc 停止）', 'ok')
        self.auto_fail = 0
        self.auto_step()

    def auto_step(self):
        if not self.v_play.get() or self.busy or self.drag:
            return
        p = self.pred
        if p is None:
            return                                   # 还在预测，等 after_pred 再叫
        if 'ms' not in p:
            if self.auto_fail < 2:                   # 可能刚好在动画中：等一下再看一次
                self.auto_fail += 1
                self.status(f'AI 看不懂这张（{p.get("err")}），再看一次…', 'warn')
                self.root.after(700, lambda: self.refresh(stable=True))
                return
            self.v_play.set(False)
            self.status(f'⏸ AI 停下来了：{p.get("err", "看不懂这个画面")}。请手动画这一跳', 'warn')
            return
        self.auto_fail = 0
        W = self.img.size[0]
        a = (p['foot'][0] * W, p['foot'][1] * W)
        b = (p['center'][0] * W, p['center'][1] * W)
        aim = self.pick_aim(p)
        ms = p['ms']
        if aim:
            ms = int(round(max(self.cfg['min_ms'], min(self.cfg['max_ms'],
                                                       (p['units'] + aim) * self.cfg['k'] + self.cfg['b']))))
        if self.press(ms):
            if aim:
                self.status(f'🤖 这跳故意不跳正中：{"偏远" if aim > 0 else "偏近"} {abs(aim):.0f}（{ms} ms）', 'ok')
            self.last = {'ms': ms, 'units': p['units'] + aim}
            extra = self.press_extra()
            extra['aim'] = round(aim, 1)
            self.cur_rec = self.store.add(self.img, a, b, p['units'], ms, pred=p, auto=True, extra=extra)
            self.pending = (self.make_jump(a, b, p['units']), self.cur_rec)
            self.update_learn()

    def pick_aim(self, p):
        """要不要故意不跳正中：回传沿跳跃方向偏多少（+ 偏远 / − 偏近，屏宽 = 1000），0 = 瞄正中。
        顶面中心到边缘（沿跳跃方向）大约是顶面宽度的 0.29 倍；最多偏到这段的 miss_amount 成，
        再留一点按压误差的余地，保证还在平台上。小平台一律瞄正中。"""
        try:
            pct = float(self.cfg.get('miss_pct', 0))
            amt = min(0.7, max(0.0, float(self.cfg.get('miss_amount', 0.6))))   # 上限 0.7：再加按压误差也不会碰到边缘
        except (TypeError, ValueError):
            return 0.0
        w = p.get('width') or 0
        if pct <= 0 or amt <= 0 or w < 0.25 or random.random() * 100 >= pct:
            return 0.0
        edge = 0.29 * w * 1000                         # 中心 → 边缘
        margin = 0.03 * p['units']                     # 按压的随机误差约 ±3%
        hi = max(0.0, amt * edge - margin)
        lo = min(hi, 0.35 * edge)
        if hi < 15:
            return 0.0
        return random.uniform(lo, hi) * random.choice((-1, 1))


def main():
    try:   # Windows 高解析度萤幕：避免视窗模糊、座标偏移
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
