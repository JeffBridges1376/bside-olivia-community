import asyncio
import json
from types import SimpleNamespace

import pytest

from runtime.memory.history_selection import _block, select_history_messages
from runtime.memory.recall_check import prepare_recall_messages


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


def test_recall_jev_keeps_originals_and_classifies_current_intent(monkeypatch):
    port = Port({'intent': 'recall_question', 'question_0': 'yes', 'evidence_0': 'relevant'})
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    original = messages()
    result = asyncio.run(prepare_recall_messages(original, ForbiddenGateway(), max_input_chars=20000))
    assert port.calls
    assert original[0]['content'] in result[0]['content']
    import re
    value = json.loads(re.search(r'<recall_check>(.*?)</recall_check>', result[0]['content'], re.S)[1])
    assert value['current_turn']['intent'] == 'recall_question'
    assert value['current_turn']['questions'] == ['周五的登记办好了吗？']
    assert value['originals']['o0']['quote'] == '周五只是计划，还没登记。'
    assert value['events'][0]['interpretation'] is None


def test_dependency_quotes_are_selected_from_originals():
    from runtime.memory.jev_history import select_history
    refs = [{'citation': 'a', 'speaker': 'user', 'text': '明天去登记。', 'evidence_scope': 'recorded_utterance'},
            {'citation': 'b', 'speaker': 'user', 'text': '刚才说错了，明天不去登记。', 'evidence_scope': 'current_input'}]
    port = Port({'dep_0_kind': 'correction', 'dep_0_earlier': '0', 'dep_0_later': '1'})
    value = asyncio.run(select_history(port, {'candidates': []}, refs))
    assert value['dependencies'] == [{'earlier': 'a', 'later': 'b', 'kind': 'correction',
                                     'earlier_quote': refs[0]['text'], 'later_quote': refs[1]['text']}]
    assert len(port.calls) == 1


def test_unsupported_recall_capacity_is_explicit():
    from runtime.memory.jev_history import check_recall
    sources = [{'source': 's0', 'scope': 'historical_exchange',
                'text': json.dumps([{'text': '长' * 1601}])}]
    with pytest.raises(ValueError, match='JEV_RECALL_QUOTE_CAPACITY'):
        asyncio.run(check_recall(Port(), '记得吗', sources))


def test_dependency_correction_between_speakers_is_rejected():
    from runtime.memory.jev_history import select_history
    refs = [{'citation': 'a', 'speaker': 'linli', 'text': '我收到了。', 'evidence_scope': 'recorded_utterance'},
            {'citation': 'b', 'speaker': 'user', 'text': '不是的，还没有。', 'evidence_scope': 'current_input'}]
    port = Port({'dep_0_kind': 'correction', 'dep_0_earlier': '0', 'dep_0_later': '1'})
    with pytest.raises(ValueError, match='JEV_HISTORY_SPEAKER_CONFLICT'):
        asyncio.run(select_history(port, {'candidates': []}, refs))
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


def test_recall_provider_failure_is_visible_without_text_fallback(monkeypatch):
    port = Port(failure=RuntimeError('offline'))
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: port)
    result = asyncio.run(prepare_recall_messages(messages(), ForbiddenGateway(), max_input_chars=20000))
    assert '"status":"unavailable"' in result[0]['content']
    assert '"reason":"jev_provider"' in result[0]['content']
    assert '周五只是计划' in result[0]['content']


def test_recall_quotes_cannot_promote_plan_to_completed_fact():
    from runtime.memory.jev_history import check_recall
    from runtime.memory.recall_check import _sources
    sources, _ = _sources(messages())
    sources.append({'source': 'current', 'scope': 'current_user_statement', 'text': '登记完成了吗？'})
    value = asyncio.run(check_recall(Port({'intent': 'recall_question', 'evidence_0': 'relevant'}),
                                    '登记完成了吗？', sources))
    assert value['findings'][0]['status'] == 'uncertain'
    assert value['findings'][0]['event_stage'] == 'unknown'
    assert 'event' not in value['findings'][0]
    assert value['findings'][0]['citations'][0]['matched_originals'][0]['speaker'] == 'linli'


def test_recall_shared_original_references_keep_roles_and_complete_conditions():
    from runtime.memory.jev_history import check_recall
    rows=[{'speaker':'user' if i%2 else 'linli', 'occurred_at':f'2026-09-{i+1:02}',
           'text':f'如果第{i}天下雨就不去，'+('完整上下文'*35)+'。'} for i in range(8)]
    sources=[{'source':'history','scope':'historical_exchange','text':json.dumps(rows,ensure_ascii=False)}]
    port=Port()
    asyncio.run(check_recall(port,'后来是否更正了？',sources))
    state,questions,_=port.calls[0]
    assert state['sources']['history']['text']==rows
    restored=[]
    for ref in state['originals']:
        loc=ref['locations'][0]
        value=state['sources'][ref['source']]['text']
        for key in loc['path']: value=value[key]
        restored.append({'source':ref['source'],'quote':value[loc['start']:loc['end']]})
    assert [r['quote'] for r in restored]==[r['text'] for r in rows]
    encode=lambda x:len(json.dumps(x,ensure_ascii=False).encode())
    old={'current_message':state['current_message'],'sources':[dict(sources[0],text=rows)],'originals':restored}
    before=encode(dict(state=old,questions=questions))
    after=encode(dict(state=state,questions=questions))
    assert after < before
    print(f'recall-check one packet bytes before={before} after={after}')


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


@pytest.mark.parametrize('answers,error', [
    ({'dependency_overflow': 'yes'}, 'JEV_HISTORY_DEPENDENCY_CAPACITY'),
    ({'dep_0_kind': 'correction', 'dep_0_earlier': '0', 'dep_0_later': '1', 'dep_0_later_quote': '1'},
     'JEV_HISTORY_REFERENCE_INVALID'),
    ({'dep_0_kind': 'correction', 'dep_0_earlier': '0', 'dep_0_later': '1',
      'dep_1_kind': 'correction', 'dep_1_earlier': '0', 'dep_1_later': '1'},
     'JEV_HISTORY_DUPLICATE_DEPENDENCY'),
])
def test_history_slot_invalid_results_do_not_retry(answers, error):
    from runtime.memory.jev_history import select_history
    refs = [{'citation': 'a', 'speaker': 'user', 'text': '明天去。后天再去。', 'evidence_scope': 'recorded_utterance'},
            {'citation': 'b', 'speaker': 'user', 'text': '说错了，不去。', 'evidence_scope': 'current_input'}]
    port = Port(answers)
    with pytest.raises(ValueError, match=error):
        asyncio.run(select_history(port, {'candidates': []}, refs))
    assert len(port.calls) == 1


def test_memory_semantic_questions_do_not_shard_at_forty_eight():
    from runtime.memory.jev_history import _ask
    port = Port()
    questions = {f'q{i}': {'instructions': 'test', 'criteria': {'none': 'none', 'yes': 'yes'}} for i in range(65)}
    asyncio.run(_ask(port, {}, questions, 'recall-check'))
    assert len(port.calls) == 1 and len(port.calls[0][1]) == 65
    with pytest.raises(ValueError, match='JEV_INPUT_TOO_LARGE'):
        asyncio.run(_ask(port, {'original': 'x' * 32768}, questions, 'recall-check'))
    assert len(port.calls) == 1
