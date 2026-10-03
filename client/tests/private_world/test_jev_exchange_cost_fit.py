"""A dependent preflight failure never buys the same anchor phase again."""
import asyncio

import pytest

from runtime.private_world import jev_exchange


class AnchorPort:
    def __init__(self, *, overflow=False):
        self.calls = []
        self.overflow = overflow

    async def ask(self, state, questions, *, purpose):
        self.calls.append(purpose)
        answers = {key: 'none' if 'none' in question['criteria'] else next(iter(question['criteria']))
                   for key, question in questions.items()}
        answers['capacity'] = 'unsupported' if self.overflow and len(self.calls) == 1 else 'ok'
        if answers['capacity'] == 'ok':
            answers['update_0_quote'] = next(ref for ref, anchor in state['action_anchors'].items()
                                             if anchor['text'] == '我练完琴了。')
        return answers


@pytest.mark.parametrize('overflow,expected_calls', [
    (False, ['exchange-facts']), (True, ['exchange-facts', 'exchange-facts']),
])
def test_dependent_oversize_does_not_repeat_completed_anchor_phase(monkeypatch, overflow, expected_calls):
    original = jev_exchange._ask
    attempts = []

    async def oversized_dependent(port, state, questions, purpose, **kwargs):
        attempts.append(purpose)
        if purpose == 'exchange-anchored-facts':
            raise ValueError('JEV_INPUT_TOO_LARGE')
        return await original(port, state, questions, purpose, **kwargs)

    monkeypatch.setattr(jev_exchange, '_ask', oversized_dependent)
    port = AnchorPort(overflow=overflow)
    with pytest.raises(ValueError, match='^JEV_EXCHANGE_DEPENDENT_CAPACITY$'):
        asyncio.run(jev_exchange.extract(port, {'linli_reply': '我练完琴了。'}, '', 'synthetic-cost-fit'))
    assert port.calls == expected_calls
    assert attempts.count('exchange-anchored-facts') == 1
    assert 'JEV_EXCHANGE_DEPENDENT_CAPACITY' in jev_exchange.EXCHANGE_ERROR_CODES


def test_first_phase_oversize_stays_before_any_provider_invocation(monkeypatch):
    monkeypatch.setattr(jev_exchange, 'EXCHANGE_MAX_INPUT_BYTES', 1)
    port = AnchorPort()
    with pytest.raises(ValueError, match='^JEV_INPUT_TOO_LARGE$'):
        asyncio.run(jev_exchange.extract(port, {'linli_reply': '我练完琴了。'}, '', 'synthetic-preflight'))
    assert port.calls == []
