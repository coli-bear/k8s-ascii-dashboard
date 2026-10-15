"""Workload selection and bounded relation scope shared by both entry points."""
import copy
import textwrap
from collections import defaultdict

from kuba_dm_app.topology import namespace_topology as graph
from kuba_dm_app.resources import resource_views as rv
WORKLOAD_RESOURCES = {'pods', 'deployments', 'deployments.apps', 'replicasets', 'replicasets.apps',
                      'statefulsets', 'statefulsets.apps', 'daemonsets', 'daemonsets.apps', 'jobs', 'jobs.batch', 'cronjobs', 'cronjobs.batch'}
KINDS = {'Deployment', 'ReplicaSet', 'StatefulSet', 'DaemonSet', 'Job', 'CronJob', 'Pod', 'Notebook'}
EXCLUDED = {'Profile', 'Namespace', 'Service', 'EndpointSlice', 'ConfigMap', 'Secret', 'ServiceAccount',
            'PersistentVolumeClaim', 'NetworkPolicy', 'ControllerRevision', 'Event', 'Role', 'RoleBinding'}


def meta(obj):
    return rv.field(obj, 'metadata', {})


def supports(item, obj=None):
    if item.get('topology_available') or (item.get('resource') or item.get('name')) in WORKLOAD_RESOURCES:
        return True
    obj = obj or {}
    template = rv.field(rv.field(rv.field(obj, 'spec', {}), 'template', {}), 'spec', {})
    return obj.get('kind') == 'Notebook' or bool(template.get('containers'))


def reference(obj):
    return dict(uid=meta(obj).get('uid'), name=meta(obj).get('name'), kind=obj.get('kind'),
                apiVersion=obj.get('apiVersion'), namespace=meta(obj).get('namespace'))


class Inventory:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.namespace = meta(snapshot['namespace']).get('name')
        self.by_uid = {}
        for rows in snapshot['objects'].values():
            for obj in rows or []:
                if meta(obj).get('uid') and meta(obj).get('namespace', self.namespace) == self.namespace:
                    self.by_uid.setdefault(meta(obj)['uid'], obj)
        self.by_name = {(graph.group(obj), obj.get('kind'), meta(obj).get('name')): obj for obj in self.by_uid.values()}
        self.children, self.parents = defaultdict(set), defaultdict(set)
        for uid, obj in self.by_uid.items():
            for owner in rv.field(meta(obj), 'ownerReferences', []):
                parent = self.by_uid.get(owner.get('uid'))
                if parent and (not owner.get('kind') or parent.get('kind') == owner['kind']) and (not owner.get('name') or meta(parent).get('name') == owner['name']) and (not owner.get('apiVersion') or graph.group(parent) == graph.group(owner)):
                    self.children[owner['uid']].add(uid)
                    self.parents[uid].add(owner['uid'])
        self.candidates = set()
        for uid, obj in self.by_uid.items():
            kind = obj.get('kind')
            api_group = graph.group(obj)
            built_in = kind in KINDS and api_group in ('', 'apps', 'batch', 'kubeflow.org')
            if kind not in EXCLUDED and (built_in or supports({}, obj)):
                self.candidates.add(uid)
        for uid, obj in self.by_uid.items():
            if obj.get('kind') == 'Pod' and graph.group(obj) == '':
                self.candidates.update(parent for parent in self.ancestors(uid) if self.by_uid[parent].get('kind') not in EXCLUDED)

    def traverse(self, uid, links):
        found, pending = set(), list(links.get(uid, ()))
        while pending:
            current = pending.pop()
            if current == uid or current in found:
                continue
            found.add(current)
            pending.extend(links.get(current, ()))
        return found

    def ancestors(self, uid):
        return self.traverse(uid, self.parents)

    def descendants(self, uid):
        return self.traverse(uid, self.children)

    def pods(self, uid):
        return [self.by_uid[key] for key in self.descendants(uid) | {uid}
                if self.by_uid[key].get('kind') == 'Pod' and graph.group(self.by_uid[key]) == '']

    def workloads(self):
        roots = [uid for uid in self.candidates if not (self.ancestors(uid) & self.candidates)]
        # A malformed cyclic owner graph must not silently remove every target.
        covered = set()
        for uid in roots:
            covered.update(self.descendants(uid) | {uid})
        for uid in sorted(self.candidates - covered):
            if uid not in covered:
                roots.append(uid)
                covered.update(self.descendants(uid) | {uid})
        return sorted((self.by_uid[uid] for uid in roots), key=lambda obj: (obj.get('kind', ''), meta(obj).get('name', '')))

    def selected(self, target):
        obj = self.by_uid.get(target.get('uid'))
        if not obj or meta(obj).get('name') != target.get('name') or (target.get('kind') and obj.get('kind') != target['kind']) or (target.get('namespace') and meta(obj).get('namespace', self.namespace) != target['namespace']) or (target.get('apiVersion') and graph.group(obj) != graph.group(target)):
            raise ValueError('The selected workload is missing or was replaced. Return to the workload list and select it again.')
        return obj


