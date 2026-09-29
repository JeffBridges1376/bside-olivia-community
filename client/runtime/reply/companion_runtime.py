"""Development reply consumption of one frozen Jev decision per input revision."""
import json
import os
from contextvars import ContextVar

from .reply_model_quality import _recent_dialogue


TURN_CONTEXT = ContextVar('companion_decision_turn', default=None)


class CompanionRuntimeError(RuntimeError):
    pass


def configured_port():
    endpoint = os.environ.get('OLIVIA_JEV_DECISION_URL', '').strip()
    if not endpoint:
        return None
    from .companion_decision import JevDecisionPort
    return JevDecisionPort(endpoint=endpoint,
        token=os.environ.get('COMPANION_CLASSIFIER_TOKEN', ''), profile='single_delivery')


def _decision_context(messages, required_sources=()):
    """Keep conversational resolution evidence, not the writer's full read window.

    The newest stored decision carries unresolved media requirements. Legacy
    unclassified user originals remain evidence; we cannot guess they are done.
    """
    from runtime.personal_chat.context import READ_WINDOW
    recent = _recent_dialogue(messages)
    window = list(READ_WINDOW.get() or ())
    required = set(required_sources)
    covered = set()
    for index in range(len(window) - 1, -1, -1):
        record = window[index].get('companion_decision')
        if not isinstance(record, dict):
            continue
        understanding = record.get('plan', {}).get('understanding', {})
        requirements = understanding.get('requirements')
        sources = record.get('source_id_map')
        if not isinstance(requirements, list) or not isinstance(sources, dict):
            continue
        for requirement in requirements:
            if requirement.get('fulfillment') == 'pending':
                required.update(sources[ref] for ref in requirement.get('evidence_turn_ids', ()) if ref in sources)
        covered = {str(row.get('letter_id')) for row in window[:index + 1]}
        break

    if any(not isinstance(source, str) or not source for source in required):
        raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE')

    def matches(row, source):
        base = source.rsplit(':', 1)[0] if source.endswith((':user', ':linli')) else source
        return row['event_id'] == source or row['source'] == base or row['source'].startswith(base + ':')

    latest = recent[-1]['source'] if recent else None
    kept = [row for row in recent if row['source'] == latest
            or any(matches(row, source) for source in required)
            or row['role'] == 'user' and not any(row['source'].startswith('reply:' + key + ':') for key in covered)]
    # Pending originals may predate the writer's bounded read window.
    restored = []
    for source in sorted(required):
        if any(matches(row, source) for row in kept):
            continue
        original = next(((index, row) for index, row in enumerate(window)
                         if source.startswith('reply:' + str(row.get('letter_id')) + ':')), None)
        if original is None:
            raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE')
        index, original = original
        assistant = source.endswith(':linli')
        text = original.get('reply_text' if assistant else 'content')
        if not isinstance(text, str) or not text.strip() or assistant and original.get('_received_only'):
            raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE')
        restored.append((index, dict(source=source, event_id=source,
            role='assistant' if assistant else 'user', text=text,
            image_delivery_confirmed=assistant and original.get('image_delivery_status') == 'DELIVERED')))
    return [row for _, row in sorted(restored, key=lambda item: item[0])] + kept


async def prepare_decision(port, messages, user_text, *, source_id, input_revision, as_of, kinds, cached=None,
                           required_sources=()):
    from .companion_decision import FrozenCompanionTurn, FrozenCompanionDecision
    metadata = TURN_CONTEXT.get() or {}
    recent = _decision_context(messages, (*required_sources, *metadata.get('companion_context_sources', ())))
    if not isinstance(user_text, str) or not user_text.strip() or any(row.get('truncated') for row in recent):
        raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE')
    # These are the native prior-turn frames from our frozen assembly, not
    # retrieved summaries, incoming image interpretations, or writer drafts.
    turns = [dict(source_id=row['event_id'], role=row['role'], text=row['text']) for row in recent]
    for turn, row in zip(turns, recent):
        if row['role'] == 'assistant' and row.get('image_delivery_confirmed') is True:
            # The vendor schema has only text turns. Keep the original intact
            # inside a labelled envelope; the ACK is an application fact, not
            # a fabricated character utterance or a claim about picture content.
            turn['text'] = json.dumps({'original_chat_text': row['text'],
                'application_delivery_record': '该回合的图片已确认发送。此记录不是角色说过的话，也不证明图片场景真实发生。'},
                ensure_ascii=False)
    turns.append(dict(source_id=source_id, role='user', text=user_text))
    # Re-use the original classification instant only for this input revision.
    # The digest below still checks every original, source and capability.
    reuse = isinstance(cached, dict) and cached.get('input_revision') == input_revision
    if reuse:
        as_of = cached.get('as_of', as_of)
    from .companion_decision import CompanionDecisionError
    protected = set(required_sources)
    while True:
        try:
            frozen = FrozenCompanionTurn.create(messages=turns, current_source_id=source_id,
                capabilities=dict(kinds=list(kinds), synchronize=False, playback_events=False,
                    compose_audio=False, compose_video=False, split_spoken_content=False),
                environment=dict(can_read=None, can_view=None, can_listen=None), forbidden_kinds=[],
                as_of=as_of, input_revision=input_revision)
            break
        except CompanionDecisionError as exc:
            # Long QQ bursts can exceed one request's size or turn count. Leave out
            # the oldest turns, never the current message, its three predecessors
            # or required originals; a genuinely invalid input still fails below.
            droppable = [i for i, turn in enumerate(turns[:-4]) if turn['source_id'] not in protected]
            if exc.code not in {'JEV_INPUT_TOO_LARGE', 'JEV_INPUT_INVALID'} or not droppable:
                raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE') from None
            del turns[droppable[0]]
        except ValueError:
            raise CompanionRuntimeError('JEV_CONTEXT_UNAVAILABLE') from None
    if reuse:
        try:
            return FrozenCompanionDecision.from_record(frozen, cached, profile=getattr(port, 'profile', 'full'))
        except ValueError:
            # An altered decision must not silently become a newly paid request.
            raise CompanionRuntimeError('JEV_STORED_DECISION_INVALID') from None
    result = await port.decide(frozen)
    if result.decision is None:
        raise CompanionRuntimeError(result.error_code or 'JEV_UNAVAILABLE')
    return result.decision


