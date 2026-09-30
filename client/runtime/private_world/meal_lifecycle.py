"""Autonomous, clock-driven meals for the fictional character's own day."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

LOCAL = timezone(timedelta(hours=8))
HOURS = {'breakfast': 8, 'lunch': 12, 'dinner': 18}
TERMINAL = {'eaten', 'skipped'}


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS life_meal_events (
        source_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS life_meal_retry (
        meal_key TEXT PRIMARY KEY, failures INTEGER NOT NULL, retry_after TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS life_meal_exchange_review (
        meal_key TEXT NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(meal_key,source_id));
        CREATE TABLE IF NOT EXISTS life_meal_recheck (
        meal_key TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL, basis TEXT NOT NULL);''')


def records(db, now):
    stamp = now.astimezone(timezone.utc).isoformat()
    return [json.loads(row[0]) for row in db.execute(
        'SELECT payload FROM life_meal_events WHERE recorded_at<=? ORDER BY recorded_at DESC,rowid DESC', (stamp,))]


def schedule(db, now, meals):
    local = now.astimezone(LOCAL)
    today = local.date().isoformat()
    existing = {m['slot']:m for m in meals if m['date'] == today}
    result = []
    for slot, hour in HOURS.items():
        due = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        old = existing.get(slot)
        planned = old.get('scheduled_for') if old and old['status'] == 'planned' else None
        if planned:
            due = datetime.fromisoformat(planned)
        failed = db.execute('SELECT retry_after FROM life_meal_retry WHERE meal_key=?', (today+':'+slot,)).fetchone()
        state = 'not_due' if now < due else 'error' if failed else 'pending'
        result.append(dict(slot=slot, date=today, scheduled_for=due.isoformat(), status=state,
                           error_code='MEAL_DECISION_UNAVAILABLE' if failed else None))
    return result


def _candidate(slot, food, status, when, now, *, started=None, scheduled=None, recovered=False):
    return dict(slot=slot, food=food, status=status, date=now.astimezone(LOCAL).date().isoformat(),
                occurred_at=when.isoformat(), recorded_at=now.isoformat(),
                started_at=started.isoformat() if started else None,
                finished_at=when.isoformat() if status == 'eaten' else None,
                scheduled_for=scheduled.isoformat() if scheduled else None, recovered=recovered)


def _cutoff(slot, now):
    local = now.astimezone(LOCAL)
    return local.replace(hour={'breakfast': 12, 'lunch': 18, 'dinner': 0}[slot], minute=0, second=0, microsecond=0) + (
        timedelta(days=1) if slot == 'dinner' else timedelta())


def _free_time(start, classes):
    for course in sorted(classes, key=lambda item: datetime.fromisoformat(item['start'])):
        begin, end = datetime.fromisoformat(course['start']), datetime.fromisoformat(course['end'])
        if start < end and start + timedelta(minutes=25) > begin:
            start = end
    return start


def _recheck_basis(snapshot, when):
    recovery = snapshot['rhythm'].get('recovery') or {}
    return json.dumps({'classes_ended': [c['end'] for c in snapshot['world']['schedule'].get('classes', [])
                                        if datetime.fromisoformat(c['end']) <= when],
                       'recovery': recovery.get('source_id') if recovery.get('occurred_at')
                                   and datetime.fromisoformat(recovery['occurred_at']) <= when else None}, sort_keys=True)


def options(slot, old, now, *, classes=(), prospective=False):
    from .jev_world import _FOODS
    local = now.astimezone(LOCAL)
    due = local.replace(hour=HOURS[slot], minute=0, second=0, microsecond=0)
    if local < due or old and old['status'] in TERMINAL:
        return {}
    if old and old['status'] == 'planned' and old.get('scheduled_for'):
        if now < datetime.fromisoformat(old['scheduled_for']):
            return {}
        if now >= _cutoff(slot, now):
            # An expired breakfast/lunch plan must not start alongside the
            # next main meal after a long offline gap.
            return {'skipped': _candidate(slot, '', 'skipped', now, now)}
    if old and old['status'] == 'eating':
        start = datetime.fromisoformat(old.get('started_at') or old['occurred_at'])
        if now < start + timedelta(minutes=20):
            return {}
        # Continuation of an authored meal, preserving its food and start time.
        choices = {}
        for duration in (20, 30, 45):
            end = start + timedelta(minutes=duration)
            if end <= now:
                choices['finished_'+str(duration)] = _candidate(slot, old['food'], 'eaten', end, now,
                    started=start, recovered=now > end+timedelta(minutes=30))
        return choices
    overdue = local >= due + timedelta(hours=2) and not prospective and not (old and old['status'] == 'planned')
    foods = list(_FOODS[slot])
    if old and old.get('food') and old['food'] not in foods:
        foods.append(old['food'])
    choices = {}
    if overdue:
        # Offline recovery authors the character's missing day, never a claim
        # that a real user ate. These times are explicitly marked reconstructed.
        for i, food in enumerate(foods):
            for minute in (0, 30, 60):
                start = due + timedelta(minutes=minute)
                choices[f'recovered_{i}_{minute}'] = _candidate(slot, food, 'eaten', start+timedelta(minutes=25),
                                                               now, started=start, recovered=True)
        choices['skipped'] = _candidate(slot, '', 'skipped', due+timedelta(hours=2), now, recovered=True)
    else:
        for i, food in enumerate(foods):
            choices['eat_'+str(i)] = _candidate(slot, food, 'eating', now, now, started=now)
            # A class can delay an authored future meal beyond the offline
            # reconstruction window. It does not establish an earlier meal.
            planned = _free_time(now + timedelta(minutes=30), classes)
            if planned < _cutoff(slot, now):
                choices['plan_'+str(i)] = _candidate(slot, food, 'planned', now, now,
                                                    scheduled=planned)
        choices['skipped'] = _candidate(slot, '', 'skipped', now, now)
    return choices


async def advance(store, port, now):
    """At most one JEV call per due meal; failures retain a durable backoff."""
    now = now.astimezone(timezone.utc)
    today = now.astimezone(LOCAL).date().isoformat()
    for slot in HOURS:
        snapshot = store.snapshot(now)
        old = next((m for m in snapshot['world']['meals'] if m['date']==today and m['slot']==slot), None)
        classes = list(snapshot['world']['schedule'].get('classes', []))
        sleep = snapshot['rhythm'].get('authored_sleep')
        if sleep and sleep['status'] == 'sleeping':
            classes.append({'start': sleep['started_at'], 'end': sleep['end_at']})
        candidates = options(slot, old, now, classes=classes)
        key = today+':'+slot
        basis = _recheck_basis(snapshot, now)
        actions = []
        if old and old['status'] in {'skipped', 'eating'}:
            actions = store.pending_exchange_actions(now, after=datetime.fromisoformat(old['occurred_at']))
            with store._db() as db:
                reviewed = {r[0] for r in db.execute('SELECT source_id FROM life_meal_exchange_review WHERE meal_key=?', (key,))}
            actions = [a for a in actions if a['source_id'] not in reviewed]
            # Reconsider after meaningful autonomous changes as well as new
            # conversation. Persist the review so polling/restarts do not bill
            # again for the same boundary or recovery event.
            with store._db() as db:
                review = db.execute('SELECT reviewed_at,basis FROM life_meal_recheck WHERE meal_key=?', (key,)).fetchone()
            last = datetime.fromisoformat(review[0] if review else old.get('recorded_at') or old['occurred_at'])
            prior_basis = review[1] if review else _recheck_basis(snapshot, last)
            autonomous = now >= last + timedelta(hours=2) or basis != prior_basis
            if actions and old['status'] == 'eating':
                start = datetime.fromisoformat(old.get('started_at') or old['occurred_at'])
                candidates = {'continue_eating': old}
                if now > start:
                    candidates['finish_now'] = _candidate(slot, old['food'], 'eaten', now, now, started=start)
            elif old['status'] == 'skipped' and (actions or autonomous) and now < _cutoff(slot, now):
                candidates = {'keep_skipped': old}
                for name, candidate in options(slot, None, now, classes=classes, prospective=True).items():
                    if name.startswith(('eat_', 'plan_')):
                        candidates[name.replace('eat_', 'start_').replace('plan_', 'later_')] = candidate
        if not old or old['status'] != 'eating':
            def outside_class(candidate):
                value = candidate.get('started_at') or candidate.get('scheduled_for')
                if not value:
                    return True
                start = datetime.fromisoformat(value)
                end = (datetime.fromisoformat(candidate['finished_at']) if candidate['finished_at']
                       else start + timedelta(minutes=25))
                return not any(start < datetime.fromisoformat(course['end'])
                               and end > datetime.fromisoformat(course['start']) for course in classes)
            candidates = {key:value for key,value in candidates.items() if outside_class(value)}
        if not candidates:
            continue
        with store._db() as db:
            retry = db.execute('SELECT failures,retry_after FROM life_meal_retry WHERE meal_key=?', (key,)).fetchone()
        if retry and now < datetime.fromisoformat(retry[1]):
            continue
        try:
            # Food options come from available life choices, not persona taste.
            today = now.astimezone(LOCAL).date().isoformat()
            state = dict(time=now.isoformat(), slot=slot,
                         world={'schedule': snapshot['world'].get('schedule', {}),
                                'meals': [m for m in snapshot['world'].get('meals', []) if m.get('date') == today]},
                         current={k: snapshot['current'][k] for k in ('activity', 'activity_kind', 'occurred_at')
                                  if snapshot.get('current') and k in snapshot['current']},
                         rhythm={k: snapshot['rhythm'][k] for k in ('phase', 'rest', 'wellbeing', 'note', 'recovery', 'authored_sleep')
                                 if k in snapshot['rhythm']},
                         previous_meal=old, exchange_actions=actions,
                          exchange_rule='本轮是角色的自主生活更新，不要求收到交流才可以吃饭。previous_meal.skipped只说明过去那次没吃，不能当成本次继续不吃的理由；下课或恢复后可新开始补吃。新交流中的已经吃完仍须核对原来的eating记录；仅询问、计划、引语不能证明完成。已有eating且明确报告本餐已吃完，可选finish_now；否则continue_eating。',
                         recovery=all(c.get('recovered',False) for c in candidates.values()))
            eaten = [m for m in snapshot['world']['meals'] if m['status'] == 'eaten']
            last_eaten = max(eaten, key=lambda m: m.get('finished_at') or m['occurred_at'], default=None)
            state['last_eaten'] = ({k: last_eaten[k] for k in ('slot', 'food', 'occurred_at', 'finished_at') if k in last_eaten}
                                   if last_eaten else None)
            # Factor exact shared fields only: each candidate is reconstructed
            # by overlaying its fields onto these defaults, without losing time
            # bounds, food, start/finish provenance or recovery semantics.
            first = next(iter(candidates.values()))
            defaults = {k: v for k, v in first.items()
                        if all(k in c and c[k] == v for c in candidates.values())}
            offered = {key: {k: v for k, v in c.items() if k not in defaults}
                       for key, c in candidates.items()}
            decision_state = {**state, 'meal_candidate_defaults': defaults,
                'meal_candidate_rule': 'Each meal criterion is the complete event formed by overlaying its fields on meal_candidate_defaults. Missing criterion fields retain the exact shared values, including nulls; an empty criterion means the defaults themselves.'}
            answers = await port.ask(decision_state, {'meal':dict(instructions=
                '从候选选择虚拟角色此刻要执行的新用餐行动，不是判断原文是否已经记载吃过饭。选项会成为新的生活事件，不需要旧事实预先证明新的开始或计划。'
                '她不能因没有用户消息或没有旧用餐记录就不吃；旧skipped只是历史。结合课表、身体作息、当日其他餐和既有活动选择。'
                'recovered表示离线期间缺失的角色生活，由你在合理候选时间补演并明确标记，不能伪称观察事实。'
                '已有开始进食则在给定时长中结束；已吃完不可重吃，跳过不等于全天不能再吃。'
                '下课后有空闲且长时间未进食，应选择开始或稍后用餐。没食欲、不能用餐等理由不能凭空假定，也不能仅凭疲劳推断；只有确有本轮原因才选keep_skipped。'
                '计划是未来安排，不提前写成已吃；下课或恢复后可以选择新的补吃。食物不是人格固定口味，兼顾变化。', criteria=offered)},
                purpose='world-meal-lifecycle')
            chosen = answers.get('meal') if isinstance(answers,dict) else None
            if chosen not in candidates:
                raise ValueError('MEAL_DECISION_INVALID')
            meal = candidates[chosen]
            if chosen in {'keep_skipped', 'continue_eating'}:
                with store._db() as db:
                    db.executemany('INSERT OR IGNORE INTO life_meal_exchange_review VALUES (?,?)',
                                   [(key,a['source_id']) for a in actions])
                    db.execute('DELETE FROM life_meal_retry WHERE meal_key=?', (key,))
                    db.execute('INSERT OR REPLACE INTO life_meal_recheck VALUES (?,?,?)', (key, now.isoformat(), basis))
                continue
            source = 'meal:' + hashlib.sha256((key+':'+meal['status']+':'+meal['occurred_at']).encode()).hexdigest()[:32]
            meal = {**meal, 'source_id':source}
            from .life_episode import create as create_episode, save as save_episode
            episode = await create_episode(port, source, now, 'meal', state, meal=meal)
            with store._db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = next((m for m in store._world(db, now)['meals'] if m['date']==today and m['slot']==slot),None)
                if current != old:
                    continue  # Concurrent author advanced it; do not overwrite.
                db.execute('INSERT OR IGNORE INTO life_meal_events VALUES (?,?,?)',
                           (source, now.isoformat(), json.dumps(meal,ensure_ascii=False)))
                db.executemany('INSERT OR IGNORE INTO life_meal_exchange_review VALUES (?,?)',
                               [(key,a['source_id']) for a in actions])
                save_episode(db, episode, source, now)
                if meal['status'] == 'eating':
                    # This is a newly authored present action, so the main
                    # world view must not keep saying she is practising/asleep.
                    titles = {'breakfast':'早餐','lunch':'午餐','dinner':'晚餐'}
                    previous = snapshot.get('current') or {}
                    activity = dict(source_id=source, occurred_at=now.isoformat(),
                        location=previous.get('location') or '住处', activity='吃'+titles[slot],
                        note='正在吃'+meal['food']+'。', activity_kind='meal', progress=[], meals=[])
                    db.execute('INSERT OR IGNORE INTO life_moments VALUES (?,?,?,?)',
                               (source, now.isoformat(), 'daily', json.dumps(activity,ensure_ascii=False)))
                db.execute('DELETE FROM life_meal_retry WHERE meal_key=?',(key,))
                db.execute('INSERT OR REPLACE INTO life_meal_recheck VALUES (?,?,?)', (key, now.isoformat(), basis))
        except Exception:
            with store._db() as db:
                failures = min((retry[0] if retry else 0)+1,5)
                retry_at = now+timedelta(minutes=min(5*2**(failures-1),60))
                db.execute('INSERT OR REPLACE INTO life_meal_retry VALUES (?,?,?)',(key,failures,retry_at.isoformat()))
