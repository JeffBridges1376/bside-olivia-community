import asyncio
from types import SimpleNamespace

from runtime.reply.letter_route_preview import project_route
from letter_triage import explicitly_requested_route


def decision(kind='text', requirements=()):
    return SimpleNamespace(plan=dict(
        understanding=dict(requirements=list(requirements)),
        proposal=dict(timing='now', steps=[dict(parts=[dict(kind=kind)])]),
        resolution=dict(status='ready')))


def requirement(*alternatives):
    return dict(alternatives=[dict(kinds=list(kinds)) for kinds in alternatives])


def test_photo_does_not_request_speech_or_video_permission():
    value = project_route(decision('image'))
    assert value.reason_code == 'jev_image_request'
    assert value.reply_mode == 'text_letter'
    assert explicitly_requested_route(value) is None


def test_explicit_video_keeps_permission_boundary():
    value = project_route(decision('video_speech', [requirement(['video_speech'])]))
    assert explicitly_requested_route(value) == 'voice_reply'
    assert 'explicit_video_output_request' in value.music_contexts


def test_optional_video_and_image_or_video_do_not_override_settings():
    for reqs in ([], [requirement(['image'], ['video_speech'])]):
        value = project_route(decision('video_speech', reqs))
        assert explicitly_requested_route(value) is None


def test_chat_and_voice_remain_distinct():
    assert project_route(decision()).reply_mode == 'text_letter'
    value = project_route(decision('audio_speech', [requirement(['audio_speech'])]))
    assert explicitly_requested_route(value) == 'voice_reply'
    assert 'explicit_video_output_request' not in value.music_contexts
