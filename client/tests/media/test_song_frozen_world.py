"""Music consumes the reply's adopted view, never a later live world."""
import json
from datetime import datetime

import pytest

from llm_gateway import GatewayConfig
from runtime.media.song_content import plan_song_content
from runtime.media.song_plan_cache import cached_song_plan
from tests.media.test_song_content_pipeline import RecordingGateway, _payload
from tests.media.test_song_plan_cache import plan


def context():
    return {'as_of': '2026-09-28T12:00:00+08:00', 'world': {
        'recent_episodes': [{'source_id': 'practice:1', 'result': {'detail': '慢练后仍未弹顺'}}]},
        'emotion': {'interpretation_only': True, 'reaction_subject': 'character',
                    'current_affect': {'label': '失落', 'source_ids': ['practice:1']}}}


def test_frozen_world_emotion_clock_and_jev_direction_reach_song(monkeypatch):
    import runtime.reply.jev_questions as questions
    import runtime.memory.history_selection as history
    calls = []
    direction = dict(emotion_arc='restrained_sadness', piano_texture='sparse_counterline',
                     vocal_delivery='quiet_songful', dynamic_arc='soft_steady_settle',
                     ending='short_settled_cadence')
    class Port:
        def ask_sync(self, state, items, *, purpose):
            calls.append(state)
            assert purpose == 'original-song-direction'
            assert '用户明确选歌/曲风优先' in state['contract']
            assert all('state.contract' in x['instructions'] for x in items.values())
            return direction
    monkeypatch.setattr(questions, 'configured_questions', lambda: Port())
    async def history_only(messages, *args, **kwargs):
        assert kwargs['as_of'] == datetime.fromisoformat(context()['as_of'])
        return messages
    monkeypatch.setattr(history, 'select_history_messages', history_only)
    class Adapter:
        config = GatewayConfig(provider='mock', persona_v2_enabled=False)
        def _now(self):
            pytest.fail('must not read a later clock')
        def reply_context_messages(self, user_input, **kwargs):
            assert kwargs['as_of'] == datetime.fromisoformat(context()['as_of'])
            fragments = kwargs['life_fragments']
            return ({'role': 'system', 'content': str(fragments)},
                    {'role': 'user', 'content': user_input})
    gateway = RecordingGateway(json.dumps(_payload()), config=Adapter.config)
    result = plan_song_content('写首安静的歌', '今天练琴不太顺。', 40,
                               gateway=gateway, reply_adapter=Adapter(), expression_context=context())
    wire = str(gateway.calls)
    assert '慢练后仍未弹顺' in wire and 'current_affect' in wire and '失落' in wire
    assert calls[0]['frozen_expression'] == context()
    assert result.semantic_plan.emotion_arc.value == 'restrained_sadness'
    assert result.semantic_plan.piano_texture.value == 'sparse_counterline'
    assert 'frozen_music_direction' in wire


def test_frozen_context_binds_song_cache(tmp_path):
    path = tmp_path / 'song.json'
    calls = []
    def planner():
        calls.append(1)
        return plan()
    first = context()
    cached_song_plan(path, 'letter', 'reply', 40, planner, expression_context=first)
    cached_song_plan(path, 'letter', 'reply', 40, planner, expression_context=first)
    changed = context()
    changed['emotion']['current_affect']['label'] = '开心'
    cached_song_plan(path, 'letter', 'reply', 40, planner, expression_context=changed)
    assert calls == [1, 1]


def test_jev_song_failure_never_falls_back_to_text_judgment(monkeypatch):
    import runtime.reply.jev_questions as questions
    class Port:
        def ask_sync(self, *args, **kwargs):
            raise RuntimeError('synthetic JEV unavailable')
    monkeypatch.setattr(questions, 'configured_questions', lambda: Port())
    gateway = RecordingGateway(json.dumps(_payload()), config=GatewayConfig(provider='mock', persona_v2_enabled=False))
    with pytest.raises(RuntimeError, match='synthetic JEV unavailable'):
        plan_song_content('歌', '回复', 40, gateway=gateway, expression_context=context())
    assert gateway.calls == []
