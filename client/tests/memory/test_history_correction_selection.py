import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from runtime.memory.history_selection import _block, select_history_messages
from runtime.memory.source_retrieval import SourceRetrieval
from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
from runtime.memory.memory_port import NullMemoryPort


def record(source, text, actor='user'):
    return {'citation': source + ':' + actor, 'speaker': actor,
            'provenance': {'source_record_id': source},
            'occurred_at': '2026-09-26T01:00:00+00:00',
            'evidence_scope': 'recorded_utterance', 'text': text}


class Gateway:
    def __init__(self, answer):
        self.answer, self.calls, self.formats = answer, [], []

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls.append(messages)
        self.formats.append(kwargs['response_format'])
        return SimpleNamespace(text=json.dumps(self.answer))


def selection(tmp_path, answer, *, current='那张照片的来源呢？', budget=16000):
    index = SourceRetrieval(tmp_path / 'originals.sqlite3')
    earlier, later = record('old', '这是我自拍的照片'), record('new', '刚才说错了，那是网上找的图')
    for r in (earlier, later):
        index.put('alice', r['provenance']['source_record_id'], r['text'], '',
                  datetime.fromisoformat(r['occurred_at']))
    builder = CompanionMemoryPromptBuilder(NullMemoryPort(), SimpleNamespace(_originals=index), user_id='alice')
    gateway = Gateway(answer)
    result = asyncio.run(select_history_messages(
        ({'role': 'system', 'content': '角色\n' + _block([[earlier], [later]])},
         {'role': 'user', 'content': current}), gateway, max_input_chars=budget,
        memory_builder=builder, as_of=datetime(2026, 9, 26, 3, tzinfo=timezone.utc)))
    return index, result, gateway


DEPENDENCY = {'earlier': 'old:user', 'later': 'new:user', 'kind': 'correction',
              'earlier_quote': '我自拍的照片', 'later_quote': '那是网上找的图'}


def test_new_correction_is_indivisible_even_if_model_selects_only_old(tmp_path):
    index, result, gateway = selection(tmp_path, {'selected_ids': ['h0'], 'dependencies': [DEPENDENCY]})
    text = '\n'.join(m['content'] for m in result)
    assert '这是我自拍的照片' in text and '刚才说错了，那是网上找的图' in text
    assert len(gateway.calls) == 1
    assert index.dependencies('alice', ['old']).relations


def test_invalid_citation_cannot_save_or_leave_the_selected_old_assertion(tmp_path):
    index, result, _ = selection(tmp_path, {'selected_ids': ['h0'],
        'dependencies': [{**DEPENDENCY, 'later_quote': '并非原话'}]})
    assert '这是我自拍的照片' not in '\n'.join(m['content'] for m in result)
    assert not index.dependencies('alice', ['old']).relations


def test_current_correction_is_not_inserted_as_a_delivered_original(tmp_path):
    index, result, _ = selection(tmp_path, {'selected_ids': ['h0'], 'dependencies': []},
                                 current='我已经醒了，之前说的自拍也是说错了')
    assert result[-1]['content'] == '我已经醒了，之前说的自拍也是说错了'
    assert {r.source_id for r in index.get_sources('alice', ['old', 'new', 'current'])} == {'old', 'new'}


def test_write_failure_does_not_drop_the_current_correction_pair(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(SourceRetrieval, 'save_dependency', fail, raising=False)
    _, result, _ = selection(tmp_path, {'selected_ids': ['h0'], 'dependencies': [DEPENDENCY]})
    text = '\n'.join(m['content'] for m in result)
    assert '这是我自拍的照片' in text and '刚才说错了，那是网上找的图' in text


def test_current_correction_citation_is_explicit_on_actual_selector_wire(tmp_path):
    correction = '我刚说错了，那张是网上找的照片'
    _, result, gateway = selection(tmp_path, {'selected_ids': ['h0'], 'dependencies': [{
        **DEPENDENCY, 'later': 'current', 'later_quote': correction}]}, current=correction)
    packet = json.loads(gateway.calls[0][-1]['content'])
    assert packet['current_citation'] == 'current'
    fields = gateway.formats[0]['schema']['properties']['dependencies']['items']['properties']
    for key in ('earlier', 'later'):
        assert set(fields[key]['enum']) == {'current', 'old:user', 'new:user'}
        assert 'current_message' not in fields[key]['enum']
    assert result[-1]['content'] == correction
    assert '这是我自拍的照片' in '\n'.join(m['content'] for m in result)