def focused(snapshot, target, selector_matches):
    inventory = Inventory(snapshot)
    root = inventory.selected(target)
    uid = meta(root)['uid']
    owned = inventory.descendants(uid) | {uid}
    pods = [root] if root.get('kind') == 'Pod' and graph.group(root) == '' else inventory.pods(uid)
    if root.get('kind') == 'Pod' and graph.group(root) == '':
        owned = {uid}
    # Going up to an owner never expands down into that owner's other Pods.
    core = (owned & inventory.candidates) | {meta(pod)['uid'] for pod in pods} | inventory.ancestors(uid) | {uid}
    keep = set(core)
    selected_pods = {meta(pod)['name']: pod for pod in pods}

    def endpoint_matches(endpoint):
        ref = rv.field(endpoint, 'targetRef', {})
        pod = selected_pods.get(ref.get('name'))
        return bool(pod and ref.get('kind') == 'Pod' and graph.group(ref) == '' and ref.get('namespace', inventory.namespace) == inventory.namespace and (not ref.get('uid') or ref['uid'] == meta(pod)['uid']))

    slices = {}
    for obj in inventory.by_uid.values():
        if obj.get('kind') != 'Service' or graph.group(obj) != '':
            continue
        matching = []
        for endpoint_slice in inventory.by_uid.values():
            if endpoint_slice.get('kind') == 'EndpointSlice' and graph.group(endpoint_slice) == 'discovery.k8s.io' and rv.field(meta(endpoint_slice), 'labels', {}).get('kubernetes.io/service-name') == meta(obj).get('name'):
                endpoints = [endpoint for endpoint in rv.field(endpoint_slice, 'endpoints', []) if endpoint_matches(endpoint)]
                if endpoints:
                    matching.append((endpoint_slice, endpoints))
        selector = rv.field(rv.field(obj, 'spec', {}), 'selector', {})
        candidate = bool(selector) and any(all(rv.field(meta(pod), 'labels', {}).get(k) == value for k, value in selector.items()) for pod in pods)
        if matching or candidate or meta(obj)['uid'] in owned:
            keep.add(meta(obj)['uid'])
            for endpoint_slice, endpoints in matching:
                keep.add(meta(endpoint_slice)['uid'])
                slices[meta(endpoint_slice)['uid']] = endpoints
    for pod in pods:
        for kind, name in graph.pod_references(pod):
            obj = inventory.by_name.get(('', kind, name))
            if obj:
                keep.add(meta(obj)['uid'])
    result = dict(namespace=copy.deepcopy(snapshot['namespace']), objects={}, errors=dict(snapshot.get('errors') or {}), stamp=snapshot.get('stamp', '-'), focus_uid=uid)
    seen = set()
    for resource, rows in snapshot['objects'].items():
        selected = []
        for obj in rows or []:
            identity = meta(obj).get('uid')
            if identity not in keep or identity in seen:
                continue
            seen.add(identity)
            value = copy.deepcopy(obj)
            if identity not in core:
                value['metadata']['ownerReferences'] = [owner for owner in rv.field(meta(value), 'ownerReferences', []) if owner.get('uid') in keep]
            if identity in slices:
                value['endpoints'] = copy.deepcopy(slices[identity])
            selected.append(value)
        if selected:
            result['objects'][resource] = selected
    return result


def render(snapshot, target, width, table, clean, selector_matches, index=0):
    if table is None:
        table = lambda headers, rows, width, **kwargs: [' | '.join(map(str, row)) for row in [headers] + rows]
    if target is None:
        inventory = Inventory(snapshot)
        rows = inventory.workloads()
        lines = ['WORKLOADS | Total: %s' % len(rows), 'Keys: Up/Down select | Enter: selected workload topology | Esc: namespace list',
                 'Scope: top-level workload owners; standalone Pods are included.']
        if rows:
            values = []
            for obj in rows:
                pods = inventory.pods(meta(obj)['uid'])
                ready = sum(not meta(pod).get('deletionTimestamp') and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in rv.field(rv.field(pod, 'status', {}), 'conditions', [])) for pod in pods)
                def cut(value, size):
                    value = str(value)
                    return value if len(value) <= size else value[:size-3] + '...'
                if width >= 65:
                    values.append([cut(meta(obj).get('name', '?'), max(8, width - 49)), cut(obj.get('kind', '?'), 18), len(pods), '%s/%s' % (ready, len(pods))])
                else:
                    values.append([cut(meta(obj).get('name', '?'), max(5, width - 23)), cut(obj.get('kind', '?'), 10)])
            lines += table(['NAME', 'KIND', 'PODS', 'READY'] if width >= 65 else ['NAME', 'KIND'], values, width, stretch=True, selected=min(index, len(rows)-1))
        else:
            lines.append('No visible workloads. Check collection errors or refresh with r.')
    else:
        try:
            subset = focused(snapshot, target, selector_matches)
            lines = ['FOCUS: [%s] %s' % (target.get('kind') or 'Workload', target.get('name')), 'Scope: this workload, its Pods, owner chain, related Services and referenced configuration.']
            pods = [obj for rows in subset['objects'].values() for obj in rows if obj.get('kind') == 'Pod' and graph.group(obj) == '']
            ready = sum(not meta(pod).get('deletionTimestamp') and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in rv.field(rv.field(pod, 'status', {}), 'conditions', [])) for pod in pods)
            lines.append('Pods: %s | Ready: %s/%s | * marks the selected workload' % (len(pods), ready, len(pods)))
            lines += graph.render(subset, width, selector_matches)
        except ValueError as exc:
            lines = ['WORKLOAD UNAVAILABLE', str(exc)]
    if snapshot.get('errors'):
        lines += ['', 'PARTIAL DATA / QUERY ERRORS'] + [resource + ': ' + str(error) for resource, error in sorted(snapshot['errors'].items())]
    return [part for line in lines for part in textwrap.wrap(clean(line), width, replace_whitespace=False) or ['']]
