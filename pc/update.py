# -*- coding: utf-8 -*-
"""
自动更新：从 GitHub 下载最新版的程序，只换掉程序档案。

不会动到你的东西：jump_config.json（设定、校准）、data\\（学习纪录）、export\\、platform-tools\\、
scrcpy-server、debug\\ 都不在 GitHub 上，更新时完全不碰。
被换掉的旧档会先备份到 _backup\\<时间>\\（只留最近 5 次）。

画线.bat 每次打开都会先跑这个（start.py）；网路不通就直接用现在的版本开。
也可以单独跑：python update.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = 'zhixiang28/jump'
BRANCH = 'main'
API = f'https://api.github.com/repos/{REPO}/commits/{BRANCH}'
ZIP = f'https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip'
VERSION_FILE = os.path.join(HERE, 'version.json')
BACKUP = os.path.join(HERE, '_backup')
KEEP_BACKUPS = 5
# 你的资料：就算 GitHub 上有同名档案也绝对不碰
PROTECTED = ('jump_config.json', 'version.json', 'data', 'export', 'debug', 'platform-tools', '_backup', 'profiles')
HEADERS = {'User-Agent': 'servo-press-updater'}


def _get(url, timeout):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
        return r.read()


def load_version():
    try:
        with open(VERSION_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _protected(rel):
    top = rel.replace('\\', '/').split('/')[0]
    return top in PROTECTED or top.startswith('scrcpy-server')


def _summary(msg):
    """合并纪录的第一行是「Merge pull request #23 from …」，第三行才是标题。"""
    lines = [ln.strip() for ln in (msg or '').splitlines() if ln.strip()]
    if lines and lines[0].startswith('Merge pull request') and len(lines) > 1:
        return lines[1]
    return lines[0] if lines else ''


def in_git_checkout():
    """在 git 资料夹里（开发用）不要自动更新，免得盖掉还没提交的修改。"""
    return os.path.exists(os.path.join(HERE, '..', '.git'))


def update(force=False, log=print):
    """检查并更新。回传 (状态, 说明)；状态：'updated' / 'up_to_date' / 'offline' / 'error'。"""
    local = load_version()
    try:
        info = json.loads(_get(API, timeout=6))
        sha, title = info['sha'], _summary(info.get('commit', {}).get('message', ''))
    except Exception as e:  # noqa: BLE001
        return 'offline', f'连不上 GitHub（{e}），先用现在的版本'
    if sha == local.get('sha') and not force:
        return 'up_to_date', f'已经是最新版（{sha[:7]}）'

    log(f'有新版本：{title or sha[:7]}，下载中…')
    try:
        z = zipfile.ZipFile(io.BytesIO(_get(ZIP, timeout=40)))
    except Exception as e:  # noqa: BLE001
        return 'offline', f'下载新版本失败（{e}），先用现在的版本'
    sha = z.comment.decode(errors='ignore').strip() or sha    # 下载到的那一版
    new = {}
    for name in z.namelist():
        parts = name.split('/', 2)                           # jump-main/pc/xxx
        if len(parts) < 3 or parts[1] != 'pc' or name.endswith('/') or not parts[2]:
            continue
        if not _protected(parts[2]):
            new[parts[2]] = z.read(name)
    if 'jump_gui.py' not in new or 'learn.py' not in new:
        return 'error', '下载的档案不完整，没有更新'

    stamp = time.strftime('%Y%m%d-%H%M%S')
    changed, removed = [], []

    def backup(rel):
        src = os.path.join(HERE, rel)
        if os.path.exists(src):
            dst = os.path.join(BACKUP, stamp, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

    old_req = _read(os.path.join(HERE, 'requirements.txt'))
    for rel, data in sorted(new.items()):
        path = os.path.join(HERE, rel)
        if _read(path) == data:
            continue
        backup(rel)
        os.makedirs(os.path.dirname(path) or HERE, exist_ok=True)
        tmp = path + '.new'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
        changed.append(rel)
    # 旧版有、新版拿掉的程序档案（只删以前由更新装上的，不会删你自己的档案）
    for rel in local.get('files', []):
        if rel not in new and not _protected(rel) and os.path.isfile(os.path.join(HERE, rel)):
            backup(rel)
            os.remove(os.path.join(HERE, rel))
            removed.append(rel)

    with open(VERSION_FILE, 'w', encoding='utf-8') as f:
        json.dump({'sha': sha, 'title': title, 'time': stamp, 'files': sorted(new)}, f, ensure_ascii=False, indent=2)
    _prune_backups()

    if new.get('requirements.txt') is not None and new['requirements.txt'] != old_req:
        log('需要的套件有变，安装中…（第一次可能要一两分钟）')
        try:
            subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', os.path.join(HERE, 'requirements.txt')],
                           check=False, timeout=900)
        except Exception as e:  # noqa: BLE001
            log(f'安装套件失败（{e}）：请手动双击 安装.bat')
    what = '、'.join(changed + [f'删除 {r}' for r in removed]) or '没有档案要换'
    return 'updated', f'已更新到最新版：{title or sha[:7]}（{what}）'


def _read(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except OSError:
        return None


def _prune_backups():
    try:
        olds = sorted(os.listdir(BACKUP))
        for d in olds[:-KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(BACKUP, d), ignore_errors=True)
    except OSError:
        pass


if __name__ == '__main__':
    state, msg = update(force='--force' in sys.argv)
    print(msg)