def delivery_for(decision, *, kinds):
    """Consume one text, speech or QQ image body and silence.

    Keep composite proposals explicit until their durable step consumer exists;
    a classification cannot turn a missing capability into completed delivery.
    """
    plan = decision.plan
    proposal, resolution = plan['proposal'], plan['resolution']
    if resolution['status'] == 'unsupported' or resolution['blocked_steps']:
        raise CompanionRuntimeError('JEV_PLAN_UNSUPPORTED')
    timing = proposal['timing']
    if timing in {'wait_user', 'defer', 'no_reply'}:
        return timing, None
    if (len(proposal['steps']) != 1 or len(proposal['contents']) != 1
            or proposal['deliver_together'] or proposal['synchronize']):
        raise CompanionRuntimeError('JEV_PLAN_UNSUPPORTED')
    step = proposal['steps'][0]
    if (len(step['parts']) != 1 or step['after']
            or proposal['contents'][0]['derived_from'] is not None):
        raise CompanionRuntimeError('JEV_PLAN_UNSUPPORTED')
    kind = step['parts'][0]['kind']
    if kind not in {'text', 'audio_speech', 'image'} or kind not in kinds:
        raise CompanionRuntimeError('JEV_PLAN_UNSUPPORTED')
    # Uncertain media must be clarified before a paid asset is generated.
    if resolution['status'] == 'needs_clarification' and kind != 'text':
        raise CompanionRuntimeError('JEV_PLAN_UNSUPPORTED')
    if 'media_requirement' in resolution['uncertain_fields'] and resolution['status'] != 'needs_clarification':
        raise CompanionRuntimeError('JEV_PLAN_UNSUPPORTED')
    return timing, kind


def project_decision(messages, decision, *, max_input_chars, delivery):
    payload = decision.writer_projection()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('<', r'\u003c').replace('>', r'\u003e')
    note = ('本轮决策参考来自当前原文及冻结历史；只影响这次回应的理解和表达。'
            '用户情绪不等于你的情绪，不改变核心人格、世界事实或关系状态。'
            '用户当前纠正优先；历史引文和候选控制均不等于已执行。'
            '存在澄清项先询问，不猜定用户未明确的选择；不得声称已删除记忆、已安排联系或已发送媒体。'
            '语气仅用于撰写正文，不产生语音情绪指令或速度控制。')
    if delivery == 'audio_speech':
        from runtime.personal_chat.presentation import VOICE_PROSE
        note += '本轮交付已选定语音，结构化回复的 delivery 必须是 voice；只写将实际朗读的一份正文。'
        note += VOICE_PROSE
    elif delivery == 'text':
        note += '本轮交付已选定文字，结构化回复的 delivery 必须是 text。'
    elif delivery == 'letter_image':
        note += ('本轮信件会附带一张图片。写自然的文字回信，回应用户当前来信；'
                 '图片由后续流程制作，不要把绘图提示词当作回信，不声称图片已经生成或发送。')
    elif delivery == 'image':
        note += ('本轮交付是一张图片，结构化回复的 delivery 使用 text，仅作为照片规划的内部描述。'
                 '该正文不会作为聊天文字发出；描述符合当前请求的画面，不声称照片已生成或已发送。')
    note += '\n<companion_decision>\n' + encoded + '\n</companion_decision>'
    result = [dict(message) for message in messages]
    position = next((i for i in range(len(result) - 1, -1, -1) if result[i].get('role') == 'user'), len(result))
    result.insert(position, dict(role='system', content=note))
    if sum(len(message.get('content', '')) for message in result) > max_input_chars:
        raise CompanionRuntimeError('JEV_CONTEXT_BUDGET_EXCEEDED')
    return tuple(result)
