"""Sidecar handler for bounded choice questions. Provider Client is injected."""
import hashlib
import json
import re

from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES as SEMANTIC_REQUEST_MAX_BYTES


def decide(client, packet):
    if not isinstance(packet, dict) or set(packet) != {'state', 'questions', 'purpose'}:
        raise ValueError('invalid_request')
    if not isinstance(packet['purpose'], str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', packet['purpose']):
        raise ValueError('invalid_purpose')
    questions = packet['questions']
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 384:
        raise ValueError('invalid_questions')
    native = {}
    for key, value in questions.items():
        if (not isinstance(key, str) or not re.fullmatch('[a-zA-Z0-9_.:-]{1,200}', key)
                or not isinstance(value, dict) or set(value) != {'instructions', 'criteria'}
                or not isinstance(value['instructions'], str) or not 1 <= len(value['instructions']) <= 12000
                or not isinstance(value['criteria'], dict) or not 1 <= len(value['criteria']) <= 255
                or any(not isinstance(k, str) or not k or len(k) > 200 for k in value['criteria'])):
            raise ValueError('invalid_question')
        native[key] = {'type': 'choice', **value}
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode('utf-8')) > SEMANTIC_REQUEST_MAX_BYTES:
        raise ValueError('invalid_body_size')
    client.purpose = packet['purpose']
    fixed = {key: next(iter(q['criteria'])) for key, q in native.items() if len(q['criteria']) == 1}
    questions = {key:q for key,q in native.items() if key not in fixed}
    answers = client.ask(packet['state'], questions) if questions else {}
    return dict(decisions={**fixed, **{key: value['choice'] for key, value in answers.items()}},
                input_digest=hashlib.sha256(encoded.encode('utf-8')).hexdigest())
