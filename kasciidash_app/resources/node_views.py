"""Node inventory and on-demand operational status (read-only)."""
import concurrent.futures
from collections import Counter

from kasciidash_app.resources import alarms
from kasciidash_app.resources import resource_views as rv
def roles(node):
    labels = rv.field(rv.field(node, 'metadata', {}), 'labels', {})
    values = [key.split('/', 1)[1] for key in labels if key.startswith('node-role.kubernetes.io/')]
    if labels.get('kubernetes.io/role'):
        values.append(labels['kubernetes.io/role'])
    return ','.join(sorted(set(values))) or '-'


def status(node):
    conditions = rv.field(rv.field(node, 'status', {}), 'conditions', [])
    ready = next((c.get('status') for c in conditions if c.get('type') == 'Ready'), 'Unknown')
    result = 'Ready' if ready == 'True' else 'NotReady' if ready == 'False' else 'Unknown'
    if rv.field(node, 'spec', {}).get('unschedulable'):
        result += ' / Cordoned'
    pressure = [c.get('type', '?') for c in conditions if c.get('status') == 'True' and c.get('type', '').endswith('Pressure')]
    return ' / '.join([result] + pressure)


def matches(data, filters):
    objects = rv.filter_items(data.get('nodes'), filters or {}, status)
    return None if objects is None else sorted(objects, key=lambda n: rv.field(n, 'metadata', {}).get('name', ''))


def header(data, width, filters, clean):
    import textwrap
    nodes = data.get('nodes')
    rows = matches(data, filters)
    ready = 'N/A' if nodes is None else str(sum(status(n).split(' / ')[0] == 'Ready' for n in nodes))
    cordoned = 'N/A' if nodes is None else str(sum(bool(rv.field(n, 'spec', {}).get('unschedulable')) for n in nodes))
    lines = ['NODES | Total: %s | Ready: %s | Cordoned: %s' % ('N/A' if nodes is None else len(nodes), ready, cordoned),
             'Keys: Up/Down: select | Enter: Status/YAML/Describe | Esc: menu',
             'Search keys: /: filter name=... status=... | x: clear | r: refresh',
             'Filter: ' + (' '.join('%s=%s' % pair for pair in (filters or {}).items()) or '(none)') +
             ' | Matches: ' + ('N/A' if rows is None else str(len(rows))), '-' * width]
    return [part for line in lines for part in textwrap.wrap(clean(line), width) or ['']]


def list_lines(data, width, filters, table):
    nodes = matches(data, filters)
    if nodes is None:
        return ['Nodes unavailable. See Event / Errors for the collection error.']
    if not nodes:
        return ['No matching nodes.' if filters else 'No nodes.']
    headers = ['NAME', 'ROLES', 'STATUS', 'INTERNAL-IP', 'KUBELET', 'CPU', 'MEMORY', 'PODS', 'ALARMS']
    rows = []
    alarm_index = alarms.index(data)
    for node in nodes:
        meta, state = rv.field(node, 'metadata', {}), rv.field(node, 'status', {})
        capacity, info = rv.field(state, 'capacity', {}), rv.field(state, 'nodeInfo', {})
        ip = ','.join(a.get('address', '-') for a in rv.field(state, 'addresses', []) if a.get('type') == 'InternalIP') or '-'
        count = 'N/A' if data.get('pods') is None else str(sum(rv.field(p, 'spec', {}).get('nodeName') == meta.get('name') for p in data['pods']))
        rows.append([meta.get('name', '-'), roles(node), status(node), ip, info.get('kubeletVersion', '-'),
                     capacity.get('cpu', '-'), capacity.get('memory', '-'), count, alarm_index.label(dict(node,kind='Node'))])
    selected = (data.get('resource_state') or {}).get('index')
    if width >= 100:
        return ['CPU / MEMORY: capacity | PODS: currently retained objects assigned to node',
                *table(headers, rows, width, selected=selected, stretch=True, max_widths={'NAME': 36, 'ROLES': 20, 'STATUS': 30})]
    return [line for i, row in enumerate(rows) for line in table(
        ['> FIELD' if i == selected else 'FIELD', 'VALUE'], list(map(list, zip(headers, row))), width)]


