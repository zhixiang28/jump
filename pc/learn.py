# -*- coding: utf-8 -*-
"""
从你玩的纪录学习（给 jump_gui.py 用）

1. 小人：你画线的起点 = 小人脚底。从那些截图切出小人当模板，用彩色 NCC 模板匹配找小人。
2. 目标：从小人沿 ±30° 跳跃线往外找第一个「不是背景、不是目前方块」的物体，
         取它的最高点当方块顶点，中心就在跳跃线上、跟顶点同一个 x。
3. 按压：每一跳记 距离 / ms / 结果（太久·刚好·太短），用最小二乘法拟合 k、b。
4. 影子模式：你自己画线时，AI 也同时预测，记下误差；误差够小才建议让它自己玩。

所有座标都用「手机屏幕宽 = 1」的比例（x/W, y/W），所以跟截图解析度无关。
纪录存在 pc/data/（log.json + 缩小的截图）。
"""
import json
import math
import os
import time
from collections import deque

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
SHOTS = os.path.join(DATA, 'shots')
LOG = os.path.join(DATA, 'log.json')

NW = 540            # 学习 / 比对用的截图宽度
MAX_RECS = 300
TPL_W, TPL_H, TPL_FOOT = 0.18, 0.28, 0.01   # 小人模板大小（屏宽比例）；脚底下面多留一点
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

    def add(self, im, a, b, units, ms, pred=None, auto=False):
        """a, b：截图像素座标。回传这笔纪录。"""
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
               'pred': ({'units': round(pred['units'], 1), 'score': round(pred['score'], 3)} if pred else None)}
        self.recs.append(rec)
        while len(self.recs) > MAX_RECS:
            old = self.recs.pop(0)
            try:
                os.remove(os.path.join(SHOTS, old['id'] + '.jpg'))
            except OSError:
                pass
        self.save()
        return rec

    def set_result(self, rec, r, est):
        if rec is None:
            return
        rec['r'] = r
        rec['est'] = int(round(est))
        self.save()

    def clear(self):
        for r in self.recs:
            try:
                os.remove(os.path.join(SHOTS, r['id'] + '.jpg'))
            except OSError:
                pass
        self.recs = []
        self.save()

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
def build_templates(store, max_n=6):
    """从最近几笔「你自己画的线」切小人模板（含左右镜像）。"""
    tpls = []
    tw, th = round(TPL_W * NW), round(TPL_H * NW)
    for rec in reversed(store.recs):
        if rec.get('auto'):
            continue                       # 只信你自己画的起点，避免越学越歪
        try:
            img = as_arr(store.shot(rec), NW)
        except Exception:  # noqa: BLE001
            continue
        fx, fy = rec['a'][0] * NW, rec['a'][1] * NW
        x0 = round(fx - tw / 2)
        y1 = round(fy + TPL_FOOT * NW)
        y0 = y1 - th
        if x0 < 0 or y0 < 0 or x0 + tw > img.shape[1] or y1 > img.shape[0]:
            continue
        crop = img[y0:y1, x0:x0 + tw]
        tpls.append({'t': crop, 'fx': fx - x0, 'fy': fy - y0})
        tpls.append({'t': crop[:, ::-1].copy(), 'fx': tw - (fx - x0), 'fy': fy - y0})
        if len(tpls) >= max_n * 2:
            break
    return tpls


