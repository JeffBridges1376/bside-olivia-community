"""Run against staged cloud sources with an isolated Django test database."""
from unittest.mock import patch

from django.test import TestCase, override_settings

from qwen.quota import grant_money, issue_key, reserve, settle
from qwen.relay_models import CURRENT_VERSION, DISCOUNT_VERSION, retail_rates


@override_settings(RELAYJETTY_API_KEY='synthetic-pricing-provider')
class PricingReceiptTests(TestCase):
    def test_pending_receipt_and_new_request_settle_at_their_own_frozen_rates(self):
        account, key = issue_key('SYNTHETIC pricing receipts')
        grant_money(account.pk, 1_000_000_000, 'synthetic-pricing-credit', 'test')
        previous = (DISCOUNT_VERSION, 10_763_040, 53_815_200)
        with patch('qwen.quota.retail_rates', return_value=previous):
            old = reserve(key, 20000, 'claude-sonnet-5-5', 'synthetic-old')
        new = reserve(key, 20000, 'claude-sonnet-5-5', 'synthetic-new')
        self.assertEqual((new.price_version, new.input_rate, new.output_rate),
                         retail_rates('claude-sonnet-5-5'))
        self.assertEqual(new.price_version, CURRENT_VERSION)
        for row in (old, new):
            settle(row.pk, 10000, 1000)
            settle(row.pk, 10000, 1000)
            row.refresh_from_db()
            self.assertEqual(row.status, 'settled')
        self.assertEqual(old.charged_units, 16_144_560)
        self.assertEqual(new.charged_units, 20_180_700)
        account.refresh_from_db()
        self.assertEqual(account.used_units, old.charged_units + new.charged_units)
        self.assertEqual(account.held_units, 0)
