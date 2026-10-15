"""Selectable, single-line event tables and immutable detail snapshots."""
import copy
import json
import shlex
import textwrap


def error_record(source, value, stamp):
    message = str(value.get('message', value)) if isinstance(value, dict) else str(value)
    ref = (value.get('reference') or value.get('target')) if isinstance(value, dict) else None
    result = dict(id=(source, message, json.dumps(ref, sort_keys=True, default=str)), source=source, message=message, stamp=stamp)
    if isinstance(ref, dict):
        result['reference'] = copy.deepcopy(ref)
    reference = ref if isinstance(ref, dict) else {}
    result['namespace'] = reference.get('namespace') or (value.get('namespace') if isinstance(value, dict) else None) or '-'
    name = reference.get('name')
    result['target'] = str(reference.get('kind') or reference.get('resource') or '') + '/' + str(name) if name else '-'
    result['reason'] = (value.get('reason') if isinstance(value, dict) else None) or message
    return result


def field(obj, key, default='-'):
    return obj.get(key) if isinstance(obj, dict) and obj.get(key) is not None else default


def parse_filter(query):
    filters = {}
    for token in shlex.split(query):
        if '=' not in token:
            raise ValueError('Use namespace=..., object=..., reason=... (quote values with spaces).')
        key, value = token.split('=', 1)
        key = {'namespace': 'ns', 'obj': 'object'}.get(key, key)
        if key not in ('ns', 'object', 'reason'):
            raise ValueError('Allowed fields: ns/namespace, object/obj, reason')
        if value.strip():
            filters[key] = value.strip()
    return filters


def filter_rows(rows, filters):
    return [row for row in rows if all(value.casefold() in str(row.get({'ns': 'namespace', 'object': 'target'}.get(key, key), '')).casefold()
                                      for key, value in filters.items())]


def records(data, view, filters=None):
    filters = (data.get('event_filters') or {}).get(view, {}) if filters is None else filters
    if view == '5':
        result = []
        for source, errors in [('Kubernetes', data.get('errors') or []),
                               ('API discovery', data.get('catalog_errors') or [])]:
            for message in errors:
                result.append(error_record(source, message, data.get('stamp', '-')))
        for source, message in (data.get('resource_errors') or {}).items():
            result.append(error_record(source, message, data.get('stamp', '-')))
        occurrences = {}
        for row in result:
            identity = row['id']
            ordinal = occurrences.get(identity, 0)
            occurrences[identity] = ordinal + 1
            row['id'] = (identity, ordinal)
        return filter_rows(result, filters)
    result = []
    for obj in data.get('events') or []:
        if field(obj, 'type') != 'Warning':
            continue
        meta, series = field(obj, 'metadata', {}), field(obj, 'series', {})
        target = field(obj, 'involvedObject', None) or field(obj, 'regarding', {})
        stamp = field(series, 'lastObservedTime', None) or field(obj, 'lastTimestamp', None) or field(obj, 'eventTime', None) or field(meta, 'creationTimestamp')
        ns, name = field(target, 'namespace', field(meta, 'namespace')), field(target, 'name')
        reason, message = field(obj, 'reason'), field(obj, 'message', field(obj, 'note', ''))
        uid = field(meta, 'uid', None) or (field(meta, 'namespace'), field(meta, 'name', None) or (ns, name, reason, message))
        result.append(dict(id=uid, stamp=stamp, namespace=ns, target=field(target, 'kind', '') + '/' + name,
                           reason=reason, count=field(series, 'count', field(obj, 'count', 1)), message=message, raw=obj))
    return filter_rows(sorted(result, key=lambda r: str(r['stamp']), reverse=True), filters)


class Browser:
    def __init__(self):
        self.indices = {'4': 0, '5': 0}
        self.ids = {'4': None, '5': None}
        self.detail = None
        self.saved_offset = 0

    def reconcile(self, view, rows):
        index = next((i for i, r in enumerate(rows) if r['id'] == self.ids[view]), self.indices[view])
        self.indices[view] = min(index, max(0, len(rows)-1))
        self.ids[view] = rows[self.indices[view]]['id'] if rows else None

    def move(self, view, rows, step):
        self.indices[view] = max(0, min(len(rows)-1, self.indices[view] + step))
        self.ids[view] = rows[self.indices[view]]['id'] if rows else None

    def open(self, view, rows, offset):
        if rows:
            self.detail = (view, copy.deepcopy(rows[self.indices[view]]))
            self.saved_offset = offset

    def state(self, view):
        return {'index': self.indices.get(view, 0),
                'detail': self.detail[1] if self.detail and self.detail[0] == view else None}


