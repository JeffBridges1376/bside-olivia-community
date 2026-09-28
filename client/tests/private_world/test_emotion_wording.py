import json
from copy import deepcopy

from runtime.private_world.emotion_wording import published_wording, presentation_text


def test_legacy_concern_uses_exact_event_subject_without_inventing_lesson_detail():
    note = '这一处暂时没弄懂，不代表整节课失败或已经结束。'
    payload = dict(note=note, activity='上钢琴专业课', activity_kind='class')
    source = dict(source_kind='published_world', text=note + '\n' + json.dumps(payload, ensure_ascii=False))
    original = deepcopy(source)
    assert presentation_text(source, note, with_subject=True) == '上钢琴专业课时，有一部分内容还没理解；具体疑问尚未记录。'
    assert presentation_text(source, note) == note
    assert presentation_text(source, '另一个独立问题', with_subject=True) == '另一个独立问题'
    assert source == original
    source['source_kind'] = 'received_input'
    assert presentation_text(source, note, with_subject=True) == note


def test_concern_subject_is_not_replaced_by_later_unrelated_activity():
    note = '这一小节还没弹稳。'
    source = dict(source_kind='published_world', text=note + '\n' + json.dumps(dict(note=note, activity='练习夜曲'), ensure_ascii=False))
    summary = presentation_text(source, note, with_subject=True)
    assert summary == '练习夜曲：这一小节还没弹稳。'
    later = dict(source_kind='published_world', text='休息。\n' + json.dumps(dict(note='休息。', activity='休息'), ensure_ascii=False))
    assert presentation_text(later, summary, with_subject=True) == summary


def test_published_json_tail_never_becomes_visible_concern():
    note = '这处练习仍然没有弄懂，先记下来，下次继续。'
    value = {'note': note, 'occurred_at': '2026-09-28T06:00:00+00:00', 'progress': [], 'source_id': 'day:test'}
    envelope = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    source = {'source_kind': 'published_world', 'text': note + '\n' + envelope}
    tail = envelope[envelope.index(',"occurred_at"'):]
    assert published_wording(source) == note
    assert presentation_text(source, tail) == note
    assert presentation_text(source, '练习进度让她有些挂心') == '练习进度让她有些挂心'


def test_user_json_and_nonmatching_envelopes_are_not_stripped():
    text = '帮我检查JSON\n{"note":"另外的文字"}'
    for kind in ('received_input', 'published_world'):
        source = {'source_kind': kind, 'text': text}
        assert published_wording(source) == text
        assert presentation_text(source, 'JSON') == 'JSON'
