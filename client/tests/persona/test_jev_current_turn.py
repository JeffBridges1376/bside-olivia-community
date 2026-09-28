import asyncio
from runtime.reply.current_turn_interpretation import CurrentTurnInterpreter


def test_optional_interpreter_uses_jev_and_preserves_original(monkeypatch):
    from runtime.reply import jev_questions
    class Decisions:
        async def ask(self, state, questions, *, purpose):
            assert purpose == 'current-turn-interpretation'
            return {key:'question' for key in questions}
    monkeypatch.setattr(jev_questions,'configured_questions',lambda:Decisions())
    value=asyncio.run(CurrentTurnInterpreter(object()).interpret('明天有课吗？'))
    assert value == {'acts':[dict(kind='question',quote='明天有课吗？',meaning='明天有课吗？')]}
