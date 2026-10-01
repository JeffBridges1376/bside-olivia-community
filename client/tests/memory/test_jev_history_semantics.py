from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES
import asyncio
import json
from types import SimpleNamespace

import pytest

from runtime.memory.history_selection import _block, select_history_messages


class ForbiddenGateway:
    config = SimpleNamespace(provider='openai_compatible')

    async def complete_structured_scoped(self, *args, **kwargs):
        pytest.fail('text model must not classify history in Jev mode')

    complete_scoped = complete_structured_scoped


class Port:
    def __init__(self, answers=None, failure=None):
        self.answers = answers or {}
        self.failure = failure
        self.calls = []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        if self.failure:
            raise self.failure
        return {key: self.answers.get(key, 'none' if 'none' in q['criteria'] else next(iter(q['criteria'])))
                for key, q in questions.items()}


def messages():
    groups = [[{'citation': 'a', 'speaker': 'linli', 'occurred_at': '2026-09-01',
                'evidence_scope': 'recorded_utterance', 'text': '周五只是计划，还没登记。'}],
              [{'citation': 'b', 'speaker': 'linli', 'occurred_at': '2026-09-02',
                'evidence_scope': 'recorded_utterance', 'text': '蓝杯子到了。'}]]
    return [{'role': 'system', 'content': '<evidence_use>{}</evidence_use>' + _block(groups)},
            {'role': 'user', 'content': '周五的登记办好了吗？'}]


def test_history_jev_selection_never_calls_text_model(monkeypatch):
    port = Port({'relevance_h0': 'yes'})
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    result = asyncio.run(select_history_messages(messages(), ForbiddenGateway(), max_input_chars=20000))
    assert port.calls
    assert '周五只是计划' in result[0]['content']
    assert '蓝杯子到了' not in result[0]['content']


def test_jev_failure_does_not_fall_back_to_text(monkeypatch):
    port = Port(failure=RuntimeError('offline'))
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    result = asyncio.run(select_history_messages(messages(), ForbiddenGateway(), max_input_chars=20000))
    assert port.calls
    assert '周五只是计划' not in result[0]['content']




def test_dependency_quotes_are_selected_from_originals():
    from runtime.memory.jev_history import select_history
    refs = [{'citation': 'a', 'speaker': 'user', 'text': '明天去登记。', 'evidence_scope': 'recorded_utterance'},
            {'citation': 'b', 'speaker': 'user', 'text': '刚才说错了，明天不去登记。', 'evidence_scope': 'current_input'}]
    port = Port({'dep_0_kind': 'correction', 'dep_0_earlier': '0', 'dep_0_later': '1'})
    value = asyncio.run(select_history(port, {'candidates': []}, refs))
    assert value['dependencies'] == [{'earlier': 'a', 'later': 'b', 'kind': 'correction',
                                     'earlier_quote': refs[0]['text'], 'later_quote': refs[1]['text']}]
    assert len(port.calls) == 1




def test_dependency_correction_between_speakers_is_rejected():
    from runtime.memory.jev_history import select_history
    refs = [{'citation': 'a', 'speaker': 'linli', 'text': '我收到了。', 'evidence_scope': 'recorded_utterance'},
            {'citation': 'b', 'speaker': 'user', 'text': '不是的，还没有。', 'evidence_scope': 'current_input'}]
    port = Port({'dep_0_kind': 'correction', 'dep_0_earlier': '0', 'dep_0_later': '1'})
    rejected = asyncio.run(select_history(port, {'candidates': []}, refs))
    assert rejected['dependencies'] == [] and rejected['overflow'] is True  # dropped, recall still runs
    port = Port({'dep_0_kind': 'challenge', 'dep_0_earlier': '0', 'dep_0_later': '1'})
    result = asyncio.run(select_history(port, {'candidates': []}, refs))
    assert result['dependencies'][0]['kind'] == 'challenge'
    assert len(port.calls) == 1


