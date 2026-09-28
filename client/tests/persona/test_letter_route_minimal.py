import asyncio
import json
import pytest
from runtime.reply.letter_route_preview import classify
from letter_triage import explicitly_requested_route


class Questions:
    def __init__(self, **answers):
        self.answers = dict.fromkeys(('image', 'speech', 'song', 'video'), 'no') | answers
        self.calls = []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        return self.answers


def test_preview_only_sends_current_original_and_one_small_question_batch():
    port = Questions(speech='yes')
    text = '忙完记得吃饭，别饿着自己，这次想听你发语音'
    result = asyncio.run(classify(port, text))
    assert explicitly_requested_route(result) == 'voice_reply'
    assert 'explicit_video_output_request' not in result.music_contexts
    assert len(port.calls) == 1
    state, questions, purpose = port.calls[0]
    assert state == {'text': text}
    assert set(questions) == {'image', 'speech', 'song', 'video'}
    assert purpose == 'letter-media-request'
    assert len(json.dumps([state, questions], ensure_ascii=False).encode()) < 3500


@pytest.mark.parametrize('answers,mode,video,image', [
    ({}, None, False, False),
    ({'image':'yes'}, None, False, True),
    ({'speech':'yes'}, 'voice_reply', False, False),
    ({'video':'yes'}, 'voice_reply', True, False),
    ({'song':'yes'}, 'singing_video', False, False),
    ({'song':'yes','video':'yes'}, 'singing_video', True, False),
    ({'speech':'yes','song':'yes'}, 'voice_song_video', False, False),
])
def test_requested_media_keep_video_and_audio_permissions_separate(answers,mode,video,image):
    result = asyncio.run(classify(Questions(**answers), 'synthetic original'))
    assert explicitly_requested_route(result) == mode
    assert ('explicit_video_output_request' in result.music_contexts) == video
    assert (result.reason_code == 'jev_image_request') == image


@pytest.mark.parametrize('answers', [{}, {'image':'yes'}, dict.fromkeys(('image','speech','song','video'), 'maybe')])
def test_invalid_answers_never_silently_choose_text(answers):
    port = Questions()
    port.answers = answers
    with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
        asyncio.run(classify(port, 'original'))
