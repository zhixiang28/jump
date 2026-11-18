# -*- coding: utf-8 -*-
"""
跳一跳 自动按压（电脑版）

流程：电脑用 adb 抓 Android 手机截图 → 自动找小人和下一个方块 → 算出按压 ms
     → 用蓝牙发 P<ms> 给 ESP32 → servo 用触控笔按。

ESP32 的程序和蓝牙协议都不用改：
  服务 6e400001-...，RX(写) 6e400002-...，TX(通知) 6e400003-...
  P<ms> 按压，ESP 按完回 D<ms>

用法：
  python jump_bot.py              正常模式（每一跳按 Enter 确认；输入 a 进入全自动）
  python jump_bot.py --auto       一开始就全自动（按任意键回到手动）
  python jump_bot.py --no-ble     不连蓝牙，只测试截图和判断（会存 debug 图）
  python jump_bot.py --image x.png  用一张截图测试判断
"""
import argparse
import asyncio
import io
import json
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import time

try:
    import numpy as np
    from PIL import Image, ImageDraw
except ImportError:
    print('缺少套件，请先执行：  pip install numpy pillow bleak')
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, 'jump_config.json')
DEBUG_DIR = os.path.join(HERE, 'debug')

SVC = '6e400001-b5a3-f393-e0a9-e50e24dcca9e'
RX = '6e400002-b5a3-f393-e0a9-e50e24dcca9e'
TX = '6e400003-b5a3-f393-e0a9-e50e24dcca9e'

DEFAULT_CONFIG = {
    # 按压时间 = 距离 × k + b。距离单位跟网页一样：手机屏幕宽 = 1000。
    'k': 1.0,
    'b': 200,
    'min_ms': 150,
    'max_ms': 2500,
    # 跳跃方向和水平线的夹角（游戏是 30°），用来从方块顶点推算方块中心
    'jump_angle_deg': 30,
    # 小人底座中心比小人最低点高多少（以屏宽 1080 为准的像素）
    'piece_base_height': 20,
    # 小人身体宽度（以屏宽 1080 为准），找方块时要避开小人那几列
    'piece_body_width': 80,
    # 小人的颜色范围 (R, G, B) 下限 / 上限
    'piece_rgb_min': [45, 48, 88],
    'piece_rgb_max': [66, 69, 116],
    # 和背景色差多少才算方块
    'board_diff': 30,
    # 按完后等多久再截下一张图（秒），会加一点随机
    'wait_after': 1.4,
    'wait_random': 0.5,
    # 调整 k 的幅度（输入 + / - 时用），方向跟上次相反就减半
    'step': 0.04,
    # adb.exe 的位置；留 "adb" 会自动在 PATH、本资料夹、C:\\platform-tools 找
    'adb': 'adb',
    # 蓝牙装置名称（留空 = 用服务 UUID 找）
    'ble_name': '',
}


# ---------------------------------------------------------------- 设定
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        save_config(cfg)
    except Exception as e:
        print('设定档读取失败，用预设值：', e)
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('设定档存不了：', e)


# ---------------------------------------------------------------- adb 截图
def find_adb(cfg):
    cands = [cfg.get('adb') or 'adb',
             os.path.join(HERE, 'platform-tools', 'adb.exe'),
             os.path.join(HERE, 'adb.exe'),
             r'C:\platform-tools\adb.exe',
             os.path.expandvars(r'%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe')]
    for c in cands:
        p = shutil.which(c) or (c if os.path.isfile(c) else None)
        if p:
            return p
    return None


def check_device(adb):
    out = subprocess.run([adb, 'devices'], capture_output=True, text=True, timeout=15).stdout
    lines = [l for l in out.splitlines()[1:] if l.strip()]
    if not lines:
        return '没有找到手机：检查 USB 线，以及手机上是否开了「USB 调试」'
    if any('unauthorized' in l for l in lines):
        return '手机还没授权：请在手机上点「允许 USB 调试」'
    if not any(l.split()[-1] == 'device' for l in lines):
        return 'adb 状态异常：\n' + out
    return None


SCREENCAP_MODE = 'png'      # 'raw'：手机不压缩、直接传像素（通常比较快）；'png'：手机先压缩
SCREENCAP_INFO = 'PNG'


