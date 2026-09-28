"""Actual world renderer orders plans and meal events using server timestamps."""
import re

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


@pytest.mark.parametrize('main', [True, False], ids=['world', 'settings'])
def test_courses_and_meals_share_a_truthful_chronological_timeline(tmp_path, main):
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    renderer = source.split('  const renderPrivateWorldPanel =', 1)[1].split('  const setupInput =', 1)[0]
    css = re.search(r'const mountWorldPage = .*?style.textContent=`(.*?)`;', source, re.S).group(1)
    day = '2026-09-28'
    stamp = lambda clock: day + 'T' + clock + ':00+08:00'
    payload = dict(schema_version='olivia.daily-life.v1', stale=False, refreshing=False,
        current=None, rhythm=dict(activity='在家休息', note='当前活动'), projects=[], shared=[], moments=[],
        emotion=dict(status='available', reactions=[], concerns=[],current_affect=dict(
            status='available',label='calm',as_of=stamp('16:45'),
            reason='练习后的休息与刚才被理解的对话，让心情渐渐平稳。')),
        world=dict(schedule=dict(date=day, classes=[
            dict(start=stamp('15:00'), end=stamp('16:00'), title='钢琴主课'),
            dict(start=stamp('09:00'), end=stamp('10:00'), title='音乐分析')]),
            meal_schedule=[dict(date=day, slot=slot, scheduled_for=stamp(clock), status=status)
                for slot, clock, status in [('breakfast','08:00','pending'),('lunch','12:00','pending'),('dinner','18:00','not_due')]],
            recent_episodes=[dict(schema='character-life-episode/1',source_id='private-episode-source',
                occurred_at=stamp('16:30'),actor='character',activity_kind='practice',
                trigger=dict(kind='continuation',detail='接着试上次还没弹稳的片段。'),
                process=[dict(obstacle='衔接处仍然容易抢拍',response='先分手慢练，再试着合起来',outcome='合手时仍不稳定')],
                result=dict(status='paused',detail='这次先停在这里，还没有练好。'),
                interpretation=dict(subjective=True,meaning='<img src=x onerror=alert(1)>进展比想象中慢，有些受挫',need='progress'),
                effects=dict(next_action='下次先回到衔接处',open_loop='衔接仍需磨合'))],
            meals=[
                dict(slot='dinner', food='青菜面', status='planned', date=day,
                    occurred_at=stamp('07:10'), scheduled_for=stamp('18:30')),
                dict(slot='breakfast', food='豆浆', status='eaten', date=day,
                    occurred_at=stamp('08:05'), started_at=stamp('07:45'), finished_at=stamp('08:05'), recovered=False),
                dict(slot='lunch', food='馄饨', status='eaten', date=day,
                    occurred_at=stamp('12:40'), started_at=stamp('12:15'), finished_at=stamp('12:40'),
                    recorded_at=stamp('14:10'), recovered=True),
                dict(slot='snack', food='昨天的点心', status='eaten', date='2026-09-27', occurred_at='2026-09-27T23:00:00+08:00'),
            ]))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width':1100,'height':1000})
        errors=[]
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content('<style>body{background:#101112;color:#eee8de;font:15px/1.65 "Microsoft YaHei",sans-serif;padding:22px}*{box-sizing:border-box}p{margin:8px 0}.text-text-secondary{color:#acb0b4}button{font:inherit}</style><div data-olivia-world-page><section id="world" '+('data-world-main' if main else '')+'></section></div>')
        page.add_style_tag(content=css)
        page.add_script_tag(content='''
const text=(tag,value,cls='')=>{const e=document.createElement(tag);e.textContent=value;e.className=cls;return e};
const button=(label,handler)=>{const e=text('button',label);e.onclick=handler;return e};
const actions=()=>document.createElement('div'),stack=actions;
const privateWorldState=c=>c.state,stateLabels={available:'可用'},setDiagnosticDetails=()=>{};
const DAILY_LIFE_PATH='/life',PRIVATE_WORLD_PATH='/relationship';
const requestJson=async path=>path==='/relationship'?{status:'READY',levels:{},relationship_stage:'unknown'}:window.payload;
const requestMutation=async()=>window.payload;
window.setTimeout=()=>0;
window.renderWorld = '''+renderer+';')
        def render():
            page.evaluate('(value)=>window.payload=value', payload)
            page.evaluate("async()=>{await window.renderWorld(document.querySelector('#world'),{state:'available'})}")
            return page.locator('#world').inner_text()
        body = render()
        labels = ['07:45–08:05', '09:00–10:00', '12:15–12:40', '15:00–16:00', '计划 18:30']
        positions = [body.index(label) for label in labels]
        assert positions == sorted(positions)
        assert '补记于 14:10' in body
        assert '课表计划' in body
        assert '早餐 · 已吃豆浆' in body and '晚餐 · 计划吃青菜面' in body
        assert '07:10' not in body and '昨天的点心' not in body
        page.screenshot(path=str(tmp_path/'timeline-recorded.png'),full_page=True)
        # Experience details live in the history tab, not the overview or today's agenda.
        if main:
            assert not page.locator('.olivia-world-episode').is_visible()
            page.get_by_role('tab',name='生活记录',exact=True).click()
        episode=page.locator('.olivia-world-episode')
        assert episode.count()==1 and episode.get_attribute('open') is None
        assert '2026/9/28 16:30' in episode.locator('summary').inner_text()
        assert '练琴 · 暂停' in episode.locator('summary').inner_text()
        episode.locator('summary').click()
        detail=episode.inner_text()
        assert '已发生的过程' in detail and '先分手慢练，再试着合起来' in detail
        assert '结果 · 暂停：这次先停在这里，还没有练好。' in detail
        assert '她的理解（主观）：<img src=x onerror=alert(1)>进展比想象中慢' in detail
        assert '下一步打算（尚未发生）：下次先回到衔接处' in detail
        assert '仍待处理：衔接仍需磨合' in detail
        assert 'private-episode-source' not in detail and 'progress' not in detail
        assert episode.locator('img').count()==0
        page.screenshot(path=str(tmp_path/'episode-expanded.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':1100})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path/'episode-narrow.png'),full_page=True)
        page.set_viewport_size({'width':1100,'height':1000})
        if main: page.get_by_role('tab',name='今天',exact=True).click()
        # Missing records depend on explicit server lifecycle, never the browser clock.
        payload['world']['meals'] = []
        payload['world']['meal_schedule'][0].update(status='error', error_code='JEV_UNAVAILABLE')
        body = render()
        assert '计划 08:00' in body and '早餐 · 用餐更新失败（JEV_UNAVAILABLE）' in body
        assert '计划 12:00' in body and '午餐 · 用餐记录待更新' in body
        assert '计划 18:00' in body and '晚餐 · 未到用餐时间' in body
        assert '尚未确认' not in body and '这餐没吃' not in body
        page.screenshot(path=str(tmp_path/'timeline-pending.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':1100})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path/'timeline-narrow.png'),full_page=True)
        assert not errors
        browser.close()
