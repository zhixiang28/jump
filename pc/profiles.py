# -*- coding: utf-8 -*-
"""
每支手机一份设定（servo 角度、k/b 校准、瞄准修正、学习纪录…）

用 adb 的序号认手机：插哪一支，就自动用那一支的设定。两支同时插着时会跳出视窗让你选。
资料放在 pc/profiles/<手机>/：jump_config.json 和 data/（跟以前 pc/ 底下的一样）。

- 第一次用：原本 pc/jump_config.json、pc/data/ 会复制成「现在插着的那支手机」的设定（原档不动，当备份）。
- 第二支手机第一次插：从上一支手机复制一般设定和你画的那几跳（AI 马上认得小人），
  k/b 先沿用当起点、但会用这支手机的落点重新校准（calib_since = 现在）。servo 角度请自己调好再「保存到板子」。
"""
import json
import os
import re
import shutil
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(HERE, 'profiles')
INDEX = os.path.join(PROFILES, 'index.json')
LEGACY_CONFIG = os.path.join(HERE, 'jump_config.json')
LEGACY_DATA = os.path.join(HERE, 'data')
# 只跟手机有关、不要从别支手机复制过来的（校准相关的会在 new_profile 里另外处理）
_PER_PHONE = ('ble_addr',)


def list_devices(adb):
    """回传 [{'serial', 'state', 'model'}]（adb devices -l）。adb 有问题回传 []。"""
    try:
        out = subprocess.run([adb, 'devices', '-l'], capture_output=True, text=True, errors='replace',
                             timeout=15).stdout
    except Exception:  # noqa: BLE001
        return []
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        info = dict(p.split(':', 1) for p in parts[2:] if ':' in p)
        devs.append({'serial': parts[0], 'state': parts[1], 'model': info.get('model', '').replace('_', ' ')})
    return devs


def _safe(serial):
    return re.sub(r'[^A-Za-z0-9._-]', '_', serial)[:60] or 'phone'


def load_index():
    try:
        with open(INDEX, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_index(idx):
    os.makedirs(PROFILES, exist_ok=True)
    tmp = INDEX + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    os.replace(tmp, INDEX)


def profile_dir(serial):
    return os.path.join(PROFILES, _safe(serial))


def display_name(serial, model):
    return f'{model or "手机"}（…{serial[-4:]}）'


def _copy_manual_records(src_data, dst_data):
    """把你画的那几跳（小人模板、目标记忆的来源）连截图复制过去。"""
    try:
        with open(os.path.join(src_data, 'log.json'), encoding='utf-8') as f:
            recs = [r for r in json.load(f) if isinstance(r, dict) and not r.get('auto')]
    except Exception:  # noqa: BLE001
        return 0
    os.makedirs(os.path.join(dst_data, 'shots'), exist_ok=True)
    for r in recs:
        for suffix in ('.jpg', '_after.jpg'):
            src = os.path.join(src_data, 'shots', r['id'] + suffix)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dst_data, 'shots', r['id'] + suffix))
    with open(os.path.join(dst_data, 'log.json'), 'w', encoding='utf-8') as f:
        json.dump(recs, f, ensure_ascii=False)
    return len(recs)


def new_profile(serial, idx):
    """建立这支手机的设定资料夹。回传说明文字。"""
    d = profile_dir(serial)
    os.makedirs(d, exist_ok=True)
    if not idx and os.path.exists(LEGACY_CONFIG):
        # 第一次用：原本的设定和学习纪录就是这支手机的
        shutil.copy2(LEGACY_CONFIG, os.path.join(d, 'jump_config.json'))
        if os.path.isdir(LEGACY_DATA):
            shutil.copytree(LEGACY_DATA, os.path.join(d, 'data'), dirs_exist_ok=True)
        return '沿用原本的设定和学习纪录'
    src = None
    if idx:                                              # 从最近用过的那支手机复制起点
        last = max(idx, key=lambda s: idx[s].get('last', 0))
        src = profile_dir(last)
    elif os.path.exists(LEGACY_CONFIG):
        src = HERE
    if not src:
        return '全新的设定'
    try:
        with open(os.path.join(src, 'jump_config.json'), encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:  # noqa: BLE001
        return '全新的设定'
    for k in _PER_PHONE:
        cfg.pop(k, None)
    cfg['calib_since'] = time.time()                     # 只用这支手机的落点校准
    with open(os.path.join(d, 'jump_config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    n = _copy_manual_records(os.path.join(src, 'data'), os.path.join(d, 'data'))
    return f'从另一支手机复制设定当起点（含你画的 {n} 跳），会用这支手机重新校准；servo 角度请调好再保存到板子'


def activate(serial, model):
    """切换到这支手机的设定：回传 (资料夹, 显示名称, 说明)。"""
    idx = load_index()
    note = ''
    if serial not in idx or not os.path.exists(os.path.join(profile_dir(serial), 'jump_config.json')):
        note = new_profile(serial, idx)
    entry = idx.get(serial, {})
    entry.update(model=model or entry.get('model', ''), last=time.time())
    idx[serial] = entry
    save_index(idx)
    os.environ['ANDROID_SERIAL'] = serial                # 之后所有 adb 指令都只对这支手机
    return profile_dir(serial), display_name(serial, entry['model']), note


def last_used():
    """最近用过的手机 (序号, 型号)；没有回传 (None, None)。"""
    idx = load_index()
    if not idx:
        return None, None
    s = max(idx, key=lambda k: idx[k].get('last', 0))
    return s, idx[s].get('model', '')
