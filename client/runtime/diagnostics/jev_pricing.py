"""Approved JEV retail quote; this module never debits an account."""
from decimal import Decimal

PRICE_VERSION = 'jev-input-cny-20260928-v1'
INPUT_CNY_PER_MILLION = Decimal('1.50')
OUTPUT_CNY_PER_MILLION = Decimal('0')


def quote_input_cny(input_tokens: int) -> Decimal:
    """Quote an already deduplicated turn total, without per-attempt rounding.

    Unknown usage must remain unknown. Account attribution, eligible receipts,
    and idempotent wallet settlement are separate requirements, not inferred
    from the provider diagnostic log.
    """
    if type(input_tokens) is not int or input_tokens < 0:
        raise ValueError('JEV_INPUT_USAGE_INVALID')
    return Decimal(input_tokens) * INPUT_CNY_PER_MILLION / Decimal(1_000_000)
