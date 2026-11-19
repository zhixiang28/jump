# -*- coding: utf-8 -*-
"""
把导出的 zip 自动上传到你的 GitHub 私人仓库（例如 zhixiang28/jump-data），之后只要跟 Claude 说「看新数据」，
它就能直接去那里拿，不用再手动传档案。

需要你在 GitHub 做一次设定（程式里「☁ 自动上传」有步骤说明）：
  1. 建一个 Private（私人）仓库，例如 jump-data
  2. 建一个 Fine-grained token：只给这个仓库、权限 Contents = Read and write
  3. 把仓库名称和 token 填进程式（只存在你电脑的 jump_config.json，不会传给任何人）

用 GitHub 官方 API（PUT /repos/{仓库}/contents/{路径}）上传，档案放在仓库的 exports/ 资料夹。
"""
import base64
import json
import os
import urllib.error
import urllib.request

API = 'https://api.github.com'
MAX_BYTES = 45_000_000          # GitHub 这个 API 单档上限约 100 MB（base64 后），保守一点


def _req(method, url, token, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'servo-press-uploader'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b'{}')


def configured(cfg):
    return bool((cfg.get('upload_repo') or '').strip() and (cfg.get('upload_token') or '').strip())


def check(cfg, api=API):
    """测试设定：仓库存在、token 有写入权限、而且是私人仓库。回传 (ok, 说明)。"""
    repo, token = cfg.get('upload_repo', '').strip(), cfg.get('upload_token', '').strip()
    if not repo or '/' not in repo:
        return False, '仓库名称要写成「帐号/仓库」，例如 zhixiang28/jump-data'
    if not token:
        return False, '还没填 token'
    try:
        info = _req('GET', f'{api}/repos/{repo}', token, timeout=20)
    except urllib.error.HTTPError as e:
        return False, {401: 'token 不对或过期了', 403: 'token 没有这个仓库的权限',
                       404: '找不到这个仓库（名称打错，或 token 没给这个仓库的权限）'}.get(e.code, f'GitHub 回应 {e.code}')
    except Exception as e:  # noqa: BLE001
        return False, f'连不上 GitHub：{e}'
    if not info.get('private'):
        return False, '这个仓库是公开的（Public）：资料里有你的截图和电脑资讯，请改用私人（Private）仓库'
    if not (info.get('permissions') or {}).get('push', True):
        return False, 'token 没有写入权限：权限 Contents 要设成 Read and write'
    return True, f'设定 OK：会上传到 {repo}（私人仓库）'


def upload(path, cfg, api=API):
    """上传一个导出的 zip。回传 (ok, 说明)。"""
    if not configured(cfg):
        return False, '还没设定自动上传'
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        return False, f'档案太大（{size / 1e6:.0f} MB），没有上传：请取消「导出全部截图」'
    ok, msg = check(cfg, api)                     # 每次都确认：仓库还在、是私人的、token 有效
    if not ok:
        return False, msg
    repo, token = cfg['upload_repo'].strip(), cfg['upload_token'].strip()
    name = os.path.basename(path)
    with open(path, 'rb') as f:
        content = base64.b64encode(f.read()).decode()
    try:
        _req('PUT', f'{api}/repos/{repo}/contents/exports/{name}', token,
             {'message': f'export {name}', 'content': content}, timeout=180)
    except urllib.error.HTTPError as e:
        return False, {401: 'token 不对或过期了', 403: 'token 没有写入权限', 404: '找不到仓库',
                       422: '仓库里已经有同名档案'}.get(e.code, f'GitHub 回应 {e.code}')
    except Exception as e:  # noqa: BLE001
        return False, f'上传失败：{e}'
    return True, f'已上传到 GitHub（{repo}/exports/{name}）'
