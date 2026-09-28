"""Small, replayable changes supported by independent character experiences.

The time/count gates are authored continuity rules, not psychological estimates.
This module never calls a model or changes persona assets or relationship rights.
"""
from datetime import datetime, timezone
import hashlib
import json
import re

from .life_rhythm import LOCAL


POLICY = 'character-development.v1'
_TOPIC_FIELDS = {'key', 'label', 'kind', 'baseline', 'anchor'}
_FIELDS = {'key', 'stance', 'user_quote', 'character_quote', 'experience_quote', 'episode_id', 'withdraws'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def declarations(persona_json):
    # Old integrations may supply a plain background string; malformed declared
    # JSON is a configuration error and must not silently lose protection rules.
    if not isinstance(persona_json, str):
        raise ValueError('DEVELOPMENT_PERSONA_INVALID')
    if not persona_json.lstrip().startswith(('[', '{')):
        return []
    values = json.loads(persona_json)
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise ValueError('DEVELOPMENT_PERSONA_INVALID')
    return values


def configure(db, persona_json):
    topics = []
    for declaration in declarations(persona_json):
        metadata = declaration.get('development', [])
        if not isinstance(metadata, (list, tuple)) or (metadata and declaration.get('inclusion') in {'core', 'phase'}):
            raise ValueError('DEVELOPMENT_TOPIC_INVALID')
        for topic in metadata:
            if (not isinstance(topic, dict) or set(topic) != _TOPIC_FIELDS
                    or not isinstance(topic['key'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', topic['key'])
                    or not isinstance(topic['label'], str) or not 1 <= len(topic['label']) <= 40
                    or topic['kind'] not in {'interest', 'taste', 'trait'}
                    or topic['baseline'] not in {'avoid', 'neutral', 'like'} or type(topic['anchor']) is not bool):
                raise ValueError('DEVELOPMENT_TOPIC_INVALID')
            old = db.execute('SELECT payload FROM character_development_topics WHERE key=?', (topic['key'],)).fetchone()
            if old and old[0] != encoded(topic):
                raise ValueError('DEVELOPMENT_BASELINE_CONFLICT')
            db.execute('INSERT OR IGNORE INTO character_development_topics VALUES (?,?)', (topic['key'], encoded(topic)))
            topics.append(topic)
    if len({t['key'] for t in topics}) != len(topics):
        raise ValueError('DEVELOPMENT_TOPIC_INVALID')
    return topics


def topics(db):
    return [json.loads(row[0]) for row in db.execute('SELECT payload FROM character_development_topics ORDER BY key')]


def _quote(value, text):
    if not isinstance(value, str) or not value.strip() or len(value) > 240 or value not in text:
        raise ValueError('DAILY_LIFE_DEVELOPMENT_EVIDENCE_INVALID')
    return value


def record_exchange(db, source_id, source_hash, user, reply, candidates, relationship, stamp, origin, updates, evidence_as_of):
    if candidates is None:
        return
    if not isinstance(candidates, list) or len(candidates) > 3:
        raise ValueError('DAILY_LIFE_DEVELOPMENT_INVALID')
    catalog = {t['key']: t for t in topics(db)}
    seen = set()
    for item in candidates:
        if (not isinstance(item, dict) or set(item) != _FIELDS or item.get('key') not in catalog
                or item['key'] in seen or item['stance'] not in {'positive', 'negative'} or origin != 'user'):
            raise ValueError('DAILY_LIFE_DEVELOPMENT_INVALID')
        seen.add(item['key'])
        user_quote = _quote(item['user_quote'], user)
        _quote(item['character_quote'], reply)
        _quote(item['experience_quote'], user_quote)
        target = item['withdraws']
        if target is not None:
            if not isinstance(target, str):
                raise ValueError('DAILY_LIFE_DEVELOPMENT_INVALID')
            prior = next((r for r in withdrawal_candidates(db, datetime.fromisoformat(evidence_as_of), limit=None)
                          if r['source_id'] == target and r['key'] == item['key']), None)
            if not prior or prior['stance'] != item['stance']:
                raise ValueError('DAILY_LIFE_DEVELOPMENT_WITHDRAWAL_INVALID')
        elif not relationship or relationship.get('kind') != 'shared_experience':
            raise ValueError('DAILY_LIFE_DEVELOPMENT_INVALID')
        episode = item['episode_id']
        if episode is not None:
            if not isinstance(episode, str):
                raise ValueError('DAILY_LIFE_DEVELOPMENT_EPISODE_INVALID')
            allowed = {e['episode_id'] for e in episodes(db, evidence_as_of)}
            allowed.update('shared:' + u['id'] for u in updates if u['kind'] == 'shared' and u['status'] in {'ongoing', 'completed'})
            if episode not in allowed:
                raise ValueError('DAILY_LIFE_DEVELOPMENT_EPISODE_INVALID')
        record = {**item, 'source_id': source_id, 'source_hash': source_hash, 'occurred_at': stamp,
                  'available_at': stamp, 'evidence_kind': 'shared_experience',
                  'experience_id': episode, 'policy': POLICY}
        db.execute('INSERT INTO character_development_events VALUES (?,?,?,?)',
                   (source_id, item['key'], stamp, encoded(record)))


def episodes(db, stamp):
    result = []
    for source_id, kind, raw in db.execute("SELECT source_id,kind,payload FROM life_moments WHERE occurred_at<=? AND kind IN ('daily','exchange') ORDER BY occurred_at DESC,rowid DESC", (stamp,)):
        payload = json.loads(raw)
        if kind == 'daily':
            if payload.get('activity_kind') not in {'rest', None}:
                result.append({'episode_id': 'daily:' + source_id, 'description': payload['note']})
        elif (payload.get('relationship') or {}).get('kind') == 'shared_experience':
            result.extend({'episode_id': 'shared:' + p['id'], 'description': p['quote']}
                          for p in payload['updates'] if p['kind'] == 'shared' and p['status'] in {'ongoing', 'completed'})
        if len(result) >= 12:
            break
    return list({e['episode_id']: e for e in result}.values())[:12]


def world_assessment(db, now):
    """Freeze sources for the existing world writer's optional interpretation."""
    from .character_emotion import CharacterEmotionStore, _time as emotion_time
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='character_emotion_appraisals'").fetchone():
        return {'as_of': now.astimezone(timezone.utc).isoformat(), 'sources': []}
    stamp = now.astimezone(timezone.utc).isoformat()
    live, _, _ = CharacterEmotionStore._state(db, emotion_time(now))
    sources = []
    for appraisal in live:
        row = db.execute("SELECT occurred_at,payload FROM life_moments WHERE source_id=? AND kind='daily'", (appraisal['source_id'],)).fetchone()
        if not row:
            continue
        world = json.loads(row[1])
        if world.get('activity_kind') in {None, 'rest'}:
            continue
        sources.append({'source_id': appraisal['source_id'], 'source_hash': digest(world), 'occurred_at': row[0],
                        'world': world, 'appraisal': {k: appraisal[k] for k in ('quote', 'reaction', 'action_tendency', 'goal_or_need')},
                        'appraisal_version': appraisal['version']})
    return {'as_of': stamp, 'sources': sources[-3:]}


def record_world(db, now, candidates, basis):
    if candidates is None:
        return
    if not isinstance(candidates, list) or len(candidates) > 3:
        raise ValueError('DAILY_LIFE_DEVELOPMENT_INVALID')
    if not candidates:
        return
    if not isinstance(basis, dict):
        raise ValueError('DAILY_LIFE_DEVELOPMENT_INVALID')
    current = world_assessment(db, now)
    if current != basis:
        raise ValueError('DAILY_LIFE_DEVELOPMENT_CONTEXT_STALE')
    catalog = {t['key']: t for t in topics(db)}
    sources = {s['source_id']: s for s in basis['sources']}
    seen = set()
    for item in candidates:
        if (not isinstance(item, dict) or set(item) != {'source_id', 'key', 'stance', 'quote', 'reason'}
                or item['source_id'] not in sources or item['key'] not in catalog
                or item['stance'] not in {'positive', 'negative'}
                or not isinstance(item['reason'], str) or not 1 <= len(item['reason']) <= 160
                or (item['source_id'], item['key']) in seen):
            raise ValueError('DAILY_LIFE_DEVELOPMENT_INVALID')
        seen.add((item['source_id'], item['key']))
        source, topic = sources[item['source_id']], catalog[item['key']]
        world = source['world']
        _quote(item['quote'], world['note'])
        if topic['kind'] == 'taste' and not any(m['status'] == 'eaten' for m in world.get('meals', [])):
            raise ValueError('DAILY_LIFE_DEVELOPMENT_EVIDENCE_INVALID')
        record = {**item, 'source_hash': source['source_hash'], 'occurred_at': source['occurred_at'],
                  'available_at': basis['as_of'], 'evidence_kind': 'published_world',
                  'character_quote': source['appraisal']['quote'], 'experience_id': 'daily:' + item['source_id'],
                  'appraisal_version': source['appraisal_version'], 'withdraws': None, 'policy': POLICY}
        # Re-assessment versions stay append-only. Projection accepts only the
        # currently valid appraisal, and one world source remains one episode.
        identity = item['key'] + ':' + source['appraisal_version']
        db.execute('INSERT OR IGNORE INTO character_development_events VALUES (?,?,?,?)',
                   (item['source_id'], identity, source['occurred_at'], encoded(record)))


def reduce(catalog, records, now):
    stamp = now.astimezone(timezone.utc).isoformat()
    records = sorted((r for r in records if r['occurred_at'] <= stamp and r['available_at'] <= stamp),
                     key=lambda r: (r['occurred_at'], r['source_id'], r['key']))
    withdrawn = {(r['withdraws'], r['key']) for r in records if r.get('withdraws')}
    items = []
    for topic in catalog:
        daily, episode_values, sources = {}, {}, []
        for event in records:
            if event['key'] != topic['key'] or event.get('withdraws') or (event['source_id'], topic['key']) in withdrawn:
                continue
            if event['experience_id'] is None:
                sources.append(event['source_id'])
                continue
            day = datetime.fromisoformat(event['occurred_at']).astimezone(LOCAL).date()
            episode = episode_values.setdefault(event['experience_id'], {'day': day, 'signs': set()})
            episode['signs'].add(1 if event['stance'] == 'positive' else -1)
            sources.append(event['source_id'])
        for episode in episode_values.values():
            daily.setdefault(episode['day'], set()).update(episode['signs'])
        if not sources:
            continue
        # One date contributes at most one vote. Mixed outcomes contribute zero;
        # repeating praise on the same date cannot drown out a negative outcome.
        score = sum(next(iter(v)) if len(v) == 1 else 0 for v in daily.values())
        span = (max(daily)-min(daily)).days if daily else 0
        count, days = (5, 28) if topic['anchor'] or topic['kind'] == 'trait' else (3, 14)
        mature = len(daily) >= count and span >= days and abs(score) >= count
        direction = 'positive' if score > 0 else 'negative' if score < 0 else 'mixed'
        stage = 'growing' if mature else 'trying'
        if topic['baseline'] == 'avoid' and direction == 'positive':
            stage = 'willing_to_try'
        if stage == 'trying':
            statement = f"对{topic['label']}积累了一些不同体验，尚不能称为稳定的新偏好。"
        elif stage == 'willing_to_try':
            statement = f"仍保留原先对{topic['label']}的明显保留，但开始愿意有限尝试；不是已经喜欢。"
        elif topic['kind'] == 'trait':
            statement = f"多次具体体验后，在{topic['label']}方面的倾向略有{'增强' if direction == 'positive' else '收敛'}；不是整个人格改变，其他性格与边界仍保留。"
        elif direction == 'positive':
            statement = f"多次体验后，对{topic['label']}的接受与兴趣略有增加；不因此抹去原有偏好或差异。"
        else:
            statement = f"多次体验后，对{topic['label']}的倾向略有减弱；不由此改写生平或其他性格。"
        items.append({**topic, 'stage': stage, 'direction': direction, 'statement': statement, 'source_ids': sources[-3:]})
    return {'schema_version': POLICY, 'as_of': stamp, 'version': digest([POLICY, catalog, records]), 'items': items}


def _valid_records(db, now):
    stamp = now.astimezone(timezone.utc).isoformat()
    records = []
    world_versions = None
    for source_id, raw in db.execute('SELECT source_id,payload FROM character_development_events WHERE occurred_at<=?', (stamp,)):
        event = json.loads(raw)
        if event['available_at'] > stamp:
            continue
        source = db.execute('SELECT kind,payload FROM life_moments WHERE source_id=?', (source_id,)).fetchone()
        if not source:
            continue
        payload = json.loads(source[1])
        if event['source_hash'] != (payload.get('digest') if source[0] == 'exchange' else digest(payload)):
            continue
        if event['evidence_kind'] == 'published_world':
            if world_versions is None:
                from .character_emotion import CharacterEmotionStore, _time as emotion_time
                world_versions = {a['source_id']: a['version'] for a in CharacterEmotionStore._state(db, emotion_time(now))[0]}
            if world_versions.get(source_id) != event.get('appraisal_version'):
                continue
        records.append(event)
    return records


def withdrawal_candidates(db, now, *, limit=12):
    """Only locally valid, still active evidence visible before this receipt."""
    records = _valid_records(db, now)
    withdrawn = {(r['withdraws'], r['key']) for r in records if r.get('withdraws')}
    stamp = now.astimezone(timezone.utc).isoformat()
    fields = ('source_id', 'key', 'stance', 'occurred_at', 'available_at', 'character_quote')
    result = {}
    for record in records:
        if (record.get('withdraws') or record['occurred_at'] >= stamp
                or (record['source_id'], record['key']) in withdrawn):
            continue
        result[record['source_id'], record['key']] = {
            **{key: record[key] for key in fields}, 'episode_id': record['experience_id'],
            'experience_quote': record.get('experience_quote', record.get('quote'))}
    # The host selects a recent, bounded target catalog before freezing the request.
    # Quotes and original ledger rows remain complete. Storage validates a selected
    # target against all active evidence, independent of this presentation limit.
    ordered = sorted(result.values(), key=lambda r: (r['occurred_at'], r['source_id'], r['key']), reverse=True)
    if limit is None:
        return ordered
    # A legal no-affect world appraisal can have no character quotation. It
    # remains valid ledger evidence, but cannot satisfy this prompt's required
    # complete quotations. Do not invent one or turn this into an authority gate.
    presentable = [row for row in ordered if all(
        isinstance(row[field], str) and row[field].strip() and len(row[field]) <= 240
        for field in ('experience_quote', 'character_quote'))]
    return presentable[:limit]


def view(db, now):
    return reduce(topics(db), _valid_records(db, now), now)


def project_persona(persona_json, development):
    values = declarations(persona_json)
    if not values:
        return persona_json
    current = {item['key']: item for item in development['items']}
    result = []
    for declaration in values:
        overlays = [current[t['key']] for t in declaration.get('development', []) if t['key'] in current]
        if overlays:
            declaration = {**declaration, 'current_development': overlays,
                           'development_meaning': 'statement保留初始基线与历史；current_development只更新标明key的倾向，不改写其他切面、核心、生平或关系权限。'}
        result.append(declaration)
    return encoded(result)
