"""New authored experiences: process facts, subjective meaning, and next steps."""
import json
from datetime import datetime, timedelta, timezone
from .world_decision import KINDS


_ACTIVITY_PATHS = {
    'class': ('跟进当前正在进行的课程', [
        ('概念还没有理清', '对照自己的笔记重新整理', 'partial', '当前这一处内容理清了一点，课程仍在进行。'),
        ('注意力有些分散', '把注意力收回当前内容，记下疑问', 'partial', '记下了还未弄懂的问题，尚未解决，课程仍在进行。'),
        ('有一部分课堂内容还没理解', '先记下疑问，继续跟进课程内容', 'failed', '课堂上有一部分内容还没理解，已记下疑问，留待继续梳理。')]),
    'reading': ('继续理解手头正在读的内容', [
        ('一段意思不容易把握', '回读前后文并做简短笔记', 'partial', '理清了部分意思，还有一处疑问。'),
        ('阅读节奏断断续续', '缩小到当前这一小段', 'completed', '完成了这一小段阅读和整理。'),
        ('注意力维持不住', '标记读到的位置，暂时合上', 'paused', '这次阅读暂停，未读部分留待之后。')]),
    'creative': ('推进自己当前的小段创作', [
        ('想法还不够清楚', '先做一个粗略版本再比较', 'partial', '有了一个初稿，仍需修改。'),
        ('细节不够顺手', '删减多余部分，完成一个小版本', 'completed', '完成了本次选定的小版本。'),
        ('几种尝试都不满意', '保留草稿，暂时停下来', 'failed', '这次没有得到满意的版本，问题仍保留。')]),
    'walk': ('给自己留一点活动和观察的空间', [
        ('步调有点急', '放慢速度，留意脚下和周围', 'partial', '已经走了一小段，还在慢慢走。'),
        ('脑中仍挂着待办', '暂时把注意力放到行走上', 'completed', '结束了这一小段散步。'),
        ('不想继续走太久', '缩短这次散步', 'paused', '这次散步提前停下。')]),
    'errand': ('处理自己当前需要办的一件小事', [
        ('所需步骤还没理清', '先检查自己已有的信息与准备', 'partial', '完成了准备检查，事情尚未办完。'),
        ('随身所需物品还没整理好', '检查并整理这次自己需要带的东西', 'completed', '整理好了这次出门需要带的东西。'),
        ('准备还有遗漏', '列出自己还需要补齐的部分', 'paused', '这件事暂时搁置，仍有准备未完成。'),
        ('这一轮未能推进', '记录目前卡住的步骤', 'failed', '本次未办成，已记下卡住的步骤。')]),
    'housework': ('整理自己使用的生活空间', [
        ('东西放得有些散', '先只整理眼前的一小块', 'partial', '整理好了一部分，其余还没有动。'),
        ('容易一边整理一边分心', '把范围限定到当前的小区域', 'completed', '完成了眼前这一小块的整理。'),
        ('精力不足以继续', '收住当前动作，留下待整理部分', 'paused', '家务暂时停下，还留有未完成的部分。')]),
}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS life_episodes (
        source_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, payload TEXT NOT NULL)''')


def recent(db, now):
    return [json.loads(row[0]) for row in db.execute(
        'SELECT payload FROM life_episodes WHERE occurred_at<=? ORDER BY occurred_at DESC,source_id DESC LIMIT 6',
        (now.astimezone(timezone.utc).isoformat(),))]


def save(db, episode, source_id, now):
    if episode is None:
        return
    if (not isinstance(episode,dict) or set(episode)!={'schema','source_id','occurred_at','actor','activity_kind','trigger','process','result','interpretation','effects'}
            or episode.get('source_id') != source_id or episode.get('occurred_at') != now.astimezone(timezone.utc).isoformat()
            or episode.get('schema') != 'character-life-episode/1'
            or episode.get('actor') != 'character' or episode.get('activity_kind') not in KINDS
            or not isinstance(episode.get('interpretation'),dict)
            or episode['interpretation'].get('subjective') is not True):
        raise ValueError('LIFE_EPISODE_INVALID')
    if not (db.execute('SELECT 1 FROM life_moments WHERE source_id=?',(source_id,)).fetchone()
            or db.execute('SELECT 1 FROM life_meal_events WHERE source_id=?',(source_id,)).fetchone()):
        raise ValueError('LIFE_EPISODE_SOURCE_UNAVAILABLE')
    recovery = episode.get('effects', {}).get('body_recovery')
    if recovery is not None:
        import math
        if (not isinstance(recovery, dict) or not {'rest', 'baseline_load_minutes'} <= set(recovery)
                or set(recovery) - {'rest', 'baseline_load_minutes', 'remaining_load_minutes'}
                or recovery['rest'] not in {'tired', 'rested'}
                or type(recovery['baseline_load_minutes']) not in {int, float}
                or not math.isfinite(recovery['baseline_load_minutes']) or recovery['baseline_load_minutes'] < 0
                or episode['activity_kind'] != 'rest' or episode.get('result', {}).get('status') != 'completed'):
            raise ValueError('LIFE_EPISODE_RECOVERY_INVALID')
        remaining = recovery.get('remaining_load_minutes')
        if remaining is not None and (type(remaining) not in {int, float} or not math.isfinite(remaining)
                                      or not 0 <= remaining <= recovery['baseline_load_minutes']):
            raise ValueError('LIFE_EPISODE_RECOVERY_INVALID')
    sleep = episode.get('effects', {}).get('sleep_plan')
    if sleep is not None:
        if (not isinstance(sleep, dict) or set(sleep) != {'duration_minutes', 'end_at'}
                or type(sleep['duration_minutes']) is not int or sleep['duration_minutes'] not in {30, 60, 90}
                or sleep['end_at'] != (now + timedelta(minutes=sleep['duration_minutes'])).astimezone(timezone.utc).isoformat()
                or episode['activity_kind'] != 'rest' or episode['result']['status'] != 'paused' or recovery is not None):
            raise ValueError('LIFE_EPISODE_SLEEP_INVALID')
    resolution = episode.get('effects', {}).get('sleep_resolution')
    if resolution is not None:
        if not isinstance(resolution, dict) or set(resolution) != {'source_id'} or not isinstance(resolution['source_id'], str):
            raise ValueError('LIFE_EPISODE_SLEEP_INVALID')
        prior = db.execute('SELECT payload FROM life_episodes WHERE source_id=? AND occurred_at<=?',
                           (resolution['source_id'], episode['occurred_at'])).fetchone()
        if episode['activity_kind'] != 'rest' or not prior or not json.loads(prior[0]).get('effects', {}).get('sleep_plan'):
            raise ValueError('LIFE_EPISODE_SLEEP_INVALID')
    encoded = json.dumps(episode,ensure_ascii=False,sort_keys=True,allow_nan=False)
    if len(encoded)>4000:
        raise ValueError('LIFE_EPISODE_TOO_LARGE')
    old=db.execute('SELECT payload FROM life_episodes WHERE source_id=?',(source_id,)).fetchone()
    if old:
        if old[0]!=encoded: raise ValueError('LIFE_EPISODE_REWRITE')
        return
    db.execute('INSERT INTO life_episodes VALUES (?,?,?)',(source_id,episode['occurred_at'],encoded))


def _path(obstacle,response,status,detail):
    return dict(process=[dict(obstacle=obstacle,response=response,outcome=detail)],
                result=dict(status=status,detail=detail))


async def create(port, source_id, now, kind, context, *, meal=None):
    if kind not in KINDS:
        return None
    if kind == 'class' and not (context.get('world') or {}).get('schedule', {}).get('current_class'):
        raise ValueError('LIFE_EPISODE_CLASS_NOT_CURRENT')
    if meal and meal.get('recovered'):
        return None  # Recovery records a meal; it cannot retroactively invent its causes.
    from .day_plan import paths as planned_paths
    planned = planned_paths(context.get('day_plan'), kind, (context.get('selected_activity') or {}).get('focus', ''))
    if planned and kind in {'practice', *_ACTIVITY_PATHS}:
        # Today's authored outcomes for this activity, plus a plain way to stop.
        triggers = {'own_activity': '按今天自己的安排做这件事', 'continuation': '继续当前自己已经选定的这一小步'}
        paths = {f'plan_{index}': _path(item['obstacle'], item['response'], item['status'], item['outcome'])
                 for index, item in enumerate(planned)}
        paths['pause'] = _path('注意力难以维持', '结束这一轮，给自己留出间隔', 'paused', '这次提前停下，之后再接着做。')
    elif kind in _ACTIVITY_PATHS:
        motive, candidates = _ACTIVITY_PATHS[kind]
        triggers = {'own_activity': motive, 'continuation': '继续当前自己已经选定的这一小步'}
        paths = {str(index): _path(*candidate) for index, candidate in enumerate(candidates)}
        if kind == 'class':
            course = context['world']['schedule']['current_class'].get('title')
            if isinstance(course, str) and course.strip():
                triggers['own_activity'] = f'跟进当前的{course}'
                for path in paths.values():
                    path['result']['detail'] = f"{course}：{path['result']['detail']}"
                    for step in path['process']:
                        step['obstacle'] = f"{course}：{step['obstacle']}"
                        step['outcome'] = path['result']['detail']
    elif kind=='practice':
        triggers={'own_goal':'想把当前选定的小片段练得更稳','continuation':'继续处理当前练习中的未完成部分'}
        paths={
            'steady':_path('衔接还不够顺','放慢速度，单独重复衔接处','partial','这一处比开始时顺了一些，仍需再练。'),
            'finished':_path('节奏容易赶','先分拍确认，再连起来练','completed','本次选定的小片段完成了一遍稳定的连贯练习。'),
            'stuck':_path('同一处反复失误','缩小练习范围并尝试放慢','failed','这次仍未处理好，先停下来，保留这个问题。'),
            'pause':_path('注意力难以维持','结束这一轮，给自己留出间隔','paused','本次练习提前停下，目标还没有完成。')}
    elif kind=='rest':
        triggers={'recover':'想恢复精力','space':'想暂时留出不处理任务的时间'}
        paths={
            'settled':_path('脑子还挂着待办','先把待办放下，安静坐一会儿','completed','这一小段休息结束后，感觉节奏缓下来。'),
            'unsettled':_path('仍惦记未完成的事','尝试放松，但注意力又回到待办','partial','只放松了一点，还没完全缓过来。'),
            'ongoing':_path('精力仍不足','减少手头活动，继续安静休息','paused','仍需要继续休息，没有开始新的任务。')}
        paths['refreshed'] = _path('需要从之前的消耗中缓过来','放下手头事务，安静休息后重新感受精力',
            'completed','这段休息后精力有所恢复，可以按自己的意愿接回轻活动。')
        now_local = now.astimezone(timezone(timedelta(hours=8)))
        next_class = (context.get('world') or {}).get('schedule', {}).get('next_class')
        for duration in (30, 60, 90):
            end = now + timedelta(minutes=duration)
            if next_class and end > datetime.fromisoformat(next_class['start']):
                continue
            if 8 <= now_local.hour < 22:
                paths[f'nap_{duration}'] = _path('想补一段觉来恢复精力', '放下手头事务，开始补觉并留出明确的休息时段',
                    'paused', f'开始补觉，计划休息{duration}分钟，醒来后再判断精力和接下来的安排。')
        sleep = (context.get('rhythm') or {}).get('authored_sleep')
        if sleep and sleep['status'] == 'due':
            if not sleep.get('interrupted'):
                paths['nap_refreshed'] = _path('之前的补觉需要续接', '结束已经开始的补觉，重新感受醒来后的精力',
                    'completed', '这段补觉结束，醒来后精力有所恢复，可以按意愿接回日常安排。')
                paths['nap_tired'] = _path('补觉后仍有一些疲惫', '结束这一段补觉，先慢慢接回吃饭和轻活动',
                    'completed', '补了一段觉，已经缓过来一些，仍适合放慢节奏。')
            else:
                paths['nap_interrupted'] = _path('补觉期间又开始通信', '结束这次未完整睡完的补觉，重新安排休息',
                    'paused', '这段补觉没有完整结束，当前已经醒着，之后再安排休息。')
    else:
        triggers={'meal_time':'给自己留出这一餐的时间','body':'照顾当下的进食需要'}
        status=(meal or {}).get('status','eating')
        if status=='eaten':
            paths={'ordinary':_path('没有明显阻碍','按自己的节奏吃完','completed','这一餐已经吃完。'),
                   'slow':_path('吃得比预想慢','放慢速度，不急着接下一项','completed','这一餐吃完了，之后再接回安排。')}
        elif status=='skipped':
            paths={'skip':_path('当下不想开始进食','决定这一餐先不吃','paused','这次没有吃这一餐。')}
        elif status=='planned':
            paths={'delay':_path('想先留一点间隔','把用餐安排到已经选定的稍后时刻','paused','这一餐尚未开始，保留明确的用餐计划。')}
        else:
            paths={'ordinary':_path('没有明显阻碍','停下其他活动，开始进食','partial','这一餐正在吃，尚未结束。'),
                   'slow':_path('暂时没有很强的食欲','慢一点吃，不勉强赶进度','partial','已经开始进食，仍在慢慢吃。')}
    interpretations={'progress':dict(meaning='觉得这一小步有价值',need='progress'),
                     'friction':dict(meaning='觉得进展受阻，有点在意',need='autonomy'),
                     'recovery':dict(meaning='觉得自己需要一点恢复空间',need='rest'),
                     'ordinary':dict(meaning='把它当作普通的一段生活，没有额外解释',need=None)}
    effects={'none':dict(next_action=None,open_loop=None),
             'pause':dict(next_action='先留一段间隔再安排下一项',open_loop=None)}
    if kind == 'rest':
        # Rest paths already describe whether to continue or resume. Avoid a
        # duplicate next-step branch for every nap duration and interpretation.
        effects = {'none': effects['none']}
    elif kind=='practice':
        effects['return']=dict(next_action='下次优先回到本次未稳的片段',open_loop='练习片段仍待巩固')
    elif kind in _ACTIVITY_PATHS:
        effects['return']=dict(next_action='之后回到这次尚未解决的部分',open_loop='本次活动中留下的问题仍待处理')
    world=context.get('world') or {}
    projected={key:context[key] for key in ('time','selected_activity') if key in context}
    projected['rhythm']={k:v for k,v in (context.get('rhythm') or {}).items()
                         if k in {'phase','rest','wellbeing','note','historical_rest','recovery','rest_observations',
                                  'current_load_minutes','authored_sleep'}}
    projected['previous']={k:v for k,v in (context.get('previous') or {}).items()
                           if k in {'activity','activity_kind','note','occurred_at'}}
    emotion=context.get('emotion') or {}
    projected['emotion']={'status':emotion.get('status'),
        'current_affect':{k:v for k,v in (emotion.get('current_affect') or {}).items()
                          if k in {'label','reason','as_of','status','pending_sources'}}}
    projected['selected_project']={k:v for k,v in (context.get('selected_project') or {}).items()
                                  if k in {'id','title','detail','status','progress'}}
    # Author only the selected new activity. Unrelated projects, the full day
    # timeline and prior episodes do not establish a motive for this step.
    projected['world']={}
    if kind=='class':
        projected['world']['current_class']=(world.get('schedule') or {}).get('current_class')
    if kind in {'walk','errand'}:
        projected['world']['weather']={k:v for k,v in (world.get('weather') or {}).items()
                                      if k in {'summary','temperature_c','condition','observed_at','stale'}}
    projected['coverage']='仅当前新活动、前态及身体情绪，不是全部历史；未提供旧事不代表不存在。'
    if len(json.dumps(projected,ensure_ascii=False).encode('utf8'))>14000:
        raise ValueError('LIFE_EPISODE_CONTEXT_TOO_LARGE')
    combinations={f'{p}:{i}:{e}':dict(path=p,interpretation=i,effect=e)
                  for p in paths for i in interpretations for e in effects}
    experience_instructions = (
        '创作并选择虚拟角色这一刻的新生活过程，不是检索或确认state中已经存在的事实；你选的过程将作为本次新事件提交。'
        '从组合中选择连贯的过程结果(path)、角色主观解释(interpretation)和后续打算(effect)，编号完整定义在state目录。'
        '可以困难、失败或未完成，不强行圆满。主观解释不改变过程事实，下一步不是已经执行，也不是要求用户行动。'
        '只描述当前选定的小步，不把课表计划当已上完课程，不编造教师同学或用户的言行、交易、他人私事。'
        'completed仅指所选小步，不代表读完整书、完成长期创作或所有家务；散步不完成待办，出门准备不代表外部事务办妥。'
        '这只创作本次角色新经历，不是事后给旧事件编原因，不凭一次经历改写长期人格。')
    if kind == 'rest':
        experience_instructions += (
            'context.rhythm.authored_sleep说明之前已开始补觉，due时需要续接；睡完后的新感受还没有记录，正要由本次选择生成。'
            'depleted/historical_rest是补觉前的精力负荷，不是补觉后的结论。既无打断也无独立不适时，合理结束补觉并选择缓过来或恢复，'
            '不要只因旧负荷高就继续照搬ongoing。独立不适不能宣布治愈，普通休息不证明睡着。'
            'nap_N只开始未来补觉，不提前恢复；nap_refreshed/nap_tired只续接已开始且到时的补觉。'
            '连续精力不足且日程有空档，可以安排nap_N补觉，不反复沿用同一句继续静坐。结合当前精力仍可继续休息，或选择新的恢复过程。')
    answers=await port.ask({'new_activity':kind,'context':projected,'meal':meal,
                           'paths':paths,'interpretations':interpretations,'effects':effects}, {
        'trigger':dict(instructions='为本次新发生的角色自身行动选择动机。不要替真实用户或他人编动作、对话或私事；不得补写旧事件的原因。',criteria=triggers),
        'experience':dict(instructions=experience_instructions, criteria=combinations)},
        purpose='world-life-episode')
    if (not isinstance(answers,dict) or set(answers)!={'trigger','experience'}
            or answers.get('trigger') not in triggers or answers.get('experience') not in combinations):
        raise ValueError('LIFE_EPISODE_DECISION_INVALID')
    answers={**answers,**combinations[answers['experience']]}
    selected_effects = dict(effects[answers['effect']])
    baseline = (context.get('rhythm') or {}).get('historical_rest', {}).get('load_minutes')
    if kind == 'rest':
        path = answers['path']
        if path in {'settled', 'refreshed', 'nap_refreshed', 'nap_tired'} and type(baseline) in {int, float}:
            rest = 'rested' if path in {'refreshed', 'nap_refreshed'} or context['rhythm'].get('rest') == 'rested' else 'tired'
            remaining = 0 if rest == 'rested' else min(baseline, context['rhythm'].get('current_load_minutes', baseline), 119)
            selected_effects['body_recovery'] = {'rest': rest, 'baseline_load_minutes': baseline,
                                                'remaining_load_minutes': remaining}
        if path in {'nap_30', 'nap_60', 'nap_90'}:
            duration = int(path.split('_')[1])
            selected_effects['sleep_plan'] = {'duration_minutes': duration,
                'end_at': (now + timedelta(minutes=duration)).astimezone(timezone.utc).isoformat()}
        sleep = (context.get('rhythm') or {}).get('authored_sleep')
        if sleep and sleep['status'] == 'due':
            selected_effects['sleep_resolution'] = {'source_id': sleep['source_id']}
    return dict(schema='character-life-episode/1',source_id=source_id,occurred_at=now.astimezone(timezone.utc).isoformat(),
        actor='character',activity_kind=kind,trigger=dict(kind=answers['trigger'],detail=triggers[answers['trigger']]),
        **paths[answers['path']],interpretation=dict(subjective=True,**interpretations[answers['interpretation']]),
        effects=selected_effects)
