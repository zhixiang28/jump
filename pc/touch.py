# -*- coding: utf-8 -*-
"""
手机「真的」认到的触控（adb shell getevent，不用 root）

每次按压，手机核心层会记下触控笔「碰到」和「离开」屏幕的精确时间。拿来跟送给 ESP32 的按压时间比：
- 手机认到按了多久（touch_ms）vs 程式要求的 ms：差多少、稳不稳 → 分得出是触控笔 / servo 的问题还是游戏本身
- 下指令到手机认到碰到，隔多久（touch_delay_ms）→ servo 下压要多久、会不会忽快忽慢
- 手机根本没认到（no_touch）→ 「手机没反应」的直接证据

只读触控事件，不会送任何东西给手机。读不到（有些手机不让）就当没有这个功能，程式照常运作。
"""
import re
import subprocess
import threading
import time

_LINE = re.compile(r'\[\s*([\d.]+)\]\s+(\S+):\s+(\S+)\s+(\S+)\s+(\S+)')


class TouchMonitor:
    def __init__(self, adb):
        self.adb = adb
        self.proc = None
        self.touches = []          # [{'down_k', 'up_k', 'down_pc', 'up_pc'}]（k = 手机核心时间，pc = 电脑收到的时间）
        self.lock = threading.Lock()
        self.error = ''
        self.lines = 0
        self.events = 0            # 真的读到的触控事件（只有表头、没事件 = 手机不给读）
        self._down = None

    def start(self):
        try:
            self.proc = subprocess.Popen([self.adb, 'shell', 'getevent', '-lt'], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        except Exception as e:  # noqa: BLE001
            self.error = f'getevent 开不了：{e}'
            return False
        threading.Thread(target=self._read, daemon=True).start()
        return True

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _read(self):
        try:
            for raw in iter(self.proc.stdout.readline, b''):
                self.lines += 1
                m = _LINE.match(raw.decode(errors='ignore'))
                if not m:
                    continue
                self.events += 1
                t, _dev, typ, code, val = m.groups()
                t = float(t)
                down = up = False
                if typ == 'EV_KEY' and code == 'BTN_TOUCH':
                    down, up = val == 'DOWN', val == 'UP'
                elif typ == 'EV_ABS' and code == 'ABS_MT_TRACKING_ID':
                    up = val.lower() == 'ffffffff'
                    down = not up
                if down and self._down is None:
                    self._down = (t, time.time())
                elif up and self._down is not None:
                    with self.lock:
                        self.touches.append({'down_k': self._down[0], 'up_k': t, 'down_pc': self._down[1], 'up_pc': time.time()})
                        del self.touches[:-200]
                    self._down = None
        except Exception as e:  # noqa: BLE001
            self.error = f'读触控事件出错：{e}'
        if not self.error:
            self.error = '触控事件读取结束（手机断线？）'

    def claim(self, t_press, ms):
        """t_press 那一次按压，手机认到的触控。回传 dict 或 None（这个功能没在跑）。"""
        if not self.events:
            return None                                    # 一个触控事件都没读到：这支手机不给读，不要乱下结论
        with self.lock:
            hits = [x for x in self.touches if t_press - 0.3 <= x['down_pc'] <= t_press + ms / 1000 + 1.0]
        if not hits:
            return {'no_touch': True}
        x = hits[0]
        return {'touch_ms': round((x['up_k'] - x['down_k']) * 1000, 1),
                'touch_delay_ms': round((x['down_pc'] - t_press) * 1000),
                'n_touches': len(hits)}
