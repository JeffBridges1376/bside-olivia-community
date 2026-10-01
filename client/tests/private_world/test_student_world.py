import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from runtime.private_world import student_world
from tests.private_world.decisions import life_decision

NOW = datetime(2026, 9, 28, 6, 15, tzinfo=timezone.utc)
CURRENT = {'location': '学校', 'activity': '专业课', 'note': '在听老师讲这一段。'}


def test_empty_world_still_projects_schedule_without_inventing_attendance(tmp_path):
    store = DailyLifeStore(tmp_path/'life.db')
    context = json.loads(store.reply_context('你现在有课吗', now=NOW))
    assert context['schedule']['current_class']['title'] == '钢琴专业课'
    assert context['current'] is None
    assert context['weather']['status'] == 'unknown'
    assert context['meals'] == []
    assert len(store.reply_context('你现在有课吗', now=NOW)) <= 1800


def test_schedule_time_boundaries_and_graduation():
    schedule = student_world.student_schedule(NOW)
    assert schedule['year'] == 2
    assert schedule['current_class']['title'] == '钢琴专业课'
    noon = student_world.student_schedule(NOW.replace(hour=4, minute=0))
    assert noon['current_class'] is None
    assert noon['next_class']['title'] == '钢琴专业课'
    assert noon['next_class']['start'].endswith('14:00:00+08:00')
    assert schedule['next_class'] is None
    assert student_world.student_schedule(NOW + timedelta(minutes=45))['current_class'] is None
    assert student_world.student_schedule(NOW - timedelta(days=1))['classes'] == []
    assert student_world.student_schedule(NOW.replace(month=8))['phase'] == 'vacation'
    assert student_world.student_schedule(NOW.replace(year=2029, month=7))['classes'] == []
    assert student_world.student_schedule(NOW.astimezone(timezone(timedelta(hours=-5)))) == schedule


def test_meals_persist_and_cannot_be_rewritten_atomically(tmp_path):
    path = tmp_path / 'life.db'
    store = DailyLifeStore(path)
    meal = {'slot': 'lunch', 'food': '番茄鸡蛋饭', 'status': 'eaten'}
    store.publish_day('day:one', CURRENT, [], occurred_at=NOW, meals=[meal])
    restarted = DailyLifeStore(path)
    assert restarted.snapshot(NOW)['world']['meals'][0]['food'] == meal['food']
    with pytest.raises(ValueError, match='MEAL_REWRITE'):
        restarted.publish_day('day:two', CURRENT, [], occurred_at=NOW+timedelta(hours=1),
                              meals=[{**meal, 'food': '小馄饨'}])
    assert not restarted.has_source('day:two')
    restarted.publish_day('day:tomorrow', CURRENT, [], occurred_at=NOW+timedelta(days=1),
                          meals=[{**meal, 'food':'咖喱饭'}])
    assert restarted.snapshot(NOW-timedelta(seconds=1))['world']['meals'] == []
    context = json.loads(restarted.reply_context('午饭吃了什么', now=NOW, max_chars=3000))
    assert context['meals'][0]['food'] == meal['food']
    assert context['schedule']['current_class']['title'] == '钢琴专业课'


def test_real_observations_are_checked_and_network_failure_is_unknown(monkeypatch):
    records = [{'icaoId':'ZSSS', 'obsTime': NOW.timestamp(), 'temp':25, 'cover':'BKN', 'wxString':'-RA'}]
    status = 200
    malformed = False

    async def handle(request):
        assert request.method == 'GET'
        assert dict(request.query) == {'ids': 'ZSSS', 'format': 'json'}
        assert request.headers['User-Agent'] == 'Olivia-World/1.0'
        if malformed:
            return web.Response(text='{broken', content_type='application/json')
        return web.json_response(records, status=status)

    async def scenario():
        nonlocal status, malformed
        app = web.Application()
        app.router.add_get('/api/data/metar', handle)
        server = TestServer(app)
        await server.start_server()
        url = server.make_url('/api/data/metar')
        real_get = aiohttp.ClientSession.get

        def local_get(client, target, **kwargs):
            assert target == 'https://aviationweather.gov/api/data/metar'
            return real_get(client, url, **kwargs)

        monkeypatch.setattr(aiohttp.ClientSession, 'get', local_get)
        try:
            value = await student_world.shanghai_weather(NOW)
            assert value['temperature_c'] == 25
            assert student_world.weather_view(value, NOW)['status'] == 'fresh'
            assert student_world.weather_view(value, NOW+timedelta(hours=3))['status'] == 'stale'
            records[0]['obsTime'] += 60
            assert await student_world.shanghai_weather(NOW) is None
            records.clear()
            assert await student_world.shanghai_weather(NOW) is None
            status = 503
            assert await student_world.shanghai_weather(NOW) is None
            malformed = True
            assert await student_world.shanghai_weather(NOW) is None
        finally:
            await server.close()
        assert await student_world.shanghai_weather(NOW) is None

    asyncio.run(scenario())


