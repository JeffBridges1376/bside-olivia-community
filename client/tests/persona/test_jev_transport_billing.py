"""Billing attaches to actual wire bytes, after the domain response is accepted."""
import asyncio
from copy import deepcopy
from email.message import Message
import hashlib
import json

import pytest

from runtime.reply import companion_decision as companion
from runtime.reply import companion_duties as duties
from runtime.reply import companion_proactive as proactive
from runtime.reply import jev_questions as questions
from tests.persona.test_companion_decision import input_args, envelope as companion_envelope
from tests.persona.test_companion_duties import packet as duty_packet, envelope as duty_envelope
from tests.persona.test_companion_proactive import packet as proactive_packet, envelope as proactive_envelope


KINDS = ['companion', 'persona', 'exchange', 'world', 'proactive', 'questions', 'questions_sync']
RECEIPT = {'receipt_id': 'synthetic-receipt', 'amount': 1}


def transport(monkeypatch, kind, *, receipt=True, invalid=False, settlement_error=None, profile='full'):
    calls, settlements, headers = [], [], []

    def billing_headers():
        headers.append(True)
        return {'X-Olivia-Billing-Account': 'synthetic-account'}

    def settle(value, digest):
        settlements.append((value, digest))
        if settlement_error:
            raise settlement_error

    async def settle_async(value, digest):
        settle(value, digest)

    for module in (companion, duties, proactive, questions):
        monkeypatch.setattr(module, 'billing_headers', billing_headers, raising=False)
        monkeypatch.setattr(module, 'settle_receipt', settle_async, raising=False)
        monkeypatch.setattr(module, 'settle_receipt_sync', settle, raising=False)

    class Response:
        status = 200
        headers = Message()
        headers['Content-Type'] = 'application/json'

        def __init__(self, value):
            self.body = companion._json(value).encode('utf-8')

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            return self.body[:limit]

    class Opener:
        def open(self, request, **_):
            calls.append(request)
            body = json.loads(request.data)
            if kind == 'companion':
                value = companion_envelope()
                if profile == 'single_delivery':
                    value['evaluation'] = dict(profile=profile, not_evaluated=['control'])
            elif kind == 'proactive':
                value = proactive_envelope(body)
            elif kind.startswith('questions'):
                value = {'backend': 'jev', 'input_digest': hashlib.sha256(request.data).hexdigest(),
                         'decisions': {'choice': 'yes'}}
            else:
                value = duty_envelope(kind, body)
            if invalid:
                value['backend'] = 'untrusted'
            if receipt:
                value['billing'] = receipt(request) if callable(receipt) else deepcopy(RECEIPT)
            return Response(value)

    if kind == 'companion':
        port = companion.JevDecisionPort(profile=profile)
        port._opener = Opener()
        turn = companion.FrozenCompanionTurn.create(**input_args())
        run = lambda: asyncio.run(port.decide(turn))
    elif kind == 'proactive':
        port = proactive.JevProactivePort()
        port._transport._transport._opener = Opener()
        run = lambda: asyncio.run(port.evaluate(proactive_packet()))
    elif kind.startswith('questions'):
        port = questions.JevQuestionsPort(companion.DEFAULT_ENDPOINT)
        port.transport._opener = Opener()
        args = ({'text': 'Synthetic question'}, {'choice': {'instructions': 'Choose', 'criteria': {'yes': 'Yes', 'no': 'No'}}})
        run = (lambda: port.ask_sync(*args, purpose='synthetic')) if kind.endswith('_sync') else (
            lambda: asyncio.run(port.ask(*args, purpose='synthetic')))
    else:
        port = duties.JevDutiesPort()
        port._transport._opener = Opener()
        run = lambda: asyncio.run(port.evaluate(kind, duty_packet(kind)))
    return run, calls, settlements, headers


