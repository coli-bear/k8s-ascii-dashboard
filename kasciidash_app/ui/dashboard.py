#!/usr/bin/env python3
"""An ASCII terminal console for Kubernetes operations. Python standard library only."""
import argparse
import concurrent.futures
import curses
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import textwrap
from kasciidash_app.resources import alarms
from kasciidash_app.ui.list_cache import RowCache, CursorContent
from kasciidash_app.resources import resource_views as rv
from kasciidash_app.config import profiles
from kasciidash_app.monitoring import prometheus as monitoring
from kasciidash_app.resources import event_views
from kasciidash_app.resources import resource_detail
from kasciidash_app.resources import resource_actions
from kasciidash_app.resources import node_views
from kasciidash_app.resources import namespace_views
BASE = Path(__file__).resolve().parents[2]
RESOURCES = ('cpu', 'memory', 'ephemeral-storage')
WORKLOAD_KINDS = ('pods', 'deployments', 'replicasets', 'statefulsets')
KINDS = ('nodes', 'pods', 'deployments', 'replicasets', 'statefulsets', 'events')


def quantity(value):
    if value is None:
        return None
    match = re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)', str(value))
    if not match:
        raise ValueError('Invalid resource quantity: ' + str(value))
    factors = {'': 1, 'n': 1e-9, 'u': 1e-6, 'm': 1e-3, 'k': 1e3, 'K': 1e3,
               'M': 1e6, 'G': 1e9, 'T': 1e12, 'P': 1e15, 'E': 1e18,
               **{s + 'i': 1024 ** i for i, s in enumerate('KMGTPE', 1)}}
    return float(match[1]) * factors[match[2]]


def pod_requests(pod):
    spec = rv.field(pod, 'spec', {})
    def req(c, r):
        return quantity(rv.field(rv.field(rv.field(c, 'resources', {}), 'requests', {}), r, '0'))
    result = {}
    for r in RESOURCES:
        app = sum(req(c, r) for c in rv.field(spec, 'containers', []))
        sidecars, peak = 0, 0
        for c in rv.field(spec, 'initContainers', []):
            v = req(c, r)
            if c.get('restartPolicy') == 'Always':
                sidecars += v
                peak = max(peak, sidecars)
            else:
                peak = max(peak, sidecars + v)
        effective = max(app + sidecars, peak)
        # Pod-level CPU/memory requests override container aggregation when present.
        pod_level = rv.field(rv.field(spec, 'resources', {}), 'requests', {})
        if r in pod_level:
            effective = quantity(pod_level[r])
        result[r] = effective + quantity(rv.field(rv.field(spec, 'overhead', {}), r, '0'))
    return result


def pod_issue(p):
    status = rv.field(p, 'status', {})
    phase = rv.field(status, 'phase', 'Unknown')
    if rv.field(p, 'metadata', {}).get('deletionTimestamp'):
        return 'Terminating'
    if phase == 'Succeeded':
        return ''
    waiting = []
    for c in rv.field(status, 'initContainerStatuses', []) + rv.field(status, 'containerStatuses', []):
        state = rv.field(c, 'state', {})
        if state.get('waiting') is not None:
            waiting.append(rv.field(state['waiting'], 'reason', 'Waiting'))
        if rv.field(rv.field(state, 'terminated', {}), 'exitCode', 0):
            waiting.append(rv.field(state['terminated'], 'reason', 'ContainerFailed'))
    # A normal app waiting for initialization must not hide a failed init.
    reasons = [reason for reason in waiting if reason not in ('PodInitializing', 'ContainerCreating', 'Waiting')]
    if reasons or waiting:
        return (reasons or waiting)[0]
    if phase != 'Running':
        return status.get('reason') or phase
    if not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in rv.field(status, 'conditions', [])):
        return 'NotReady'
    return ''


def controller_issue(obj):
    spec, status = rv.field(obj, 'spec', {}), rv.field(obj, 'status', {})
    desired = rv.field(spec, 'replicas', 1)
    if rv.field(status, 'readyReplicas', 0) < desired:
        return 'Ready %s/%s' % (rv.field(status, 'readyReplicas', 0), desired)
    if rv.field(status, 'observedGeneration', 0) < rv.field(rv.field(obj, 'metadata', {}), 'generation', 0):
        return 'Reconciling'
    if obj.get('kind') == 'Deployment':
        if rv.field(status, 'updatedReplicas', 0) < desired or rv.field(status, 'availableReplicas', 0) < desired:
            return 'Rollout/Unavailable'
        if any(c.get('type') == 'Progressing' and c.get('status') == 'False' for c in rv.field(status, 'conditions', [])):
            return 'ProgressDeadlineExceeded'
    if obj.get('kind') == 'StatefulSet' and rv.field(rv.field(spec, 'updateStrategy', {}), 'type', 'RollingUpdate') == 'RollingUpdate':
        partition = rv.field(rv.field(rv.field(spec, 'updateStrategy', {}), 'rollingUpdate', {}), 'partition', 0)
        if rv.field(status, 'updatedReplicas', 0) < max(0, desired - partition):
            return 'RollingUpdate'
    return ''


class Client:
    def __init__(self, config, context, timeout):
        self.base = ['kubectl', '--kubeconfig', str(config), '--request-timeout=%ss' % timeout]
        if context:
            self.base += ['--context', context]
        self.timeout = timeout + 5

    def run(self, *args, input_text=None):
        return subprocess.run(self.base + list(args), capture_output=True, encoding='utf-8', timeout=self.timeout, input=input_text)

    def text(self, *args, input_text=None):
        try:
            proc = self.run(*args, input_text=input_text)
            if proc.returncode:
                return proc.stdout, proc.stderr.strip() or 'kubectl failed'
            return proc.stdout, None
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return None, str(exc)

    def call(self, *args, input_text=None):
        output, error = self.text(*args, input_text=input_text)
        if error:
            return None, error
        try:
            return json.loads(output), None
        except (ValueError, TypeError) as exc:
            return None, str(exc)


