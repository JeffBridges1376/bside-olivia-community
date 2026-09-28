"""A silent decision is a terminal outcome, never a failed or invented reply."""
from copy import deepcopy

import pytest

from original_client_letter_contract import serialize_letter_detail, serialize_letter_summary


@pytest.mark.parametrize('timing', ['wait_user', 'defer', 'no_reply'])
@pytest.mark.parametrize('mode', ['text_letter', 'voice_reply', 'musical_video'])
def test_silent_letter_has_own_terminal_status_and_no_previous_draft(timing, mode):
    letter = dict(letter_id='synthetic-silent', content='用户本轮原文', letter_status='SKIPPED',
        companion_timing=timing, reply_mode=mode, reply_not_before=200,
        reply_text='旧稿不得当作本轮回复', reply_signature='林离', reply_sticker_id='linli-01',
        image_reply_settings={'enabled': True}, image_status='COMPLETED',
        reply_image_url='http://127.0.0.1:8899/toy/media/stale.png',
        media_status='PROCESSING', reply_video_enabled=True,
        reply_audio_url='http://127.0.0.1:8899/toy/media/stale.wav',
        reply_video_url='http://127.0.0.1:8899/toy/media/stale.mp4', replied_at=99)
    original = deepcopy(letter)
    for serialize in (serialize_letter_summary, serialize_letter_detail):
        result = serialize(letter, now=100, include_legacy_aliases=True)
        assert result['letterStatus'] == result['letter_status'] == 6
        assert result['replyDisposition'] == 'no_reply'
        assert result['replyType'] == result['reply_type'] == 0
        assert 'repliedAt' not in result and 'replied_at' not in result
        assert not any(key in result for key in ('videoPending', 'audioStatus', 'coverId',
            'replyImageUrl', 'replyAudioUrl', 'replySongUrl', 'replyStickerId', 'replySignature'))
        assert result.get('replyText', '') == result.get('reply_text', '') == result.get('reply_content', '') == ''
        assert result.get('replyVideoUrl', '') == result.get('reply_video_url', '') == ''
        if serialize is serialize_letter_detail:
            assert result['content'] == letter['content']
            assert result['media_status'] == 'NOT_REQUESTED' and result['media_retryable'] is False
    assert letter == original


def test_silent_status_is_preserved_as_wire_enum_but_unknown_remains_failed():
    from contracts.letter_status import original_letter_status, original_letter_status_or_failed
    assert original_letter_status('SKIPPED') == original_letter_status(6) == 6
    assert original_letter_status_or_failed('unknown') == 5
    result = serialize_letter_summary(dict(letter_id='restored', letterStatus=6, reply_not_before=200), now=100)
    assert result['letterStatus'] == 6 and result['replyDisposition'] == 'no_reply'
    assert serialize_letter_summary(dict(letter_id='unknown', letter_status='unknown'), now=100)['letterStatus'] == 5