def test_single_coverage_not_accepted_by_full_transport_before_settlement(monkeypatch):
    original = companion_envelope
    def partial():
        value = original()
        value['evaluation'] = dict(profile='single_delivery', not_evaluated=['control'])
        return value
    monkeypatch.setattr(__import__(__name__, fromlist=['companion_envelope']), 'companion_envelope', partial)
    run, calls, settlements, _ = transport(monkeypatch, 'companion')
    assert run().error_code == 'JEV_RESPONSE_INVALID'
    assert len(calls) == 1 and settlements == []


@pytest.mark.parametrize('kind', KINDS)
def test_validated_response_settles_once_against_exact_http_body(monkeypatch, kind):
    run, calls, settlements, headers = transport(monkeypatch, kind)
    result = run()
    if kind.startswith('questions'):
        assert result == {'choice': 'yes'}
    else:
        assert result.error_code is None
        if kind != 'companion':
            assert 'billing' not in json.loads(result.response_json)
    assert len(calls) == len(headers) == len(settlements) == 1
    request = calls[0]
    assert dict((k.lower(), v) for k, v in request.header_items())['x-olivia-billing-account'] == 'synthetic-account'
    assert settlements == [(RECEIPT, hashlib.sha256(request.data).hexdigest())]
    assert 'billing' not in json.loads(request.data)
    if kind == 'companion':
        assert settlements[0][1] != result.decision.input_digest


@pytest.mark.parametrize('kind', KINDS)
def test_invalid_domain_response_never_settles(monkeypatch, kind):
    run, calls, settlements, _ = transport(monkeypatch, kind, invalid=True)
    if kind.startswith('questions'):
        with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
            run()
    else:
        assert run().error_code == 'JEV_RESPONSE_INVALID'
    assert len(calls) == 1 and settlements == []


@pytest.mark.parametrize('kind', KINDS)
def test_missing_receipt_still_reaches_scope_validation_and_fails_closed(monkeypatch, kind):
    run, calls, settlements, _ = transport(monkeypatch, kind, receipt=False,
        settlement_error=ValueError('JEV_UNAVAILABLE'))
    if kind.startswith('questions'):
        with pytest.raises(ValueError, match='JEV_UNAVAILABLE'):
            run()
    else:
        assert run().error_code == 'JEV_UNAVAILABLE'
    assert settlements == [(None, hashlib.sha256(calls[0].data).hexdigest())]


