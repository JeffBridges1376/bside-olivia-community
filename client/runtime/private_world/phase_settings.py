"""Move authored semester seeds into the existing world journal once.

Activation creates an initial *plan*, not evidence of attendance or practice.
Later world events own its progress. A new semester never resurrects the seed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

from .daily_life import _identifier, _json, _text, _time
from .life_rhythm import LOCAL


def ensure_phase_projects(store, persona_json: str, now: datetime) -> int:
    stamp = _time(now)
    values = json.loads(persona_json)
    if not isinstance(values, list):
        raise ValueError('PHASE_SETTING_INVALID')
    seeds = []
    for item in values:
        if not isinstance(item, dict) or item.get('inclusion') != 'phase' or item.get('phase_seed') is None:
            continue
        seed = item['phase_seed']
        if (not isinstance(seed, dict) or set(seed) != {'title', 'detail', 'activity_kind', 'scope'}
                or seed['activity_kind'] not in {'practice', 'reading', 'creative'}
                or seed['scope'] != 'activation_semester'):
            raise ValueError('PHASE_SETTING_INVALID')
        try:
            seeds.append((_identifier(item['declaration_id']), _text(seed['title'], 60),
                          _text(seed['detail'], 240)))
        except (ValueError, TypeError, KeyError):
            raise ValueError('PHASE_SETTING_INVALID') from None
    if len({s[0] for s in seeds}) != len(seeds):
        raise ValueError('PHASE_SETTING_INVALID')
    local = now.astimezone(LOCAL)
    if not datetime(2025, 9, 1, tzinfo=LOCAL) <= local < datetime(2029, 7, 1, tzinfo=LOCAL):
        return 0
    if local.month >= 9 or local.month < 2:
        end = datetime(local.year + (local.month >= 9), 2, 1, tzinfo=LOCAL)
    else:
        end = datetime(local.year, 9, 1, tzinfo=LOCAL)
    end = min(end, datetime(2029, 7, 1, tzinfo=LOCAL))
    count = 0
    with store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        for declaration_id, title, detail in seeds:
            source_id = 'phase:' + declaration_id
            if db.execute('SELECT 1 FROM life_moments WHERE source_id=?', (source_id,)).fetchone():
                continue
            existing = [json.loads(row[0]) for row in db.execute('SELECT payload FROM life_projects')]
            conflict = any(p['id'] == source_id or (p.get('kind') == 'linli' and p['title'] == title) for p in existing)
            projects = []
            if not conflict:
                project = {'id': source_id, 'title': title, 'detail': detail, 'status': 'planned',
                           'kind': 'linli', 'actor': 'template', 'source_id': source_id,
                           'updated_at': stamp, 'valid_until': _time(end.astimezone(timezone.utc)),
                           'evidence_kind': 'initial_plan', 'declaration_id': declaration_id}
                projects.append(project)
                db.execute('INSERT INTO life_projects VALUES (?,?)', (source_id, _json(project)))
                count += 1
            # Remember a skipped seed too: edits or a later semester must not
            # manufacture a new version of an already existing practice project.
            db.execute('INSERT INTO life_moments VALUES (?,?,?,?)',
                       (source_id, stamp, 'phase_seed', _json({'updates': projects})))
    return count


def project_at(project: dict, now: datetime) -> dict:
    if project.get('evidence_kind') != 'initial_plan' or project.get('status') not in {'planned', 'ongoing'}:
        return project
    if _time(now) < project['valid_until']:
        return project
    return {**project, 'status': 'paused', 'phase_expired': True,
            'detail': '初始学期安排已过期，待重新安排；没有完成记录，也没有实际开始或主动暂停的证据。paused仅表示旧计划不再自动沿用，实际进展未知。'}
