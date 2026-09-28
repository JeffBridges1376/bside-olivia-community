"""Valid generated decisions for tests concerned with storage/retry wiring."""
import json


def life_decision(messages, *, focus='', kind='rest', meal=None, project=None):
    data = json.loads(messages[1]['content'])
    if kind not in data['allowed_activity_kinds']:
        kind = data['allowed_activity_kinds'][0]
    return {'activity': {'kind': kind, 'place_id': 'campus' if kind == 'class' else 'home', 'focus': focus},
            'meal': meal, 'project': project}
