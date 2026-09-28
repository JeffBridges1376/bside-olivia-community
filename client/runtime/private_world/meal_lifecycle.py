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
        meal_key TEXT NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(meal_key,source_id));''')


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


def options(slot, old, now):
    from .jev_world import _FOODS
    local = now.astimezone(LOCAL)
    due = local.replace(hour=HOURS[slot], minute=0, second=0, microsecond=0)
    if local < due or old and old['status'] in TERMINAL:
        return {}
    if old and old['status'] == 'planned' and old.get('scheduled_for'):
        if now < datetime.fromisoformat(old['scheduled_for']):
            return {}
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
    overdue = local >= due + timedelta(hours=2)
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
            # Plans cannot push a meal beyond its bounded recovery window.
            if now + timedelta(minutes=30) < due + timedelta(hours=2):
                choices['plan_'+str(i)] = _candidate(slot, food, 'planned', now, now,
                                                    scheduled=now+timedelta(minutes=30))
        choices['skipped'] = _candidate(slot, '', 'skipped', now, now)
    return choices


async def advance(store, port, now):
    """At most one JEV call per due meal; failures retain a durable backoff."""
    now = now.astimezone(timezone.utc)
    today = now.astimezone(LOCAL).date().isoformat()
    for slot in HOURS:
        snapshot = store.snapshot(now)
        old = next((m for m in snapshot['world']['meals'] if m['date']==today and m['slot']==slot), None)
        candidates = options(slot, old, now)
        key = today+':'+slot
        actions = []
        if old and old['status'] in {'skipped', 'eating'}:
            actions = store.pending_exchange_actions(now, after=datetime.fromisoformat(old['occurred_at']))
            with store._db() as db:
                reviewed = {r[0] for r in db.execute('SELECT source_id FROM life_meal_exchange_review WHERE meal_key=?', (key,))}
            actions = [a for a in actions if a['source_id'] not in reviewed]
            # Reconsider a skipped meal only with new conversation evidence.
            # Author a prospective action; never reconstruct eating from words.
            if actions and old['status'] == 'eating':
                start = datetime.fromisoformat(old.get('started_at') or old['occurred_at'])
                candidates = {'continue_eating': old}
                if now > start:
                    candidates['finish_now'] = _candidate(slot, old['food'], 'eaten', now, now, started=start)
            elif actions:
                from .jev_world import _FOODS
                candidates = {'keep_skipped': old}
                for i, food in enumerate(_FOODS[slot]):
                    candidates['start_'+str(i)] = _candidate(slot, food, 'eating', now, now, started=now)
                    candidates['later_'+str(i)] = _candidate(slot, food, 'planned', now, now, scheduled=now+timedelta(minutes=30))
        if not old or old['status'] != 'eating':
            classes = snapshot['world']['schedule'].get('classes', [])
            def outside_class(candidate):
                if not candidate.get('started_at'):
                    return True
                start = datetime.fromisoformat(candidate['started_at'])
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
                         rhythm={k: snapshot['rhythm'][k] for k in ('phase', 'rest', 'wellbeing', 'note')
                                 if k in snapshot['rhythm']},
                         previous_meal=old, exchange_actions=actions,
                         exchange_rule='核对新交流与既有用餐。仅询问、计划、引语不能证明完成。已有正在吃的记录且角色明确报告本餐已经吃完，可选finish_now，由世界提交本次完成；否则continue_eating。此前没吃的餐，若与补吃无关选keep_skipped，否则选择新的开始或计划，不能直接完成。',
                         recovery=all(c.get('recovered',False) for c in candidates.values()))
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
                '自主推进虚拟角色本人的日常用餐，不等待用户确认，也不判断真实用户吃没吃。结合课表、身体作息、当日其他餐和既有活动，从候选中选一项。'
                'recovered表示离线期间缺失的角色生活，由你在合理候选时间补演并明确标记，不能伪称观察事实。'
                '已有开始进食则在给定时长中结束；不可重吃终态餐。食物不是人格固定口味，兼顾变化。', criteria=offered)},
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
        except Exception:
            with store._db() as db:
                failures = min((retry[0] if retry else 0)+1,5)
                retry_at = now+timedelta(minutes=min(5*2**(failures-1),60))
                db.execute('INSERT OR REPLACE INTO life_meal_retry VALUES (?,?,?)',(key,failures,retry_at.isoformat()))