@pytest.mark.parametrize('kind', ['companion', 'persona', 'exchange', 'world', 'proactive'])
def test_cancellation_during_settlement_never_returns_a_decision(monkeypatch, kind):
    run, calls, settlements, _ = transport(monkeypatch, kind, settlement_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        run()
    assert len(calls) == len(settlements) == 1


def test_local_proactive_defer_does_not_request_or_settle(monkeypatch):
    _, calls, settlements, headers = transport(monkeypatch, 'proactive')
    value = proactive_packet()
    value['hard_gates']['paused'] = True
    result = asyncio.run(proactive.JevProactivePort().evaluate(value))
    assert result.decision['action'] == 'defer'
    assert calls == settlements == headers == []


@pytest.mark.parametrize('kind', KINDS)
def test_real_scope_survives_transport_threads_and_settles_validated_body(monkeypatch, kind):
    from runtime.reply import jev_billing

    account = 'olivia-synthetic-transport-account'
    account_digest = hashlib.sha256(account.encode()).hexdigest()
    turn_id = 'synthetic:transport:7'

    def receipt(request):
        return {'signature': 'synthetic-signed-receipt', 'receipt': {
            'v': 1, 'turn_id': turn_id, 'account_key_digest': account_digest,
            'input_digest': hashlib.sha256(request.data).hexdigest(),
            'price_version': jev_billing.PRICE_VERSION, 'input_tokens': 17,
            'operation_id': 'synthetic-operation',
        }}

    run, calls, _, _ = transport(monkeypatch, kind, receipt=receipt)
    for module in (companion, duties, proactive, questions):
        monkeypatch.setattr(module, 'billing_headers', jev_billing.billing_headers, raising=False)
        monkeypatch.setattr(module, 'settle_receipt', jev_billing.settle_receipt, raising=False)
        monkeypatch.setattr(module, 'settle_receipt_sync', jev_billing.settle_receipt_sync, raising=False)
    paid = []

    def settlement(key, signed):
        paid.append((key, signed))
        data = signed['receipt']
        return dict(status='settled', turn_id=data['turn_id'], operation_id=data['operation_id'],
            price_version=data['price_version'], input_tokens=17, charged_units=2550,
            replayed=False, debited_units=2550)

    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(jev_billing, '_account_key', lambda: account)
    monkeypatch.setattr(jev_billing, '_post_settlement', settlement)
    with jev_billing.billing_scope(turn_id):
        result = run()
    assert result == {'choice': 'yes'} if kind.startswith('questions') else result.error_code is None
    assert len(paid) == len(calls) == 1 and paid[0][0] == account
    request_headers = {key.lower(): value for key, value in calls[0].header_items()}
    assert request_headers['x-olivia-account-digest'] == account_digest
    assert request_headers['x-olivia-turn-id'] == turn_id
    assert account not in calls[0].data.decode()
    assert paid[0][1]['receipt']['input_digest'] == hashlib.sha256(calls[0].data).hexdigest()
    assert jev_billing.CURRENT.get() is None


@pytest.mark.parametrize('wrong_profile_digest', [False, True])
def test_single_delivery_signed_receipt_binds_profile_in_exact_wire_body(monkeypatch, wrong_profile_digest):
    import hmac
    from runtime.reply import jev_billing

    account = 'olivia-synthetic-single-delivery-account'
    turn_id = 'synthetic:single:1'
    secret = b'synthetic-test-signing-secret-not-a-production-key'

    def sign(value):
        return hmac.new(secret, companion._json(value).encode(), hashlib.sha256).hexdigest()

    def receipt(request):
        body = json.loads(request.data)
        assert body['profile'] == 'single_delivery'
        if wrong_profile_digest:
            body.pop('profile')
        data = dict(v=1, turn_id=turn_id, account_key_digest=hashlib.sha256(account.encode()).hexdigest(),
                    input_digest=hashlib.sha256(companion._json(body).encode()).hexdigest(),
                    price_version=jev_billing.PRICE_VERSION, input_tokens=17, operation_id='synthetic-single-operation')
        return {'receipt': data, 'signature': sign(data)}

    run, calls, _, _ = transport(monkeypatch, 'companion', profile='single_delivery', receipt=receipt)
    monkeypatch.setattr(companion, 'billing_headers', jev_billing.billing_headers)
    monkeypatch.setattr(companion, 'settle_receipt', jev_billing.settle_receipt)
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(jev_billing, '_account_key', lambda: account)
    paid = []

    def settle(key, signed):
        assert key == account
        assert hmac.compare_digest(signed['signature'], sign(signed['receipt']))
        paid.append(signed)
        data = signed['receipt']
        return dict(status='settled', turn_id=turn_id, operation_id=data['operation_id'],
                    price_version=data['price_version'], input_tokens=17,
                    charged_units=2550, replayed=False, debited_units=2550)

    monkeypatch.setattr(jev_billing, '_post_settlement', settle)
    with jev_billing.billing_scope(turn_id):
        result = run()
    assert len(calls) == 1
    body = json.loads(calls[0].data)
    assert set(body) == {'input', 'profile'}
    assert body['profile'] == 'single_delivery'
    if wrong_profile_digest:
        assert result.error_code == 'JEV_UNAVAILABLE'
        assert paid == []
    else:
        assert result.error_code is None and len(paid) == 1
        assert paid[0]['receipt']['input_digest'] == hashlib.sha256(calls[0].data).hexdigest()


def test_default_profile_retains_legacy_wire_shape(monkeypatch):
    run, calls, settlements, _ = transport(monkeypatch, 'companion')
    assert run().error_code is None
    assert set(json.loads(calls[0].data)) == {'input'}
    assert settlements[0][1] == hashlib.sha256(calls[0].data).hexdigest()