def test_history_packets_deduplicate_originals_without_dropping_sources():
    from runtime.memory.jev_history import select_history
    refs = [{'citation':f's{i}', 'speaker':'user', 'text':f'第{i}条：'+('完整原话' * 100),
             'evidence_scope':'recorded_utterance', 'occurred_at':f'2026-09-{i+1:02}'} for i in range(8)]
    packet = {'current_message':'后来改了吗？', 'candidates':[{'id':f'h{i}','records':[r]} for i,r in enumerate(refs)],
              'recent_records':refs, 'recent_dialogue':[{'role':'user','content':r['text']} for r in refs]}
    port = Port()
    asyncio.run(select_history(port, packet, refs))
    assert len(port.calls) == 1
    for state, questions, purpose in port.calls:
        assert 'dependency_records' not in state and 'recent_records' not in state
        for record in state['records'].values():
            assert record == refs[int(record['citation'][1:])]
        for key in questions:
            if key.startswith('pair_'):
                for index in key.split('_')[1:]:
                    assert f's{index}' in state['records']
    encode = lambda x: len(json.dumps(x,ensure_ascii=False).encode())
    actual = sum(encode({'state':s,'questions':q}) for s,q,_ in port.calls)
    old_state = {**packet, 'dependency_records':refs}
    all_questions = {key:q for _,questions,_ in port.calls for key,q in questions.items()}
    before = encode({'state':old_state,'questions':all_questions})
    assert actual < before
    print(f'history synthetic bytes before={before} after={actual}')








def test_jev_recall_offers_the_top_candidates_instead_of_failing(monkeypatch):
    """With long histories every recall failed (JEV_HISTORY_CANDIDATE_CAPACITY), so replies never had history."""
    port = Port({'relevance_h0': 'yes'})
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    groups = [[{'citation': f'{i}-{j}', 'speaker': 'user', 'text': f'第{i}条计划第{j}句'} for j in range(3)]
              for i in range(40)]
    original = [{'role': 'system', 'content': '人设' + _block(groups)}, {'role': 'user', 'content': '计划呢'}]
    captured = []
    monkeypatch.setattr('runtime.diagnostics.recall_trace.finish', lambda *args: captured.append(args[-1]))
    asyncio.run(select_history_messages(original, ForbiddenGateway(), max_input_chars=30000))
    assert len(port.calls) == 1
    state, questions, _purpose = port.calls[0]
    offered = [c['id'] for c in state['candidates']]
    assert 0 < len(offered) <= 12
    assert len({r for c in state['candidates'] for r in c['record_ids']}) <= 24
    assert state['candidates'][0]['record_ids'] == ['0-0', '0-1', '0-2']  # ranking order kept
    assert captured[-1]['status'] != 'unavailable'
    assert captured[-1].get('reason') != 'JEV_HISTORY_CANDIDATE_CAPACITY'


def test_history_twenty_four_sources_use_fixed_relation_slots():
    from runtime.memory.jev_history import select_history
    refs = [{'citation': f's{i}', 'speaker': 'user', 'text': f'第{i}条原话。',
             'evidence_scope': 'recorded_utterance'} for i in range(24)]
    port = Port()
    assert asyncio.run(select_history(port, {'candidates': []}, refs))['dependencies'] == []
    assert len(port.calls) == 1
    assert len(port.calls[0][1]) == 21
    assert not any(key.startswith('pair_') for key in port.calls[0][1])


@pytest.mark.parametrize('answers', [
    {'dependency_overflow': 'yes'},
    {'dep_0_kind': 'correction', 'dep_0_earlier': '0', 'dep_0_later': '1', 'dep_0_later_quote': '1'},
    {'dep_0_kind': 'correction', 'dep_0_earlier': '0', 'dep_0_later': '1',
     'dep_1_kind': 'correction', 'dep_1_earlier': '0', 'dep_1_later': '1'},
])
def test_history_slot_problems_drop_the_relation_not_the_recall(answers):
    """An invalid or overflowing relation used to fail the whole recall."""
    from runtime.memory.jev_history import select_history
    refs = [{'citation': 'a', 'speaker': 'user', 'text': '明天去。后天再去。', 'evidence_scope': 'recorded_utterance'},
            {'citation': 'b', 'speaker': 'user', 'text': '说错了，不去。', 'evidence_scope': 'current_input'}]
    port = Port(answers)
    result = asyncio.run(select_history(port, {'candidates': []}, refs))
    assert result.get('overflow') is True  # the writer gets the "left out" note
    assert len({(d['earlier'], d['later']) for d in result['dependencies']}) == len(result['dependencies'])
    assert len(port.calls) == 1