SHOT_TIMEOUT = 8            # 一次截图最多等几秒（手机偶尔会卡住，等再久也不会回来）


def screenshot_raw(adb, timeout=None):
    """screencap 不加 -p：前面 12 或 16 bytes 是 宽、高、格式（1 = RGBA），后面是原始像素。"""
    d = subprocess.run([adb, 'exec-out', 'screencap'], capture_output=True, timeout=timeout or SHOT_TIMEOUT).stdout
    if len(d) < 16:
        return None
    w, h, fmt = struct.unpack('<III', d[:12])
    n = w * h * 4
    if not (0 < w < 10000 and 0 < h < 10000) or fmt not in (1, 5):
        return None
    hdr = len(d) - n
    if hdr not in (12, 16):
        return None
    return Image.frombuffer('RGBA', (w, h), d[hdr:hdr + n], 'raw', 'RGBA' if fmt == 1 else 'BGRA', 0, 1).convert('RGB')


def pick_screencap_mode(adb):
    """两种截图方式各试一次，用比较快的那种。"""
    global SCREENCAP_MODE, SCREENCAP_INFO
    try:
        t = time.time(); screenshot_png(adb); t_png = time.time() - t
    except Exception:  # noqa: BLE001
        t_png = 99
    try:
        t = time.time(); ok = screenshot_raw(adb) is not None; t_raw = time.time() - t if ok else 99
    except Exception:  # noqa: BLE001
        t_raw = 99
    SCREENCAP_MODE = 'raw' if t_raw < t_png else 'png'
    SCREENCAP_INFO = f'{"原始像素" if SCREENCAP_MODE == "raw" else "PNG"}（PNG {t_png:.2f}s / 原始 {t_raw:.2f}s）'
    return SCREENCAP_MODE


