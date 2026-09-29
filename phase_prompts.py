"""Keep business phase instructions separate from transport metadata."""
import json
from feishu_settings import render_prompt


def review_template(config, source):
    route = json.loads(source.get('route_json') or '{}')
    if 'review_prompt_template' in route:
        return route['review_prompt_template']
    current = next((r for r in config.get('routes', []) if r['chat_id'] == source['chat_id']), {})
    return current.get('review_prompt_template', config.get('review_prompt_template', ''))


def review_prompt(config, source, original, body, event_kind):
    template = review_template(config, source)
    if not template:
        return body
    metadata = {'event': event_kind, 'task_id': source.get('task_id'),
                'original_message_id': original['message_id'],
                'original_input_excerpt': original['text'][:2000],
                'original_input_truncated': len(original['text']) > 2000,
                'source_message_id': source['message_id'],
                'chat_id': source['chat_id'],
                'document_comments_included': False}
    return render_prompt(template, body + '\n\n[Bridge 任务关联数据]\n' +
                         json.dumps(metadata, ensure_ascii=False) +
                         '\n文档评论不在卡片回调中；第二阶段须主动读取文档最新内容和全部评论、回复。')