def test_memory_semantic_questions_do_not_shard_at_forty_eight():
    from runtime.memory.jev_history import _ask
    port = Port()
    questions = {f'q{i}': {'instructions': 'test', 'criteria': {'none': 'none', 'yes': 'yes'}} for i in range(65)}
    asyncio.run(_ask(port, {}, questions, 'recall-check'))
    assert len(port.calls) == 1 and len(port.calls[0][1]) == 65
    with pytest.raises(ValueError, match='JEV_INPUT_TOO_LARGE'):
        asyncio.run(_ask(port, {'original': 'x' * JEV_MAX_INPUT_BYTES}, questions, 'recall-check'))
    assert len(port.calls) == 1


def test_recall_survives_empty_and_long_originals():
    """One sticker-only message (empty text) made every recall fail, so replies invented the past."""
    from runtime.memory.jev_history import select_history
    long_sentence = '我' * 1200 + '。'
    refs = [{'citation': 'empty', 'speaker': 'user', 'text': '', 'evidence_scope': 'recorded_utterance'},
            {'citation': 'long', 'speaker': 'linli', 'text': long_sentence, 'evidence_scope': 'recorded_utterance'},
            {'citation': 'plain', 'speaker': 'user', 'text': '别刚从琴房出来就吹冷风。', 'evidence_scope': 'recorded_utterance'},
            {'citation': 'current', 'speaker': 'user', 'text': '当时是你自己应下来的，忘记啦？', 'evidence_scope': 'current_input'}]
    packet = {'current_message': refs[-1]['text'],
              'candidates': [{'id': 'h0', 'records': refs[:3]}]}
    port = Port({'relevance_h0': 'yes'})
    result = asyncio.run(select_history(port, packet, refs))
    assert result['selected_ids'] == ['h0']
    state, _questions, _purpose = port.calls[0]
    assert 'empty' not in state['record_ids'].values()  # nothing to quote, but recall still runs
    long_key = next(k for k, v in state['record_ids'].items() if v == 'long')
    offsets = state['sentence_offsets'][long_key]
    assert len(offsets) >= 3 and all(o['end'] - o['start'] <= 500 for o in offsets)
    assert ''.join(long_sentence[o['start']:o['end']] for o in offsets) == long_sentence


def test_failed_recall_tells_the_writer_not_to_reconstruct_the_past(monkeypatch):
    """With recall failing, a reply claimed "你昨晚说的呀" and invented where it was written down."""
    from runtime.diagnostics.recall_trace import project
    port = Port(failure=ValueError('JEV_UNAVAILABLE'))
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    captured = []
    monkeypatch.setattr('runtime.diagnostics.recall_trace.finish', lambda *args: captured.append(args[-1]))
    result = asyncio.run(select_history_messages(messages(), ForbiddenGateway(), max_input_chars=30000))
    notes = [m['content'] for m in result if m.get('role') == 'system']
    assert any('不得指认是谁说的' in note and '不得编造' in note for note in notes)
    assert captured[-1]['reason'] == 'JEV_UNAVAILABLE'
    projected = project({'event': 'history_recall', 'check_status': 'unavailable', 'reason': 'JEV_HISTORY_QUOTE_CAPACITY'})
    assert projected['reason'] == 'JEV_HISTORY_QUOTE_CAPACITY'  # exported, not dropped
    assert 'reason' not in project({'event': 'history_recall', 'reason': '用户原话'})


def test_recall_trace_records_intent_and_whether_evidence_reached_the_reply():
    """Needed to measure how often a question about the past gets checked originals."""
    from runtime.diagnostics import recall_trace
    recall_trace._PENDING.set({'event': 'history_recall', 'trace_id': 'a' * 32})
    recall_trace.finish([], [], {'status': 'checked', 'reply_intent': 'recall_question',
                                 'findings': [{'validation_status': 'verified'}]})
    record = recall_trace.snapshot()[-1]
    assert record['reply_intent'] == 'recall_question' and record['evidence_used'] is True
    recall_trace.finish([], [], {'status': 'unavailable', 'reply_intent': 'made-up', 'findings': []})
    record = recall_trace.snapshot()[-1]
    assert record['evidence_used'] is False and 'reply_intent' not in record
