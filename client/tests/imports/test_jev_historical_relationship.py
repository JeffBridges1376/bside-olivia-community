import asyncio
from datetime import datetime, timezone
from runtime.imports.historical_memory import HistoricalExchange, assess_historical_relationship


def test_historical_relationship_uses_jev_only(monkeypatch):
    from runtime.reply import jev_questions
    class Decisions:
        async def ask(self, state, questions, *, purpose):
            assert purpose == 'historical-relationship'
            return {key: 'familiar' if key == 'stage' else 'yes' if key.startswith('e') else '20'
                    for key in questions}
    monkeypatch.setattr(jev_questions,'configured_questions',lambda:Decisions())
    # No complete method: a hidden text model call makes this fail.
    result = asyncio.run(assess_historical_relationship([
        HistoricalExchange('test',datetime.now(timezone.utc),'最近练习还好吗','慢慢有进步了')],
        gateway=object(),persona_policy='身份需要双方明确确认'))
    assert result.trust == 20 and result.evidence_indexes == (1,)