def half(arr):
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    return np.asarray(im.resize((max(1, arr.shape[1] // 2), max(1, arr.shape[0] // 2)), Image.BILINEAR),
                      dtype=np.float64)


def find_char(im, tpls):
    """回传 {'foot': (x, y) 屏宽比例, 'score'} 或 None。先在宽 270 找，再到 540 细调。"""
    if not tpls:
        return None
    small = as_arr(im, NW // 2)
    best = None
    for i, T in enumerate(tpls):
        m = ncc_map(small, half(T['t']))
        if m is None:
            continue
        y, x = np.unravel_index(int(np.argmax(m)), m.shape)
        sc = float(m[y, x])
        if best is None or sc > best[0]:
            best = (sc, x, y, i)
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
    return {'foot': ((bx + T['fx']) / NW, (by + T['fy']) / NW), 'score': bs}


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
        for s in range(2, int(1.3 * Wn)):
            x, y = int(round(fx + ux * s)), int(round(fy + uy * s))
            if not (0 <= x < Wn and 0 <= y < Hn):
                break
            L = int(lab[y, x])
            if L and L not in cur and sizes[L] >= min_area and is_face(L):
                found.append((s, side, L))
                break
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


def landing(im, tpls, jump):
    """跳完之后量落点。jump = {'dir': (ux, uy) 跳跃方向单位向量, 'units': 原本要跳的距离}。
    回传 {'err': 沿跳跃方向偏了多少（+ 跳过头 / − 不够远，屏宽=1000）, 'traveled': 实际跳的距离}，量不到回传 None。"""
    ch = find_char(im, tpls)
    if not ch or ch['score'] < 0.55:
        return {'lost': True}                           # 找不到小人：可能掉下去了
    face = standing_face(im, ch['foot'])
    if not face:
        return None
    ux, uy = jump['dir']
    dx = (ch['foot'][0] - face['center'][0]) * 1000
    dy = (ch['foot'][1] - face['center'][1]) * 1000
    err = dx * ux + dy * uy
    if abs(err) > face['width'] * 1000 * 0.7:           # 偏太多：可能不是站在这块上，不采用
        return None
    return {'err': err, 'traveled': jump['units'] + err}


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


def predict(im, tpls, cfg, store, char_thr=0.55):
    """回传预测 dict（失败时 'err' 说明原因）。"""
    ch = find_char(im, tpls)
    if not ch:
        return {'err': '还没有小人模板（先自己画线玩几跳）'}
    if ch['score'] < char_thr:
        return {'err': f'找不到小人（信心 {ch["score"]:.2f}）', 'score': ch['score']}
    ang = learned_angle(store) or cfg.get('jump_angle_deg', 30)
    tg = find_target(im, ch['foot'], ang)
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
    ms = units * cfg['k'] + cfg['b']
    ms = int(round(max(cfg['min_ms'], min(cfg['max_ms'], ms))))
    return {'foot': ch['foot'], 'center': tg['center'], 'top': tg['top'], 'side': tg['side'],
            'score': ch['score'], 'raw_units': raw_units, 'units': units, 'corr': corr, 'ms': ms, 'angle': ang}


MIN_LAND = 6


def land_count(store):
    return sum(1 for r in store.recs if r.get('land'))


def fit_land(store, n=40):
    """用量到的落点拟合：按了 ms → 实际跳了 traveled。ms = k × traveled + b。"""
    pts = [(r['land']['traveled'], r['ms']) for r in store.recs if r.get('land')][-n:]
    if len(pts) < MIN_LAND:
        return None
    x = np.array([p[0] for p in pts], float)
    y = np.array([p[1] for p in pts], float)
    if x.max() - x.min() < 80:
        return None
    keep = np.ones(len(x), bool)
    for _ in range(2):                                   # 去掉离群的（例如量错的那几跳）再算一次
        A = np.vstack([x[keep], np.ones(keep.sum())]).T
        (k, b), *_ = np.linalg.lstsq(A, y[keep], rcond=None)
        res = np.abs(x * k + b - y)
        mad = np.median(res[keep]) + 1e-6
        keep = res <= max(3 * mad, 15)
        if keep.sum() < MIN_LAND:
            return None
    rms = float(np.sqrt(((x[keep] * k + b - y[keep]) ** 2).mean()))
    if k <= 0:
        return None
    return {'k': float(k), 'b': float(b), 'rms': rms, 'n': int(keep.sum())}


def land_stats(store, n=10):
    rs = [r for r in store.recs if r.get('land')][-n:]
    if not rs:
        return None
    errs = [abs(r['land']['err']) / max(1.0, r['land']['traveled'] - r['land']['err']) for r in rs]
    return {'n': len(rs), 'mean': float(np.mean(errs)), 'last': rs[-1]['land']['err']}


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
