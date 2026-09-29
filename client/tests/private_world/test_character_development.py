import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from runtime.private_world.daily_life import DailyLifeStore


NOW = datetime(2026, 9, 1, 4, tzinfo=timezone.utc)
TOPIC = {'key': 'photography', 'label': '摄影', 'kind': 'interest', 'baseline': 'neutral', 'anchor': False}
PERSONA = json.dumps([{'declaration_id': 'background.independent_life', 'statement': '她有自己的生活。',
                       'development': [TOPIC]}], ensure_ascii=False)


def add(store, index, day, *, stance='positive', topic='photography', withdraws=None, episode_id='new'):
    user = f'我们今天一起拍完第{index}组照片，讨论了摄影构图。'
    reply = '这次摄影构图的练习让我觉得很有意思。' if stance == 'positive' else '这次摄影构图让我觉得很不适合自己。'
    if withdraws:
        user = '我更正一下，之前说一起完成的摄影练习其实没有发生。'
        reply = '明白，那次摄影练习的评价应当撤回。'
    updates = [] if withdraws else [{'id':f'photo{index}', 'title':'一起拍照', 'detail':user, 'kind':'shared',
                                    'actor':'user', 'quote':user, 'status':'completed'}]
    return store.record_exchange(f'reply:development:{index}', user, reply, updates, occurred_at=NOW+timedelta(days=day),
        relationship=None if withdraws else {'kind': 'shared_experience', 'user_quote': user, 'reply_quote': reply},
        development=[{'key': topic, 'stance': stance, 'user_quote': user,
                      'character_quote': reply, 'experience_quote': user,
                      'episode_id':None if withdraws else 'shared:photo'+str(index) if episode_id == 'new' else episode_id,
                      'withdraws': withdraws}])


def test_slow_growth_restart_same_day_cap_and_as_of(tmp_path):
    path = tmp_path/'life.sqlite3'
    store = DailyLifeStore(path)
    store.configure_development(PERSONA)
    for i in range(12):
        add(store, i, 0)
    assert store.development_view(NOW)['items'][0]['stage'] == 'trying'
    add(store, 12, 7)
    add(store, 13, 14)
    view = store.development_view(NOW+timedelta(days=14))
    assert view['items'][0]['stage'] == 'growing'
    assert view == DailyLifeStore(path).development_view(NOW+timedelta(days=14))
    assert store.development_view(NOW)['items'][0]['stage'] == 'trying'
    with pytest.raises(ValueError, match='SOURCE_CONFLICT'):
        store.record_exchange('reply:development:13', 'different', 'different', [], occurred_at=NOW)


def test_negative_evidence_and_evidenced_withdrawal_replay(tmp_path):
    store = DailyLifeStore(tmp_path/'life.sqlite3'); store.configure_development(PERSONA)
    for i, day in enumerate((0, 7, 14)):
        add(store, i, day)
    before = store.development_view(NOW+timedelta(days=14))
    add(store, 3, 15, withdraws='reply:development:2')
    assert store.development_view(NOW+timedelta(days=15))['items'][0]['stage'] == 'trying'
    assert store.development_view(NOW+timedelta(days=14)) == before
    add(store, 4, 16, stance='negative')
    assert store.development_view(NOW+timedelta(days=16))['items'][0]['stage'] != 'growing'


def test_conflicting_evaluation_of_same_episode_weakens_instead_of_double_counting(tmp_path):
    store = DailyLifeStore(tmp_path/'life.sqlite3'); store.configure_development(PERSONA)
    for i, day in enumerate((0,7,14)):
        add(store,i,day)
    add(store,3,15,stance='negative',episode_id='shared:photo0')
    assert store.development_view(NOW+timedelta(days=15))['items'][0]['stage']=='trying'


def test_protected_core_unknown_key_and_user_preference_are_not_evidence(tmp_path):
    store = DailyLifeStore(tmp_path/'life.sqlite3'); store.configure_development(PERSONA)
    with pytest.raises(ValueError, match='DEVELOPMENT'):
        add(store, 0, 0, topic='identity.name')
    with pytest.raises(ValueError, match='DEVELOPMENT'):
        store.record_exchange('reply:preference', '我喜欢摄影，你也要喜欢。', '我喜欢摄影。', [], occurred_at=NOW,
            development=[{'key':'photography','stance':'positive','user_quote':'我喜欢摄影，你也要喜欢。',
                          'character_quote':'我喜欢摄影。','experience_quote':'我喜欢摄影，你也要喜欢。','episode_id':None,'withdraws':None}])
    assert store.development_view(NOW)['items'] == []


