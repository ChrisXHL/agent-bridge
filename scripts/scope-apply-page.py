#!/usr/bin/env python3
"""Write a local page with one-click Feishu scope applications for every business assistant.

Each assistant gets a link carrying only the scopes it still lacks (checked against
the app's actually granted scopes), so re-running after publishing shows what is left.
Usage: scope-apply-page.py STATE_DIR OUTPUT_HTML
"""
import glob
import html
import json
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from assistant_onboarding import SCOPES  # noqa: E402
from feishu_probe import _call  # noqa: E402


def granted_scopes(app_id, app_secret):
    auth = _call('/auth/v3/tenant_access_token/internal', body={'app_id': app_id, 'app_secret': app_secret})
    result = _call('/application/v6/scopes', token=auth.get('tenant_access_token', ''))
    if result.get('code') != 0:
        return None
    return {x.get('scope_name') for x in result['data'].get('scopes', [])
            if x.get('scope_type') == 'tenant' and x.get('grant_status') == 1}


def main(state_dir, output):
    sections = []
    for path in sorted(glob.glob(str(Path(state_dir) / 'feishu-apps/app_*/config.json'))):
        config = json.load(open(path))
        if config.get('assistant_manager') or config.get('assistant_archived') or not config.get('app_id'):
            continue
        app, name = config['app_id'], config.get('bot_name') or config.get('app_name') or config['app_id']
        granted = granted_scopes(app, config['app_secret'])
        missing = SCOPES if granted is None else [s for s in SCOPES if s not in granted]
        state = '无法查询当前权限，按全部申请' if granted is None else (
            '✅ 已全部生效' if not missing else f'还缺 {len(missing)} 项（含群消息权限）' if 'im:message.group_msg' in missing
            else f'还缺 {len(missing)} 项')
        apply = (f'<a class="btn" target="_blank" rel="noopener" href="https://open.feishu.cn/page/scope-apply?clientID={app}'
                 f'&amp;scopes={quote(",".join(missing), safe="")}">申请缺少的 {len(missing)} 项权限</a>'
                 f'<a class="sub" target="_blank" rel="noopener" href="https://open.feishu.cn/app/{app}/version">'
                 f'第 2 步 → 创建并发布新版本（不发布不生效）</a>') if missing else ''
        sections.append(f'<section><h2>{html.escape(name)}</h2><p class="state">{state}</p>{apply}</section>')
    page = f'''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>飞书机器人权限申请</title><style>
body{{font:16px/1.6 -apple-system,"PingFang SC",sans-serif;max-width:680px;margin:40px auto;padding:0 16px;color:#1f2329;background:#fff}}
section{{border:1px solid #e5e6eb;border-radius:12px;padding:16px 20px;margin:20px 0}} h2{{margin:0}} .state{{margin:4px 0 12px;color:#646a73}}
.btn{{display:block;padding:14px;border-radius:10px;background:#3370ff;color:#fff;text-decoration:none;font-weight:600;text-align:center}}
.sub{{display:block;margin-top:12px;font-weight:600;color:#3370ff}} small{{color:#646a73}}
</style><h1>飞书机器人权限申请</h1>
<p>每个未完成的助手：① 点蓝色按钮申请 → ② 点「创建并发布新版本」提交发布（如需审批，等管理员通过）。完成后让 Claude 重新生成本页核对。</p>
{"".join(sections)}<small>只申请应用（机器人）身份权限；不含以个人身份操作、删除群、加急电话短信、手机号邮箱等。</small></html>'''
    Path(output).write_text(page, encoding='utf-8')
    print(f'wrote {output} ({len(sections)} assistants)')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
