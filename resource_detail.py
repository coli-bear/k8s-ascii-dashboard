"""Read-only kubectl object detail views; buffers live only while a detail is open."""
import copy
import json
import re
import textwrap
import node_views
import namespace_views


def identity(obj):
    meta = obj.get('metadata') or {}
    return meta.get('uid') or (meta.get('namespace', ''), meta.get('name', ''))


def sort_objects(objects):
    return sorted(objects or [], key=lambda obj: ((obj.get('metadata') or {}).get('namespace') or '', (obj.get('metadata') or {}).get('name') or ''))


class Selection:
    def __init__(self):
        self.indices, self.ids = {}, {}

    def reconcile(self, key, rows):
        index = next((i for i, obj in enumerate(rows) if identity(obj) == self.ids.get(key)), self.indices.get(key, 0))
        self.indices[key] = min(index, max(0, len(rows)-1))
        self.ids[key] = identity(rows[self.indices[key]]) if rows else None
        return self.indices[key]

    def move(self, key, rows, step):
        index = max(0, min(len(rows)-1, self.indices.get(key, 0)+step))
        self.indices[key] = index
        self.ids[key] = identity(rows[index]) if rows else None


def target(resource, obj):
    meta = obj.get('metadata') or {}
    return dict(resource=resource['name'], namespaced=resource['namespaced'], name=meta.get('name'),
                namespace=meta.get('namespace') if resource['namespaced'] else None, uid=meta.get('uid'), read_only=resource.get('read_only', False))


LOG_RESOURCES = {'pods', 'deployments', 'deployments.apps', 'replicasets', 'replicasets.apps',
                 'statefulsets', 'statefulsets.apps', 'daemonsets', 'daemonsets.apps', 'jobs', 'jobs.batch'}


def selector_text(selector):
    parts = [str(k) + '=' + str(v) for k, v in sorted((selector.get('matchLabels') or {}).items())]
    for item in selector.get('matchExpressions') or []:
        key, operator, values = item.get('key'), item.get('operator'), item.get('values') or []
        if operator in ('In', 'NotIn') and values:
            parts.append(key + (' in (' if operator == 'In' else ' notin (') + ','.join(values) + ')')
        elif operator in ('Exists', 'DoesNotExist'):
            parts.append(('!' if operator == 'DoesNotExist' else '') + key)
        else:
            raise ValueError('Unsupported or empty workload selector expression.')
    if not parts:
        raise ValueError('Workload selector is empty; refusing to query all namespace logs.')
    return ','.join(parts)


def load(client, item, mode):
    resource, name, namespace = item['resource'], item['name'], item.get('namespace')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', resource or '') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', name or ''):
        return None, 'Invalid resource or object name. Refresh the list.'
    if item['namespaced'] and not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', namespace or ''):
        return None, 'Namespace is missing or invalid. Refresh the list.'
    scope = ['--namespace', namespace] if item['namespaced'] else []
    if mode == 'status' and resource == 'nodes':
        return node_views.load(client, item)
    if resource == 'namespaces' and mode in namespace_views.MODES:
        return namespace_views.load(client, item)
    if mode == 'yaml':
        return client.text('get', resource, name, *scope, '-o', 'yaml')
    if mode in ('logs', 'logs_previous'):
        if resource not in LOG_RESOURCES:
            return None, 'Logs are available for Pods and supported workload controllers.'
        options = ['--all-containers=true', '--prefix=true', '--timestamps=true', '--tail=200',
                   '--limit-bytes=262144', '--pod-running-timeout=5s']
        if mode == 'logs_previous':
            options.append('--previous=true')
        if resource == 'pods':
            return client.text('logs', name, *scope, *options)
        obj, error = client.call('get', resource, name, *scope, '-o', 'json')
        if error:
            return None, error
        try:
            spec = (obj or {}).get('spec') or {}
            selector = selector_text(spec.get('selector') or {})
        except (ValueError, TypeError, AttributeError) as exc:
            return None, str(exc)
        return client.text('logs', '--selector', selector, *scope, *options)
    if mode != 'describe':
        return None, 'Unknown detail format.'
    # describe TYPE NAME may fall back to prefix matches when NAME was deleted.
    # An exact GET plus a minimal manifest on stdin avoids describing neighbours.
    value, error = client.call('get', resource, name, *scope, '-o', 'json')
    if error:
        return None, error
    if not isinstance(value, dict) or not value.get('apiVersion') or not value.get('kind'):
        return None, 'Object API version/kind is unavailable.'
    meta = value.get('metadata') or {}
    if meta.get('name') != name or (item['namespaced'] and meta.get('namespace') != namespace):
        return None, 'Returned object does not match the selected row. Refresh the list.'
    manifest = dict(apiVersion=value['apiVersion'], kind=value['kind'], metadata={'name': name})
    if item['namespaced']:
        manifest['metadata']['namespace'] = namespace
    return client.text('describe', '-f', '-', *scope, input_text=json.dumps(manifest))