def adb_reconnect(adb):
    """adb 卡住时让它重新连线（不用拔线）。"""
    for args in (['reconnect'], ['wait-for-device']):
        try:
            subprocess.run([adb] + args, capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass


STREAM = None               # stream.ScrcpyStream：影像串流开着时，截图直接拿串流的最新画面


def screenshot(adb, tries=3):
    """截图。影像串流开着就直接用串流的最新画面（不会卡住）；否则用 screencap。手机偶尔会卡住：每次最多等 SHOT_TIMEOUT 秒，失败就换另一种方式再试；
    第二次失败时让 adb 重新连线。全部失败才丢出错误。"""
    if STREAM is not None and STREAM.alive:
        im = STREAM.latest()
        if im is not None:
            return im
    order = [SCREENCAP_MODE, 'png' if SCREENCAP_MODE == 'raw' else 'raw']
    last = None
    for i in range(tries):
        mode = order[i % 2]
        try:
            im = screenshot_raw(adb) if mode == 'raw' else screenshot_png(adb)
            if im is not None:
                return im
            last = RuntimeError('截到的资料不完整')
        except subprocess.TimeoutExpired:
            last = RuntimeError(f'手机 {SHOT_TIMEOUT} 秒没有回应')
        except Exception as e:  # noqa: BLE001
            last = e
        if i == 1:
            adb_reconnect(adb)
    raise RuntimeError(f'试了 {tries} 次都失败（{last}）。检查 USB 线、手机是否解锁亮着')


def keep_awake(adb):
    """接着 USB 时让手机不要休眠（屏幕暗掉时截图常会卡住）。回传原本的设定，结束时用 restore_awake 还原。"""
    try:
        old = subprocess.run([adb, 'shell', 'settings', 'get', 'global', 'stay_on_while_plugged_in'],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        subprocess.run([adb, 'shell', 'svc', 'power', 'stayon', 'usb'], capture_output=True, timeout=8)
        return old
    except Exception:  # noqa: BLE001
        return None


def restore_awake(adb, old):
    if old is None or not old.strip().isdigit():
        return
    try:
        subprocess.run([adb, 'shell', 'settings', 'put', 'global', 'stay_on_while_plugged_in', old.strip()],
                       capture_output=True, timeout=8)
    except Exception:  # noqa: BLE001
        pass


def screenshot_png(adb, timeout=None):
    r = subprocess.run([adb, 'exec-out', 'screencap', '-p'], capture_output=True, timeout=timeout or SHOT_TIMEOUT)
    data = r.stdout
    if not data.startswith(b'\x89PNG'):
        # 某些旧手机 / Windows 会把 \n 换成 \r\n
        data = data.replace(b'\r\n', b'\n')
    return Image.open(io.BytesIO(data)).convert('RGB')


# ---------------------------------------------------------------- 判断小人和方块
def find_piece_and_board(im, cfg):
    """回传 (piece_x, piece_y, board_x, board_y, info)；找不到就丢 ValueError。"""
    a = np.asarray(im, dtype=np.int16)
    h, w, _ = a.shape
    sc = w / 1080.0
    y0, y1 = int(h / 3), int(h * 2 / 3)          # 上面是分数、下面是空白，只看中间

    # 1) 小人：特定的深紫色，取最低点和平均 x
    lo = np.array(cfg['piece_rgb_min']); hi = np.array(cfg['piece_rgb_max'])
    zone = a[y0:y1]
    mask = np.all((zone > lo) & (zone < hi), axis=2)
    ys, xs = np.nonzero(mask)
    if len(xs) < 50 * sc * sc:
        raise ValueError('找不到小人（颜色不符）')
    piece_y_max = y0 + ys.max()
    bottom = ys >= ys.max() - int(40 * sc)              # 只用底座附近的点算 x，比较准
    piece_x = float(xs[bottom].mean())
    piece_y = piece_y_max - cfg['piece_base_height'] * sc

    # 2) 方块：从上往下扫，第一列跟背景（该列最左边的颜色）明显不同的，就是下一个方块的顶点
    half = cfg['piece_body_width'] * sc / 2
    board_x = None
    top_y = None
    for y in range(y0, int(piece_y)):
        row = a[y]
        bg = row[0]
        diff = np.abs(row - bg).sum(axis=1)
        cand = diff > cfg['board_diff']
        cols = np.arange(w)
        cand &= np.abs(cols - piece_x) > half            # 避开小人（头可能比方块还高）
        cand &= ~np.all((row > lo) & (row < hi), axis=1)
        idx = np.nonzero(cand)[0]
        if len(idx):
            board_x = float(idx.mean())
            top_y = y
            break
    if board_x is None:
        raise ValueError('找不到下一个方块')

    # 3) 方块中心：在小人往 30° 方向的那条线上
    t = math.tan(math.radians(cfg['jump_angle_deg']))
    board_y = piece_y - abs(board_x - piece_x) * t
    if board_y <= top_y:                                  # 太扁的方块，至少在顶点下面一点
        board_y = top_y + 10 * sc
    return piece_x, piece_y, board_x, board_y, {'w': w, 'h': h, 'top_y': top_y}


def compute_ms(px, py, bx, by, w, cfg):
    dist = math.hypot(bx - px, by - py) * 1000.0 / w      # 屏宽 = 1000
    ms = dist * cfg['k'] + cfg['b']
    ms = int(round(max(cfg['min_ms'], min(cfg['max_ms'], ms))))
    return dist, ms


def save_debug(im, px, py, bx, by, dist, ms, note=''):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        im = im.copy()
        d = ImageDraw.Draw(im)
        r = max(6, im.width // 90)
        d.line([(px, py), (bx, by)], fill=(255, 0, 0), width=max(3, r // 2))
        d.ellipse([px - r, py - r, px + r, py + r], outline=(0, 200, 0), width=4)
        d.ellipse([bx - r, by - r, bx + r, by + r], outline=(255, 0, 0), width=4)
        try:
            from PIL import ImageFont
            font = ImageFont.load_default(size=max(24, im.width // 25))
        except Exception:
            font = None
        d.text((30, 30), f'dist {dist:.0f}  ->  {ms} ms {note}', fill=(255, 0, 0), font=font)
        im.thumbnail((720, 1600))
        im.save(os.path.join(DEBUG_DIR, 'latest.png'))
        im.save(os.path.join(DEBUG_DIR, time.strftime('%H%M%S') + '.png'))
        # 只留最近 30 张
        files = sorted(f for f in os.listdir(DEBUG_DIR) if f[0].isdigit())
        for f in files[:-30]:
            os.remove(os.path.join(DEBUG_DIR, f))
    except Exception as e:
        print('debug 图存不了：', e)


def save_fail(im):
    """判断失败时存原始截图，方便检查（debug/fail.png）。"""
    if im is None:
        return
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, 'fail.png')
        im.save(path)
        a = np.asarray(im, dtype=np.int16)
        h = a.shape[0]
        zone = a[h // 3: h * 2 // 3]
        r, g, b = zone[..., 0], zone[..., 1], zone[..., 2]
        dark_blue = (r < 110) & (g < 110) & (b < 160) & (b > r + 20) & (b > g + 20)
        n = int(dark_blue.sum())
        print(f'   原始截图已存：{path}')
        if a.max() < 10:
            print('   截图是全黑的：手机可能禁止截屏。')
        elif n:
            med = np.median(zone[dark_blue], axis=0).astype(int).tolist()
            print(f'   中间区域有 {n} 个深蓝紫色像素，中位颜色 RGB {med}（把这行和 fail.png 传给我）')
    except Exception as e:
        print('   存失败截图时出错：', e)


# ---------------------------------------------------------------- 蓝牙
class Esp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None
        self.done = asyncio.Event()
        self.on_angles = None          # 收到 ESP 回报角度（A<静止>,<压下>）时呼叫 on_angles(静止, 压下)

    async def connect(self):
        from bleak import BleakClient, BleakScanner
        name = (self.cfg.get('ble_name') or '').strip()
        print('搜索 ESP32 蓝牙中…（ESP32 同时只能连一个装置）')

        def match(d, ad):
            if name and (d.name or '') == name:
                return True
            return SVC in [u.lower() for u in (ad.service_uuids or [])]

        dev = await BleakScanner.find_device_by_filter(match, timeout=15)
        if not dev:
            raise RuntimeError('找不到 ESP32：确认它有通电，而且没有被别的装置连着')
        self.client = BleakClient(dev, disconnected_callback=lambda c: print('\n⚠ 蓝牙断开'))
        await self.client.connect()
        loop = asyncio.get_running_loop()

        def on_notify(_, data):
            msg = bytes(data).decode(errors='ignore')
            if msg.startswith('D'):
                loop.call_soon_threadsafe(self.done.set)
            elif msg.startswith('A') and self.on_angles:
                try:
                    r, p = (int(float(v)) for v in msg[1:].split(','))
                    self.on_angles(r, p)
                except ValueError:
                    pass

        await self.client.start_notify(TX, on_notify)
        print(f'已连接 {dev.name or dev.address}')

    async def press(self, ms):
        self.done.clear()
        await self.client.write_gatt_char(RX, f'P{ms}'.encode(), response=True)
        try:   # 等 ESP 回 D（按完）；没回就按时间估
            await asyncio.wait_for(self.done.wait(), timeout=ms / 1000 + 3)
        except asyncio.TimeoutError:
            pass

    async def send(self, cmd):
        """直接送一个指令（M<角度> 即时转动、A<静止>,<压下> 存角度、G 读角度）。"""
        await self.client.write_gatt_char(RX, cmd.encode(), response=True)

    async def close(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass


# ---------------------------------------------------------------- 主程序
HELP = '''指令：
  Enter   按这一跳            a     进入全自动（全自动中按任意键回到手动）
  +  / -  上一跳跳太近 / 跳太远（调整 k）     =  上一跳刚好（缩小调整幅度）
  数字    用这个 ms 按（例如 650）        k 1.52 / b 30   直接设定 k、b
  r       重新截图           q     离开'''


def feedback(cfg, state, d):
    if state.get('last_dir') and d != state['last_dir']:
        cfg['step'] = max(0.005, cfg['step'] / 2)
    state['last_dir'] = d
    if not state.get('last_dist'):
        print('还没有上一跳的距离')
        return
    new_ms = state['last_ms'] * (1 + d * cfg['step'])
    cfg['k'] = round(max(0.01, (new_ms - cfg['b']) / state['last_dist']), 4)
    save_config(cfg)
    print(f'{"加长" if d > 0 else "缩短"} {cfg["step"]*100:.1f}% → k = {cfg["k"]}')


def key_pressed():
    """全自动时，检查使用者有没有按任何键（不会卡住）。"""
    try:
        import msvcrt  # Windows
        if msvcrt.kbhit():
            while msvcrt.kbhit():
                msvcrt.getwch()
            return True
        return False
    except ImportError:
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            sys.stdin.readline()
            return True
        return False


async def ainput(prompt):
    try:
        return (await asyncio.to_thread(input, prompt)).strip()
    except EOFError:
        return 'q'


async def main():
    ap = argparse.ArgumentParser(description='跳一跳 自动按压（电脑版）')
    ap.add_argument('--auto', action='store_true', help='一开始就全自动（按任意键回到手动）')
    ap.add_argument('--no-ble', action='store_true', help='不连蓝牙，只测试判断')
    ap.add_argument('--image', help='用一张截图测试判断')
    args = ap.parse_args()
    cfg = load_config()

    if args.image:
        im = Image.open(args.image).convert('RGB')
        try:
            px, py, bx, by, info = find_piece_and_board(im, cfg)
        except ValueError as e:
            print('✗', e); save_fail(im); return
        dist, ms = compute_ms(px, py, bx, by, info['w'], cfg)
        save_debug(im, px, py, bx, by, dist, ms)
        print(f'小人 ({px:.0f},{py:.0f})  方块 ({bx:.0f},{by:.0f})  距离 {dist:.0f}  → {ms} ms')
        print('标记图：', os.path.join(DEBUG_DIR, 'latest.png'))
        return

    adb = find_adb(cfg)
    if not adb:
        print('找不到 adb.exe。请下载 Android Platform Tools 解压到 C:\\platform-tools，'
              '或在 jump_config.json 的 "adb" 写上 adb.exe 的完整路径。')
        return
    err = check_device(adb)
    if err:
        print(err)
        return

    esp = None
    if not args.no_ble:
        try:
            import bleak  # noqa: F401
        except ImportError:
            print('缺少 bleak：请执行  pip install bleak')
            return
        esp = Esp(cfg)
        await esp.connect()

    print(f'k = {cfg["k"]}, b = {cfg["b"]}（设定档：{CONFIG_PATH}）')
    print(HELP)
    auto = args.auto
    state = {}
    fails = 0
    try:
        while True:
            im = None
            try:
                im = await asyncio.to_thread(screenshot, adb)
                px, py, bx, by, info = find_piece_and_board(im, cfg)
                dist, ms = compute_ms(px, py, bx, by, info['w'], cfg)
                save_debug(im, px, py, bx, by, dist, ms)
                fails = 0
            except Exception as e:
                fails += 1
                print('✗', e)
                save_fail(im)
                if auto and fails < 3:
                    await asyncio.sleep(1.5)
                    continue
                auto = False
                cmd = await ainput('判断失败。Enter 重试 / 数字=手动 ms / q 离开 > ')
                if cmd == 'q':
                    break
                if cmd.isdigit() and esp:
                    await esp.press(int(cmd))
                continue

            print(f'距离 {dist:.0f}（屏宽=1000） → {ms} ms', end='')
            use_ms = ms
            if not auto:
                cmd = await ainput('   > ')
                if cmd == 'q':
                    break
                if cmd == 'a':
                    auto = True
                    print('全自动开始（按任意键回到手动）')
                elif cmd == 'r':
                    continue
                elif cmd in ('+', '-'):
                    feedback(cfg, state, 1 if cmd == '+' else -1)
                    continue
                elif cmd == '=':
                    cfg['step'] = max(0.005, cfg['step'] / 2); state['last_dir'] = 0
                    save_config(cfg); print(f'调整幅度缩小为 ±{cfg["step"]*100:.1f}%')
                    continue
                elif cmd.startswith(('k ', 'b ')):
                    try:
                        cfg[cmd[0]] = float(cmd[2:]); save_config(cfg)
                        print(f'k = {cfg["k"]}, b = {cfg["b"]}')
                    except ValueError:
                        print('格式：k 1.52  或  b 30')
                    continue
                elif cmd.isdigit():
                    use_ms = int(cmd)
                elif cmd:
                    print(HELP)
                    continue
            else:
                print()
                if key_pressed():
                    auto = False
                    print('⏸ 回到手动模式')
                    continue

            if esp:
                await esp.press(use_ms)
            else:
                print('（--no-ble：没有真的按）')
            state.update(last_ms=use_ms, last_dist=dist)
            await asyncio.sleep(cfg['wait_after'] + random.random() * cfg['wait_random'])
    except KeyboardInterrupt:
        pass
    finally:
        if esp:
            await esp.close()
        print('结束')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n结束')
