"""Every paid JEV step must degrade to a smaller request, never fail the reply.

Each case feeds one judgment the inputs that broke 2.0.x in production (hundreds
of live threads, sticker-only messages, very long originals and replies, long
QQ bursts) and asserts two things: no capacity error, and every request that is
sent fits the 32 KB transport cap. Add a case here for any new JEV step.
"""
import asyncio
import json
from datetime import datetime, timezone

import pytest

CAP = 32768


def size(state, questions, purpose):
    return len(json.dumps({'state': state, 'questions': questions, 'purpose': purpose},
                          ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


class RecordingPort:
    """Answers every question with its first non-'none' option and records request sizes."""

    def __init__(self):
        self.sizes = []

    async def ask(self, state, questions, *, purpose):
        self.sizes.append(size(state, questions, purpose))
        return {key: next((c for c in q['criteria'] if c != 'none'), 'none') for key, q in questions.items()}


def long_reply(sentences):
    return ''.join(f'第{i}句，我把今天练琴的细节慢慢讲给你听。' for i in range(sentences))


def test_world_selection_with_hundreds_of_live_threads(tmp_path):
    from runtime.private_world.daily_life import DailyLifeStore
    from runtime.reply.world_context_selection import select_world_context
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime(2026, 9, 28, 5, tzinfo=timezone.utc))
    packet['records'] = [*packet['records'], *({'field': 'threads', 'many': True, 'value': {
        'id': f't{i}', 'title': '一直在进行的事项' * 4, 'status': 'ongoing',
        'updated_at': f'2026-09-{1 + i % 27:02d}T00:00:00+00:00'}} for i in range(500))]
    port = RecordingPort()
    asyncio.run(select_world_context(port, packet, '最近在忙什么？'))
    assert port.sizes and max(port.sizes) <= CAP


def test_recall_selection_with_empty_long_and_many_originals():
    from runtime.memory.jev_history import select_history
    records = [{'citation': 'empty', 'speaker': 'user', 'text': '', 'evidence_scope': 'recorded_utterance'},
               {'citation': 'long', 'speaker': 'linli', 'text': '长' * 3000 + '。', 'evidence_scope': 'recorded_utterance'},
               *({'citation': f'r{i}', 'speaker': 'linli', 'text': long_reply(30), 'evidence_scope': 'recorded_utterance'}
                 for i in range(20))]
    refs = [*records, {'citation': 'current', 'speaker': 'user', 'text': '忘记啦？', 'evidence_scope': 'current_input'}]
    port = RecordingPort()
    asyncio.run(select_history(port, {'candidates': [{'id': 'h0', 'records': records[:3]}]}, refs[:3] + refs[-1:]))
    assert port.sizes and max(port.sizes) <= CAP




@pytest.mark.parametrize('sentences', [1, 30, 120])
def test_quality_review_of_short_and_very_long_replies(monkeypatch, sentences):
    from tests.persona.test_jev_quality import Decisions, request, transport
    port = Decisions()
    transport(monkeypatch, port).review_json(request(long_reply(sentences)), model='jev', timeout_seconds=5)
    assert port.calls and all(size(state, questions, 'x') <= CAP for state, questions in port.calls)


def test_decision_with_a_long_qq_burst(monkeypatch):
    from runtime.reply import companion_runtime
    from runtime.reply.companion_runtime import prepare_decision
    from tests.http.test_jev_image_delivery import Port
    burst = [dict(source=f'reply:m{i}:1', event_id=f'reply:m{i}:1:user', role='user', text=long_reply(20))
             for i in range(40)]
    monkeypatch.setattr(companion_runtime, '_decision_context', lambda messages, required=(): [dict(r) for r in burst])
    port = Port()
    asyncio.run(prepare_decision(port, [], '在吗', source_id='current', input_revision=0,
                                 as_of=datetime.now(timezone.utc), kinds=['text']))
    assert len(json.dumps({'input': port.turns[0].input}, ensure_ascii=False).encode()) <= CAP
