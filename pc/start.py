# -*- coding: utf-8 -*-
"""画线.bat 会跑这个：先自动更新（有新版本才下载），再打开程序。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)


def auto_update_enabled():
    try:
        with open(os.path.join(HERE, 'jump_config.json'), encoding='utf-8') as f:
            return json.load(f).get('auto_update', True)
    except Exception:  # noqa: BLE001
        return True


note = ''
try:
    import update
    if update.in_git_checkout():
        note = ''
    elif auto_update_enabled():
        print('检查更新中…')
        state, msg = update.update()
        print(msg)
        note = msg if state in ('updated', 'error') else ''
except Exception as e:  # noqa: BLE001
    print('自动更新出错，先用现在的版本：', e)

import jump_gui  # noqa: E402  （更新完才载入，用的就是新版）

jump_gui.main(update_note=note)
