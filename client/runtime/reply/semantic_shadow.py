"""Opt-in local classifier observations; never authorize an action or rewrite."""
import asyncio
import json
import math
import os
import re
from urllib.parse import urlsplit

import aiohttp

from runtime.reply.reply_model_quality import _recent_dialogue


SCHEMA = 'companion-shadow/1'


def shadow_input(messages, user_text, kinds):
    """Preserve complete utterances; the service rejects over-budget input."""
    recent = _recent_dialogue(messages)
    if (not isinstance(user_text, str) or not user_text.strip()
            or len(recent) > 31 or any(row.get('truncated') for row in recent)):
        raise ValueError('context_insufficient')
    turns = [dict(id=f'm{i}', role=row['role'], text=row['text']) for i, row in enumerate(recent)]
    turns.append(dict(id='current', role='user', text=user_text))
    return dict(messages=turns, current_turn_id='current',
        capabilities=dict(kinds=list(kinds), synchronize=False, playback_events=False,
                          compose_audio=False, compose_video=False, split_spoken_content=False),
        environment=dict(can_read=None, can_view=None, can_listen=None), forbidden_kinds=[])


def metadata(value):
    """Only bounded non-content fields may enter persisted diagnostics."""
    fields = ('schema_version', 'model', 'contract_valid', 'latency_ms',
              'fallback', 'action_executed', 'production_approved')
    if (not isinstance(value, dict) or not set(fields).issubset(value)
            or value.get('schema_version') != SCHEMA
            or value.get('fallback') is not True or value.get('action_executed') is not False
            or value.get('production_approved') is not False
            or not (value.get('contract_valid') is None or type(value.get('contract_valid')) is bool)
            or not isinstance(value.get('model'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value['model'])
            or type(value.get('latency_ms')) not in {int, float}
            or not math.isfinite(value['latency_ms']) or value['latency_ms'] < 0):
        raise ValueError('invalid_response')
    return {key: value[key] for key in fields}


async def observe(messages, user_text, kinds):
    endpoint = os.environ.get('OLIVIA_SEMANTIC_SHADOW_URL', '').strip()
    if not endpoint:
        return None
    # This experimental checkpoint has only been accepted for local observation.
    # Deployment beyond loopback needs separate authentication and acceptance.
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return dict(status='invalid_endpoint', fallback=True)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != '/v1/companion/shadow'):
        return dict(status='invalid_endpoint', fallback=True)
    try:
        payload = dict(input=shadow_input(messages, user_text, kinds))
        headers = {}
        token = os.environ.get('COMPANION_SHADOW_TOKEN', '')
        if token:
            headers['Authorization'] = 'Bearer ' + token
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(endpoint, json=payload, headers=headers, allow_redirects=False) as response:
                try:
                    body = await response.content.readexactly(32769)
                except asyncio.IncompleteReadError as exc:
                    body = exc.partial
                if len(body) > 32768:
                    return dict(status='invalid_response', fallback=True)
                value = json.loads(body)
                if response.status != 200:
                    code = value.get('error') if isinstance(value, dict) else None
                    return dict(status=code if code in {'context_too_long', 'model_busy', 'invalid_input',
                        'unauthorized', 'inference_unavailable'} else 'service_unavailable', fallback=True)
                return dict(status='observed', **metadata(value))
    except ValueError as exc:
        return dict(status='context_insufficient' if str(exc) == 'context_insufficient'
                    else 'invalid_response', fallback=True)
    except Exception:
        # Optional diagnostics must not change delivery on a transport failure.
        # Cancellation still propagates (CancelledError is a BaseException).
        return dict(status='service_unavailable', fallback=True)
