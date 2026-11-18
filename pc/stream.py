# -*- coding: utf-8 -*-
"""
手机画面「影像串流」（用 scrcpy 官方的手机端小程序）

不再每次叫手机截一张图（偶尔会卡住），而是让手机持续把画面用 H.264 串流过来，
电脑这边随时都有最新的一张画面，也能连续判断「画面停下来了没」。

流程（scrcpy 官方文件的 standalone server 用法）：
  1. 把 scrcpy-server 推到手机 /data/local/tmp/
  2. adb forward tcp:<port> localabstract:scrcpy
  3. adb shell 启动 server（raw_stream=true：只送纯 H.264，没有其他标头）
  4. 电脑连到 localhost:<port>，用 PyAV 解码

串流连不上或中断时，jump_bot.screenshot() 会自动退回原本的截图方式。
"""
import os
import socket
import subprocess
import threading
import time
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_VERSION = '3.3.1'
SERVER_FILE = os.path.join(HERE, f'scrcpy-server-v{SERVER_VERSION}')
SERVER_URL = f'https://github.com/Genymobile/scrcpy/releases/download/v{SERVER_VERSION}/scrcpy-server-v{SERVER_VERSION}'
DEVICE_PATH = '/data/local/tmp/scrcpy-server-servo.jar'
MOTION_THR = 3.0          # 小图（64×140 灰阶）平均差超过这个值 = 画面在动（雪花飘只会差一点点）
STALL_SEC = 3.0           # 等画面时，这么久完全没收到新画面 = 串流卡住了（游戏有雪花在飘，正常一直会有新画面）


def ensure_server():
    """手机端小程序：没有就从 scrcpy 官方 GitHub 下载（约 90 KB）。回传 (路径 或 None, 说明)。"""
    if os.path.isfile(SERVER_FILE) and os.path.getsize(SERVER_FILE) > 10000:
        return SERVER_FILE, ''
    try:
        tmp = SERVER_FILE + '.part'
        with urllib.request.urlopen(SERVER_URL, timeout=30) as r, open(tmp, 'wb') as f:
            f.write(r.read())
        if os.path.getsize(tmp) < 10000:
            raise RuntimeError('下载的档案太小')
        os.replace(tmp, SERVER_FILE)
        return SERVER_FILE, '已下载 scrcpy-server'
    except Exception as e:  # noqa: BLE001
        return None, (f'下载 scrcpy-server 失败（{e}）。可以手动下载 {SERVER_URL} '
                      f'放到 pc 资料夹（档名 scrcpy-server-v{SERVER_VERSION}）')


