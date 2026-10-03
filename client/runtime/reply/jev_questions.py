"""Finite semantic decisions through the configured Jev sidecar, never a chat fallback."""
import asyncio
import hashlib
import json
import os
import urllib.error
import urllib.request

from .companion_decision import JevDecisionPort, _json, _pairs, _nonfinite, _http_error_code
from .jev_billing import billing_headers, settle_receipt_sync
from .jev_semantic_service import _decision_detail

from .jev_limits import JEV_MAX_INPUT_BYTES as SEMANTIC_REQUEST_MAX_BYTES


class JevQuestionsPort:
    def __init__(self, endpoint, *, token='', timeout_seconds=50):
        self.transport = JevDecisionPort(endpoint, token=token, timeout_seconds=timeout_seconds)

    def _request(self, packet, *, detailed=False):
        from runtime.model_policy import encode_decision
        packet = encode_decision(packet, self.transport.endpoint)
        body = _json(packet).encode('utf-8')
        if len(body) > SEMANTIC_REQUEST_MAX_BYTES:
            raise ValueError('JEV_INPUT_TOO_LARGE')
        digest = hashlib.sha256(body).hexdigest()
        headers = self.transport.request_headers(digest)
        req = urllib.request.Request(self.transport.endpoint.rsplit('/', 1)[0] + '/semantic-decisions',
                                     data=body, headers=headers, method='POST')
        try:
            with self.transport._opener.open(req, timeout=self.transport.timeout_seconds) as response:
                if response.status != 200 or response.headers.get_content_type() != 'application/json':
                    raise ValueError('JEV_RESPONSE_INVALID')
                raw = response.read(262145)
        except urllib.error.HTTPError as error:
            raise ValueError(_http_error_code(error)) from None
        except (TimeoutError, urllib.error.URLError, OSError):
            raise ValueError('JEV_UNAVAILABLE') from None
        if len(raw) > 262144:
            raise ValueError('JEV_RESPONSE_INVALID')
        try:
            value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
        except (ValueError, TypeError):
            raise ValueError('JEV_RESPONSE_INVALID') from None
        if not isinstance(value, dict):
            raise ValueError('JEV_RESPONSE_INVALID')
        billing = value.pop('billing', None)
        answers = value.get('decisions')
        if (value.get('backend') != 'jev' or value.get('input_digest') != digest
                or not isinstance(answers, dict) or set(answers) != set(packet['questions'])
                or any(not isinstance(answer, str) or answer not in packet['questions'][key]['criteria']
                       for key, answer in answers.items())):
            raise ValueError('JEV_RESPONSE_INVALID')
        details = value.get('decision_details', {})
        if not isinstance(details, dict) or not set(details) <= set(answers):
            raise ValueError('JEV_RESPONSE_INVALID')
        validated = {}
        for key, choice in answers.items():
            detail = details.get(key, {'choice': choice})
            if (not isinstance(detail, dict) or detail.get('choice') != choice
                    or not set(detail) <= {'choice', 'probabilities', 'confidence', 'confidence_source'}):
                raise ValueError('JEV_RESPONSE_INVALID')
            try:
                validated[key] = _decision_detail(detail, packet['questions'][key]['criteria'])
            except ValueError:
                raise ValueError('JEV_RESPONSE_INVALID') from None
        settle_receipt_sync(billing, digest)
        return validated if detailed else answers

    async def ask(self, state, questions, *, purpose):
        if not questions:
            return {}
        # Serialize before yielding: mutable caller state cannot alter an in-flight decision.
        packet = json.loads(_json(dict(state=state, questions=questions, purpose=purpose)))
        return await asyncio.to_thread(self._request, packet)

    def ask_sync(self, state, questions, *, purpose):
        if not questions:
            return {}
        return self._request(json.loads(_json(dict(state=state, questions=questions, purpose=purpose))))

    async def ask_detailed(self, state, questions, *, purpose):
        """Return validated choices and available confidence in the same request."""
        if not questions:
            return {}
        packet = json.loads(_json(dict(state=state, questions=questions, purpose=purpose)))
        return await asyncio.to_thread(self._request, packet, detailed=True)

    def ask_detailed_sync(self, state, questions, *, purpose):
        if not questions:
            return {}
        packet = json.loads(_json(dict(state=state, questions=questions, purpose=purpose)))
        return self._request(packet, detailed=True)


def configured_questions():
    endpoint = os.environ.get('OLIVIA_JEV_DECISION_URL', '').strip()
    return JevQuestionsPort(endpoint, token=os.environ.get('COMPANION_CLASSIFIER_TOKEN', '')) if endpoint else None
