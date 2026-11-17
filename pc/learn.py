# -*- coding: utf-8 -*-
"""
从你玩的纪录学习（给 jump_gui.py 用）

1. 小人：你画线的起点 = 小人脚底。从那些截图切出小人当模板，用彩色 NCC 模板匹配找小人。
2. 目标：从小人沿 ±30° 跳跃线往外找第一个「不是背景、不是目前方块」的物体，
         取它的最高点当方块顶点，中心就在跳跃线上、跟顶点同一个 x。
3. 按压：每一跳记 距离 / ms / 结果（跳太远·刚好·跳太近），用最小二乘法拟合 k、b。
4. 影子模式：你自己画线时，AI 也同时预测，记下误差；误差够小才建议让它自己玩。

所有座标都用「手机屏幕宽 = 1」的比例（x/W, y/W），所以跟截图解析度无关。
纪录存在 pc/data/（log.json + 缩小的截图）。
"""
import csv
import json
import math
import os
import platform
import time
import zipfile
from collections import deque

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
SHOTS = os.path.join(DATA, 'shots')
LOG = os.path.join(DATA, 'log.json')
EVENTS = os.path.join(DATA, 'events.csv')
EXPORT = os.path.join(HERE, 'export')

NW = 540            # 学习 / 比对用的截图宽度
MAX_RECS = 300
# 小人模板（屏宽比例）：只切小人的身体（帽子、头、围巾），不要脚下的方块和两边的背景，
# 否则小人站到不同颜色的方块上就认不出来。起点常常点在脚前面一点，所以底部离起点留一段。
TPL_W, TPL_TOP, TPL_BOTTOM = 0.14, 0.30, 0.07
MIN_SHADOW = 8      # 影子模式至少几笔才评估
GOOD_ERR = 0.03     # 中位误差 ≤ 3% 算够准


# ================================================================ 纪录
class Store:
    def __init__(self):
        os.makedirs(SHOTS, exist_ok=True)
        self.recs = []
        try:
            with open(LOG, encoding='utf-8') as f:
                self.recs = [r for r in json.load(f) if isinstance(r, dict)]
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            print('学习纪录读不了，重新开始：', e)

    def save(self):
        tmp = LOG + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.recs, f, ensure_ascii=False)
        os.replace(tmp, LOG)

    def add(self, im, a, b, units, ms, pred=None, auto=False, extra=None):
        """a, b：截图像素座标。extra：其他要记下来的栏位（当时的 k、b、按下的时间…）。回传这笔纪录。"""
        W, H = im.size
        rid = time.strftime('%Y%m%d-%H%M%S') + f'-{int(time.time() * 1000) % 1000:03d}'
        try:
            im.resize((NW, round(H * NW / W)), Image.BILINEAR).save(os.path.join(SHOTS, rid + '.jpg'), quality=90)
        except Exception as e:  # noqa: BLE001
            print('截图存不了：', e)
        rec = {'id': rid, 'W': W, 'H': H,
               'a': [a[0] / W, a[1] / W], 'b': [b[0] / W, b[1] / W],
               'units': round(units, 1), 'ms': int(ms), 'auto': bool(auto),
               'r': None, 'est': None,
               'pred': ({'units': round(pred['units'], 1), 'score': round(pred['score'], 3),
                         'foot': [round(v, 4) for v in pred['foot']], 'center': [round(v, 4) for v in pred['center']]}
                        if pred and 'foot' in pred else None)}
        if extra:
            rec.update({k: v for k, v in extra.items() if k not in rec})   # 不覆盖原本的栏位
        self.recs.append(rec)
        while len(self.recs) > MAX_RECS:
            old = self.recs.pop(0)
            for suffix in ('.jpg', '_after.jpg'):
                try:
                    os.remove(os.path.join(SHOTS, old['id'] + suffix))
                except OSError:
                    pass
        self.save()
        self.event('press', id=rid, ms=int(ms), units=round(units, 1), auto=bool(auto))
        return rec

    def save_after(self, rec, im):
        """跳完、画面停下来之后的截图（量落点用的那张），导出给你检查用。"""
        if rec is None or im is None:
            return
        try:
            W, H = im.size
            im.resize((NW, round(H * NW / W)), Image.BILINEAR).save(os.path.join(SHOTS, rec['id'] + '_after.jpg'),
                                                                    quality=90)
        except Exception as e:  # noqa: BLE001
            print('跳完的截图存不了：', e)

    def event(self, kind, **kv):
        """操作纪录（data/events.csv）：时间精确到毫秒，方便跟录影对时间。"""
        try:
            new = not os.path.exists(EVENTS)
            with open(EVENTS, 'a', encoding='utf-8', newline='') as f:
                w = csv.writer(f)
                if new:
                    w.writerow(['time', 'clock', 'event', 'detail'])
                t = time.time()
                w.writerow([f'{t:.3f}', time.strftime('%H:%M:%S', time.localtime(t)) + f'.{int(t * 1000) % 1000:03d}',
                            kind, json.dumps(kv, ensure_ascii=False)])
        except Exception:  # noqa: BLE001
            pass

    def set_result(self, rec, r, est):
        if rec is None:
            return
        rec['r'] = r
        rec['est'] = int(round(est))
        self.save()

    def clear(self):
        for r in self.recs:
            for suffix in ('.jpg', '_after.jpg'):
                try:
                    os.remove(os.path.join(SHOTS, r['id'] + suffix))
                except OSError:
                    pass
        self.recs = []
        self.save()
        self.event('clear')

    def shot(self, rec):
        return Image.open(os.path.join(SHOTS, rec['id'] + '.jpg')).convert('RGB')


# ================================================================ 影像工具
def as_arr(im, width):
    h = round(im.height * width / im.width)
    return np.asarray(im.resize((width, h), Image.BILINEAR), dtype=np.float64)


