import sqlite3

from runtime.diagnostics.jev_metering import JevMeter


def test_attempts_are_durable_and_unknown_is_not_zero(tmp_path):
    path = tmp_path / 'usage.sqlite3'
    meter = JevMeter(path)
    first = meter.begin('turn-digest', 'decide', 'jev-1.13.0')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT outcome,input_tokens FROM provider_attempts').fetchone() == ('pending', None)
    meter.finish(first, 'http_503')
    retry = meter.begin('turn-digest', 'decide', 'jev-1.13.0')
    meter.finish(retry, 'completed', {'usage': {'input_tokens': 1200}})
    meter.finish(retry, 'completed', {'usage': {'input_tokens': 9999}})
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT outcome,input_tokens FROM provider_attempts ORDER BY started_at').fetchall() == [
            ('http_503', None), ('completed', 1200)]


def test_invalid_plan_can_still_have_provider_cost(tmp_path):
    path = tmp_path / 'usage.sqlite3'
    meter = JevMeter(path)
    attempt = meter.begin('digest', 'experience-appraisal', 'jev-1.13.0')
    meter.finish(attempt, 'failed', {'usage': {'input_tokens': 42}})
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT input_tokens FROM provider_attempts').fetchone() == (42,)