def test_weather_timeout_is_unknown(monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError('offline')
    monkeypatch.setattr(aiohttp.ClientSession, 'get', timeout)
    assert asyncio.run(student_world.shanghai_weather(NOW)) is None


def test_refresh_connects_weather_schedule_meals_and_history(tmp_path):
    calls = []
    weather = {'observed_at': NOW.isoformat(), 'temperature_c':25, 'station':'ZSSS'}
    async def provider(now):
        return weather
    class Gateway:
        async def complete(self, messages, **kwargs):
            calls.append(json.loads(messages[1]['content']))
            return SimpleNamespace(text=json.dumps(life_decision(messages)))
    store = DailyLifeStore(tmp_path/'life.db')
    store.publish_day('day:lunch', CURRENT, [], occurred_at=NOW-timedelta(hours=2),
                      meals=[{'slot':'lunch','food':'米饭和青菜','status':'eaten'}])
    runtime = DailyLifeRuntime(store, Gateway, lambda:'钢琴专业', weather_provider=provider)
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code is None
    assert calls[0]['world']['weather']['status'] == 'fresh'
    assert calls[0]['world']['schedule']['current_class']['title'] == '钢琴专业课'
    assert store.snapshot(NOW)['world']['meals'][0]['food'] == '米饭和青菜'
    assert store.snapshot(NOW)['world']['weather']['temperature_c'] == 25
    assert store.snapshot(NOW+timedelta(hours=3))['world']['weather']['status'] == 'stale'


def test_weather_refresh_does_not_wait_for_life_or_depend_on_llm_success(tmp_path):
    calls = []
    async def provider(now):
        calls.append(now)
        return {'observed_at': now.isoformat(), 'temperature_c': 25, 'station': 'ZSSS'}
    class UnavailableGateway:
        async def complete(self, *args, **kwargs):
            raise RuntimeError('unavailable')
    store = DailyLifeStore(tmp_path/'life.db')
    runtime = DailyLifeRuntime(store, UnavailableGateway, lambda: '', weather_provider=provider)
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code == 'DAILY_LIFE_GENERATION_UNAVAILABLE'
    assert DailyLifeStore(store.path).snapshot(NOW)['world']['weather']['status'] == 'fresh'
    context = json.loads(DailyLifeStore(store.path).reply_context('现在天气如何', now=NOW))
    assert context['weather']['status'] == 'fresh'
    assert context['current'] is None
    store.publish_day('day:current', CURRENT, [], occurred_at=NOW)
    asyncio.run(runtime.refresh(NOW+timedelta(minutes=31)))
    assert calls == [NOW, NOW+timedelta(minutes=31)]
    assert store.snapshot(NOW)['world']['weather']['observed_at'] == NOW.isoformat()
    assert store.snapshot(NOW-timedelta(seconds=1))['world']['weather']['status'] == 'unknown'


@pytest.mark.parametrize('error', [aiohttp.ClientConnectionError('offline'), TimeoutError('offline')])
def test_weather_failure_does_not_block_daily_life_or_retry_every_minute(tmp_path, error):
    calls = []
    async def provider(now):
        calls.append(now)
        raise error
    class Gateway:
        async def complete(self, *args, **kwargs):
            return SimpleNamespace(text=json.dumps(life_decision(args[0])))
    store = DailyLifeStore(tmp_path/'life.db')
    runtime = DailyLifeRuntime(store, Gateway, lambda: '', weather_provider=provider)
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code is None
    asyncio.run(runtime.refresh(NOW+timedelta(minutes=1)))
    assert calls == [NOW]
    assert store.snapshot(NOW)['world']['weather']['status'] == 'unknown'


def test_class_boundaries_expire_current_activity_without_inventing_attendance(tmp_path):
    store = DailyLifeStore(tmp_path/'life.db')
    before = NOW.replace(hour=5, minute=50)  # Shanghai 13:50, ten minutes before class.
    store.publish_day('day:home', {'location':'家里', 'activity':'休息', 'note':'坐着歇会儿。'}, [], occurred_at=before)
    assert not store.snapshot(before)['stale']
    during = before+timedelta(minutes=10)
    assert store.snapshot(during)['stale']
    context = json.loads(store.reply_context('现在在做什么', now=during, max_chars=3000))
    assert context['current'] is None
    assert context['schedule']['current_class'] is not None
    store.publish_day('day:class', CURRENT, [], occurred_at=during)
    assert not store.snapshot(during)['stale']
    assert store.snapshot(during+timedelta(hours=1))['stale']


@pytest.mark.parametrize('value', [
    {'observed_at':'invalid', 'temperature_c':25},
    {'observed_at':'2026-09-28T06:15:00', 'temperature_c':25},
    {'observed_at':NOW.isoformat(), 'temperature_c':float('nan')},
    {'observed_at':NOW.isoformat(), 'temperature_c':100},
])
def test_bad_cached_weather_is_unknown_instead_of_breaking_world(value):
    assert student_world.weather_view(value, NOW)['status'] == 'unknown'


def test_skipped_meal_does_not_require_inventing_food(tmp_path):
    store = DailyLifeStore(tmp_path/'life.db')
    store.publish_day('day:skip', CURRENT, [], occurred_at=NOW,
                      meals=[{'slot':'lunch','food':'','status':'skipped'}])
    assert store.snapshot(NOW)['world']['meals'][0]['status'] == 'skipped'
    with pytest.raises(ValueError):
        store.publish_day('day:empty-eaten', CURRENT, [], occurred_at=NOW+timedelta(days=1),
                          meals=[{'slot':'lunch','food':'','status':'eaten'}])


@pytest.mark.parametrize('activity', ['刚结束下午的钢琴专业课，正在收拾乐谱', '刚吃完饭，正在收拾餐桌', '吃完午饭收拾碗筷'])
def test_contradictory_current_activity_gets_one_correction_and_is_not_published(tmp_path, activity):
    calls = []
    class Gateway:
        async def complete(self, messages, **kwargs):
            calls.append(json.loads(messages[1]['content']))
            return SimpleNamespace(text=json.dumps({'current':{**CURRENT,'activity':activity}, 'projects':[], 'meals':[]}))
    store = DailyLifeStore(tmp_path/'life.db')
    runtime = DailyLifeRuntime(store, Gateway, lambda: '')
    asyncio.run(runtime.refresh(NOW))
    assert len(calls) == 2
    assert 'validation_error' in calls[1]
    assert runtime.error_code == 'DAILY_LIFE_GENERATION_UNAVAILABLE'
    assert store.snapshot(NOW)['current'] is None


def test_world_accepts_corrected_activity(tmp_path):
    calls=[]
    class Gateway:
        async def complete(self, messages, **kwargs):
            calls.append(messages)
            payload = ({'current':{**CURRENT,'activity':'刚下课，收拾乐谱'}, 'projects':[], 'meals':[]}
                       if len(calls)==1 else life_decision(messages))
            return SimpleNamespace(text=json.dumps(payload))
    store = DailyLifeStore(tmp_path/'life.db')
    runtime = DailyLifeRuntime(store, Gateway, lambda: '')
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code is None
    assert len(calls)==2
    assert store.snapshot(NOW)['current']['activity']=='上钢琴专业课'


def test_meal_correction_cannot_escape_into_a_finished_morning_class(tmp_path):
    now = datetime(2026,9,29,4,15,tzinfo=timezone.utc)
    calls=[]
    class Gateway:
        async def complete(self, messages, **kwargs):
            calls.append(messages)
            activity = '刚吃完饭，正在收拾餐桌' if len(calls)==1 else '正在上大学外语课，坐在靠窗的位置听讲。'
            return SimpleNamespace(text=json.dumps({'current':{**CURRENT,'activity':activity}, 'projects':[], 'meals':[]}))
    store = DailyLifeStore(tmp_path/'life.db')
    runtime = DailyLifeRuntime(store, Gateway, lambda: '')
    asyncio.run(runtime.refresh(now))
    assert len(calls)==2
    assert runtime.error_code == 'DAILY_LIFE_GENERATION_UNAVAILABLE'
    assert store.snapshot(now)['current'] is None
