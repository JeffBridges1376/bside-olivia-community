from copy import deepcopy
import hashlib
import json

import pytest

from runtime.model_policy import PREFIX, aliases
from runtime.reply import jev_questions
from original_client_relay_api import RELAY_BASE


@pytest.mark.parametrize('endpoint,managed', [
    ('http://127.0.0.1:1/v1/companion/decide', False),
    (RELAY_BASE + '/companion/decide', True),
])
def test_size_is_exact_encoded_request_body_and_boundary_without_mutating_inputs(monkeypatch, endpoint, managed):
    instruction = aliases()[0][0]
    state = {'rules': instruction, 'history': '合成\"原文\n🙂'}
    questions = {'q': {'instructions': instruction, 'criteria': {'ok': '是', 'no': '否'}}}
    original = deepcopy((state, questions))
    port = jev_questions.JevQuestionsPort(endpoint)
    sent = []
    class Response:
        status = 200
        headers = type('Headers', (), {'get_content_type': lambda _: 'application/json'})()
        def __init__(self, raw): self.raw = raw
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return self.raw
    class Opener:
        def open(self, req, **kwargs):
            sent.append(req.data)
            return Response(json.dumps(dict(backend='jev',
                input_digest=hashlib.sha256(req.data).hexdigest(), decisions={'q': 'ok'})).encode('utf-8'))
    port.transport._opener = Opener()
    monkeypatch.setattr(port.transport, 'request_headers',
                        lambda digest: {'Content-Type': 'application/json'})
    monkeypatch.setattr(jev_questions, 'settle_receipt_sync', lambda *args: None)
    size = port.request_size_bytes(state, questions, purpose='historical-relationship')
    monkeypatch.setattr(jev_questions, 'SEMANTIC_REQUEST_MAX_BYTES', size)
    assert port.ask_sync(state, questions, purpose='historical-relationship') == {'q': 'ok'}
    assert len(sent) == 1 and len(sent[0]) == size
    encoded = json.loads(sent[0])['questions']['q']['instructions']
    assert encoded.startswith(PREFIX) is managed
    assert (state, questions) == original
    monkeypatch.setattr(jev_questions, 'SEMANTIC_REQUEST_MAX_BYTES', size - 1)
    with pytest.raises(ValueError, match='^JEV_INPUT_TOO_LARGE$'):
        port.ask_sync(state, questions, purpose='historical-relationship')
    assert len(sent) == 1  # One-byte overflow is rejected before the transport.