def test_anchored_dislike_never_flips_to_love(tmp_path):
    store = DailyLifeStore(tmp_path/'life.sqlite3')
    store.configure_development(PERSONA.replace('"neutral"', '"avoid"').replace('false', 'true'))
    for i in range(12):
        add(store, i, i*7)
    item = store.development_view(NOW+timedelta(days=90))['items'][0]
    assert item['stage'] == 'willing_to_try'
    assert item['baseline'] == 'avoid'


def test_traits_need_longer_span_and_explicit_core_metadata_is_rejected(tmp_path):
    store=DailyLifeStore(tmp_path/'life.sqlite3')
    store.configure_development(PERSONA.replace('"interest"','"trait"'))
    for i,day in enumerate((0,7,14)):
        add(store,i,day)
    assert store.development_view(NOW+timedelta(days=14))['items'][0]['stage']=='trying'
    add(store,3,21);add(store,4,28)
    assert store.development_view(NOW+timedelta(days=28))['items'][0]['stage']=='growing'
    with pytest.raises(ValueError,match='DEVELOPMENT_TOPIC_INVALID'):
        store.configure_development(json.dumps([{'inclusion':'core','development':[TOPIC]}]))


def test_delayed_exchange_cannot_select_episode_after_its_receipt(tmp_path):
    store=DailyLifeStore(tmp_path/'life.sqlite3'); store.configure_development(PERSONA)
    add(store,0,7)
    user='我们完成了一次拍照。';reply='这次拍照很有意思。'
    with pytest.raises(ValueError,match='DEVELOPMENT_EPISODE_INVALID'):
        store.record_exchange('reply:late',user,reply,[],occurred_at=NOW+timedelta(days=9), received_at=NOW,
            relationship={'kind':'shared_experience','user_quote':user,'reply_quote':reply},
            development=[{'key':'photography','stance':'positive','user_quote':user,'character_quote':reply,
                          'experience_quote':user,'episode_id':'shared:photo0','withdraws':None}])
    assert not store.has_source('reply:late')


def test_actual_runtime_extraction_commits_overlay_and_context(tmp_path):
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    store = DailyLifeStore(tmp_path/'life.sqlite3')
    runtime = DailyLifeRuntime(store, lambda: None, lambda: PERSONA)
    calls=[]
    async def complete(prompt, data, request_id, **kwargs):
        calls.append(data)
        identity='photo'+str(len(calls))
        return {'updates':[{'id':identity,'title':'一起拍照','detail':data['user_letter'],'kind':'shared','actor':'user',
                            'quote':data['user_letter'],'status':'completed'}],
                'relationship':{'kind':'shared_experience', 'user_quote':data['user_letter'], 'reply_quote':data['linli_reply']},
                'development':[{'key':'photography','stance':'positive','user_quote':data['user_letter'],
                                'character_quote':data['linli_reply'],'experience_quote':data['user_letter'],
                                'episode_id':'shared:'+identity,'withdraws':None}]}
    runtime._complete = complete
    for i, day in enumerate((0,7,14)):
        asyncio.run(runtime.consume_exchange(f'reply:runtime:{i}', f'今天一起拍完第{i}组摄影作品。',
            '这次拍照让我觉得很有意思。', occurred_at=NOW+timedelta(days=day)))
    assert len(calls) == 3
    context=json.loads(store.reply_context('', now=NOW+timedelta(days=14), max_chars=6000))
    assert context['character_development']['items'][0]['stage'] == 'growing'
    assert calls[-1]['development_topics'][0]['key'] == 'photography'


def test_old_episode_paraphrases_and_unverified_attempts_never_mature(tmp_path):
    store = DailyLifeStore(tmp_path/'life.sqlite3'); store.configure_development(PERSONA)
    add(store, 0, 0)
    for i in range(1, 8):
        add(store, i, i*7, episode_id='shared:photo0')
    assert store.development_view(NOW+timedelta(days=60))['items'][0]['stage'] == 'trying'
    fresh=DailyLifeStore(tmp_path/'fresh.sqlite3'); fresh.configure_development(PERSONA)
    for i in range(8):
        add(fresh, i, i*7, episode_id=None)
    assert fresh.development_view(NOW+timedelta(days=60))['items'][0]['stage'] == 'trying'


def test_default_reply_budget_keeps_mature_topics_when_several_exist(tmp_path):
    store = DailyLifeStore(tmp_path/'life.sqlite3')
    catalog=[{**TOPIC,'key':key} for key in ('photography','walking','cooking','jazz','reading','vinyl')]
    store.configure_development(json.dumps([{'development':catalog}]))
    for j, topic in enumerate(catalog):
        for i, day in enumerate((0,7,14)):
            add(store, j*3+i, day, topic=topic['key'])
    context=json.loads(store.reply_context('',now=NOW+timedelta(days=15)))
    assert context['character_development']['items']
    assert all(i['stage']=='growing' for i in context['character_development']['items'])


