# -*- coding: utf-8 -*-
"""
手机画面 + 手动画线（电脑版）

电脑用 adb 抓手机截图显示在视窗里，用滑鼠从小人拉到目标，放开就用蓝牙叫 ESP32 按。
不用相机、不用画边框，任何「跳一跳」类的游戏都能用（不管方块长什么样）。

快捷键：
  空白键 = 按       R = 重新截图       右键 = 清除线
  1 = 太久          2 = 刚好           3 = 太短
"""
import asyncio
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk

import jump_bot as jb

FONT = ('Microsoft YaHei UI', 11)
FONT_B = ('Microsoft YaHei UI', 11, 'bold')
BG, PANEL, TEXT, SUB = '#0e0f12', '#1a1c21', '#f3f4f6', '#c0c5cd'


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
        self.build()
        self.root.after(50, self.poll)
        self.root.after(100, self.start)

    # ------------------------------------------------------------ 介面
    def build(self):
        r = self.root
        r.title('Servo Press · 手机画面画线')
        r.configure(bg=BG)
        sh = r.winfo_screenheight()
        self.max_h = max(400, sh - 140)

        self.cv = tk.Canvas(r, bg='#000', highlightthickness=0, width=int(self.max_h / 2.1), height=self.max_h,
                            cursor='crosshair')
        self.cv.pack(side='left', padx=(8, 4), pady=8)
        self.cv.bind('<ButtonPress-1>', self.on_down)
        self.cv.bind('<B1-Motion>', self.on_move)
        self.cv.bind('<ButtonRelease-1>', self.on_up)
        self.cv.bind('<Button-3>', lambda e: self.clear_line())

        p = tk.Frame(r, bg=PANEL, padx=12, pady=10)
        p.pack(side='left', fill='y', padx=(4, 8), pady=8)

        def btn(parent, text, cmd, bg='#262a31', fg=TEXT, **kw):
            b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                          relief='flat', font=kw.pop('font', FONT_B), padx=10, pady=6, cursor='hand2', **kw)
            return b

        row = tk.Frame(p, bg=PANEL); row.pack(fill='x')
        self.bt = btn(row, '连接蓝牙', self.connect, bg='#3b7cff', fg='#fff')
        self.bt.pack(side='left', fill='x', expand=True)
        btn(row, '⟳ 截图 (R)', self.refresh).pack(side='left', fill='x', expand=True, padx=(6, 0))

        self.ms_lbl = tk.Label(p, text='--', font=('Microsoft YaHei UI', 40, 'bold'), bg=PANEL, fg=TEXT)
        self.ms_lbl.pack(anchor='w', pady=(14, 0))
        self.dist_lbl = tk.Label(p, text='用滑鼠从小人拉到目标', font=FONT, bg=PANEL, fg=SUB, justify='left')
        self.dist_lbl.pack(anchor='w')

        self.press_btn = btn(p, '按！(空白键)', self.press_line, bg='#3b7cff', fg='#fff',
                             font=('Microsoft YaHei UI', 18, 'bold'))
        self.press_btn.pack(fill='x', pady=(12, 8))

        fb = tk.Frame(p, bg=PANEL); fb.pack(fill='x')
        btn(fb, '− 太久 (1)', lambda: self.feedback(-1), bg='#d9472f', fg='#fff').pack(side='left', fill='x', expand=True)
        btn(fb, '✓ 刚好 (2)', self.ok, bg='#1fa955', fg='#fff').pack(side='left', fill='x', expand=True, padx=6)
        btn(fb, '＋ 太短 (3)', lambda: self.feedback(1), bg='#d58f12', fg='#fff').pack(side='left', fill='x', expand=True)

        self.st = tk.Label(p, text='', font=FONT, bg=PANEL, fg=SUB, wraplength=330, justify='left', height=3, anchor='nw')
        self.st.pack(fill='x', pady=(10, 4))

        def check(text, key, cmd=None):
            var = tk.BooleanVar(value=bool(self.cfg.get(key)))

            def on():
                self.cfg[key] = var.get(); jb.save_config(self.cfg)
                if cmd: cmd()
            tk.Checkbutton(p, text=text, variable=var, command=on, font=FONT, bg=PANEL, fg=TEXT, selectcolor='#262a31',
                           activebackground=PANEL, activeforeground=TEXT, anchor='w').pack(fill='x')
            return var
        self.v_auto = check('画完自动按', 'gui_auto')
        self.v_proj = check('只算沿跳跃方向的距离', 'gui_proj', self.recalc)
        self.v_live = check('自动刷新画面（每 1.5 秒）', 'gui_live')

        def entry(label, key, width=7):
            f = tk.Frame(p, bg=PANEL); f.pack(fill='x', pady=2)
            tk.Label(f, text=label, font=FONT, bg=PANEL, fg=TEXT, width=12, anchor='w').pack(side='left')
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
        tk.Label(p, text='', bg=PANEL).pack()
        self.e_k = entry('k', 'k')
        self.e_b = entry('b (ms)', 'b')
        self.e_ang = entry('跳跃角度 (°)', 'jump_angle_deg')
        self.e_wait = entry('按完等几秒', 'wait_after')
        tk.Label(p, text='按压时间 = 距离 × k + b\n距离单位：手机屏幕宽 = 1000\n'
                         '\n空白键 按 · R 截图 · 右键 清除线\n1 太久 · 2 刚好 · 3 太短',
                 font=('Microsoft YaHei UI', 10), bg=PANEL, fg=SUB, justify='left').pack(anchor='w', pady=(10, 0))
        self.info_lbl = tk.Label(p, text='', font=('Microsoft YaHei UI', 10), bg=PANEL, fg=SUB, justify='left')
        self.info_lbl.pack(anchor='w', pady=(6, 0))
        self.show_info()

        r.bind('<KeyPress>', self.on_key)

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
        self.bg(lambda: jb.check_device(self.adb), self.after_check)
        self.live_tick()

    def after_check(self, err_msg, err):
        if err or err_msg:
            self.status(str(err or err_msg), 'err'); return
        self.refresh()

    def refresh(self):
        if not self.adb or self.grabbing:
            return
        self.grabbing = True
        self.bg(lambda: jb.screenshot(self.adb), self.after_shot)

    def after_shot(self, im, err):
        self.grabbing = False
        if err:
            self.status(f'截图失败：{err}', 'err'); return
        self.img = im
        w, h = im.size
        self.scale = min(self.max_h / h, 1.0)
        disp = im.resize((max(1, int(w * self.scale)), max(1, int(h * self.scale))), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(disp)
        self.cv.config(width=disp.width, height=disp.height)
        self.cv.delete('img')
        self.cv.create_image(0, 0, anchor='nw', image=self.photo, tags='img')
        self.cv.tag_lower('img')
        self.draw()

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
        self.status(f'已按 {ms} ms ✓ 结果怎样？按 1 太久 / 2 刚好 / 3 太短', 'ok')

        def done():
            self.busy = False; self.press_btn.config(state='normal')
            self.line = None; self.recalc(); self.refresh()   # 新画面、清掉旧线
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
        self.status(f'{"加长" if d > 0 else "缩短"} {self.cfg["step"] * 100:.1f}%：刚才那条线下次按 {round(new_ms)} ms'
                    f'（k = {self.cfg["k"]}）', 'ok')
        self.show_info(); self.recalc()

    def ok(self):
        self.cfg['step'] = max(0.005, self.cfg['step'] / 2); self.last_dir = 0
        jb.save_config(self.cfg); self.show_info()
        self.status(f'👍 调整幅度收窄为 ±{self.cfg["step"] * 100:.1f}%', 'ok')


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