def collect(client, identity):
    data = dict(identity, errors=[], stamp=dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    calls = {k: ('get', k, '-A', '-o', 'json') if k != 'nodes' else ('get', k, '-o', 'json') for k in KINDS}
    calls['metrics'] = ('get', '--raw', '/apis/metrics.k8s.io/v1beta1/nodes')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {k: pool.submit(client.call, *args) for k, args in calls.items()}
        for k, f in futures.items():
            value, error = f.result()
            data[k] = rv.field(value, 'items', []) if value else None
            if error:
                data['errors'].append('%s: %s' % (k, error))
        nodes = data['nodes'] or []
        futures = {n['metadata']['name']: pool.submit(client.call, 'get', '--raw',
                   '/api/v1/nodes/%s/proxy/stats/summary' % n['metadata']['name']) for n in nodes}
        data['disk'] = {}
        for name, f in futures.items():
            value, error = f.result()
            data['disk'][name] = rv.field(rv.field(value or {}, 'node', {}), 'fs', {})
            if error:
                data['errors'].append('disk %s: %s' % (name, error))
    return data


def clean(value):
    # Keep all output ASCII, and strip terminal control characters from remote data.
    return ''.join(c if 32 <= ord(c) < 127 else '?' for c in str(value))


def amount(v, resource):
    if v is None:
        return 'N/A'
    if resource == 'cpu':
        return '%.2fc' % v
    return '%.2fGi' % (v / 1024 ** 3)


def node_state(n):
    conditions = rv.field(rv.field(n, 'status', {}), 'conditions', [])
    ready = next((c['status'] for c in conditions if c['type'] == 'Ready'), 'Unknown')
    state = 'Ready' if ready == 'True' else 'NotReady' if ready == 'False' else 'Unknown'
    pressure = [c['type'] for c in conditions if c['status'] == 'True' and c['type'].endswith('Pressure')]
    return state + (' / Cordoned' if rv.field(n, 'spec', {}).get('unschedulable') else '') + ''.join(' / ' + p for p in pressure)


def ascii_table(headers, rows, width, max_widths=None, stretch=False, selected=None):
    """Format ruled ASCII cells, wrapping within cells rather than breaking borders."""
    if selected is not None:
        headers = ['SEL'] + headers
        rows = [['>' if i == selected else ''] + row for i, row in enumerate(rows)]
    cells = [[clean(value) for value in row] for row in [headers] + rows]
    minimums = {'RESOURCE': 11, 'TOTAL': 9, 'ALLOCATABLE': 11, 'REQUEST': 9, 'USED': 9, 'USED/TOTAL': 10}
    widths = [max(rv.field(minimums, header, len(header)), max(len(row[i]) for row in cells))
              for i, header in enumerate(headers)]
    if max_widths:
        widths = [min(w, rv.field(max_widths, header, w)) for header, w in zip(headers, widths)]
    budget = width - 3 * len(headers) - 1
    while sum(widths) > budget:
        largest = max(range(len(widths)), key=widths.__getitem__)
        if widths[largest] <= 1:
            raise ValueError('Table width too small')
        widths[largest] -= 1
    if stretch:
        widths[1 if selected is not None else 0] += max(0, budget - sum(widths))
    border = '+' + '+'.join('-' * (w + 2) for w in widths) + '+'
    output = [border]
    for row in cells:
        chunks = [textwrap.wrap(value, w, replace_whitespace=False) or ['']
                  for value, w in zip(row, widths)]
        for i in range(max(map(len, chunks))):
            output.append('| ' + ' | '.join((part[i] if i < len(part) else '').ljust(w)
                          for part, w in zip(chunks, widths)) + ' |')
        output.append(border)
    return output


def node_lines(data, width=100):
    if data['nodes'] is None:
        return ['Node data unavailable (see errors).']
    metrics = {n['metadata']['name']: rv.field(n, 'usage', {}) for n in data['metrics'] or []}
    requests = {}
    for pod in data['pods'] or []:
        if rv.field(pod, 'status', {}).get('phase') in ('Succeeded', 'Failed'):
            continue
        name = rv.field(pod, 'spec', {}).get('nodeName')
        if name:
            target = requests.setdefault(name, {r: 0 for r in RESOURCES})
            for r, v in pod_requests(pod).items():
                target[r] += v
    lines = ['Disk: ephemeral capacity/requests; rootfs usage. PVC volumes excluded.', '']
    for n in sorted(data['nodes'], key=lambda n: n['metadata']['name']):
        name = n['metadata']['name']
        status = rv.field(n, 'status', {})
        ips = ', '.join(a['address'] for a in rv.field(status, 'addresses', []) if a['type'] in ('InternalIP', 'ExternalIP')) or 'N/A'
        lines += ['%s | Cluster: %s | %s' % (name, data['cluster'], node_state(n)), 'IP: ' + ips]
        rows = []
        for r, label in zip(RESOURCES, ('CPU', 'Memory', 'Disk(ephem)')):
            total = quantity(rv.field(status, 'capacity', {}).get(r))
            alloc = quantity(rv.field(status, 'allocatable', {}).get(r))
            requested = rv.field(rv.field(requests, name, {}), r, 0) if data['pods'] is not None else None
            if r == 'ephemeral-storage':
                used = rv.field(data['disk'], name, {}).get('usedBytes')
            else:
                used = quantity(rv.field(metrics, name, {}).get(r))
            # Disk usage percentage uses the same filesystem capacity as its usage metric.
            basis = rv.field(data['disk'], name, {}).get('capacityBytes') if r == 'ephemeral-storage' else total
            percent = '%.1f%%' % (used / basis * 100) if used is not None and basis else 'N/A'
            rows.append([label, amount(total, r), amount(alloc, r), amount(requested, r), amount(used, r), percent])
        headers = ['RESOURCE', 'TOTAL', 'ALLOCATABLE', 'REQUEST', 'USED', 'USED/TOTAL']
        if width >= 69:
            lines += ascii_table(headers, rows, width)
        else:
            # Keep all values visible when the full six-column table cannot fit.
            for indices in ((0, 1, 2), (0, 3, 4, 5)):
                lines += ascii_table([headers[i] for i in indices],
                                     [[row[i] for i in indices] for row in rows], width)
        fs_capacity = rv.field(data['disk'], name, {}).get('capacityBytes')
        lines += ['Disk USED/TOTAL basis (node rootfs): ' + amount(fs_capacity, 'ephemeral-storage'), '']
    return lines or ['No nodes.']


def summary(data):
    def count(k, predicate=lambda x: True):
        return 'N/A' if data[k] is None else str(sum(bool(predicate(x)) for x in data[k]))
    return [
        'Pods Running: %s | Total: %s | Pending: %s | Issues: %s' %
        (count('pods', lambda p: rv.field(p, 'status', {}).get('phase') == 'Running'), count('pods'),
         count('pods', lambda p: rv.field(p, 'status', {}).get('phase') == 'Pending'), count('pods', pod_issue)),
        'Deployments: %s (issues %s) | ReplicaSets: %s (issues %s) | StatefulSets: %s (issues %s)' %
        tuple(v for k in ('deployments', 'replicasets', 'statefulsets') for v in (count(k), count(k, controller_issue))),
        'Warning events retained: %s | Event occurrences: %s' %
        (count('events', lambda e: e.get('type') == 'Warning'), 'N/A' if data['events'] is None else
         sum(rv.field(rv.field(e, 'series', {}), 'count', rv.field(e, 'count', 1)) for e in data['events'] if e.get('type') == 'Warning')),
    ]


def details(data, view):
    if view == '1' or view == '2':
        return (summary(data) + ['', 'NODE RESOURCE INVENTORY', ''] if view == '1' else []) + node_lines(data)
    if view == '4':
        lines = []
        for e in sorted(data['events'] or [], key=lambda e: e.get('lastTimestamp') or e.get('eventTime') or rv.field(rv.field(e, 'metadata', {}), 'creationTimestamp', ''), reverse=True):
            if e.get('type') != 'Warning':
                continue
            obj = rv.field(e, 'involvedObject', {})
            lines += ['%s | %s | %s/%s | count=%s' % (e.get('lastTimestamp') or e.get('eventTime') or '-',
                      rv.field(e, 'reason', '-'), rv.field(obj, 'namespace', '-'), rv.field(obj, 'name', '-'),
                      rv.field(rv.field(e, 'series', {}), 'count', rv.field(e, 'count', 1))), rv.field(e, 'message', ''), '']
        return lines or ['Warning events unavailable.' if data['events'] is None else 'No retained Warning events.']
    if view == '5':
        return (data['errors'] + list(rv.field(data, 'resource_errors', {}).values()) + rv.field(data, 'catalog_errors', [])) or ['All requested data sources responded successfully.']
    return workload_lines(data)


def workload_lines(data, width=120, kind=None):
    lines = []
    for kind in ((kind,) if kind else WORKLOAD_KINDS):
        objects = data[kind]
        lines += [kind.upper() + (' | N/A' if objects is None else ' | %s items' % len(objects))]
        if objects is None:
            lines += ['Data unavailable (see Errors).', '']
            continue
        if not objects:
            lines += ['No resources.', '']
            continue
        headers = (['NAMESPACE', 'NAME', 'PHASE', 'RESTARTS', 'NODE', 'STATUS'] if kind == 'pods'
                   else ['NAMESPACE', 'NAME', 'READY', 'DESIRED', 'STATUS'])
        headers.append('ALARMS')
        alarm_index = alarms.index(data)
        rows = []
        for obj in sorted(objects, key=lambda o: (rv.field(o['metadata'], 'namespace', 'default'), o['metadata']['name'])):
            metadata, status = obj['metadata'], rv.field(obj, 'status', {})
            row = [rv.field(metadata, 'namespace', 'default'), metadata['name']]
            if kind == 'pods':
                restarts = sum(rv.field(c, 'restartCount', 0) for c in
                               rv.field(status, 'containerStatuses', []) + rv.field(status, 'initContainerStatuses', []))
                row += [rv.field(status, 'phase', 'Unknown'), str(restarts),
                        rv.field(rv.field(obj, 'spec', {}), 'nodeName', '-'), pod_issue(obj) or 'OK']
            else:
                row += [str(rv.field(status, 'readyReplicas', 0)), str(rv.field(rv.field(obj, 'spec', {}), 'replicas', 1)),
                        controller_issue(obj) or 'OK']
            row.append(alarm_index.label(dict(obj, kind=obj.get('kind') or alarms.KINDS[kind])))
            rows.append(row)
        if width >= 85:
            lines += ascii_table(headers, rows, width,
                                 max_widths={'NAMESPACE': 24, 'NAME': 64, 'NODE': 24, 'STATUS': 32},
                                 selected=(data.get('resource_state') or {}).get('index'))
        else:
            # At small widths use a separate field/value table per resource.
            for i, row in enumerate(rows):
                marker = '> FIELD' if i == (data.get('resource_state') or {}).get('index') else 'FIELD'
                lines += ascii_table([marker, 'VALUE'], list(map(list, zip(headers, row))), width)
        lines += ['']
    return lines


def workload_summary(data, kind):
    objects = data[kind]
    def count(predicate=lambda obj: True):
        return 'N/A' if objects is None else str(sum(bool(predicate(obj)) for obj in objects))
    if kind == 'pods':
        phase = lambda value: count(lambda p: rv.field(rv.field(p, 'status', {}), 'phase', 'Unknown') == value)
        return [
            'Total: %s | Healthy: %s | Issues: %s' % (count(), count(lambda p: rv.field(p, 'status', {}).get('phase') == 'Running' and not pod_issue(p)), count(pod_issue)),
            'Running: %s | Pending: %s | Failed: %s | Succeeded: %s | Unknown: %s' % tuple(phase(v) for v in ('Running', 'Pending', 'Failed', 'Succeeded', 'Unknown')),
            'NotReady: %s | CrashLoopBackOff: %s | Terminating: %s' % tuple(count(lambda p, reason=r: pod_issue(p) == reason) for r in ('NotReady', 'CrashLoopBackOff', 'Terminating')),
            'ImagePullBackOff: %s | ErrImagePull: %s | CreateContainerConfigError: %s' % tuple(count(lambda p, reason=r: pod_issue(p) == reason) for r in ('ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError'))]
    issues = count(controller_issue)
    return ['Total: %s | Healthy: %s | Issues: %s' % (count(), count(lambda obj: not controller_issue(obj)), issues),
            'Replicas desired: %s | Ready: %s' %
            ('N/A' if objects is None else sum(rv.field(rv.field(obj, 'spec', {}), 'replicas', 1) for obj in objects),
             'N/A' if objects is None else sum(rv.field(rv.field(obj, 'status', {}), 'readyReplicas', 0) for obj in objects))]


def filtered_workloads(data, kind, filters=None):
    def status(obj):
        issue = pod_issue(obj) if kind == 'pods' else controller_issue(obj)
        phase = str(rv.field(rv.field(obj, 'status', {}), 'phase', ''))
        healthy = not issue and (kind != 'pods' or phase == 'Running')
        return ' '.join([phase, issue, 'issues' if issue else 'ok', 'healthy' if healthy else ''])
    filtered = dict(data)
    filtered[kind] = rv.filter_items(data[kind], filters or {}, status)
    return filtered


def filter_label(filters):
    return ' '.join('%s=%s' % pair for pair in (filters or {}).items()) or '(none)'


def workload_header(data, kind, width, filters=None):
    labels = ('Pods', 'Deployments', 'ReplicaSets', 'StatefulSets')
    tabs = 'Tabs: ' + ' | '.join(('[' + label + ']' if k == kind else label) for k, label in zip(WORKLOAD_KINDS, labels))
    filtered = filtered_workloads(data, kind, filters)
    matched = 'N/A' if filtered[kind] is None else len(filtered[kind])
    total = 'N/A' if data[kind] is None else len(data[kind])
    lines = [tabs, 'Keys: Left/Right: change tab | /: filter | x: clear | Esc: navigation',
             'Filter: ' + filter_label(filters), 'Matches: %s / Total: %s' % (matched, total), '-' * width, 'STATUS SUMMARY'] + workload_summary(filtered, kind) + ['-' * width, kind.upper() + ' LIST']
    return [chunk for line in lines for chunk in textwrap.wrap(clean(line), width) or ['']]


def fit(value, width):
    value = clean(value)
    return (value[:max(0, width - 3)] + '...' if len(value) > width else value).ljust(width)


def box(lines, width):
    border = '+' + '-' * (width - 2) + '+'
    return [border] + ['| ' + fit(line, width - 4) + ' |' for line in lines] + [border]


def dashboard_cards(data, width):
    def count(k, pred=lambda x: True):
        return 'N/A' if data[k] is None else str(sum(bool(pred(x)) for x in data[k]))
    cards = [
        ['PODS', 'Running: ' + count('pods', lambda p: rv.field(p, 'status', {}).get('phase') == 'Running'),
         'Total: ' + count('pods'), 'Pending: ' + count('pods', lambda p: rv.field(p, 'status', {}).get('phase') == 'Pending'),
         'Issues: ' + count('pods', pod_issue)],
        ['CONTROLLERS', 'Deployments: ' + count('deployments'), 'ReplicaSets: ' + count('replicasets'),
         'StatefulSets: ' + count('statefulsets'), 'Issues D/R/S: ' + '/'.join(count(k, controller_issue) for k in ('deployments', 'replicasets', 'statefulsets'))],
        ['WARNING EVENTS', 'Retained: ' + count('events', lambda e: e.get('type') == 'Warning'),
         'Occurrences: ' + ('N/A' if data['events'] is None else str(sum(rv.field(rv.field(e, 'series', {}), 'count', rv.field(e, 'count', 1)) for e in data['events'] if e.get('type') == 'Warning'))),
         'Nodes: ' + count('nodes'), 'Data errors: ' + str(len(data['errors']))]]
    cols = 3 if width >= 78 else 2 if width >= 54 else 1
    card_width = (width - (cols - 1)) // cols
    output = []
    for start in range(0, len(cards), cols):
        group = [box(c, card_width) for c in cards[start:start + cols]]
        output.extend(' '.join(rows) for rows in zip(*group))
    return output


MONITORING_TABS = [
    ('Cluster & Nodes', ['Cluster totals and per-node CPU / memory trends.',
                         'Disk read/write throughput, IOPS and network RX/TX.',
                         'Node pressure and resource saturation.']),
    ('Namespace Capacity', ['Namespace CPU / memory requests, limits and actual usage.',
                            'ResourceQuota headroom and usage trends.']),
    ('Service Traffic', ['Service traffic, connections and drops.',
                         'HTTP request rate, error rate and latency when available.']),
    ('Workload Health', ['Top CPU / memory consumers and resource trends.',
                         'Throttling, restarts, OOM and pending workload signals.'])]


def dashboard_tabs(view, width, monitoring_tab=0):
    labels = [label for label, _ in MONITORING_TABS] if view == 'm' else ['Summary', 'Nodes']
    selected = monitoring_tab if view == 'm' else int(view == '2')
    lines = ['Tabs: ' + ' | '.join('[' + label + ']' if i == selected else label for i, label in enumerate(labels)),
             'Keys: Left/Right: change tab | Esc: submenu', '-' * width]
    return [chunk for line in lines for chunk in textwrap.wrap(line, width) or ['']]


def navigation_lines(group, selected, focus):
    """Group titles are visual dividers; only submenus can be selected."""
    rows, cursor = [], 0
    short_labels = {'7': 'Config', '9': 'Access / RBAC', '0': 'API Resource'}
    for label, keys in rv.MENU_GROUPS:
        if rows:
            rows.append('')
        rows.append(' [' + label + ']')
        for key in keys:
            active_item = key == selected
            if active_item:
                cursor = len(rows)
            marker = '>' if active_item and focus == 'nav' else '*' if active_item else ' '
            rows.append(marker + '  - ' + short_labels.get(key, dict(rv.MENUS)[key]))
    return rows, cursor


def active_resource(view, category_tab=0, api_resource=None):
    if view == 'N':
        return {'name': 'nodes', 'namespaced': False, 'read_only': True}
    if view == 'Z':
        return {'name': 'namespaces', 'namespaced': False, 'read_only': True}
    if view in rv.CATEGORIES:
        label, name, scope = rv.CATEGORIES[view][category_tab]
        return {'name': name, 'namespaced': scope}
    return api_resource if view == '0' else None


def catalog_matches(data, query=''):
    return [entry for entry in rv.field(data, 'catalog', []) if query.casefold() in entry['name'].casefold()]


def resource_lines(data, resource, width, filters=None):
    name = resource['name']
    if name not in rv.field(data, 'extras', {}):
        return ['Loading ' + name + '...']
    objects = data['extras'][name]
    if objects is None:
        return ['Data unavailable: ' + rv.field(rv.field(data, 'resource_errors', {}), name, 'Unknown error')]
    if not objects:
        return ['No resources.']
    try:
        objects = rv.filter_items(objects, filters or {}, rv.status_text)
        objects = resource_detail.sort_objects(objects)
        headers, rows = rv.columns(name, objects)
        headers.append('ALARMS')
        alarm_index = alarms.index(data)
        for obj,row in zip(objects,rows):row.append(alarm_index.label(obj))
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        message = 'Cannot render %s: %s: %s' % (name, type(exc).__name__, exc)
        data.setdefault('resource_errors', {})['render:' + name] = message
        return [message, 'Use Left/Right or Esc to switch views; r to retry.']
    data.setdefault('resource_errors', {}).pop('render:' + name, None)
    if not objects:
        return ['No matching resources.']
    if width >= 85:
        return ascii_table(headers, rows, width, max_widths={h: 64 if h == 'NAME' else 36 for h in headers},
                           selected=(data.get('resource_state') or {}).get('index'))
    return [line for i, row in enumerate(rows) for line in ascii_table(
        ['> FIELD' if i == (data.get('resource_state') or {}).get('index') else 'FIELD', 'VALUE'],
        list(map(list, zip(headers, row))), width)]


def resource_header(data, view, width, category_tab=0, api_resource=None, catalog_query='', catalog_index=0, filters=None):
    resource = active_resource(view, category_tab, api_resource)
    if view in rv.CATEGORIES:
        tabs = 'Tabs: ' + ' | '.join('[' + label + ']' if index == category_tab else label for index, (label, _, _) in enumerate(rv.CATEGORIES[view]))
        lines = [tabs, 'Keys: Left/Right: change tab | Esc: navigation', '-' * width]
    else:
        lines = ['API RESOURCES | ' + ('RESOURCE LIST' if resource else 'DISCOVERY'),
                 'Keys: Esc: resource catalog' if resource else 'Keys: Up/Down: select | Enter: list | n: new selected resource | D: saved drafts | /: search | x: clear', '-' * width]
    if resource:
        objects = rv.field(data, 'extras', {}).get(resource['name'])
        count = 'N/A' if objects is None else str(len(objects))
        try:
            matches = rv.filter_items(objects, filters or {}, rv.status_text)
            matched = 'N/A' if matches is None else str(len(matches))
        except (TypeError, ValueError, AttributeError, KeyError):
            matched = 'N/A'
        lines += ['Resource: ' + resource['name'],
                  'Search keys: /: filter name=... ns=... status=... | x: clear',
                  'Filter: ' + filter_label(filters or {}),
                  'Scope: %s | Matches: %s / Total: %s' %
                  ('All namespaces' if resource['namespaced'] else 'Cluster', matched, count)]
    else:
        lines += ['Search: ' + (catalog_query or '(none)'), 'Resources: %s | Discovery errors: %s' %
                  (len(catalog_matches(data, catalog_query)), len(rv.field(data, 'catalog_errors', [])))]
        matches = catalog_matches(data, catalog_query)
        if matches:
            lines += ['Selected: ' + matches[min(catalog_index, len(matches) - 1)]['name']]
    lines += ['-' * width, 'RESOURCE LIST' if resource else 'AVAILABLE API RESOURCES']
    return [chunk for line in lines for chunk in textwrap.wrap(clean(line), width) or ['']]


def resource_selection(data, view, kind, request, filters):
    resource = {'name': kind, 'namespaced': True} if view == '3' else request
    if not resource or data is None:
        return None, []
    try:
        if view == 'N':
            return resource, node_views.matches(data, filters) or []
        if view == 'Z':
            return resource, namespace_views.matches(data, filters) or []
        objects = (filtered_workloads(data, kind, filters)[kind] if view == '3' else
                   rv.filter_items((data.get('extras') or {}).get(resource['name']), filters, rv.status_text))
        return resource, resource_detail.sort_objects(objects)
    except (TypeError, ValueError, AttributeError, KeyError):
        return resource, []


def terminal_action(stdscr, callback):
    curses.def_prog_mode()
    sys.stdout.write('\x1b[?1006l\x1b[?1000l'); sys.stdout.flush()
    curses.endwin()
    try:
        curses.reset_shell_mode()
        callback()
    finally:
        curses.reset_prog_mode()
        curses.curs_set(0)
        stdscr.timeout(40)
        stdscr.clearok(True)
        sys.stdout.write('\x1b[?1000h\x1b[?1006h'); sys.stdout.flush()


def content_lines(data, view, width, workload_kind='pods', filters=None, category_tab=0, api_resource=None, catalog_query='', catalog_index=0, monitoring_tab=0):
    output = []
    if data.get('resource_detail') and (rv.menu_group(view) == 'Resources' or view in ('N', 'Z', '4', '5')):
        source = resource_detail.content(data['resource_detail'], width, clean, ascii_table)
    elif view == 's':
        source = ['CLUSTER PROFILES', '', 'Enter Settings / Profiles to register, edit, remove or select a profile.',
                  'Manage per-profile Prometheus connections from the same screen.', 'Shortcut: c']
    elif view == 'm':
        source = monitoring.render(data, monitoring_tab, width, ascii_table)
    elif view == 'N':
        source = node_views.list_lines(data, width, filters, ascii_table)
    elif view == 'Z':
        source = namespace_views.list_lines(data, width, filters, ascii_table)
    elif view in ('4', '5'):
        source = event_views.content(data, view, width, clean)
    elif view == '1':
        output = dashboard_cards(data, width) + ['', 'NODE RESOURCE INVENTORY', '']
        source = node_lines(data, width)
    elif view == '2':
        source = node_lines(data, width)
    elif view == '3':
        source = workload_lines(filtered_workloads(data, workload_kind, filters), width, workload_kind)
    elif view in rv.CATEGORIES or view == '0':
        resource = active_resource(view, category_tab, api_resource)
        if resource:
            source = resource_lines(data, resource, width, filters)
        else:
            entries = catalog_matches(data, catalog_query)
            source = [('> ' if i == catalog_index else '  ') + fit(entry['name'], max(10, width - 15)) + (' [namespace]' if entry['namespaced'] else ' [cluster]') for i, entry in enumerate(entries)]
            if not source:
                source = ['No matching API resources.' if data.get('catalog_loaded') else 'Loading API resource discovery...']
            source += rv.field(data, 'catalog_errors', [])
    else:
        source = details(data, view)
    for line in source:
        output.extend(textwrap.wrap(clean(line), width, replace_whitespace=False) or [''])
    return output


def layout(data, view='1', width=120, offset=0, height=None, content=None, workload_kind='pods', focus='nav', selected=None, filters=None, category_tab=0, api_resource=None, catalog_query='', catalog_index=0, nav_group=None, monitoring_tab=0):
    width = max(40, width)
    sidebar_width = 24 if width >= 85 else 0
    panel_width = width - sidebar_width - (1 if sidebar_width else 0)
    inner = panel_width - 4
    group = nav_group or rv.menu_group(selected or view)
    selected = selected or ('1' if view == '2' else view)
    title = rv.menu_group(view) + ' / ' + dict(rv.MENUS)['1' if view == '2' else view]
    header = box([
        'KAsciiDash  /  KUBERNETES  /  ' + ('DEMO PREVIEW' if data.get('demo') else 'OPERATIONS'),
        'Profile: %s | Cluster: %s | Conf: %s' % (data.get('alias', data['cluster']), data['cluster'], Path(data['config']).name if data.get('config') else '-'),
        'API: %s' % data['server'],
        'Nodes API: %s | Snapshot: %s | Errors: %s' % ('OK' if data['nodes'] is not None else 'UNAVAILABLE', data['stamp'], len(data['errors']) + len(rv.field(data, 'resource_errors', {})) + len(rv.field(data, 'catalog_errors', [])))], width)
    if content is None:
        content = content_lines(data, view, inner, workload_kind, filters, category_tab, api_resource, catalog_query, catalog_index, monitoring_tab)
    nav_rows, nav_cursor = navigation_lines(group, selected, focus)
    if not sidebar_width:
        choice = group + ' / ' + dict(rv.MENUS)[selected]
        header += box(['Menu: ' + choice + ' | Up/Down select; Enter open; Esc back'], width)
    # Keep the focused item visible even in a small terminal.
    nav_slots = min(len(nav_rows), max(1, height - len(header) - 7)) if height else len(nav_rows)
    nav_start = max(0, nav_cursor - nav_slots + 1)
    sidebar = ['NAVIGATION', '',
               *nav_rows[nav_start:nav_start + nav_slots],
               '', 'Enter: Open', 'Esc: Back', '', 'ACTIONS', '[r] Refresh', '[c] Profiles', '[q] Quit',
               '', 'SCROLL', 'j/k: Down/Up', 'u/d: Page Up/Down']
    pinned = workload_header(data, workload_kind, inner, filters) if view == '3' else resource_header(data, view, inner, category_tab, api_resource, catalog_query, catalog_index, filters) if view in rv.CATEGORIES or view == '0' else []
    if view in ('1', '2', 'm'):
        pinned = dashboard_tabs(view, inner, monitoring_tab)
    if rv.menu_group(view) == 'Resources' or view in ('N', 'Z'):
        if data.get('resource_detail'):
            pinned = resource_detail.header(data['resource_detail'], inner, clean)
        elif view == 'N':
            pinned = node_views.header(data, inner, filters, clean)
        elif view == 'Z':
            pinned = namespace_views.header(data, inner, filters, clean)
        elif data.get('resource_state') is not None:
            pinned += [part for part in textwrap.wrap('Keys: Up/Down: select | Enter: YAML/Describe/Logs | n: new YAML | D: saved drafts', inner)]
    if view in ('4', '5'):
        pinned = resource_detail.header(data['resource_detail'], inner, clean) if data.get('resource_detail') else event_views.header(data, view, inner, clean)
    if height:
        pinned = pinned[:max(0, height - len(header) - 8)]
    available = max(1, height - len(header) - 7 - len(pinned)) if height else max(len(content), len(sidebar))
    if view in ('4', '5') and not (data.get('event_state') or {}).get('detail'):
        row_line = (data.get('event_state') or {}).get('index', 0) * 2
        offset = min(offset, row_line)
        if row_line >= offset + available:
            offset = row_line - available + 1
    topology_detail = data.get('resource_detail') or {}
    topology_list = (topology_detail.get('mode') == 'topology' and (topology_detail.get('target') or {}).get('resource') == 'namespaces' and not (topology_detail.get('topology_state') or {}).get('target'))
    if topology_list or ((rv.menu_group(view) == 'Resources' or view in ('N', 'Z')) and data.get('resource_state') is not None and not data.get('resource_detail')):
        row_line = getattr(content, 'selected_line', None)
        if row_line is None:
            row_line = next((i for i, line in enumerate(content) if '| > ' in line), None)
        if row_line is not None:
            offset = min(offset, row_line)
            if row_line >= offset + available:
                offset = row_line - available + 1
    offset = min(max(0, offset), max(0, len(content) - available))
    visible = content[offset:offset + available]
    panel = box([title + (' | Enter to open' if focus == 'nav' else ' | ACTIVE'), ''] + pinned + visible + [''] * (available - len(visible)), panel_width)
    # Section boundaries span the full panel and stay fixed above the list.
    for index, line in enumerate(pinned):
        if line == '-' * inner:
            panel[3 + index] = panel[0]
    if sidebar_width:
        side_height = available + 2 + len(pinned)
        side = box(sidebar[:side_height] + [''] * max(0, side_height - len(sidebar)), sidebar_width)
        body = [left + ' ' + right for left, right in zip(side, panel)]
    else:
        body = panel
    footer_hint = ('Esc: list | u/d: page' if (data.get('event_state') or {}).get('detail') else 'Enter: details | Up/Down: select | u/d: page') if view in ('4', '5') else 'CPU cores; Mem/Disk GiB | N/A = unavailable'
    if rv.menu_group(view) == 'Resources' or view in ('N', 'Z'):
        footer_hint = 'Esc: list | Left/Right: detail tabs | u/d: page' if data.get('resource_detail') else 'Up/Down: select | Enter: details' + (' | n: new YAML | D: saved drafts' if view != 'N' else '')
    footer = box(['Lines %s-%s/%s | %s' %
                  (offset + 1, min(offset + available, len(content)), len(content), footer_hint)], width)
    return header + body + footer, len(content), available, offset


def frame(data, view='1', width=120):
    return layout(data, view, width)[0]


def demo():
    node = lambda name, ip: {'metadata': {'name': name}, 'status': {'capacity': {'cpu': '8', 'memory': '32Gi', 'ephemeral-storage': '100Gi'},
        'allocatable': {'cpu': '7500m', 'memory': '30Gi', 'ephemeral-storage': '90Gi'},
        'addresses': [{'type': 'InternalIP', 'address': ip}], 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
    pod = {'metadata': {'name': 'web-abc', 'namespace': 'demo'}, 'spec': {'nodeName': 'worker-01', 'containers': [
        {'resources': {'requests': {'cpu': '500m', 'memory': '512Mi', 'ephemeral-storage': '1Gi'}}}]},
        'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
    pending = {'metadata': {'name': 'batch-pending', 'namespace': 'demo'}, 'spec': {}, 'status': {'phase': 'Pending'}}
    return {'cluster': 'demo-cluster', 'context': 'demo-context', 'server': 'https://demo.invalid:6443', 'demo': True,
        'stamp': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'errors': [], 'nodes': [node('worker-01', '10.0.0.11'), node('worker-02', '10.0.0.12')],
        'pods': [pod, pending], 'deployments': [{'kind': 'Deployment', 'metadata': {'name': 'web', 'namespace': 'demo'}, 'spec': {'replicas': 1},
          'status': {'readyReplicas': 1, 'updatedReplicas': 1, 'availableReplicas': 1}}], 'replicasets': [], 'statefulsets': [],
        'metrics': [{'metadata': {'name': 'worker-01'}, 'usage': {'cpu': '1250m', 'memory': '6Gi'}}],
        'disk': {'worker-01': {'usedBytes': 20 * 1024 ** 3, 'capacityBytes': 100 * 1024 ** 3}},
        'events': [{'type': 'Warning', 'reason': 'FailedScheduling', 'message': 'Insufficient cpu', 'count': 3,
                    'involvedObject': {'apiVersion': 'v1', 'kind': 'Pod', 'namespace': 'demo', 'name': 'batch-pending', 'uid': 'demo-pending'}}]}


def wheel_direction(stdscr, key):
    if key == curses.KEY_MOUSE:
        try:
            state = curses.getmouse()[4]
        except curses.error:
            return 0
        if state & curses.BUTTON4_PRESSED:
            return -1
        if state & getattr(curses, 'BUTTON5_PRESSED', 0):
            return 1
        return 0
    if key != 27:
        return 0
    # macOS ncurses may lack BUTTON5 and SGR decoding. Read SGR wheel reports
    # directly in that case, retaining unrelated Escape/key input.
    received = []
    stdscr.timeout(10)
    try:
        for expected in (ord('['), ord('<')):
            char = stdscr.getch()
            if char != -1:
                received.append(char)
            if char != expected:
                for value in reversed(received):
                    curses.ungetch(value)
                return None
        for _ in range(40):
            char = stdscr.getch()
            if char == -1:
                break
            received.append(char)
            if char in (ord('M'), ord('m')):
                report = ''.join(chr(v) for v in received)
                match = re.fullmatch(r'\[<(\d+);\d+;\d+M', report)
                if match:
                    button = int(match[1]) & 0xC3
                    return -1 if button == 64 else 1 if button == 65 else 0
                break
        return 0
    finally:
        stdscr.timeout(40)


def screen(stdscr, loader, interval, scroll_step=5, resource_loader=None, discovery_loader=None, monitoring_loader=None, metrics_interval=5, detail_loader=None, action_handler=None):
    curses.curs_set(0)
    stdscr.timeout(40)
    if hasattr(curses, 'set_escdelay'):
        curses.set_escdelay(25)
    native_mouse = hasattr(curses, 'BUTTON5_PRESSED')
    if native_mouse:
        curses.mouseinterval(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
    # SGR gives unambiguous wheel direction on both old macOS and modern ncurses.
    sys.stdout.write('\x1b[?1000h\x1b[?1006h')
    sys.stdout.flush()
    view, offset = '1', 0
    selected, focus, tab = '1', 'nav', 0
    nav_group, overview_view, monitoring_tab = 'Dashboard', '1', 0
    category_tabs = {key: 0 for key in rv.CATEGORIES}
    filters = {kind: {} for kind in WORKLOAD_KINDS}
    queries = {kind: '' for kind in WORKLOAD_KINDS}
    resource_filters, resource_queries = {}, {}
    catalog_query, catalog_index, api_resource = '', 0, None
    draft, filter_error = '', ''
    extras, resource_errors = {}, {}
    catalog, catalog_errors, catalog_loaded = [], [], False
    resource_future, resource_request, discovery_future = None, None, None
    data, last_draw = None, None
    cached_key, cached_content = None, None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    detail_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    row_cache, event_cache = RowCache(), RowCache()
    collection_revision = 0
    future = executor.submit(loader)
    due = time.monotonic() + interval
    force_resource = False
    force_snapshot = False
    monitor_filters, monitor_queries = {}, {}
    monitor_future, monitor_request, monitor_cache = None, None, {}
    monitor_due, monitor_period, monitor_paused = 0, 300, False
    monitor_force = False
    event_filters, event_queries = {'4': {}, '5': {}}, {'4': '', '5': ''}
    event_browser = event_views.Browser()
    resource_browser, detail_browser = resource_detail.Selection(), resource_detail.Detail()
    try:
        while True:
            changed = False
            if future and future.done():
                collection_revision += 1
                data = future.result()
                future = None
                due = time.monotonic() + interval
                if force_snapshot:
                    future = executor.submit(loader)
                    force_snapshot = False
                changed = True
            if resource_future and resource_future.done():
                collection_revision += 1
                try:
                    objects, error = resource_future.result()
                except Exception as exc:
                    objects, error = None, 'Resource collection failed: %s: %s' % (type(exc).__name__, exc)
                extras[resource_request['name']] = objects
                if error:
                    resource_errors[resource_request['name']] = resource_request['name'] + ': ' + error
                else:
                    resource_errors.pop(resource_request['name'], None)
                resource_future = None
                changed = True
            if discovery_future and discovery_future.done():
                collection_revision += 1
                try:
                    catalog, catalog_errors = discovery_future.result()
                except Exception as exc:
                    catalog_errors = ['Discovery failed: %s: %s' % (type(exc).__name__, exc)]
                catalog_loaded, discovery_future = True, None
                changed = True
            if future is None and time.monotonic() >= due:
                future = executor.submit(loader)
                force_resource = True
            category_tab = rv.field(category_tabs, view, 0)
            request = active_resource(view, category_tab, api_resource)
            if request and view != 'N' and resource_loader and resource_future is None and (request['name'] not in extras or force_resource):
                resource_request = request
                resource_future = executor.submit(resource_loader, request['name'], request['namespaced'])
                force_resource = False
            if view == '0' and not catalog_loaded and discovery_future is None and discovery_loader:
                discovery_future = executor.submit(discovery_loader)
            monitor_key = (monitoring_tab, tuple(sorted(monitor_filters.get(monitoring_tab, {}).items())), monitor_period)
            if monitor_future and monitor_future.done():
                try:
                    monitor_cache[monitor_request] = monitor_future.result()
                except Exception as exc:
                    monitor_cache[monitor_request] = dict(end=time.time(), cards=[], notes=[], errors=['Monitoring query failed: ' + str(exc)], names={})
                while len(monitor_cache) > 12:
                    monitor_cache.pop(next(iter(monitor_cache)))
                monitor_future = None
                monitor_due = time.monotonic() + metrics_interval
                changed = True
            if (view == 'm' and monitoring_loader and monitor_future is None and
                    (monitor_force or (not monitor_paused and (monitor_key not in monitor_cache or time.monotonic() >= monitor_due)))):
                monitor_request = monitor_key
                monitor_future = executor.submit(monitoring_loader, monitoring_tab, dict(monitor_filters.get(monitoring_tab, {})), monitor_period)
                monitor_force = False
                changed = True
            if data is not None:
                state = dict(filter=monitor_queries.get(monitoring_tab, ''), interval=metrics_interval,
                             period=monitor_period, paused=monitor_paused, loading=bool(monitor_future))
                if data.get('monitor_state') != state or data.get('monitoring') is not monitor_cache.get(monitor_key):
                    changed = True
                data.update(monitoring=monitor_cache.get(monitor_key), monitor_state=state)
                data.update(extras=extras, resource_errors=resource_errors, catalog=catalog,
                            catalog_errors=catalog_errors, catalog_loaded=catalog_loaded)
            if data is not None:
                data['event_filters'] = event_filters
            event_rows = event_cache.get((view, collection_revision, tuple(sorted(event_filters.get(view, {}).items())), str(data.get('resource_errors') or {})), data, lambda: event_views.records(data, view)) if data is not None and view in ('4', '5') else []
            if data is not None and view in ('4', '5'):
                event_browser.reconcile(view, event_rows)
                event_state = event_browser.state(view)
                if data.get('event_state') != event_state:
                    changed = True
                data['event_state'] = event_state
            is_event_list = view in ('4', '5') and not event_browser.state(view)['detail']
            h, w = stdscr.getmaxyx()
            kind = WORKLOAD_KINDS[tab]
            is_catalog = view == '0' and request is None
            can_filter = view in ('3', 'm') or is_event_list or request is not None
            active_filters = (event_filters[view] if view in ('4', '5') else monitor_filters.get(monitoring_tab, {}) if view == 'm' else filters[kind] if view == '3' else
                              resource_filters.get(request['name'], {}) if request else {})
            selection_cache_key = (view, kind, str(request), tuple(sorted(active_filters.items())), collection_revision)
            selectable, resource_rows = row_cache.get(selection_cache_key, data, lambda: resource_selection(data, view, kind, request, active_filters))
            selection_key = (view, selectable['name']) if selectable else None
            detail_browser.poll(detail_executor, detail_loader, data)
            if data is not None:
                selection_state = {'index': resource_browser.reconcile(selection_key, resource_rows)} if selectable else None
                detail_state = detail_browser.state()
                selection_changed = data.get('resource_state') != selection_state
                detail_changed = data.get('resource_detail') != detail_state
                if selection_changed and selection_state is not None and not detail_changed and isinstance(cached_content, CursorContent) and cached_content.enabled:
                    cached_content.select(selection_state['index'])
                    last_draw = None
                elif selection_changed or detail_changed:
                    changed = True
                data.update(resource_state=selection_state, resource_detail=detail_state)
            is_resource_list = selectable is not None and detail_browser.item is None
            is_topology_list = detail_browser.topology_list()
            if detail_browser.item:
                can_filter = False
            if changed:
                cached_key, last_draw = None, None
            draw_key = (id(data), view, offset, h, w, bool(future), selected, focus, tab,
                        category_tab, str(api_resource), catalog_query, catalog_index,
                        str(active_filters), draft, filter_error, bool(resource_future), bool(discovery_future), nav_group, monitoring_tab)
            if draw_key != last_draw:
                stdscr.erase()
                if h < 18 or w < 60:
                    lines = ['KAsciiDash | Resize terminal to at least 60x18', '[q] Quit']
                    visible = max(1, h - 1)
                elif data:
                    inner = w - 1 - (25 if w - 1 >= 85 else 0) - 4
                    if view == '0' and api_resource is None:
                        entries = catalog_matches(data, catalog_query)
                        catalog_index = min(catalog_index, max(0, len(entries) - 1))
                        # One catalog entry occupies one line, so keep its cursor visible.
                        pinned_height = len(resource_header(data, view, inner, catalog_query=catalog_query, catalog_index=catalog_index))
                        catalog_visible = max(1, h - 1 - (6 if w - 1 >= 85 else 9) - 7 - pinned_height)
                        offset = min(offset, catalog_index)
                        if catalog_index >= offset + catalog_visible:
                            offset = catalog_index - catalog_visible + 1
                    content_key = (id(data), view, inner, tab, category_tab, str(api_resource),
                                   catalog_query, catalog_index, str(active_filters), monitoring_tab)
                    if cached_key != content_key:
                        cached_content = content_lines(data, view, inner, kind, active_filters,
                                                       category_tab, api_resource, catalog_query, catalog_index, monitoring_tab)
                        if is_resource_list and inner >= 100:
                            cached_content = CursorContent(cached_content, len(resource_rows))
                        cached_key = content_key
                    lines, total, visible, offset = layout(data, view, w - 1, offset, h - 1,
                        content=cached_content, workload_kind=kind, focus=focus, selected=selected,
                        filters=active_filters, category_tab=category_tab, api_resource=api_resource,
                        catalog_query=catalog_query, catalog_index=catalog_index, nav_group=nav_group, monitoring_tab=monitoring_tab)
                else:
                    lines, visible = ['KAsciiDash | Connecting...', '[q] Quit | Fetching cluster data...'], max(1, h - 1)
                for y, line in enumerate(lines[:max(0, h - 1)]):
                    try:
                        stdscr.addnstr(y, 0, line, max(0, w - 1))
                        if (is_event_list or is_resource_list or is_topology_list) and focus == 'content':
                            # Highlight only the table row inside the content panel.
                            # Exclude navigation, panel borders and trailing padding.
                            content_start = (25 if w - 1 >= 85 else 0) + 2
                            row = line[content_start:max(content_start, w - 3)].rstrip()
                            if row.startswith('| > '):
                                stdscr.addnstr(y, content_start, row, len(row), curses.A_REVERSE)
                    except curses.error:
                        pass
                if focus == 'filter':
                    footer = (filter_error + ' | ' if filter_error else '') + ('Search> ' if is_catalog else 'Filter> ') + draft + ' | Enter apply / Esc cancel'
                else:
                    footer = ('MENU: Up/Down select | Enter open' if focus == 'nav' else 'VIEW: Esc back | Left/Right tabs' + (' | / search | x clear' if is_catalog else ' | / filter | x clear' if can_filter else '') + ' | u/d page') + ' | r refresh | q quit'
                    if view in ('4', '5') and focus == 'content':
                        footer = ('EVENTS: Up/Down select | Enter details | / filter | x clear | Esc menu' if is_event_list else 'DETAIL: Up/Down scroll | Esc list') + ' | u/d page | r refresh | q quit'
                    if detail_browser.item and focus == 'content':
                        footer = 'DETAIL: Left/Right tabs | Esc list | e edit | n new | D drafts | a actions | r reload | u/d page'
                        if detail_browser.item['resource'] in resource_detail.LOG_RESOURCES:
                            footer += ' | ! exec'
                    if detail_browser.item and detail_browser.item.get('event_context') and focus == 'content':
                        footer = 'EVENT: Left/Right tabs | Esc list | u/d page | r refresh' + (' | e edit target YAML' if not detail_browser.item.get('read_only') else ' | target unavailable')
                    elif is_resource_list and focus == 'content':
                        footer = 'RESOURCES: Up/Down select | Enter details | Left/Right tabs | / filter | n new | D drafts | Esc menu | q quit'
                    if view == 'Z' and focus == 'content':
                        footer = ('NAMESPACES: Up/Down select | Enter details | n new Namespace | D drafts | / filter | x clear | Esc menu' if is_resource_list else 'NAMESPACE: Left/Right tabs | n new Namespace | D drafts | Esc list | u/d page') + ' | r refresh | q quit'
                    if view == 'N' and focus == 'content':
                        footer = ('NODES: Up/Down select | Enter details | / filter | x clear | Esc menu' if is_resource_list else 'NODE: Left/Right tabs | Esc list | u/d page') + ' | r refresh | q quit'
                    if detail_browser.item and detail_browser.mode == 'topology' and focus == 'content':
                        footer = ('TOPOLOGY: Up/Down select workload | Enter graph | Esc namespace list' if is_topology_list else
                                  'TOPOLOGY: Esc workload list' if detail_browser.item['resource'] == 'namespaces' else 'TOPOLOGY: Esc resource list') + ' | u/d page | r refresh | Left/Right tabs'
                    if future or resource_future or discovery_future:
                        footer += ' | loading...'
                try:
                    stdscr.addnstr(h - 1, 0, footer, max(0, w - 1), curses.A_REVERSE)
                except curses.error:
                    pass
                stdscr.refresh()
                # Use the normalized scroll/cursor after layout to avoid idle redraws.
                last_draw = (id(data), view, offset, h, w, bool(future), selected, focus, tab,
                             category_tab, str(api_resource), catalog_query, catalog_index,
                             str(active_filters), draft, filter_error, bool(resource_future), bool(discovery_future), nav_group, monitoring_tab)
            key = stdscr.getch()
            if focus == 'filter':
                if key in (curses.KEY_MOUSE, 27):
                    if wheel_direction(stdscr, key) is None:
                        focus, filter_error = 'content', ''
                elif key in (10, 13, curses.KEY_ENTER):
                    try:
                        if is_catalog:
                            catalog_query, catalog_index = draft.strip(), 0
                        elif view in ('4', '5'):
                            event_filters[view] = event_views.parse_filter(draft)
                            event_queries[view] = draft
                            event_browser.indices[view], event_browser.ids[view] = 0, None
                        elif view == 'm':
                            monitor_filters[monitoring_tab] = monitoring.parse_filter(draft, monitoring_tab)
                            monitor_queries[monitoring_tab] = draft
                            monitor_force = True
                        elif request:
                            resource_filters[request['name']] = rv.parse_filter(draft)
                            resource_queries[request['name']] = draft
                        else:
                            filters[kind] = rv.parse_filter(draft)
                            queries[kind] = draft
                        focus, offset, filter_error = 'content', 0, ''
                    except ValueError as exc:
                        filter_error = str(exc)
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    draft = draft[:-1]
                elif key == 21:
                    draft = ''
                elif 32 <= key <= 126 and len(draft) < 512:
                    draft += chr(key)
                continue
            if focus == 'content' and view in ('4', '5') and detail_browser.item:
                if key == ord('e'):
                    if not detail_browser.item.get('read_only') and action_handler:
                        event_target = dict(detail_browser.item)
                        terminal_action(stdscr, lambda: action_handler('edit', event_target))
                        detail_browser.refresh(all_modes=True)
                        cached_key, last_draw = None, None
                        if future is None or future.done():
                            future = executor.submit(loader)
                        else:
                            force_snapshot = True
                    continue
                if key == ord('r') and detail_browser.mode != 'event':
                    detail_browser.refresh()
                    continue
            if focus == 'content' and detail_browser.item and detail_browser.mode in ('warnings', 'errors') and key == ord('r'):
                detail_browser.refresh()
                if future is None:
                    future = executor.submit(loader)
                else:
                    force_snapshot = True
                force_resource = True
                continue
            if focus == 'content' and view in ('N', 'Z') and detail_browser.item and key == ord('r'):
                detail_browser.refresh()
                continue
            if focus == 'content' and (rv.menu_group(view) == 'Resources' or (view == 'Z' and key in (ord('n'), ord('D')))):
                action = {ord('n'): 'new', ord('D'): 'drafts', ord('e'): 'edit', ord('a'): 'actions', ord('!'): 'exec'}.get(key)
                if action and (action in ('new', 'drafts') or detail_browser.item):
                    if action_handler:
                        item = detail_browser.item or (resource_detail.target(selectable, resource_rows[resource_browser.indices[selection_key]]) if resource_rows else None)
                        if action in ('new', 'drafts'):
                            creation_resource = selectable
                            if is_catalog:
                                entries = catalog_matches(data, catalog_query) if data else []
                                creation_resource = entries[min(catalog_index, len(entries) - 1)] if entries else None
                            if not creation_resource:
                                continue
                            # Creation belongs to the active resource type, even in an empty list.
                            item = dict(resource=creation_resource['name'], namespaced=creation_resource['namespaced'],
                                        namespace=(item or {}).get('namespace'), name=None)
                        terminal_action(stdscr, lambda: action_handler(action, item))
                        detail_browser.refresh(all_modes=True)
                        force_resource = True
                        if future is None or future.done():
                            future = executor.submit(loader)
                        else:
                            force_snapshot = True
                        cached_key, last_draw = None, None
                    continue
                if detail_browser.item and key == ord('r'):
                    detail_browser.refresh()
                    continue
                if detail_browser.item and key == ord('p'):
                    detail_browser.previous()
                    offset = 0
                    continue
            if key in (ord('q'), ord('c')):
                return 'configs' if key == ord('c') else 'quit'
            if key in (curses.KEY_MOUSE, 27):
                direction = wheel_direction(stdscr, key)
                if direction is None:
                    if focus == 'content' and detail_browser.topology_back():
                        offset = 0
                    elif detail_browser.item and focus == 'content':
                        offset = detail_browser.saved_offset
                        detail_browser.close()
                        if view in ('4', '5'):
                            event_browser.detail = None
                    elif view in ('4', '5') and event_browser.state(view)['detail'] and focus == 'content':
                        event_browser.detail = None
                        offset = event_browser.saved_offset
                    elif view == '0' and api_resource and focus == 'content':
                        api_resource, offset = None, 0
                    elif focus == 'content':
                        focus, selected = 'nav', '1' if view == '2' else view
                        nav_group = rv.menu_group(view)
                elif focus == 'content':
                    if is_topology_list:
                        detail_browser.topology_move(direction * scroll_step)
                    elif is_resource_list:
                        resource_browser.move(selection_key, resource_rows, direction * scroll_step)
                    elif is_event_list:
                        event_browser.move(view, event_rows, direction * scroll_step)
                    elif view == '0' and api_resource is None:
                        catalog_index = max(0, catalog_index + direction * scroll_step)
                    else:
                        offset = max(0, offset + direction * scroll_step)
                continue
            if key in map(ord, '0123456789msNZ'):
                selected, focus = ('1' if key == ord('2') else chr(key)), 'nav'
                if key in (ord('1'), ord('2')):
                    overview_view = chr(key)
                nav_group = rv.menu_group(selected)
            elif key in (10, 13, curses.KEY_ENTER):
                if focus == 'nav':
                    if selected == 's':
                        return 'configs'
                    event_browser.detail = None
                    detail_browser.close()
                    view, offset, focus = overview_view if selected == '1' else selected, 0, 'content'
                elif is_topology_list:
                    detail_browser.topology_enter()
                    offset = 0
                elif is_resource_list and resource_rows:
                    detail_browser.open(resource_detail.target(selectable, resource_rows[resource_browser.indices[selection_key]]), offset)
                    offset = 0
                elif is_event_list:
                    event_browser.open(view, event_rows, offset)
                    if event_browser.detail:
                        detail_browser.open_event(event_browser.detail[1], view, offset)
                        offset = 0
                elif view == '0' and api_resource is None and data:
                    entries = catalog_matches(data, catalog_query)
                    if entries:
                        api_resource, offset = entries[min(catalog_index, len(entries) - 1)], 0
            elif key in (curses.KEY_DOWN, ord('j'), curses.KEY_UP, ord('k')):
                step = 1 if key in (curses.KEY_DOWN, ord('j')) else -1
                if focus == 'nav':
                    order = [key for _, keys in rv.MENU_GROUPS for key in keys]
                    selected = order[(order.index(selected) + step) % len(order)]
                    nav_group = rv.menu_group(selected)
                elif is_topology_list:
                    detail_browser.topology_move(step)
                elif is_resource_list:
                    resource_browser.move(selection_key, resource_rows, step)
                elif is_event_list:
                    event_browser.move(view, event_rows, step)
                elif view == '0' and api_resource is None:
                    catalog_index = max(0, catalog_index + step)
                else:
                    offset = max(0, offset + step)
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT) and focus == 'content':
                step = 1 if key == curses.KEY_RIGHT else -1
                if detail_browser.item:
                    detail_browser.switch(step)
                    offset = 0
                elif view in ('1', '2'):
                    view = overview_view = '2' if view == '1' else '1'
                    offset = 0
                elif view == 'm':
                    monitoring_tab = (monitoring_tab + step) % len(MONITORING_TABS)
                    offset = 0
                elif view == '3':
                    tab = (tab + step) % len(WORKLOAD_KINDS)
                    offset = 0
                elif view in rv.CATEGORIES:
                    category_tabs[view] = (category_tabs[view] + step) % len(rv.CATEGORIES[view])
                    offset = 0
            elif key in (curses.KEY_NPAGE, ord('d'), curses.KEY_PPAGE, ord('u')) and focus == 'content':
                step = visible if key in (curses.KEY_NPAGE, ord('d')) else -visible
                if is_topology_list:
                    detail_browser.topology_move(max(1, visible // 2) * (1 if step > 0 else -1))
                elif is_resource_list:
                    resource_browser.move(selection_key, resource_rows, max(1, visible // 2) * (1 if step > 0 else -1))
                elif is_event_list:
                    event_browser.move(view, event_rows, max(1, visible // 2) * (1 if step > 0 else -1))
                elif view == '0' and api_resource is None:
                    catalog_index = max(0, catalog_index + step)
                else:
                    offset = max(0, offset + step)
            elif key == ord('/') and focus == 'content' and (can_filter or is_catalog):
                focus, draft, filter_error = 'filter', (event_queries[view] if view in ('4', '5') else monitor_queries.get(monitoring_tab, '') if view == 'm' else catalog_query if is_catalog else
                    resource_queries.get(request['name'], '') if request else queries[kind]), ''
            elif key == ord('t') and focus == 'content' and view == 'm':
                monitor_period = monitoring.PERIODS[(monitoring.PERIODS.index(monitor_period) + 1) % len(monitoring.PERIODS)]
                monitor_force, offset = True, 0
            elif key == ord('p') and focus == 'content' and view == 'm':
                monitor_paused = not monitor_paused
                monitor_force = not monitor_paused
            elif key == ord('x') and focus == 'content' and not detail_browser.item:
                if view in ('4', '5'):
                    event_filters[view], event_queries[view], offset = {}, '', 0
                    event_browser.indices[view], event_browser.ids[view] = 0, None
                elif view == 'm':
                    monitor_filters[monitoring_tab], monitor_queries[monitoring_tab], offset = {}, '', 0
                    monitor_force = True
                elif view == '3':
                    filters[kind], queries[kind], offset = {}, '', 0
                elif request:
                    resource_filters[request['name']], resource_queries[request['name']], offset = {}, '', 0
                elif is_catalog:
                    catalog_query, catalog_index, offset = '', 0, 0
            elif key == ord('r'):
                monitor_force = view == 'm'
                if future is None:
                    future = executor.submit(loader)
                force_resource = True
                if view == '0' and discovery_future is None and discovery_loader:
                    discovery_future = executor.submit(discovery_loader)
    finally:
        sys.stdout.write('\x1b[?1006l\x1b[?1000l')
        sys.stdout.flush()
        if native_mouse:
            curses.mousemask(0)
        executor.shutdown(wait=True, cancel_futures=True)
        detail_executor.shutdown(wait=True, cancel_futures=True)


def identity(client, config, requested_context):
    cfg, error = client.call('config', 'view', '-o', 'json')
    if error:
        raise ValueError('Cannot read kubeconfig: ' + error)
    contexts = rv.field(cfg, 'contexts', [])
    name = requested_context or cfg.get('current-context')
    if not name:
        if len(contexts) == 1:
            name = contexts[0]['name']
        elif contexts:
            try:
                selected = profiles.choose_connection(cfg)
            except profiles.InputCancelled:
                return None
            if selected is None:
                return None
            name = selected['name']
        else:
            raise ValueError('No contexts in kubeconfig.')
    context = next((c['context'] for c in contexts if c['name'] == name), None)
    if context is None:
        raise ValueError('Context not found: ' + name)
    cluster = context['cluster']
    server = next((rv.field(c['cluster'], 'server', 'N/A') for c in rv.field(cfg, 'clusters', []) if c['name'] == cluster), 'N/A')
    client.base += ['--context', name] if not requested_context else []
    return {'cluster': cluster, 'context': name, 'server': server, 'config': str(config)}


def main():
    parser = argparse.ArgumentParser(prog='kasciidash', description=__doc__)
    parser.add_argument('--demo', action='store_true', help='Synthetic data; no cluster calls')
    parser.add_argument('--once', action='store_true', help='Print one snapshot without curses')
    parser.add_argument('--config', type=Path, help='Explicit kubeconfig path')
    parser.add_argument('--context', help='Context within selected file')
    parser.add_argument('--profile', help='Registered profile alias or ID')
    parser.add_argument('--interval', type=int, default=30, help='Auto refresh seconds (default 30)')
    parser.add_argument('--metrics-interval', type=int, default=5, help='Prometheus refresh seconds (default 5)')
    parser.add_argument('--timeout', type=int, default=8, help='Per kubectl timeout seconds')
    parser.add_argument('--scroll-step', type=int, default=5, help='Lines per mouse wheel event (default 5)')
    parser.add_argument('--width', type=int, default=120, help='Output width for --once')
    args = parser.parse_args()
    if args.metrics_interval < 1 or args.interval < 1 or args.timeout < 1 or args.scroll_step < 1 or args.width < 40:
        parser.error('metrics-interval/interval/timeout/scroll-step must be positive; width must be >= 40')
    if not args.demo and not shutil.which('kubectl'):
        parser.error('kubectl is required in PATH')
    if not args.once and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        parser.error('Interactive dashboard requires a terminal. Use --once for text output.')
    if args.config and args.profile:
        parser.error('Use --config or --profile, not both.')
    try:
        store, selected_profile = None, None
        client_factory = lambda path, context: Client(path, context, args.timeout)
        if not args.demo and not args.config:
            store = profiles.Store(BASE, resolver=profiles.choose_registry if not args.once else None)
            if args.profile:
                selected_profile = next((p for p in store.items if args.profile in (p['id'], p['alias'])), None)
                if selected_profile is None:
                    raise ValueError('Profile not found: ' + args.profile)
            elif store.items:
                selected_profile = store.preferred()
            elif args.once:
                raise ValueError('No profiles. Run ./kasciidash to register a kubeconfig.')
            else:
                selected_profile = profiles.manage(store, None, client_factory, args.timeout)
                if selected_profile is None:
                    return 0
        while True:
            monitoring_loader = None
            if args.demo:
                detail_loader = resource_detail.demo
                action_handler = lambda action, item: resource_actions.demo_action(action, item)
                loader = demo
                resource_loader = rv.demo_resource
                discovery_loader = rv.demo_catalog
            else:
                config = args.config.expanduser().resolve() if args.config else store.config_path(selected_profile)
                context = args.context or (selected_profile or {}).get('context')
                try:
                    if not config.is_file():
                        raise ValueError('Config file not found: ' + str(config))
                    client = client_factory(config, context)
                    info = identity(client, config, context)
                    if info is None:
                        return 0
                    if selected_profile:
                        if not selected_profile.get('context'):
                            selected_profile = store.update(selected_profile['id'], context=info['context'])
                        info.update(alias=selected_profile['alias'],
                                    prometheus=(selected_profile.get('prometheus') or {}) if info['context'] == selected_profile.get('context') else {})
                except (ValueError, OSError) as exc:
                    if args.once:
                        raise
                    profiles.clear()
                    print('KAsciiDash: ' + clean(exc))
                    input('Enter to manage profiles: ')
                    store = store or profiles.Store(BASE, resolver=profiles.choose_registry if not args.once else None)
                    selected_profile = profiles.manage(store, selected_profile, client_factory, args.timeout)
                    if selected_profile is None:
                        return 0
                    args.config, args.context = None, None
                    continue
                detail_loader = lambda item, mode: resource_detail.load(client, item, mode)
                action_handler = lambda action, item: resource_actions.perform(client, BASE, info, action, item)
                loader = lambda: collect(client, info)
                resource_loader = lambda name, scope: rv.list_resource(client, name, scope)
                discovery_loader = lambda: rv.discover(client)
                if (info.get('prometheus') or {}).get('endpoint'):
                    monitoring_loader = monitoring.Reader(info['prometheus'], args.timeout)
            if args.once:
                data = loader()
                print('\n'.join(frame(data, width=args.width)))
                return 1 if data['nodes'] is None else 0
            if selected_profile and not args.demo:
                store.remember(selected_profile['id'])
            profiles.clear()
            result = curses.wrapper(screen, loader, args.interval, args.scroll_step, resource_loader, discovery_loader, monitoring_loader, args.metrics_interval, detail_loader, action_handler)
            if result == 'quit':
                return 0
            store = store or profiles.Store(BASE, resolver=profiles.choose_registry if not args.once else None)
            selected_profile = profiles.manage(store, selected_profile, client_factory, args.timeout)
            if selected_profile is None:
                return 0
            args.config, args.context, args.demo = None, None, False
    except (KeyboardInterrupt, EOFError, profiles.InputCancelled):
        return 0
    except (ValueError, OSError, curses.error) as exc:
        print('KAsciiDash: ' + clean(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
