"""Independent integration regressions: Jev decisions cannot be overridden locally."""
import pytest

from tests.memory.test_mem0_memory import FakeMem0, _config, NOW
from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter


@pytest.mark.parametrize('user_text', ['稳定偏好：喝茶。', '你可以称呼我小周。'])
def test_jev_skip_is_not_overridden_by_regex_memory_writers(tmp_path, monkeypatch, user_text):
    class Skip:
        def ask_sync(self, state, questions, *, purpose):
            return {key: 'skip' for key in questions}
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Skip())
    backend = FakeMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    result = adapter.remember_exchange(user_message=user_text, assistant_message='知道了。',
        occurred_at=NOW, source_id='reply:jev-review', user_id='local-user')
    assert result.status.value in {'SKIPPED', 'skipped'}
    assert not [call for method, call in backend.calls if method == 'add']
