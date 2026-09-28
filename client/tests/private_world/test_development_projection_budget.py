from datetime import datetime, timedelta, timezone
import json

import pytest

from runtime.private_world.daily_life import DailyLifeStore


@pytest.mark.parametrize('stale', [False, True])
@pytest.mark.parametrize('note_length', [40, 120])
def test_default_world_budget_keeps_current_and_complete_development_items(tmp_path, stale, note_length):
    store = DailyLifeStore(tmp_path / 'life.sqlite')
    start = datetime(2026, 9, 1, 4, tzinfo=timezone.utc)
    topics = [
        {'key': key, 'label': label, 'kind': 'interest', 'baseline': 'neutral', 'anchor': False}
        for key, label in [('photography', '摄影'), ('walking', '散步'), ('cooking', '做饭'),
                           ('jazz', '爵士乐'), ('reading', '阅读'), ('vinyl', '黑胶')]
    ]
    store.configure_development(json.dumps([{'development': topics}], ensure_ascii=False))
    for topic in topics:
        for index, day in enumerate((0, 7, 14)):
            episode = f"{topic['key']}{index}"
            user = f"我们今天一起完成第{index}次{topic['label']}体验，交流了具体细节。"
            reply = f"这次{topic['label']}体验让我觉得很有意思。"
            store.record_exchange(
                'reply:' + episode, user, reply,
                [{'id': episode, 'title': topic['label'], 'detail': user, 'kind': 'shared',
                  'actor': 'user', 'quote': user, 'status': 'completed'}],
                occurred_at=start + timedelta(days=day),
                relationship={'kind': 'shared_experience', 'user_quote': user, 'reply_quote': reply},
                development=[{'key': topic['key'], 'stance': 'positive', 'user_quote': user,
                              'character_quote': reply, 'experience_quote': user,
                              'episode_id': 'shared:' + episode, 'withdraws': None}])
    observed_at = start + timedelta(days=15)
    note = ('在家读书，停在窗边那一页，准备继续整理关于构图的笔记。' * 5)[:note_length]
    store.publish_day('day:review:current', {'location': '家里', 'activity': '阅读', 'note': note},
                      [], occurred_at=observed_at, activity_kind='reading')
    now = observed_at + timedelta(hours=8 if stale else 0)
    full = store.development_view(now)
    assert len(full['items']) == 6 and all(item['stage'] == 'growing' for item in full['items'])

    text = store.reply_context('摄影', now=now)  # Actual default budget, not a larger fixture budget.
    assert text and len(text) <= 1800
    world = json.loads(text)
    assert world['stale'] is stale
    current = world['last_observation'] if stale else world['current']
    assert current['source_id'] == 'day:review:current' and current['note'] == note
    assert current['actor'] == 'linli' and current['evidence_kind'] == 'published_life'
    if stale:
        assert world['current'] is None
    development = world['character_development']
    assert 0 < len(development['items']) < 6
    assert development['omitted_count'] == 6 - len(development['items'])
    assert development['version'] == full['version']
    assert development['as_of'] == full['as_of']
    assert development['items'][0]['key'] == 'photography'
    assert all(item in full['items'] for item in development['items'])
    assert store.has_source('day:review:current')
