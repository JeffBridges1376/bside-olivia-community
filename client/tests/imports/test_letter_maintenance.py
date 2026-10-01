from copy import deepcopy

import pytest

from runtime.imports.letter_maintenance import preview, apply_selection, project


def letter(id, content='去信', reply='回信', **extra):
    return dict(letter_id=id, content=content, reply_text=reply,
                letter_status='COMPLETED', created_at=None, **extra)


def soul(rows):
    return {'format': 'soul', 'manifest': {'memory': {'exchanges': rows}}}


def test_exact_duplicates_reversible_and_do_not_mutate_originals():
    rows = [letter('a'), letter('b'), letter('c', '另一封')]
    original = deepcopy(rows)
    plan = preview(rows, {})
    hide = next(x for x in plan['items'] if x['kind'] == 'duplicate')
    edits = apply_selection(plan, [hide['options'][0]['id']], {})
    assert len(project(rows, edits)) == 2
    assert rows == original
    restore = next(x for x in preview(rows, edits)['items'] if x['kind'] == 'restore')
    assert project(rows, apply_selection(preview(rows, edits), [restore['options'][0]['id']], edits)) == rows


def test_time_and_order_repairs_match_full_pair_and_preserve_export_text():
    rows = [letter('a', '第一封'), letter('b', '第二封')]
    backup = soul([dict(incoming=c, reply='回信', date='2026-09-30', time='12:34')
                   for c in ('第二封', '第一封')])
    plan = preview(rows, {}, backup)
    fixes = [x['options'][0]['id'] for x in plan['items'] if x['kind'] == 'time']
    assert len(fixes) == 2
    updated = project(rows, apply_selection(plan, fixes, {}))
    assert updated[0]['created_at'] == 1790742840
    assert updated[1]['_maintenance_order'] < updated[0]['_maintenance_order']
    assert [x['content'] for x in updated] == ['第一封', '第二封']
    assert all(x['created_at'] is None for x in rows)


def test_ambiguous_dates_unknown_dates_and_active_letters_are_not_repaired():
    rows = [letter('a'), dict(letter('busy'), letter_status='PROCESSING')]
    backup = soul([dict(incoming='去信', reply='回信', date=d, time='12:34')
                   for d in ('2026-09-30', '2026-09-29')])
    plan = preview(rows, {}, backup)
    assert not any(x['kind'] in {'time', 'duplicate', 'failed'} for x in plan['items'])
    assert plan['ambiguous'] == 1


def test_cleanup_only_terminal_failures_and_explicit_import_markers():
    rows = [dict(letter('failed'), letter_status='FAILED'),
            dict(letter('busy'), letter_status='SENDING'),
            letter('native', '原生'), letter('imported', '导入', imported_by='old-tool')]
    plan = preview(rows, {})
    kinds = [x['kind'] for x in plan['items']]
    assert kinds.count('failed') == 1
    assert kinds.count('imported') == 1
    assert not any(x['left']['letter_id'] in {'busy', 'native'} for x in plan['items'])


def test_near_duplicates_are_choices_not_automatic_deletion():
    text = '今天沿着河岸散步，看见一只橙色的小猫，坐在旧书店旁边。' * 3
    rows = [letter('a', text), letter('b', text.replace('橙色', '白色'))]
    plan = preview(rows, {})
    near = next(x for x in plan['items'] if x['kind'] == 'near')
    assert len(near['options']) == 2
    assert apply_selection(plan, [], {}) == {}
    with pytest.raises(ValueError):
        apply_selection(plan, [o['id'] for o in near['options']], {})
    assert len(project(rows, apply_selection(plan, [near['options'][1]['id']], {}))) == 1


def test_invalid_selection_and_source_fail_without_changes():
    with pytest.raises(ValueError):
        apply_selection(preview([letter('a')], {}), ['forged'], {})
    with pytest.raises(ValueError):
        preview([letter('a')], {}, soul([{'incoming': []}]))


def test_json_pairs_compare_without_inventing_dates_and_pending_media_stay_visible():
    rows = [letter('old'), letter('video', media_status='PENDING'),
            letter('photo', image_status='PROCESSING')]
    plan = preview(rows, {}, '[{"content":"去信","reply":"回信"}]')
    assert [item['kind'] for item in plan['items']] == ['same']
    assert plan['source_count'] == 1
