"""Selectable, single-line event tables and immutable detail snapshots."""
import copy
import json
import textwrap


def field(obj, key, default='-'):
    return obj.get(key) if isinstance(obj, dict) and obj.get(key) is not None else default


def records(data, view):
    if view == '5':
        result = []
        for source, errors in [('Kubernetes', data.get('errors') or []),
                               ('API discovery', data.get('catalog_errors') or [])]:
            for message in errors:
                result.append(dict(id=(source, str(message)), source=source, message=str(message), stamp=data.get('stamp', '-')))
        for source, message in (data.get('resource_errors') or {}).items():
            result.append(dict(id=('resource', source, str(message)), source=source, message=str(message), stamp=data.get('stamp', '-')))
        occurrences = {}
        for row in result:
            identity = row['id']
            ordinal = occurrences.get(identity, 0)
            occurrences[identity] = ordinal + 1
            row['id'] = (identity, ordinal)
        return result
    result = []
    for obj in data.get('events') or []:
        if field(obj, 'type') != 'Warning':
            continue
        meta, target, series = field(obj, 'metadata', {}), field(obj, 'involvedObject', field(obj, 'regarding', {})), field(obj, 'series', {})
        stamp = field(series, 'lastObservedTime', None) or field(obj, 'lastTimestamp', None) or field(obj, 'eventTime', None) or field(meta, 'creationTimestamp')
        ns, name = field(target, 'namespace', field(meta, 'namespace')), field(target, 'name')
        reason, message = field(obj, 'reason'), field(obj, 'message', field(obj, 'note', ''))
        uid = field(meta, 'uid', None) or (field(meta, 'namespace'), field(meta, 'name', None) or (ns, name, reason, message))
        result.append(dict(id=uid, stamp=stamp, namespace=ns, target=field(target, 'kind', '') + '/' + name,
                           reason=reason, count=field(series, 'count', field(obj, 'count', 1)), message=message, raw=obj))
    return sorted(result, key=lambda r: str(r['stamp']), reverse=True)


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
        columns = [('SEL', 3, lambda r: ''), ('SOURCE', 26, lambda r: r['source']), ('MESSAGE', 80, lambda r: r['message'])]
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
        body = [message]
    return header, body


def header(data, view, width, clean):
    state = data.get('event_state') or {}
    if state.get('detail'):
        lines = ['EVENT DETAIL' if view == '4' else 'COLLECTION ERROR DETAIL',
                 'Esc: back to list | Up/Down: scroll | u/d: page',
                 'Snapshot captured when opened; r refreshes the underlying list.', '-' * width]
    else:
        rows = records(data, view)
        total = 'N/A' if view == '4' and data.get('events') is None else str(len(rows))
        selected = min(state.get('index', 0)+1, len(rows))
        lines = [('WARNINGS' if view == '4' else 'COLLECTION ERRORS') + ' | Total: ' + total + ' | Selected: ' + str(selected),
                 'Up/Down: select | Enter: details | u/d: page | Esc: menu']
    wrapped = [part for line in lines for part in textwrap.wrap(clean(line), width) or ['']]
    return wrapped if state.get('detail') else wrapped + table(data, view, width, clean)[0]


def content(data, view, width, clean):
    row = (data.get('event_state') or {}).get('detail')
    if row is None:
        return table(data, view, width, clean)[1]
    if view == '5':
        fields = [('Source', row['source']), ('Snapshot', row['stamp'])]
    else:
        fields = [('Last seen', row['stamp']), ('Namespace', row['namespace']), ('Object', row['target']),
                  ('Reason', row['reason']), ('Count', row['count'])]
    lines = [str(k) + ': ' + str(v) for k, v in fields] + ['', 'MESSAGE', str(row['message'])]
    if view == '4':
        lines += ['', 'FULL EVENT JSON', json.dumps(row['raw'], indent=2, ensure_ascii=True)]
    # Detail text wraps without truncation, including long tokens and multi-line messages.
    return [chunk for line in lines for part in line.splitlines() or ['']
            for chunk in textwrap.wrap(clean(part), width, replace_whitespace=False) or ['']]
