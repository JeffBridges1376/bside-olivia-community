import hashlib
import pytest
from runtime.reply.companion_decision import JevDecisionPort, CompanionDecisionError
from runtime.reply import jev_billing
from original_client_relay_api import RELAY_BASE


def test_only_fixed_cloud_endpoint_can_receive_account_auth(monkeypatch):
    monkeypatch.setattr(jev_billing, '_account_key', lambda: 'olivia-synthetic-cloud-key')
    port = JevDecisionPort(RELAY_BASE + '/companion/decide', token='local-token-not-cloud')
    digest = hashlib.sha256(b'{}').hexdigest()
    headers = port.request_headers(digest)
    assert headers['Authorization'] == 'Bearer ' + 'olivia-synthetic-cloud-key'
    assert headers['X-Olivia-Turn-Id'] == 'jev:' + digest
    assert headers['X-Olivia-Account-Digest'] == hashlib.sha256(b'olivia-synthetic-cloud-key').hexdigest()
    assert 'local-token-not-cloud' not in str(headers)


@pytest.mark.parametrize('url', ['https://example.test/v1/companion/decide',
    RELAY_BASE.replace('https:', 'http:') + '/companion/decide',
    RELAY_BASE + '/companion/decide?redirect=1', RELAY_BASE + '/other/decide'])
def test_untrusted_endpoint_never_resolves_key(monkeypatch, url):
    def no_key():
        pytest.fail('key lookup before endpoint validation')
    monkeypatch.setattr(jev_billing, '_account_key', no_key)
    with pytest.raises(CompanionDecisionError):
        JevDecisionPort(url)


def test_local_transport_keeps_dev_token_without_cloud_key(monkeypatch):
    monkeypatch.setattr(jev_billing, '_account_key', lambda: pytest.fail('local must not read cloud key'))
    assert JevDecisionPort(token='local-test').request_headers('a'*64)['Authorization'] == 'Bearer local-test'


def test_cloud_preserves_existing_turn_and_account(monkeypatch):
    token = jev_billing.CURRENT.set(jev_billing.BillingTurn('letter:test', 'olivia-synthetic-turn-key'))
    try:
        monkeypatch.setattr(jev_billing, '_account_key', lambda: pytest.fail('must use frozen turn account'))
        headers = JevDecisionPort(RELAY_BASE + '/companion/decide').request_headers('a'*64)
        assert headers['X-Olivia-Turn-Id'] == 'letter:test'
        assert headers['Authorization'] == 'Bearer ' + 'olivia-synthetic-turn-key'
    finally:
        jev_billing.CURRENT.reset(token)