class ScrcpyStream:
    def __init__(self, adb, max_size=1920, bit_rate=12_000_000, max_fps=30, port=27183, connect=None):
        self.adb = adb
        self.max_size = max_size
        self.bit_rate = bit_rate
        self.max_fps = max_fps
        self.port = port
        self._connect_override = connect      # 测试用：直接给一个 (host, port)，不经过 adb
        self.proc = None
        self.sock = None
        self.thread = None
        self.lock = threading.Lock()
        self.frame = None                     # 最新的一张（PIL Image）
        self.frame_t = 0.0
        self.frames = 0
        self.last_motion = time.time()
        self.prev_small = None
        self.running = False
        self.error = ''
        self.log = []                         # 手机端小程序印出来的讯息（最后 40 行）
        self.log_thread = None

    # ------------------------------------------------------------ 启动 / 停止
    def _adb(self, *args, timeout=15):
        return subprocess.run([self.adb] + list(args), capture_output=True, text=True, errors='replace', timeout=timeout)

    def start(self, wait_first_frame=8.0):
        """回传 (成功?, 说明)。"""
        try:
            import av  # noqa: F401
        except ImportError:
            return False, '缺少 PyAV（请执行  pip install av ，或重新双击 安装.bat）'
        if self._connect_override:
            host, port = self._connect_override
        else:
            path, msg = ensure_server()
            if not path:
                return False, msg
            try:
                r = self._adb('push', path, DEVICE_PATH, timeout=30)
                if r.returncode != 0:
                    return False, f'推送 scrcpy-server 失败：{(r.stderr or r.stdout).strip()[:200]}'
                self._adb('forward', '--remove', f'tcp:{self.port}')
                r = self._adb('forward', f'tcp:{self.port}', 'localabstract:scrcpy')
                if r.returncode != 0:
                    return False, f'adb forward 失败：{(r.stderr or r.stdout).strip()[:200]}'
                self.proc = subprocess.Popen(
                    [self.adb, 'shell', f'CLASSPATH={DEVICE_PATH}', 'app_process', '/', 'com.genymobile.scrcpy.Server',
                     SERVER_VERSION, 'tunnel_forward=true', 'audio=false', 'control=false', 'cleanup=false',
                     'raw_stream=true', 'video_codec=h264', f'max_size={self.max_size}',
                     f'video_bit_rate={self.bit_rate}', f'max_fps={self.max_fps}', 'stay_awake=true'],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                # 一定要一直读掉它的输出：没人读的话管线塞满（Windows 只有几 KB），手机端小程序会卡住 → 串流冻结 / 中断
                self.log_thread = threading.Thread(target=self._drain, args=(self.proc,), daemon=True)
                self.log_thread.start()
            except Exception as e:  # noqa: BLE001
                return False, f'启动串流失败：{e}'
            host, port = '127.0.0.1', self.port

        # server 需要一点时间才会开始听；adb 会先接受连线，server 还没好时会马上断掉 → 重试
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                s = socket.create_connection((host, port), timeout=3)
                s.settimeout(5)
                first = s.recv(65536)
                if first:
                    self.sock = s
                    break
                s.close()
            except OSError:
                pass
            time.sleep(0.3)
        if not self.sock:
            out = self._server_output()
            self.stop()
            return False, '连不上手机端的串流程式' + (f'：{out}' if out else '')
        self.running = True
        self.thread = threading.Thread(target=self._reader, args=(first,), daemon=True)
        self.thread.start()
        t0 = time.time()
        while time.time() - t0 < wait_first_frame and self.frame is None and self.running:
            time.sleep(0.05)
        if self.frame is None:
            err = self.error or self._server_output() or '没有收到画面'
            self.stop()
            return False, f'串流没有画面：{err}'
        return True, f'影像串流（{self.frame.size[0]}×{self.frame.size[1]}）'

    def _drain(self, proc):
        try:
            for line in iter(proc.stdout.readline, b''):
                t = line.decode(errors='ignore').strip()
                if t:
                    self.log.append(t)
                    del self.log[:-40]
        except Exception:  # noqa: BLE001
            pass

    def _server_output(self):
        return ' | '.join(self.log[-4:])[-300:]

    def stop(self):
        self.running = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:  # noqa: BLE001
            pass
        self.sock = None
        if self.proc:
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
        if not self._connect_override:
            try:
                self._adb('forward', '--remove', f'tcp:{self.port}', timeout=5)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 解码
    def _reader(self, first):
        import av
        codec = av.CodecContext.create('h264', 'r')
        data = first
        try:
            while self.running:
                if data:
                    for pkt in codec.parse(data):
                        for fr in codec.decode(pkt):
                            self._on_frame(fr)
                try:
                    data = self.sock.recv(1 << 16)
                except socket.timeout:
                    data = b''                # 画面没变时手机不会送新画面，这是正常的
                    continue
                if not data:
                    self.error = '串流中断'
                    break
        except Exception as e:  # noqa: BLE001
            self.error = f'解码错误：{e}'
        self.running = False

    def _on_frame(self, fr):
        small = fr.reformat(width=64, height=140, format='gray').to_ndarray().astype(np.float32)
        img = fr.to_image()
        now = time.time()
        with self.lock:
            if self.prev_small is not None and float(np.abs(small - self.prev_small).mean()) > MOTION_THR:
                self.last_motion = now
            self.prev_small = small
            self.frame = img
            self.frame_t = now
            self.frames += 1

    # ------------------------------------------------------------ 给程序用
    @property
    def alive(self):
        return self.running and self.frame is not None

    def mark_stalled(self, why):
        """串流卡住（连着但不送画面）：当作断了，让程序重开。"""
        if self.running:
            self.error = why
            self.running = False
            try:
                if self.sock:
                    self.sock.close()
            except Exception:  # noqa: BLE001
                pass

    def describe_error(self):
        out = self._server_output()
        return (self.error or '串流停了') + (f'（手机端：{out}）' if out else '')

    def latest(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def wait_stable(self, quiet=0.45, timeout=8.0):
        """等画面停下来（呼叫之后，连续 quiet 秒没有明显变化）再回传最新画面。超过 timeout 就回传当下的画面。
        只算呼叫之后看到的静止：刚开始动的画面可能还没解码出来，不能用之前的静止来判断。
        卡住（STALL_SEC 秒完全没新画面）或断掉就回传 None，让呼叫的人改用别的方法、重开串流。"""
        t0 = time.time()
        n0 = self.frames
        while time.time() - t0 < timeout:
            if not self.alive:
                return None
            with self.lock:
                now = time.time()
                still = now - max(self.last_motion, t0)
                stalled = now - max(self.frame_t, t0) >= STALL_SEC
            if stalled:
                self.mark_stalled(f'{STALL_SEC:.0f} 秒没有新画面（串流卡住）' + ('' if self.frames > n0 else '，刚跳完却没收到任何画面'))
                return None
            if still >= quiet and self.frames > n0:
                return self.latest()
            time.sleep(0.03)
        return self.latest()
