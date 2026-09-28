"""Separate published prose from its internal evidence envelope."""
import json


def published_wording(source):
    text = source['text']
    if source.get('source_kind') != 'published_world':
        return text
    prose, separator, envelope = text.rpartition('\n')
    if separator:
        try:
            payload = json.loads(envelope)
        except (ValueError, TypeError):
            return text
        if isinstance(payload, dict) and payload.get('note') == prose:
            return prose
    return text


def presentation_text(source, value, *, with_subject=False):
    prose = published_wording(source)
    if prose != source['text'] and value and value not in prose and value in source['text'][len(prose):]:
        value = prose[:200]
    if with_subject and prose != source['text'] and value and value in prose:
        payload = json.loads(source['text'].rpartition('\n')[2])
        activity = payload.get('activity')
        if isinstance(activity, str) and activity.strip() and activity not in value:
            # Exact legacy template migration, not a guess about lesson content.
            if (payload.get('activity_kind') == 'class'
                    and prose == '这一处暂时没弄懂，不代表整节课失败或已经结束。'):
                return f'{activity}时，有一部分内容还没理解；具体疑问尚未记录。'
            return f'{activity}：{value}'
    return value