def box_sum(a, h, w):
    c = np.pad(a.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    return c[h:, w:] - c[:-h, w:] - c[h:, :-w] + c[:-h, :-w]


def ncc_map(img, t):
    """彩色零均值正规化互相关（同 OpenCV TM_CCOEFF_NORMED），用 FFT 算。回传 (H-h+1, W-w+1)。"""
    H, W = img.shape[:2]
    h, w = t.shape[:2]
    if h > H or w > W:
        return None
    n = h * w * 3
    tz = t - t.mean()
    tn = math.sqrt(float((tz ** 2).sum()))
    if tn < 1e-6:
        return None
    num = 0
    for c in range(3):
        F = np.fft.rfft2(img[..., c], (H, W))
        G = np.fft.rfft2(tz[..., c], (H, W))
        num = num + np.fft.irfft2(F * np.conj(G), (H, W))[:H - h + 1, :W - w + 1]
    s = box_sum(img.sum(2), h, w)
    ss = box_sum((img ** 2).sum(2), h, w)
    var = np.maximum(ss - s * s / n, 1e-6)
    return num / (np.sqrt(var) * tn)


def ncc_at(img, t, x, y):
    h, w = t.shape[:2]
    win = img[y:y + h, x:x + w]
    if win.shape[:2] != (h, w):
        return -1.0
    a = win - win.mean()
    b = t - t.mean()
    d = math.sqrt(float((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-6 else -1.0


# ================================================================ 小人
def build_templates(store, max_n=12, scan=60):
    """从最近的「你自己画的线」切小人模板（含左右镜像）。
    小人会转向（背面、侧面…），所以尽量收集不同姿势：跟已经有的样板几乎一样的就跳过。"""
    tpls = []
    tw, th = round(TPL_W * NW), round((TPL_TOP - TPL_BOTTOM) * NW)
    for rec in reversed(store.recs):
        if rec.get('auto'):
            continue                       # 只信你自己画的起点，避免越学越歪
        try:
            img = as_arr(store.shot(rec), NW)
        except Exception:  # noqa: BLE001
            continue
        fx, fy = rec['a'][0] * NW, rec['a'][1] * NW
        x0 = round(fx - tw / 2)
        y1 = round(fy - TPL_BOTTOM * NW)
        y0 = y1 - th
        if x0 < 0 or y0 < 0 or x0 + tw > img.shape[1] or y1 > img.shape[0]:
            continue
        crop = img[y0:y1, x0:x0 + tw]
        if any(ncc_at(crop, T['t'], 0, 0) > 0.93 for T in tpls):
            continue                       # 这个姿势已经有了
        tpls.append({'t': crop, 'fx': fx - x0, 'fy': fy - y0})
        tpls.append({'t': crop[:, ::-1].copy(), 'fx': tw - (fx - x0), 'fy': fy - y0})
        if len(tpls) >= max_n * 2:
            break
        scan -= 1
        if scan <= 0:
            break
    return tpls


def half(arr):
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    return np.asarray(im.resize((max(1, arr.shape[1] // 2), max(1, arr.shape[0] // 2)), Image.BILINEAR),
                      dtype=np.float64)


_TPL_FFT = {}       # 样板的 FFT 快取：(样板, 搜寻区大小) → (共轭频谱, 高, 宽, 范数)


def _tpl_fft(T, shape):
    key = (id(T), shape)
    hit = _TPL_FFT.get(key)
    if hit is None:
        if len(_TPL_FFT) > 64:
            _TPL_FFT.clear()
        t = T['half']
        tz = t - t.mean()
        tn = math.sqrt(float((tz ** 2).sum()))
        Gs = [np.conj(np.fft.rfft2(tz[..., c], shape)) for c in range(3)]
        hit = (Gs, t.shape[0], t.shape[1], tn)
        _TPL_FFT[key] = hit
    return hit


def find_char(im, tpls, y_range=(0.38, 0.85)):
    """回传 {'foot': (x, y) 屏宽比例, 'score'} 或 None。先在宽 270 找，再到 540 细调。
    加速：小人只会在画面中间偏下，只搜那一段；整张图的 FFT 只算一次，所有样板共用；样板的 FFT 也快取。"""
    if not tpls:
        return None
    small = as_arr(im, NW // 2)
    H = small.shape[0]
    ya, yb = int(H * y_range[0]), int(H * y_range[1])
    reg = small[ya:yb]
    Hr, Wr = reg.shape[:2]
    F = [np.fft.rfft2(reg[..., c]) for c in range(3)]
    cS = np.pad(reg.sum(2).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    cSS = np.pad((reg ** 2).sum(2).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    best = None
    for i, T in enumerate(tpls):
        if 'half' not in T:
            T['half'] = half(T['t'])
        Gs, h, w, tn = _tpl_fft(T, (Hr, Wr))
        if h > Hr or w > Wr or tn < 1e-6:
            continue
        num = np.fft.irfft2(F[0] * Gs[0] + F[1] * Gs[1] + F[2] * Gs[2], (Hr, Wr))[:Hr - h + 1, :Wr - w + 1]
        n = h * w * 3
        s1 = cS[h:, w:] - cS[:-h, w:] - cS[h:, :-w] + cS[:-h, :-w]
        s2 = cSS[h:, w:] - cSS[:-h, w:] - cSS[h:, :-w] + cSS[:-h, :-w]
        m = num / (np.sqrt(np.maximum(s2 - s1 * s1 / n, 1e-6)) * tn)
        y, x = np.unravel_index(int(np.argmax(m)), m.shape)
        sc = float(m[y, x])
        if best is None or sc > best[0]:
            best = (sc, x, y + ya, i)
    if best is None:
        return None
    _, x, y, i = best
    T = tpls[i]
    big = as_arr(im, NW)
    bs, bx, by = -2.0, 2 * x, 2 * y
    for dy in range(-4, 5):
        for dx in range(-4, 5):
            sc = ncc_at(big, T['t'], 2 * x + dx, 2 * y + dy)
            if sc > bs:
                bs, bx, by = sc, 2 * x + dx, 2 * y + dy
    foot = ((bx + T['fx']) / NW, (by + T['fy']) / NW)
    # 第二道检查：小人一定戴红色毛线帽。比对分数会因为姿势（背面 / 侧面）掉到 0.4 左右，
    # 但位置其实是对的；这时看找到的位置上方有没有帽子的红色。
    hat = hat_red(small, foot)
    ok = bs >= 0.55 or (bs >= 0.30 and 0.06 <= hat <= 0.22)
    lost = not ok and (bs < 0.30 or hat < 0.03)     # 真的不见了（游戏结束、跳出奖励画面…）
    return {'foot': foot, 'score': bs, 'hat': hat, 'ok': ok, 'lost': lost}


def hat_red(small, foot):
    """小人脚底上方（帽子那一带）红色毛线帽像素的比例。small：宽 270 的影像。"""
    Wn = small.shape[1]
    fx, fy = foot[0] * Wn, foot[1] * Wn
    box = small[max(0, int(fy - 0.30 * Wn)):max(0, int(fy - 0.10 * Wn)), max(0, int(fx - 0.08 * Wn)):int(fx + 0.08 * Wn)]
    if not box.size:
        return 0.0
    r, g, b = box[..., 0], box[..., 1], box[..., 2]
    return float(((r > 140) & (g < 90) & (b < 90) & (r - g > 80)).mean())


# ================================================================ 目标
def _open(m):
    """3×3 开运算：去掉雪花、小点之类的细东西。"""
    p = np.pad(m, 1)
    e = np.ones_like(m)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            e &= p[dy:dy + m.shape[0], dx:dx + m.shape[1]]
    p = np.pad(e, 1)
    d = np.zeros_like(m)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            d |= p[dy:dy + m.shape[0], dx:dx + m.shape[1]]
    return d


def _label(m):
    H, W = m.shape
    lab = np.zeros((H, W), np.int32)
    sizes = [0]
    n = 0
    ys, xs = np.nonzero(m)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if lab[y0, x0]:
            continue
        n += 1
        cnt = 0
        dq = deque([(y0, x0)])
        lab[y0, x0] = n
        while dq:
            y, x = dq.popleft()
            cnt += 1
            for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= yy < H and 0 <= xx < W and m[yy, xx] and not lab[yy, xx]:
                    lab[yy, xx] = n
                    dq.append((yy, xx))
        sizes.append(cnt)
    return lab, sizes


def _dilate(m):
    p = np.pad(m, 1)
    d = np.zeros_like(m)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            d |= p[dy:dy + m.shape[0], dx:dx + m.shape[1]]
    return d


def find_face(img, fx, fy, angle_deg, Wn):
    """用顶面的奶油色找目标：几乎每个方块的顶面都是同一种奶油色 (≈250,237,205)。
    沿跳跃线往外走，第一块「不是脚下那块」的奶油色顶面，它的中心就是目标中心。"""
    Hn = img.shape[0]
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    cream = (r > 225) & (g > 205) & (b > 165) & (r - b > 18) & (r - b < 80) & (r - g > 3) & (r - g < 30)
    cream[:int(0.25 * Hn)] = False                               # 上面是分数、雪山
    cream[int(fy + 0.08 * Wn):] = False                          # 脚下以下是之前的方块
    lab, sizes = _label(_dilate(cream))                          # 稍微膨胀，把顶面上细线分开的部分接起来
    cur = set()                                                  # 脚下那块顶面（可能被小人挡成两半）
    ya, yb = max(0, int(fy - 0.07 * Wn)), int(fy + 0.06 * Wn)
    xa, xb = max(0, int(fx - 0.12 * Wn)), int(fx + 0.12 * Wn)
    cur.update(int(v) for v in np.unique(lab[ya:yb, xa:xb]) if v)
    t = math.radians(angle_deg)
    min_area = 0.003 * Wn * Wn
    shape_ok = {}

    def is_face(L):                                              # 顶面（菱形/椭圆）高÷宽约 0.47~0.6；底座那圈是弯的扁带子
        if L not in shape_ok:
            ys, xs = np.nonzero(lab == L)
            w, h = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
            shape_ok[L] = h / w >= 0.38 and w >= 0.12 * Wn
        return shape_ok[L]

    found = []
    for side in (1, -1):
        ux, uy = side * math.cos(t), -math.sin(t)
        first, best = None, None
        for s in range(2, int(1.3 * Wn)):
            if first is not None and s > first + 0.25 * Wn:
                break
            x, y = int(round(fx + ux * s)), int(round(fy + uy * s))
            if not (0 <= x < Wn and 0 <= y < Hn):
                break
            L = int(lab[y, x])
            if L and L not in cur and sizes[L] >= min_area and is_face(L):
                # 第一块碰到的可能是方块前面的装饰（例如炸鸡盘前面的荷包蛋，颜色跟顶面一样）；
                # 再往前看一小段，选最大的那一块才是真正的顶面
                if first is None:
                    first = s
                if best is None or sizes[L] > sizes[best[1]]:
                    best = (first, L)
        if best:
            found.append((best[0], side, best[1]))
    if not found:
        return None
    s, side, L = min(found)
    ys, xs = np.nonzero((lab == L) & cream)
    w = float(xs.max() - xs.min() + 1)
    if (ys.max() - ys.min() + 1) / w > 0.7:
        # 比正常顶面（高÷宽 ≈ 0.5）高很多：连到了同颜色的墙壁 → 用左右端点算中心
        cx, cy = map(float, face_center_extremes(ys, xs))
    else:
        cx, cy = float(xs.mean()), float(ys.mean())          # 完整的顶面：直接取重心
    if w < 0.12 * Wn or (cx - fx) * side <= 0 or cy >= fy:
        return None
    return {'center': (cx / Wn, cy / Wn), 'top': (cx / Wn, ys.min() / Wn), 'side': side, 'width': w / Wn,
            'method': 'face'}


def face_center_extremes(ys, xs):
    """顶面中心 = 最左、最右两点的中点。菱形 / 椭圆的左右端点都在中心高度；
    每一列取「最高」的那一点，这样下面同颜色的墙壁、被小人挡住的部分都不影响。"""
    xl, xr = xs.min(), xs.max()
    yl = ys[xs <= xl + 1].min()
    yr = ys[xs >= xr - 1].min()
    return (xl + xr) / 2, (yl + yr) / 2


def cream_mask(img):
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    return (r > 225) & (g > 205) & (b > 165) & (r - b > 18) & (r - b < 80) & (r - g > 3) & (r - g < 30)


def standing_face(im, foot):
    """小人现在站的那块顶面的中心（屏宽比例）。小人会挡住顶面中间，
    所以用顶面最左、最右两个点：菱形和椭圆的左右端点都在中心高度，中点就是中心。"""
    Wn = NW // 2
    img = as_arr(im, Wn)
    fx, fy = foot[0] * Wn, foot[1] * Wn
    cream = cream_mask(img)
    lab, sizes = _label(_dilate(cream))
    ya, yb = max(0, int(fy - 0.10 * Wn)), int(fy + 0.06 * Wn)
    xa, xb = max(0, int(fx - 0.15 * Wn)), int(fx + 0.15 * Wn)
    labs = [int(v) for v in np.unique(lab[ya:yb, xa:xb]) if v and sizes[int(v)] >= 0.001 * Wn * Wn]
    if not labs:
        return None                                    # 没有奶油色顶面（例如金币）
    m = np.isin(lab, labs) & cream
    ys, xs = np.nonzero(m)
    if xs.max() - xs.min() < 0.15 * Wn or (ys.max() - ys.min()) / (xs.max() - xs.min()) > 0.9:
        return None
    cx, cy = face_center_extremes(ys, xs)
    return {'center': (cx / Wn, cy / Wn), 'width': (xs.max() - xs.min()) / Wn}


def landing(im, tpls, jump, ch=None):
    """跳完之后量落点。jump = {'dir': (ux, uy) 跳跃方向单位向量, 'units': 原本要跳的距离}。
    回传 {'err': 沿跳跃方向偏了多少（+ 跳过头 / − 不够远，屏宽=1000）, 'traveled': 实际跳的距离}，量不到回传 None。"""
    ch = ch or find_char(im, tpls)
    if not ch or ch.get('lost'):
        return {'lost': True}                           # 找不到小人：可能掉下去了 / 游戏结束
    if not ch.get('ok'):
        return None                                     # 不太确定是不是小人：这跳不量
    face = standing_face(im, ch['foot'])
    if not face:
        return None
    ux, uy = jump['dir']
    dx = (ch['foot'][0] - face['center'][0]) * 1000
    dy = (ch['foot'][1] - face['center'][1]) * 1000
    err = dx * ux + dy * uy
    if abs(err) > face['width'] * 1000 * 0.7:           # 偏太多：可能不是站在这块上，不采用
        return None
    # 偏超过顶面一半：多半是掉下去了（小人倒在边上）。方向是对的，但量到的距离不可靠 → 只算到上限
    cap = face['width'] * 1000 * 0.45
    fell = abs(err) > cap
    used = max(-cap, min(cap, err))
    return {'err': used, 'raw_err': err, 'fell': fell, 'traveled': jump['units'] + used,
            'foot': [round(v, 4) for v in ch['foot']], 'face': [round(v, 4) for v in face['center']],
            'score': round(ch['score'], 3)}


def find_target(im, foot, angle_deg=30, diff_thr=45):
    """foot：屏宽比例。回传 {'center': (x, y) 屏宽比例, 'top', 'side', 'width'} 或 None。
    先用奶油色顶面找；找不到（例如金币）再用「物体外形」的方法。"""
    Wn = NW // 2
    img = as_arr(im, Wn)
    Hn = img.shape[0]
    fx, fy = foot[0] * Wn, foot[1] * Wn
    face = find_face(img, fx, fy, angle_deg, Wn)
    if face:
        return face
    y0 = int(0.18 * Hn)
    y1 = min(Hn, int(fy + 0.08 * Wn))
    if y1 - y0 < 20:
        return None
    reg = img[y0:y1]
    bg = np.median(reg, axis=1, keepdims=True)                  # 每一列的背景色（渐层背景）
    m = np.abs(reg - bg).sum(2) > diff_thr
    cx0, cx1 = int(fx - 0.11 * Wn), int(fx + 0.11 * Wn)         # 挖掉小人
    cy0, cy1 = int(fy - 0.32 * Wn) - y0, int(fy + 0.015 * Wn) - y0
    excl = np.zeros_like(m)
    excl[max(0, cy0):max(0, cy1), max(0, cx0):max(0, cx1)] = True
    m &= ~excl
    m = _open(m)
    lab, sizes = _label(m)

    # 小人脚下那个方块（包括它上面的装饰，例如椰子树）
    cur = set()
    for dy in (0.01, 0.03, 0.05):
        yy = int(fy + dy * Wn) - y0
        if 0 <= yy < lab.shape[0]:
            for dx in (-0.04, 0, 0.04):
                xx = int(fx + dx * Wn)
                if 0 <= xx < Wn and lab[yy, xx]:
                    cur.add(int(lab[yy, xx]))

    t = math.radians(angle_deg)
    min_size = 0.002 * Wn * Wn

    def face_center(L, side):
        """量这个物体的顶面：回传 (cx, cy, 顶点y, 顶面宽) 或 None。"""
        ys, xs = np.nonzero(lab == L)
        keep = (xs - fx) * side > 0.1 * Wn                      # 只看目标那一侧（可能跟脚下方块连在一起）
        ys, xs = ys[keep], xs[keep]
        if not len(ys):
            return None
        top = int(ys.min())
        tx = float(xs[ys <= top + 1].mean())
        # 从顶点往下，每一列取包含中心那一段的宽度。菱形 / 椭圆顶面越往下越宽，
        # 到顶面中心那一列之后就是侧面，宽度不再增加 → 第一个「宽度停止增加」的列就是中心高度。
        comp = lab == L
        xc = int(round(tx))
        widths = []
        for k in range(0, int(0.3 * Wn)):
            y = top + k
            if y >= comp.shape[0]:
                break
            row = comp[y]
            if not (0 <= xc < Wn) or not row[xc]:
                hits = np.nonzero(row[max(0, xc - 3):xc + 4])[0]
                if not len(hits):
                    break
                xc = max(0, xc - 3) + int(hits[len(hits) // 2])
            l = xc
            while l > 0 and row[l - 1]:
                l -= 1
            r = xc
            while r < Wn - 1 and row[r + 1]:
                r += 1
            widths.append(r - l + 1)
        kc = None
        for k in range(2, len(widths) - 4):
            if widths[k + 4] - widths[k] <= 2:
                kc = k
                break
        if kc is None:
            return None
        w = widths[kc]
        if w < 0.15 * Wn:                                      # 太窄：雪花、小装饰
            return None
        # 方块的顶面都是奶油色 / 白色 / 金色；椰子树、草是绿的，水是蓝的 → 不是方块
        yy, xx = top + kc - max(2, kc // 2), int(round(tx))   # 顶点和中心之间，一定在顶面上
        patch = img[y0 + yy - 1:y0 + yy + 2, max(0, xx - 2):xx + 3].reshape(-1, 3)
        if not len(patch):
            return None
        r, g, b = patch.mean(0)
        if not (r > 160 and g <= r + 10 and b <= r + 10):
            return None
        return tx, top + kc, top, w

    # 沿跳跃线往外走：先经过小人（挖掉的区域）和脚下的方块，看到一段真正的背景之后，
    # 碰到的第一个「像方块」的物体就是目标。太窄的（装饰）跳过继续找。
    # （两个方块在画面上常常连在一起，所以不能只靠「连通区块」来区分。）
    found = []
    for side in (1, -1):
        ux, uy = side * math.cos(t), -math.sin(t)
        gap = 0
        skip = set()
        for s in range(2, int(1.2 * Wn)):
            x, y = int(round(fx + ux * s)), int(round(fy + uy * s)) - y0
            if not (0 <= x < Wn and 0 <= y < lab.shape[0]):
                break
            if excl[y, x]:
                continue
            L = int(lab[y, x])
            if not L or L in skip:
                gap += 1
                continue
            if gap >= 2 and sizes[L] >= min_size and (L not in cur or s >= 0.25 * Wn):
                fc = face_center(L, side)
                if fc:
                    found.append((L in cur, s, side, fc))
                    break
                skip.add(L)
                gap = 0
    if not found:
        return None
    # 优先选「没有跟脚下方块连在一起」的那一个，再选比较近的
    _, s, side, (cx, cyl, top, w) = min(found, key=lambda f: (f[0], f[1]))
    ty, cy = top + y0, cyl + y0
    if not (ty < cy < ty + 0.4 * Wn) or (cx - fx) * side <= 0 or cy > fy:
        return None
    return {'center': (cx / Wn, cy / Wn), 'top': (cx / Wn, ty / Wn), 'side': side, 'width': w / Wn, 'method': 'shape'}


# ================================================================ 目标记忆
# 规则（奶油色顶面）偶尔会认错或认不出某种方块。你手动画线时，如果 AI 当时找不到目标、
# 或 AI 的终点跟你画的差很多，就把你画的终点附近那块方块记下来（含中心在哪里）。
# 之后遇到同一种方块，就用记忆比对。
MEM_W, MEM_H = 0.34, 0.26          # 记忆样板大小（屏宽比例），中心放在样板中间偏下一点
MEM_DISAGREE = 0.05                # AI 跟你画的终点差超过屏宽的 5% 才算「AI 不会」


def build_target_memory(store, max_n=20, scan=150):
    mem = []
    tw, th = round(MEM_W * NW), round(MEM_H * NW)
    for rec in reversed(store.recs):
        scan -= 1
        if scan <= 0 or len(mem) >= max_n:
            break
        if rec.get('auto'):
            continue
        p = rec.get('pred') or {}
        b = rec['b']
        if p.get('center') and math.hypot(p['center'][0] - b[0], p['center'][1] - b[1]) <= MEM_DISAGREE:
            continue                                   # AI 当时就对了，不用记
        try:
            img = as_arr(store.shot(rec), NW)
        except Exception:  # noqa: BLE001
            continue
        cx, cy = b[0] * NW, b[1] * NW
        x0, y0 = round(cx - tw / 2), round(cy - th * 0.55)
        if x0 < 0 or y0 < 0 or x0 + tw > img.shape[1] or y0 + th > img.shape[0]:
            continue
        crop = img[y0:y0 + th, x0:x0 + tw]
        if any(ncc_at(crop, M['t'], 0, 0) > 0.9 for M in mem):
            continue                                   # 这种方块已经记过了
        mem.append({'t': crop, 'half': half(crop), 'cx': cx - x0, 'cy': cy - y0, 'id': rec['id']})
    return mem


def find_target_memory(im, foot, mem, angle_deg=30):
    """在小人斜上方、靠近跳跃线的地方找记忆里的方块。回传 {'center', 'score', 'side'} 或 None。"""
    if not mem:
        return None
    small = as_arr(im, NW // 2)
    Wn = small.shape[1]
    fx, fy = foot[0] * Wn, foot[1] * Wn
    ya, yb = max(0, int(fy - 0.85 * Wn)), int(fy + 0.05 * Wn)
    reg = small[ya:yb]
    t = math.tan(math.radians(angle_deg))
    best = None
    for M in mem:
        m = ncc_map(reg, M['half'])
        if m is None:
            continue
        # 只接受中心落在跳跃线附近、在小人斜上方的位置
        hh, ww = m.shape
        ys, xs = np.mgrid[0:hh, 0:ww]
        ccx = xs + M['cx'] / 2
        ccy = ys + ya + M['cy'] / 2
        dx, dy = ccx - fx, fy - ccy
        ok = (np.abs(dx) >= 0.12 * Wn) & (dy > 0) & (np.abs(dy - np.abs(dx) * t) <= 0.06 * Wn + 0.12 * np.abs(dx) * t)
        if not ok.any():
            continue
        mm = np.where(ok, m, -2)
        y, x = np.unravel_index(int(np.argmax(mm)), mm.shape)
        sc = float(mm[y, x])
        if best is None or sc > best[0]:
            best = (sc, (ccx[y, x]) / Wn, (ccy[y, x]) / Wn, M['id'])
    if not best:
        return None
    sc, cx, cy, mid = best
    return {'center': (cx, cy), 'score': sc, 'side': 1 if cx > foot[0] else -1, 'mem_id': mid}


# ================================================================ 预测 / 统计
def shadow_stats(store, n=10):
    """最近 n 笔「你画线、AI 也有预测」的纪录：AI 距离 / 你的距离 的误差。"""
    rs = [r for r in store.recs if not r.get('auto') and r.get('pred') and r['units'] > 0][-n:]
    if not rs:
        return {'n': 0}
    errs = [(r['pred']['units'] - r['units']) / r['units'] for r in rs]
    ratio = float(np.median([r['units'] / r['pred']['units'] for r in rs if r['pred']['units'] > 0]))
    med = float(np.median(np.abs(errs)))
    return {'n': len(rs), 'med': med, 'ratio': ratio, 'ok': len(rs) >= MIN_SHADOW and med <= GOOD_ERR}


def learned_angle(store, n=20):
    """从你最近画的线学跳跃角度（中位数）；不够 5 条就回传 None。"""
    angs = []
    for r in store.recs[-60:]:
        if r.get('auto'):
            continue
        dx, dy = r['b'][0] - r['a'][0], r['b'][1] - r['a'][1]
        if dy > 0:
            dx, dy = -dx, -dy
        a = math.degrees(math.atan2(-dy, abs(dx)))
        if 15 <= a <= 45 and math.hypot(dx, dy) > 0.1:
            angs.append(a)
    angs = angs[-n:]
    return float(np.median(angs)) if len(angs) >= 5 else None


def predict(im, tpls, cfg, store, char_thr=0.55, ch=None, mem=None):
    """回传预测 dict（失败时 'err' 说明原因）。ch：已经找过的小人（省一次）。"""
    ch = ch or find_char(im, tpls)
    if not ch:
        return {'err': '还没有小人模板（先自己画线玩几跳）'}
    if not ch.get('ok'):
        return {'err': f'找不到小人（信心 {ch["score"]:.2f}）', 'score': ch['score']}
    ang = learned_angle(store) or cfg.get('jump_angle_deg', 30)
    tg = find_target(im, ch['foot'], ang)
    if mem:
        mm = find_target_memory(im, ch['foot'], mem, ang)
        if mm:
            far = (not tg) or math.hypot(mm['center'][0] - tg['center'][0], mm['center'][1] - tg['center'][1]) > 0.06
            # 规则找不到 → 记忆够像就用；规则找到但跟「你纠正过的那种方块」很像却不同位置 → 相信记忆
            if (not tg and mm['score'] >= 0.75) or (tg and far and mm['score'] >= 0.93):
                tg = {'center': mm['center'], 'top': mm['center'], 'side': mm['side'],
                      'width': (tg or {}).get('width', 0.3), 'method': 'memory', 'mem_score': mm['score']}
    if not tg:
        return {'err': '找不到目标方块', 'foot': ch['foot'], 'score': ch['score']}
    fx, fy = ch['foot']
    cx, cy = tg['center']
    dx, dy = cx - fx, cy - fy
    raw_units = math.hypot(dx, dy) * 1000
    if cfg.get('gui_proj', True):                 # 跟画线时的算法一样：投影到比较接近的跳跃方向
        t = math.radians(cfg.get('jump_angle_deg', 30))
        raw_units = max(abs(dx * math.cos(t) - dy * math.sin(t)), abs(-dx * math.cos(t) - dy * math.sin(t))) * 1000
    st = shadow_stats(store)
    # 依你的画线习惯修正比例；一旦有足够的落点资料（真正的物理结果），就不需要了
    corr = st['ratio'] if st['n'] >= 5 and land_count(store) < MIN_LAND else 1.0
    units = raw_units * corr
    ms = ms_for(units, tg['side'], fy, cfg)
    return {'foot': ch['foot'], 'center': tg['center'], 'top': tg['top'], 'side': tg['side'],
            'score': ch['score'], 'raw_units': raw_units, 'units': units, 'corr': corr, 'ms': ms, 'angle': ang,
            'width': tg.get('width', 0), 'method': tg.get('method', 'face'), 'mem_score': tg.get('mem_score')}


MIN_LAND = 6


def land_count(store):
    return sum(1 for r in store.recs if r.get('land'))


def _jump_side(r):
    return 1.0 if (r['b'][0] - r['a'][0]) > 0 else -1.0


def ms_for(units, side, foot_y, cfg):
    """按压时间 = k × 距离 + b（+ 左右方向修正 + 小人高度修正，自动校准学到才有）。回传整数 ms。"""
    ms = (units * cfg['k'] + cfg['b'] + cfg.get('k_side', 0) * side
          + cfg.get('k_foot', 0) * (foot_y - cfg.get('foot0', foot_y)))
    return int(round(max(cfg.get('min_ms', 150), min(cfg.get('max_ms', 2500), ms))))


def _robust_lstsq(X, y, min_keep):
    keep = np.ones(len(y), bool)
    w = None
    for _ in range(2):                                   # 去掉离群的（例如量错的那几跳）再算一次
        w, *_ = np.linalg.lstsq(X[keep], y[keep], rcond=None)
        res = np.abs(X @ w - y)
        mad = np.median(res[keep]) + 1e-6
        keep = res <= max(3 * mad, 15)
        if keep.sum() < min_keep:
            return None, None
    return w, keep


K_RANGE, B_RANGE = (0.6, 2.0), (-50.0, 450.0)     # k、b 的合理范围
K_DEFAULT, B_DEFAULT = 1.0, 200                     # 设定不合理时重设的值（之前几次资料大约 k≈1.0、b≈50~270）


def sane_calibration(cfg):
    """k、b 在合理范围内吗？（学坏的例子：k = 0.30、b = 524，等于不管多远都按差不多久）"""
    return K_RANGE[0] <= cfg.get('k', 0) <= K_RANGE[1] and B_RANGE[0] <= cfg.get('b', 0) <= B_RANGE[1]
MS_SPREAD_FOR_B = 150                                # 按压时间至少要有这么大的差别，才重新估 b


def _land_recs(store, cfg, n):
    since = (cfg or {}).get('calib_since', 0) or 0   # 「调整了装置」之后的资料才算
    return [r for r in store.recs if r.get('land') and (r.get('t_press') or 0) >= since][-n:]


def fit_land(store, n=80, cfg=None):
    """用量到的落点拟合按压时间。
    注意方向：按压时间是程序精确控制的（没有误差），误差都在「实际跳了多远」，
    所以要拟合「跳多远 = a × ms + c」再换算回 ms = k × 距离 + b（k = 1/a）。
    反过来拟合会让 k 系统性偏小、越学越歪。
    按压时间的范围不够大时，b 估不准：这时只估 k、b 沿用目前的值。
    资料够多时，再试「左右方向修正」和「小人高度修正」，用一半资料学、另一半测，真的比较准才采用。"""
    cfg = cfg or {}
    rs = _land_recs(store, cfg, n)
    if len(rs) < MIN_LAND:
        return None
    t = np.array([r['land']['traveled'] for r in rs], float)
    ms = np.array([r['ms'] for r in rs], float)
    side = np.array([_jump_side(r) for r in rs])
    fy = np.array([r['a'][1] for r in rs], float)
    f0 = float(np.median(fy))
    if ms.max() - ms.min() < MS_SPREAD_FOR_B:
        b = float(cfg.get('b', 150))
        u = ms - b - cfg.get('k_side', 0) * side - cfg.get('k_foot', 0) * (fy - cfg.get('foot0', f0))
        keep = np.ones(len(t), bool)
        for _ in range(2):
            a = float((u[keep] * t[keep]).sum() / max(1e-6, (u[keep] ** 2).sum()))
            res = np.abs(a * u - t)
            keep = res <= max(3 * np.median(res[keep]), 15)
        if a <= 0:
            return None
        k = 1.0 / a
        rms_t = float(np.sqrt(((a * u[keep] - t[keep]) ** 2).mean()))
        return {'k': k, 'b': b, 'k_side': cfg.get('k_side', 0.0), 'k_foot': cfg.get('k_foot', 0.0),
                'foot0': cfg.get('foot0', f0), 'terms': 'k-only', 'rms': rms_t * k, 'cv': rms_t * k,
                'n': int(keep.sum())}

    cols = {'base': [ms, np.ones_like(ms)]}
    if len(rs) >= 30 and (side > 0).sum() >= 8 and (side < 0).sum() >= 8:
        cols['side'] = cols['base'] + [side]
        if len(rs) >= 40:
            cols['side+foot'] = cols['side'] + [fy - f0]

    def cv_rms(c):
        X = np.vstack(c).T
        idx = np.arange(len(t))
        errs = []
        for part in (idx % 2 == 0, idx % 2 == 1):
            w, _ = _robust_lstsq(X[~part], t[~part], len(c) + 2)
            if w is None:
                return 1e9
            errs.append(np.abs(X[part] @ w - t[part]))
        e = np.concatenate(errs)
        e = e[e <= max(3 * np.median(e), 15)]
        return float(np.sqrt((e ** 2).mean()))

    best, best_cv = 'base', cv_rms(cols['base'])
    for name in ('side', 'side+foot'):
        if name in cols:
            cv = cv_rms(cols[name])
            if cv < best_cv * 0.95:
                best, best_cv = name, cv
    X = np.vstack(cols[best]).T
    w, keep = _robust_lstsq(X, t, MIN_LAND)
    if w is None or w[0] <= 0:
        return None
    a, c = float(w[0]), float(w[1])
    k, b = 1.0 / a, -c / a
    rms_t = float(np.sqrt(((X[keep] @ w - t[keep]) ** 2).mean()))
    return {'k': k, 'b': b, 'k_side': -float(w[2]) / a if len(w) > 2 else 0.0,
            'k_foot': -float(w[3]) / a if len(w) > 3 else 0.0, 'foot0': f0, 'terms': best,
            'rms': rms_t * k, 'cv': best_cv * k, 'n': int(keep.sum())}


def calibration_update(store, cfg, rec):
    """跳完量到落点后，算新的 k、b（含合理范围和每次最多变多少）。回传 (新参数 dict, 说明) 或 (None, '')。"""
    f = fit_land(store, cfg=cfg)
    fresh = len(_land_recs(store, cfg, 999)) < 15           # 刚重新校准：允许变化大一点
    if f and f['rms'] < (60 if fresh else 30):           # 刚重新校准时资料比较乱，也先用（下面有每次变动上限）
        new = {'k': f['k'], 'b': f['b'], 'k_side': f['k_side'], 'k_foot': f['k_foot'], 'foot0': f['foot0']}
        terms = {'base': '', 'side': ' + 左右修正', 'side+foot': ' + 左右修正 + 高度修正',
                 'k-only': '（只调 k）'}[f['terms']]
        how = f'（{f["n"]} 跳拟合{terms}，误差 ±{f["rms"]:.0f} ms）'
    else:
        traveled = rec['land']['traveled']
        if traveled < 50:
            return None, ''
        corr = (cfg.get('k_side', 0) * _jump_side(rec) + cfg.get('k_foot', 0) * (rec['a'][1] - cfg.get('foot0', rec['a'][1])))
        miss = rec['ms'] - (cfg['k'] * traveled + cfg['b'] + corr)       # 这跳多按（+）或少按（−）了几 ms
        b_try = cfg['b'] + 0.7 * miss
        if fresh and B_RANGE[0] <= b_try <= B_RANGE[1]:
            # 刚调整过装置：主要变的是触控笔的行程时间（b），先修 b，资料够了再一起拟合 k
            new = {'k': cfg['k'], 'b': b_try}
            how = '（单跳修正 b）'
        else:                                            # b 已经到上下限（或资料够多了）：改修 k
            k_true = (rec['ms'] - cfg['b'] - corr) / traveled
            new = {'k': cfg['k'] + 0.6 * (k_true - cfg['k'])}
            how = '（单跳修正）'
    # 合理范围 + 每次最多变多少（避免一次量错或资料太少就大幅偏掉）
    step = 0.35 if fresh else 0.15
    k_old = cfg['k']
    new['k'] = float(min(K_RANGE[1], max(K_RANGE[0], min(k_old * (1 + step), max(k_old * (1 - step), new['k'])))))
    if 'b' in new:
        b_old = cfg['b']
        db = 120 if fresh else 40
        new['b'] = float(min(B_RANGE[1], max(B_RANGE[0], min(b_old + db, max(b_old - db, new['b'])))))
    new = {key: (round(v, 4) if key in ('k', 'foot0') else round(v, 1) if key in ('k_side', 'k_foot') else round(v))
           for key, v in new.items()}
    return new, how


def bias_alarm(store, cfg, n=5, thr=0.05):
    """最近 n 跳的落点都往同一边偏超过 thr：装置可能变了（例如换了按压角度）。
    触发后要再累积 10 跳以上才会再触发（不然会一直清掉资料、永远学不起来）。"""
    if len(_land_recs(store, cfg, 999)) < 10:
        return False
    rs = _land_recs(store, cfg, n)
    if len(rs) < n:
        return False
    pct = [(r['land']['err'] - (r.get('aim') or 0)) / max(1.0, r['land']['traveled'] - r['land']['err']) for r in rs]
    return all(p > thr for p in pct) or all(p < -thr for p in pct)


def land_stats(store, n=10):
    rs = [r for r in store.recs if r.get('land')][-n:]
    if not rs:
        return None
    # 故意不跳正中的那几跳：扣掉故意偏的量，只算「没跳到想跳的地方」的误差
    errs = [abs(r['land']['err'] - r.get('aim', 0)) / max(1.0, r['land']['traveled'] - r['land']['err']) for r in rs]
    return {'n': len(rs), 'mean': float(np.mean(errs)), 'last': rs[-1]['land']['err'] - rs[-1].get('aim', 0)}


def fit_kb(store, proj=True):
    rs = [r for r in store.recs if r.get('r') is not None and r.get('est') and r['units'] > 0]
    n = len(rs)
    cnt = {-1: 0, 0: 0, 1: 0}
    for r in rs:
        cnt[r['r']] = cnt.get(r['r'], 0) + 1
    if n < 8:
        return {'n': n, 'cnt': cnt}
    x = np.array([r['units'] for r in rs], float)
    y = np.array([r['est'] for r in rs], float)
    if x.max() - x.min() < 100:                 # 距离都差不多，算不出斜率
        return {'n': n, 'cnt': cnt, 'bad': True}
    den = n * (x * x).sum() - x.sum() ** 2
    k = (n * (x * y).sum() - x.sum() * y.sum()) / den
    b = (y.sum() - k * x.sum()) / n
    rms = float(np.sqrt(((x * k + b - y) ** 2).mean()))
    if k <= 0:
        return {'n': n, 'cnt': cnt, 'bad': True}
    return {'n': n, 'cnt': cnt, 'k': float(k), 'b': float(b), 'rms': rms}


# ================================================================ 导出
def export(store, cfg, extra_info=None):
    """把学习纪录、设定、操作纪录、截图打包成一个 zip（pc/export/）。回传 zip 路径。"""
    os.makedirs(EXPORT, exist_ok=True)
    name = 'servo_export_' + time.strftime('%Y%m%d-%H%M%S') + '.zip'
    path = os.path.join(EXPORT, name)
    recs = store.recs

    # 每一跳一行，Excel 可以直接打开（utf-8-sig 让 Excel 认得中文）
    rows = [['#', 'id', '时间', '谁', '距离', '按压ms', '当时k', '当时b', 'AI预测距离', 'AI小人信心',
             '落点偏差', '落点偏差%', '实际跳的距离', '结果(-1跳太远 0刚好 1跳太近)', '按下时间', 'ESP回报按完时间',
             '故意偏离', '按之前的分数', '故意结束']]
    for i, r in enumerate(recs, 1):
        land = r.get('land') or {}
        units = (land.get('traveled', 0) - land.get('err', 0)) if land else 0
        pct = f'{land["err"] / units * 100:+.2f}' if land and units > 0 else ''
        pred = r.get('pred') or {}
        rows.append([i, r['id'], r['id'][:15], 'AI' if r.get('auto') else '你', r['units'], r['ms'],
                     r.get('k_used', ''), r.get('b_used', ''), pred.get('units', ''), pred.get('score', ''),
                     land.get('err', ''), pct, land.get('traveled', ''), '' if r.get('r') is None else r['r'],
                     r.get('t_press', ''), r.get('t_done', ''), r.get('aim', ''), r.get('score', ''),
                     'Y' if r.get('end_game') else ''])
    lc = land_count(store)
    sh = shadow_stats(store)
    ls = land_stats(store)
    fl = fit_land(store)
    summary = [
        f'导出时间：{time.strftime("%Y-%m-%d %H:%M:%S")}',
        f'纪录：{len(recs)} 跳（你画的 {sum(1 for r in recs if not r.get("auto"))}，AI {sum(1 for r in recs if r.get("auto"))}）',
        f'量到落点：{lc} 跳' + (f'，最近 {ls["n"]} 跳平均偏 {ls["mean"] * 100:.2f}%' if ls else ''),
        f'AI vs 你：' + (f'最近 {sh["n"]} 跳中位误差 {sh["med"] * 100:.2f}%' if sh.get('n') else '没有资料'),
        f'目前 k = {cfg.get("k")}, b = {cfg.get("b")}, 跳跃角度 {cfg.get("jump_angle_deg")}°, 投影 {cfg.get("gui_proj")}',
        '落点拟合：' + (f'k = {fl["k"]:.4f}, b = {fl["b"]:.1f}, 左右修正 {fl["k_side"]:+.1f} ms, 高度修正 {fl["k_foot"]:+.1f}'
                       f'（{fl["terms"]}，{fl["n"]} 跳，±{fl["rms"]:.1f} ms，另一半资料测 ±{fl["cv"]:.1f} ms）' if fl else '资料不够'),
        f'学到的跳跃角度：{learned_angle(store)}',
        f'电脑：{platform.platform()}，Python {platform.python_version()}',
    ] + [f'{k}：{v}' for k, v in (extra_info or {}).items()]

    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('summary.txt', '\n'.join(summary))
        z.writestr('jump_config.json', json.dumps(cfg, ensure_ascii=False, indent=2))
        z.writestr('log.json', json.dumps(recs, ensure_ascii=False, indent=1))
        buf = __import__('io').StringIO()
        csv.writer(buf).writerows(rows)
        z.writestr('jumps.csv', '\ufeff' + buf.getvalue())
        if os.path.exists(EVENTS):
            z.write(EVENTS, 'events.csv')
        for r in recs:
            for suffix in ('.jpg', '_after.jpg'):
                f = os.path.join(SHOTS, r['id'] + suffix)
                if os.path.exists(f):
                    z.write(f, 'shots/' + r['id'] + suffix, compress_type=zipfile.ZIP_STORED)
    return path


# ================================================================ 读分数（画面上方的白色大数字）
_DIGITS = None


def _digit_templates():
    global _DIGITS
    if _DIGITS is None:
        try:
            with open(os.path.join(HERE, 'digits.json'), encoding='utf-8') as f:
                raw = json.load(f)
            _DIGITS = [(ch, np.array([[c == '#' for c in row] for row in t], float))
                       for ch, ts in raw.items() for t in ts]
        except Exception:  # noqa: BLE001
            _DIGITS = []
    return _DIGITS


def read_score(im):
    """读画面上方的分数（白色大数字）。读不到回传 None。数字样板来自你的截图（pc/digits.json）。"""
    T = _digit_templates()
    if not T:
        return None
    W0 = 540
    a = as_arr(im, W0)
    H = a.shape[0]
    y0, y1 = int(0.105 * H), int(0.215 * H)
    x0, x1 = int(0.18 * W0), int(0.90 * W0)
    m = a[y0:y1, x0:x1].min(2) > 235                      # 纯白的像素
    lab, sizes = _label(m)
    glyphs = []
    for L in range(1, len(sizes)):
        ys, xs = np.nonzero(lab == L)
        h, w = ys.max() - ys.min() + 1, xs.max() - xs.min() + 1
        if 0.10 * W0 <= h <= 0.145 * W0 and w <= 0.11 * W0:   # 大数字的高度；排除标签上的小白字
            glyphs.append((xs.min(), ys.min(), h, w, (lab == L)[ys.min():ys.max() + 1, xs.min():xs.max() + 1]))
    if not glyphs or len(glyphs) > 6:
        return None
    glyphs.sort(key=lambda g: g[0])
    hmax = max(g[2] for g in glyphs)
    if any(g[2] < 0.85 * hmax for g in glyphs):
        return None
    out = ''
    for _, _, _, _, g in glyphs:
        gi = np.asarray(Image.fromarray((g * 255).astype(np.uint8)).resize((16, 24), Image.BILINEAR)) > 127
        d = [(((gi - t) ** 2).mean(), ch) for ch, t in T]
        d.sort()
        if d[0][0] > 0.12:                                   # 不像任何数字
            return None
        out += d[0][1]
    return int(out)