def table(data, view, width, clean):
    rows = records(data, view)
    index = (data.get('event_state') or {}).get('index', 0)
    if view == '4':
        columns = [('SEL', 3, lambda r: ''), ('LAST SEEN', 19, lambda r: str(r['stamp']).replace('T', ' ').removesuffix('Z')),
                   ('NAMESPACE', 16, lambda r: r['namespace']), ('OBJECT', 28, lambda r: r['target']),
                   ('REASON', 22, lambda r: r['reason']), ('COUNT', 6, lambda r: r['count']), ('MESSAGE', 60, lambda r: r['message'])]
        if width < 110: columns = [columns[i] for i in (0, 2, 4, 6)]
        if width < 65: columns = [columns[i] for i in (0, 2, 3)]
    else:
        columns = [('SEL', 3, lambda r: ''), ('SOURCE', 20, lambda r: r['source']),
                   ('NAMESPACE', 16, lambda r: r['namespace']), ('OBJECT', 28, lambda r: r['target']),
                   ('REASON', 24, lambda r: r['reason']), ('MESSAGE', 60, lambda r: r['message'])]
        if width < 110: columns = [columns[i] for i in (0, 1, 4, 5)]
        if width < 65: columns = [columns[i] for i in (0, 1, 3)]
    widths = [c[1] for c in columns]
    budget = width - 3 * len(columns) - 1
    while sum(widths) > budget:
        candidates = [i for i in range(1, len(widths)) if columns[i][0] not in ('LAST SEEN', 'COUNT') and widths[i] > 3]
        i = max(candidates, key=widths.__getitem__)
        widths[i] -= 1
    # Expand the message column into spare space; every row is exactly one line.
    widths[-1] += max(0, budget-sum(widths))
    def cell(value, size):
        value = ' '.join(clean(value).split())
        return (value[:size-3] + '...' if len(value) > size else value).ljust(size)
    def line(values):
        return '| ' + ' | '.join(cell(v, w) for v, w in zip(values, widths)) + ' |'
    border = '+' + '+'.join('-' * (w+2) for w in widths) + '+'
    header = [border, line([c[0] for c in columns]), border]
    body = []
    for i, row in enumerate(rows):
        body += [line(['>' if i == index else ''] + [c[2](row) for c in columns[1:]]), border]
    if not rows:
        message = ('Warning events unavailable (see Errors).' if data.get('events') is None else 'No retained Warning events.') if view == '4' else 'No collection errors.'
        body = ['No matching events/errors. Press x to clear filters.' if records(data, view, {}) else message]
    return header, body


def header(data, view, width, clean):
    state = data.get('event_state') or {}
    if state.get('detail'):
        lines = ['EVENT DETAIL' if view == '4' else 'COLLECTION ERROR DETAIL',
                 'Keys: Esc: back to list | Up/Down: scroll | u/d: page',
                 'Snapshot: captured when opened; r refreshes the underlying list.', '-' * width]
    else:
        rows = records(data, view)
        total = 'N/A' if view == '4' and data.get('events') is None else str(len(records(data, view, {})))
        selected = min(state.get('index', 0)+1, len(rows))
        lines = [('WARNINGS' if view == '4' else 'COLLECTION ERRORS') + ' | Total: ' + total + ' | Matches: ' + str(len(rows)) + ' | Selected: ' + str(selected),
                 'Keys: Up/Down: select | Enter: details | /: filter | x: clear | u/d: page | Esc: menu',
                 'Filter fields: namespace=... object=... reason=... (AND)',
                 'Filter: ' + (' '.join('%s=%s' % pair for pair in ((data.get('event_filters') or {}).get(view) or {}).items()) or '(none)')]
    wrapped = [part for line in lines for part in textwrap.wrap(clean(line), width) or ['']]
    return wrapped if state.get('detail') else wrapped + table(data, view, width, clean)[0]


def content(data, view, width, clean):
    row = (data.get('event_state') or {}).get('detail')
    if row is None:
        return table(data, view, width, clean)[1]
    if view == '5':
        fields = [('Source', row['source']), ('Snapshot', row['stamp']), ('Namespace', row['namespace']),
                  ('Object', row['target']), ('Reason', row['reason'])]
    else:
        fields = [('Last seen', row['stamp']), ('Namespace', row['namespace']), ('Object', row['target']),
                  ('Reason', row['reason']), ('Count', row['count'])]
    lines = [str(k) + ': ' + str(v) for k, v in fields] + ['', 'MESSAGE', str(row['message'])]
    if view == '4':
        lines += ['', 'FULL EVENT JSON', json.dumps(row['raw'], indent=2, ensure_ascii=True)]
    # Detail text wraps without truncation, including long tokens and multi-line messages.
    return [chunk for line in lines for part in line.splitlines() or ['']
            for chunk in textwrap.wrap(clean(part), width, replace_whitespace=False) or ['']]