def status_text(node, metrics, pods, disk, errors):
    meta, spec, state = rv.field(node, 'metadata', {}), rv.field(node, 'spec', {}), rv.field(node, 'status', {})
    name = meta.get('name', '-')
    lines = ['NODE STATUS', 'Name: ' + name, 'State: ' + status(node), 'Roles: ' + roles(node),
             'Scheduling: ' + ('Disabled (cordoned)' if spec.get('unschedulable') else 'Enabled'),
             'Created: ' + str(meta.get('creationTimestamp') or '-'), 'UID: ' + str(meta.get('uid') or '-'),
             'Provider ID: ' + str(spec.get('providerID') or '-'),
             'Pod CIDRs: ' + ', '.join(spec.get('podCIDRs') or [spec.get('podCIDR') or '-']), '', 'ADDRESSES']
    lines += ['%s: %s' % (a.get('type', '-'), a.get('address', '-')) for a in rv.field(state, 'addresses', [])] or ['No addresses reported.']
    lines += ['', 'CONDITIONS']
    conditions = rv.field(state, 'conditions', [])
    if not conditions:
        lines.append('No conditions reported (status unknown).')
    for condition in conditions:
        lines += ['%s: %s | Reason: %s' % (condition.get('type', '-'), condition.get('status', '-'), condition.get('reason') or '-'),
                  '  Last heartbeat: ' + str(condition.get('lastHeartbeatTime') or '-'),
                  '  Last transition: ' + str(condition.get('lastTransitionTime') or '-'),
                  '  Message: ' + str(condition.get('message') or '-')]
    lines += ['', 'CAPACITY / ALLOCATABLE (Kubernetes quantity units)']
    capacity, allocatable = rv.field(state, 'capacity', {}), rv.field(state, 'allocatable', {})
    for key in sorted(set(capacity) | set(allocatable)):
        lines.append('%s: total=%s | allocatable=%s' % (key, capacity.get(key, 'N/A'), allocatable.get(key, 'N/A')))
    usage = rv.field(metrics, 'usage', {})
    lines += ['', 'CURRENT USAGE', 'CPU: %s | Memory: %s (metrics.k8s.io)' % (usage.get('cpu', 'N/A'), usage.get('memory', 'N/A')),
              'Metrics timestamp: %s | Window: %s' % (rv.field(metrics, 'timestamp', 'N/A'), rv.field(metrics, 'window', 'N/A'))]
    fs = rv.field(rv.field(disk, 'node', {}), 'fs', {})
    lines += ['Root filesystem: used=%s | capacity=%s | available=%s bytes' % tuple(fs.get(k, 'N/A') for k in ('usedBytes', 'capacityBytes', 'availableBytes')),
              'Filesystem timestamp: ' + str(fs.get('time') or 'N/A'), 'Root filesystem only; PVC volumes excluded.', '', 'NODE SYSTEM INFO']
    lines += ['%s: %s' % (key, value) for key, value in sorted(rv.field(state, 'nodeInfo', {}).items())] or ['No system information reported.']
    lines += ['', 'TAINTS']
    lines += ['%s=%s:%s | Added: %s' % (t.get('key', ''), t.get('value', ''), t.get('effect', ''), t.get('timeAdded') or '-') for t in rv.field(spec, 'taints', [])] or ['No taints.']
    lines += ['', 'LABELS']
    lines += ['%s=%s' % pair for pair in sorted(rv.field(meta, 'labels', {}).items())] or ['No labels.']
    lines += ['', 'ASSIGNED PODS (retained objects, including completed Pods)']
    if pods is None:
        lines.append('N/A: Pod list unavailable.')
    else:
        counts = Counter(rv.field(p, 'status', {}).get('phase') or 'Unknown' for p in pods)
        lines.append('Total: %s | %s' % (len(pods), ' | '.join('%s: %s' % pair for pair in sorted(counts.items()))))
        for pod in sorted(pods, key=lambda p: (rv.field(p, 'metadata', {}).get('namespace', ''), rv.field(p, 'metadata', {}).get('name', ''))):
            pm, ps = rv.field(pod, 'metadata', {}), rv.field(pod, 'status', {})
            statuses = rv.field(ps, 'containerStatuses', [])
            restarts = sum(rv.field(c, 'restartCount', 0) for c in statuses + rv.field(ps, 'initContainerStatuses', []))
            lines.append('%s/%s | %s | ready=%s/%s | restarts=%s' % (pm.get('namespace', '-'), pm.get('name', '-'), ps.get('phase', 'Unknown'), sum(bool(c.get('ready')) for c in statuses), len(rv.field(rv.field(pod, 'spec', {}), 'containers', [])), restarts))
    if errors:
        lines += ['', 'PARTIAL DATA / QUERY ERRORS'] + errors
    return '\n'.join(lines)


def load(client, item):
    node, error = client.call('get', 'nodes', item['name'], '-o', 'json')
    if error:
        return None, error
    if not node or rv.field(node, 'metadata', {}).get('name') != item['name']:
        return None, 'Returned node does not match the selected row.'
    jobs = {'metrics': ('get', '--raw', '/apis/metrics.k8s.io/v1beta1/nodes/' + item['name']),
            'pods': ('get', 'pods', '-A', '--field-selector', 'spec.nodeName=' + item['name'], '-o', 'json'),
            'disk': ('get', '--raw', '/api/v1/nodes/' + item['name'] + '/proxy/stats/summary')}
    values, errors = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        pending = {key: pool.submit(client.call, *args) for key, args in jobs.items()}
        for key, future in pending.items():
            try:
                value, error = future.result()
            except Exception as exc:
                value, error = None, str(exc)
            values[key] = None if error else value
            if error:
                errors.append(key + ': ' + error)
    pods = values['pods']
    return status_text(node, values['metrics'], None if pods is None else rv.field(pods, 'items', []), values['disk'], errors), None
