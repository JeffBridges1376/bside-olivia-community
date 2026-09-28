"""Pure persona candidate validation and bounded projection; no provider calls."""
import json
import re
from dataclasses import replace


SELECTION_INSTRUCTION = (
    '另从persona_candidates选择与本轮话题直接相关的人格细节，返回persona_ids，最多12项、按相关性排序。'
    '核心人格已固定保留，不在候选中；不能删除核心、改写设定或把兴趣偏好当本轮已发生的活动。'
    '阶段安排由当前世界状态提供。普通问候允许一项不选；问到人物具体背景、喜好或指代时选择对应资料。'
    '人物候选与历史原话分开，不给人格候选添加dependencies，不推断共同经历。'
)
_DEGRADED = '本轮人格细节筛选暂不可用，仅保留核心人格。未提供的细节不表示不存在；具体经历或喜好缺乏本轮依据时保持未知，不补造事实。'


def contextual_catalog(snapshot, mode):
    return tuple({'id': item.declaration_id, 'statement': item.statement}
                 for item in snapshot.declarations
                 if item.inclusion == 'contextual' and item.tier not in {'CONSTITUTION', 'MODE_STYLE'}
                 and (item.mode is None or item.mode == mode))


def validate_persona_ids(snapshot, mode, selected_ids):
    allowed = {item['id'] for item in contextual_catalog(snapshot, mode)}
    if (not isinstance(selected_ids, (list, tuple)) or len(selected_ids) > 12
            or any(not isinstance(key, str) or key not in allowed for key in selected_ids)
            or len(set(selected_ids)) != len(selected_ids)):
        raise ValueError('PERSONA_SELECTION_INVALID')
    return tuple(selected_ids)


def selected_declarations(snapshot, mode, selected_ids=None):
    selected = None if selected_ids is None else set(validate_persona_ids(snapshot, mode, selected_ids))
    return tuple(item for item in snapshot.declarations
                 if item.inclusion != 'phase'
                 and (item.inclusion == 'core' or item.tier in {'CONSTITUTION', 'MODE_STYLE'}
                      or selected is None or item.declaration_id in selected))


def snapshot_for_messages(snapshot, messages):
    """Bind review authority to our frozen asset and actually rendered details."""
    if snapshot is None:
        return None
    by_id = {item.declaration_id: item for item in snapshot.declarations}
    selected = set()
    decoder = json.JSONDecoder()
    for message in messages:
        if message.get('role') != 'system':
            continue
        content, position = message.get('content', ''), 0
        while match := re.search(r'<([a-z_]+)>\s*', content[position:]):
            tag, start = match.group(1), position + match.end()
            try:
                payload, end = decoder.raw_decode(content, start)
            except ValueError:
                break
            closing = re.match(r'\s*</' + tag + '>', content[end:])
            if closing is None:
                break
            position = end + closing.end()
            item = by_id.get(payload.get('declaration_id')) if isinstance(payload, dict) else None
            if (item is not None and tag == item.tier.lower() and item.inclusion == 'contextual'
                    and payload.get('statement') == item.statement):
                selected.add(item.declaration_id)
    return replace(snapshot, declarations=tuple(item for item in snapshot.declarations
        if item.inclusion != 'phase' and (item.inclusion == 'core'
            or item.tier in {'CONSTITUTION','MODE_STYLE'} or item.declaration_id in selected)))


def project_persona_selection(messages, snapshot, mode, selected_ids, *, max_input_chars, unavailable=False,
                              development=None):
    """Only original, offered contextual text is appended; core is never removed."""
    from .persona_assembly import _declaration_blocks
    from runtime.reply.prompt_budget import PromptSection
    ids = validate_persona_ids(snapshot, mode, selected_ids)
    by_id = {item.declaration_id: item for item in snapshot.declarations}
    sections = {'PUBLIC_CANON': PromptSection.PUBLIC_CANON,
                'COMMUNITY_SOFT_CANON': PromptSection.SOFT_CANON,
                'INFERRED': PromptSection.INFERRED_TRAIT,
                'UNCERTAINTY': PromptSection.EVIDENCE_SUMMARY}
    remaining = max_input_chars - sum(len(message['content']) for message in messages)
    blocks = []
    for key in ids:
        item = by_id[key]
        block = _declaration_blocks((item,), item.tier, sections[item.tier], development=development)[0].content
        if len(block) <= remaining:
            blocks.append(block)
            remaining -= len(block)
    if unavailable:
        note = '<persona_selection>' + json.dumps({'status':'core_only', 'instruction':_DEGRADED}, ensure_ascii=False) + '</persona_selection>'
        if len(note) <= remaining:
            blocks.append(note)
    if not blocks:
        return tuple(messages)
    projected = [dict(message) for message in messages]
    index = next((i for i, message in enumerate(projected) if message.get('role') == 'system'), None)
    if index is None:
        raise ValueError('PERSONA_CORE_CONTEXT_UNAVAILABLE')
    projected[index]['content'] += ''.join(blocks)
    return tuple(projected)