def test_source_damage_cannot_supply_growth_or_revoke_valid_evidence(tmp_path):
    store = DailyLifeStore(tmp_path/'life.sqlite3'); store.configure_development(PERSONA)
    for i,day in enumerate((0,7,14)):
        add(store,i,day)
    add(store,3,15,withdraws='reply:development:2')
    assert store.development_view(NOW+timedelta(days=15))['items'][0]['stage']=='trying'
    with store._db() as db:
        raw=json.loads(db.execute("SELECT payload FROM life_moments WHERE source_id='reply:development:3'").fetchone()[0])
        raw['digest']='damaged'
        db.execute("UPDATE life_moments SET payload=? WHERE source_id='reply:development:3'",(json.dumps(raw),))
    assert store.development_view(NOW+timedelta(days=15))['items'][0]['stage']=='growing'


def approve(emotion, source_id, quote, *, reaction='pleased', revises=None):
    emotion.commit(emotion.assessment([source_id]), {'appraisals':[{
        'source_id':source_id, 'quote':quote, 'reaction':reaction, 'action_tendency':'continue',
        'goal_or_need':None, 'concern':None, 'revises':revises, 'reported_affect':None}]})


def test_world_writer_selects_grounded_evaluation_without_reaction_to_taste_shortcut(tmp_path):
    from runtime.private_world.character_emotion import CharacterEmotionStore
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    store=DailyLifeStore(tmp_path/'life.sqlite3'); store.configure_development(PERSONA)
    emotion=CharacterEmotionStore(store.path)
    runtime=DailyLifeRuntime(store, lambda:None, lambda:PERSONA)
    async def no_emotion(now):
        pass
    calls=[]
    async def complete(prompt, data, request_id, **kwargs):
        calls.append(data)
        prior=data['development_basis']['sources'][-1]
        return {'activity':{'kind':'rest','place_id':'home','focus':''}, 'meal':None, 'project':None,
                'development':[{'source_id':prior['source_id'],'key':'photography','stance':'positive',
                                'quote':prior['world']['note'],'reason':'对构图观察本身产生了具体兴趣，不只是被称赞。'}]}
    runtime._complete=complete
    for i, day in enumerate((0,7,14)):
        when=NOW+timedelta(days=day)
        source=f'day:photo{i}'
        note=f'在窗边拍照，尝试第{i}种构图。'
        store.publish_day(source, {'location':'家里','activity':'拍照','note':note}, [],
                          occurred_at=when, activity_kind='creative')
        emotion.publish(source); approve(emotion, source, note)
        # An approved happy reaction by itself never writes a preference.
        assert len(store.development_view(when)['items']) == (0 if i == 0 else 1)
        asyncio.run(runtime.refresh(when+timedelta(hours=5)))
        assert runtime.error_code is None
    assert len(calls) == 3
    assert store.development_view(NOW+timedelta(days=15))['items'][0]['stage'] == 'growing'
    assert store.development_view(NOW)['items'] == []
    correction_time=NOW+timedelta(days=14,hours=5,minutes=1)
    emotion.receive('new:correction', '那次对拍照的理解不准确。', occurred_at=correction_time)
    approve(emotion, 'new:correction', '那次对拍照的理解不准确。', reaction='none',
            revises={'source_id':'day:photo2','action':'withdraw'})
    assert store.development_view(correction_time)['items'][0]['stage'] == 'trying'
    assert store.development_view(correction_time-timedelta(minutes=1))['items'][0]['stage'] == 'growing'


def test_world_evaluation_cannot_use_unknown_or_planned_food_source(tmp_path):
    from runtime.private_world.character_emotion import CharacterEmotionStore
    store=DailyLifeStore(tmp_path/'life.sqlite3')
    topic={**TOPIC,'key':'light_food','label':'清淡口味','kind':'taste','baseline':'like'}
    store.configure_development(json.dumps([{'development':[topic]}]))
    emotion=CharacterEmotionStore(store.path)
    note='准备吃小馄饨。'
    store.publish_day('day:food', {'location':'家里','activity':'准备吃饭','note':note}, [], occurred_at=NOW,
                      activity_kind='meal', meals=[{'slot':'lunch','food':'小馄饨','status':'planned'}])
    emotion.publish('day:food'); approve(emotion,'day:food',note)
    basis=store.development_world_assessment(NOW)
    with pytest.raises(ValueError,match='DEVELOPMENT'):
        store.publish_day('day:next', {'location':'家里','activity':'休息','note':'休息。'}, [], occurred_at=NOW,
            development_basis=basis, development=[{'source_id':'day:food','key':'light_food','stance':'positive','quote':note,'reason':'喜欢清淡食物。'}])
    assert not store.has_source('day:next')
    assert store.development_view(NOW)['items'] == []
