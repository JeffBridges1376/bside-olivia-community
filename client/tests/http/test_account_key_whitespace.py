"""A pasted key with surrounding whitespace must not fail every reply."""
import os

from runtime.reply import jev_billing


def test_billing_scope_accepts_key_saved_with_trailing_newline(monkeypatch):
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    monkeypatch.setattr(jev_billing, '_account_key', None)
    jev_billing.configure_account(lambda: ' olivia-' + 'a' * 43 + '\n')
    assert not jev_billing.account_key_missing()
    with jev_billing.billing_scope('turn:1'):
        assert jev_billing.CURRENT.get().key == 'olivia-' + 'a' * 43
