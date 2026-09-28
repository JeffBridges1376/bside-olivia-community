"""Keep source corrections together, without turning an interpretation into a fact."""
import json
import sqlite3

from .companion_memory_context import _ConversationMemoryView
from .memory_prompt import _original_evidence_item


def source(record):
    provenance = record.get('provenance')
    value = provenance.get('source_record_id') if isinstance(provenance, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def recent_records(messages):
    records = []
    # The final input cannot impersonate one of our stored conversation headers.
    current = next((i for i in range(len(messages)-1, -1, -1)
                    if messages[i].get('role') == 'user'), None)
    for index, message in enumerate(messages):
        if index == current or message.get('role') not in {'user', 'assistant'}:
            continue
        header, separator, text = message.get('content', '').partition(']\n')
        if not separator or not header.startswith('[历史消息 '):
            continue
        try:
            meta = json.loads(header[len('[历史消息 '):])
            if meta.get('truncated') or not text:
                continue
            pending = meta.get('delivery_state') == 'user_received_reply_unconfirmed'
            records.append({'citation': meta['event_id'],
                'provenance': {} if pending else {'source_record_id': meta['source']},
                **({'receipt_source': meta['source']} if pending else {}),
                'speaker': 'user' if message['role'] == 'user' else 'linli',
                'occurred_at': meta.get('time'), 'text': text,
                'evidence_scope': 'current_input' if pending else 'recorded_utterance'})
        except (ValueError, KeyError, TypeError):
            continue
    return records


def bind_received(record, index, user, *, sources=(), as_of=None):
    """Bind only text already present in this owner's durable received originals."""
    if index is None or record.get('speaker') != 'user' or record.get('evidence_scope') != 'current_input':
        return record
    hints = sources or ((record['receipt_source'],) if record.get('receipt_source') else ())
    if not hints:
        return record
    try:
        originals = index.get_received_sources(user, hints, before=as_of)
        matches = [_original_evidence_item(_ConversationMemoryView._convert(r))
                   for r in originals if r.text in record.get('text', '')]
        return {**record, '_received_records': matches}
    except (OSError, ValueError, sqlite3.Error):
        return record


def expand_group(group, index, user, *, excluded, as_of):
    """A dependency read failure omits optional recall, never restores a lone claim."""
    seeds = tuple(dict.fromkeys(source(r) for item in group
                  for r in [item, *item.get('_received_records', ())] if source(r)))
    if not seeds or index is None:
        return group, False
    try:
        result = index.dependencies(user, seeds, exclude_source_ids=excluded, before=as_of)
        if result.blocked_source_ids:
            return [], True
        if not result.relations:
            return group, False
        complete = [_original_evidence_item(_ConversationMemoryView._convert(r)) for r in result.records]
        # These are possible dependencies between utterances, not verified facts.
        for record in complete:
            record['interpretation_dependencies'] = [r for r in result.relations
                if source(record) in (r['earlier_source'], r['later_source'])]
        return complete, False
    except (OSError, ValueError, sqlite3.Error):
        return [], True


def validate_dependencies(value, records):
    """Only exact quotations from this request may identify an association."""
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError('INVALID_HISTORY_DEPENDENCIES')
    refs = {r['citation']: r for r in records if isinstance(r.get('citation'), str)}
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
                'earlier', 'later', 'kind', 'earlier_quote', 'later_quote'}:
            raise ValueError('INVALID_HISTORY_DEPENDENCIES')
        if item['kind'] not in {'correction', 'state_change', 'challenge'}:
            raise ValueError('INVALID_HISTORY_DEPENDENCIES')
        pair = []
        for key in ('earlier', 'later'):
            ref, quote = item[key], item[key + '_quote']
            record = refs.get(ref) if isinstance(ref, str) else None
            if (record is None or record.get('speaker') not in {'user', 'linli'}
                    or record.get('evidence_scope') not in {'recorded_utterance', 'current_input'}
                    or not isinstance(quote, str) or not 2 <= len(quote.strip()) <= 500
                    or quote not in record.get('text', '')):
                raise ValueError('INVALID_HISTORY_DEPENDENCIES')
            pair.append(record)
        if item['earlier'] == item['later']:
            raise ValueError('INVALID_HISTORY_DEPENDENCIES')
        result.append((item, *pair))
    return result


def save_dependencies(dependencies, index, user):
    if index is None:
        return
    def quoted_source(record, quote):
        if source(record):
            return source(record)
        matches = {source(r) for r in record.get('_received_records', ()) if quote in r['text']}
        return next(iter(matches)) if len(matches) == 1 else None
    for item, earlier, later in dependencies:
        first = quoted_source(earlier, item['earlier_quote'])
        second = quoted_source(later, item['later_quote'])
        if not first or not second:
            continue  # Do not fabricate an original when receipt indexing is unavailable.
        try:
            index.save_dependency(user, first, second,
                earlier['speaker'], later['speaker'], item['earlier_quote'],
                item['later_quote'], item['kind'])
        except (OSError, ValueError, sqlite3.Error):
            pass  # The complete pair still accompanies this request.


def close_group(group, offered, dependencies):
    """Transitive closure within the already offered, bounded source set."""
    combined = list(group)
    ids = {r.get('citation') for r in combined}
    changed = True
    while changed:
        changed = False
        for item, earlier, later in dependencies:
            earlier_sources = {source(r) for r in [earlier, *earlier.get('_received_records', ())] if source(r)}
            present = item['earlier'] in ids or any(
                source(r) in earlier_sources and r.get('speaker') == earlier.get('speaker')
                and item['earlier_quote'] in r.get('text', '') for r in combined)
            if not present or item['later'] in ids:
                continue
            # Recent/current originals already remain in the base dialogue.
            addition = next((g for g in offered.values()
                             if any(r.get('citation') == item['later'] for r in g)), [])
            for record in addition:
                if record.get('citation') not in ids:
                    combined.append(record)
                    ids.add(record.get('citation'))
            ids.add(later['citation'])
            changed = True
    return combined