def demo(item, mode):
    if item['resource'] == 'namespaces' and mode in namespace_views.MODES:
        return namespace_views.demo(item['name']), None
    if mode == 'status' and item['resource'] == 'nodes':
        node = {'metadata': {'name': item['name'], 'labels': {'node-role.kubernetes.io/worker': ''}},
                'status': {'conditions': [{'type': 'Ready', 'status': 'True', 'reason': 'KubeletReady', 'message': 'DEMO: node is ready'}],
                           'capacity': {'cpu': '8', 'memory': '32Gi', 'pods': '110'}, 'allocatable': {'cpu': '7500m', 'memory': '30Gi', 'pods': '110'}}}
        return node_views.status_text(node, None, [], None, ['DEMO: live usage is not queried.']), None
    if mode in ('logs', 'logs_previous'):
        return '[DEMO pod/container] synthetic ' + ('previous' if mode == 'logs_previous' else 'current') + ' log line\n', None
    if mode == 'yaml':
        return '---\n# DEMO: synthetic object preview\nkind: DemoResource\nmetadata:\n  name: ' + json.dumps(item['name']) + ('\n  namespace: ' + json.dumps(item['namespace']) if item['namespaced'] else '') + '\n', None
    return 'DEMO: synthetic describe preview\nName: ' + item['name'] + '\nNamespace: ' + (item.get('namespace') or '<cluster>') + '\nResource: ' + item['resource'] + '\n', None


class Detail:
    def __init__(self):
        self.item, self.future = None, None
        self.generation, self.mode, self.saved_offset = 0, 'yaml', 0
        self.buffers = {}
        self.request = None

    def open(self, item, offset):
        self.generation += 1
        self.item, self.mode = copy.deepcopy(item), 'status' if item['resource'] == 'nodes' else 'overview' if item['resource'] == 'namespaces' else 'yaml'
        self.saved_offset, self.buffers = offset, {}

    def close(self):
        self.generation += 1
        self.item, self.buffers = None, {}
        if self.future:
            self.future.cancel()

    def switch(self, step=1):
        modes = (['status'] if self.item['resource'] == 'nodes' else list(namespace_views.MODES) if self.item['resource'] == 'namespaces' else []) + ['yaml', 'describe'] + (['logs'] if self.item['resource'] in LOG_RESOURCES else [])
        current = 'logs' if self.mode == 'logs_previous' else self.mode
        self.mode = modes[(modes.index(current) + step) % len(modes)]

    def previous(self):
        if self.mode in ('logs', 'logs_previous'):
            self.mode = 'logs_previous' if self.mode == 'logs' else 'logs'

    def refresh(self, all_modes=False):
        self.generation += 1
        if all_modes:
            self.buffers.clear()
        elif self.item and self.item['resource'] == 'namespaces' and self.mode in namespace_views.MODES:
            for mode in namespace_views.MODES:
                self.buffers.pop(mode, None)
        else:
            self.buffers.pop(self.mode, None)

    def poll(self, executor, loader):
        if self.future and self.future.done():
            if not self.future.cancelled() and self.request[0] == self.generation and self.item:
                try:
                    result = self.future.result()
                    self.buffers[self.request[1]] = result
                    if self.item['resource'] == 'namespaces' and self.request[1] in namespace_views.MODES and isinstance(result[0], dict):
                        for mode in namespace_views.MODES:
                            self.buffers[mode] = result
                except Exception as exc:
                    self.buffers[self.request[1]] = (None, 'Detail query failed: ' + str(exc))
            self.future = None
        if self.item and self.mode not in self.buffers and self.future is None:
            if loader:
                self.request = (self.generation, self.mode)
                self.future = executor.submit(loader, copy.deepcopy(self.item), self.mode)
            else:
                self.buffers[self.mode] = (None, 'Detail query is unavailable for this session.')

    def state(self):
        if not self.item:
            return None
        output, error = self.buffers.get(self.mode, (None, None))
        return dict(target=self.item, mode=self.mode, output=output, error=error,
                    loading=self.mode not in self.buffers, generation=self.generation)


def header(state, width, clean):
    item = state['target']
    mode = 'logs' if state['mode'] == 'logs_previous' else state['mode']
    tabs = (['Status'] if item['resource'] == 'nodes' else ['Overview', 'Resources', 'Topology'] if item['resource'] == 'namespaces' else []) + ['YAML', 'Describe'] + (['Logs'] if item['resource'] in LOG_RESOURCES else [])
    lines = [('[' + name + ']' if mode == name.lower() else name) for name in tabs]
    lines = ['  '.join(lines), 'Left/Right: detail tab | Esc: list | r: reload',
             item['resource'] + ' | ' + (item.get('namespace') + '/' if item.get('namespace') else '') + item['name'], '-' * width]
    operations = 'e: edit YAML | n: new YAML | a: operations'
    if item['resource'] in LOG_RESOURCES:
        operations += ' | !: exec'
    lines[-1:-1] = ['READ ONLY | u/d: page | r: refresh current tab' if item.get('read_only') else operations]
    if mode == 'logs':
        lines[-1:-1] = ['Logs: ' + ('PREVIOUS instance' if state['mode'] == 'logs_previous' else 'CURRENT instance') + ' | p: previous/current',
                        'Snapshot: last 200 lines, max 256 KiB per container | r: reload',
                        'All containers' + (' of selector-matching Pods' if item['resource'] != 'pods' else ' in selected Pod') + '; source prefixes included.']
    return [part for line in lines for part in textwrap.wrap(clean(line), width) or ['']]


def content(state, width, clean, table=None):
    if state['loading']:
        return ['Loading ' + state['mode'].upper() + '... | Esc: return to list']
    if isinstance(state.get('output'), dict) and state['target']['resource'] == 'namespaces':
        return namespace_views.render(state['output'], state['mode'], width, table, clean)
    if state['error']:
        raw = 'QUERY ERROR\n' + str(state['error']) + '\n\nPress r to retry or Esc to return to the list.'
        if state.get('output'):
            raw += '\n\nPARTIAL OUTPUT\n' + state['output']
    else:
        raw = state['output'] or '(No output)'
    return [chunk for line in raw.expandtabs(4).splitlines() for chunk in textwrap.wrap(clean(line), width, replace_whitespace=False) or ['']]
