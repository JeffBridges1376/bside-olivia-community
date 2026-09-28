import json

from runtime.image_understanding import incoming_context, IMAGE_BOUNDARY


def test_text_followup_does_not_inherit_previous_image_as_new_upload():
    row = {'letter_id':'synthetic-followup', 'content':'扯平了，换个话题', 'incoming_images':[]}
    packet = json.loads(incoming_context(row).split('\n', 2)[2])
    assert packet['current_turn_has_images'] is False
    assert 'images' not in packet


def test_unrecognized_new_image_is_not_replaced_by_old_observation():
    row = {'letter_id':'synthetic-image', 'incoming_images':[['new','https://example.invalid/image']],
           'incoming_image_failed':1}
    packet = json.loads(incoming_context(row).split('\n', 2)[2])
    assert packet['current_turn_has_images'] is True
    assert packet['images'] == []
    assert packet['unrecognized_count'] == 1
    assert packet['meaning'] == IMAGE_BOUNDARY
