"""Namespace inventory and configuration topology, fetched only on demand."""
from collections import Counter, defaultdict
import concurrent.futures
import datetime as dt
import re
import textwrap

import resource_views as rv

MODES = ('overview', 'resources', 'topology')
COMMON = ('pods', 'deployments.apps', 'replicasets.apps', 'statefulsets.apps',
          'daemonsets.apps', 'jobs.batch', 'cronjobs.batch', 'services',
          'endpointslices.discovery.k8s.io', 'ingresses.networking.k8s.io',
          'networkpolicies.networking.k8s.io', 'persistentvolumeclaims',
          'configmaps', 'secrets', 'resourcequotas', 'limitranges',
          'serviceaccounts', 'roles.rbac.authorization.k8s.io',
          'rolebindings.rbac.authorization.k8s.io', 'events')


def meta(obj):
    return rv.field(obj, 'metadata', {})


def phase(obj):
    return rv.field(rv.field(obj, 'status', {}), 'phase', 'Unknown')


def matches(data, filters):
    objects = (data.get('extras') or {}).get('namespaces')
    filters = dict(filters or {})
    namespace_filter = filters.pop('ns', '')
    if objects is None:
        return None
    rows = rv.filter_items(objects, filters, rv.status_text)
    return sorted((obj for obj in rows if namespace_filter.casefold() in meta(obj).get('name', '').casefold()), key=lambda obj: meta(obj).get('name', ''))


def header(data, width, filters, clean):
    rows = matches(data, filters)
    objects = (data.get('extras') or {}).get('namespaces')
    lines = ['NAMESPACES | Total: %s | Matches: %s' % ('N/A' if objects is None else len(objects), 'N/A' if rows is None else len(rows)),
             'Up/Down: select | Enter: Overview/Resources/Topology/YAML/Describe',
             '/: filter name=... status=... | x: clear | r: refresh | Esc: menu',
             'Filter: ' + (' '.join('%s=%s' % pair for pair in (filters or {}).items()) or '(none)'), '-' * width]
    return [part for line in lines for part in textwrap.wrap(clean(line), width) or ['']]


def list_lines(data, width, filters, table):
    if 'namespaces' not in (data.get('extras') or {}):
        return ['Loading namespaces...']
    rows = matches(data, filters)
    if rows is None:
        return ['Namespaces unavailable: ' + str((data.get('resource_errors') or {}).get('namespaces', 'Unknown error'))]
    if not rows:
        return ['No matching namespaces.' if filters else 'No namespaces.']
    counts = Counter(meta(p).get('namespace') for p in data.get('pods') or [])
    values = [[meta(obj).get('name', '-'), phase(obj), 'N/A' if data.get('pods') is None else counts[meta(obj).get('name')], rv.age(obj)] for obj in rows]
    selected = (data.get('resource_state') or {}).get('index')
    if width >= 65:
        return table(['NAME', 'STATE', 'PODS', 'AGE'], values, width, selected=selected, stretch=True)
    return [line for i, row in enumerate(values) for line in table(['> FIELD' if i == selected else 'FIELD', 'VALUE'], list(map(list, zip(('NAME', 'STATE', 'PODS', 'AGE'), row))), width)]


def load(client, item):
    name = item['name']
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name or ''):
        return None, 'Invalid namespace name.'
    namespace, error = client.call('get', 'namespaces', name, '-o', 'json')
    if error:
        return None, error
    if meta(namespace).get('name') != name or (item.get('uid') and meta(namespace).get('uid') != item['uid']):
        return None, 'The namespace was replaced. Refresh the list.'
    errors = {}
    output, error = client.text('api-resources', '--namespaced=true', '--verbs=list', '-o', 'name')
    if error:
        errors['discovery'] = str(error) + ' (common resources still queried)'
    discovered = [resource.strip() for resource in (output or '').splitlines()
                  if re.fullmatch(r'[a-z0-9][a-z0-9.-]*', resource.strip())]
    resources = sorted(set(COMMON) | set(discovered))
    objects = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {resource: pool.submit(rv.list_resource, client, resource, True, name) for resource in resources}
        for resource, job in jobs.items():
            try:
                values, error = job.result()
            except Exception as exc:
                values, error = None, str(exc)
            objects[resource] = None if error else [obj for obj in values or [] if meta(obj).get('namespace') == name]
            if error:
                errors[resource] = str(error)
    # A deleted/recreated namespace must not inherit the old namespace's view.
    current, error = client.call('get', 'namespaces', name, '-o', 'json')
    if error or meta(current).get('uid') != meta(namespace).get('uid'):
        return None, error or 'The namespace changed identity during collection. Refresh the list.'
    return dict(namespace=current, objects=objects, errors=errors,
                stamp=dt.datetime.now(dt.timezone.utc).isoformat()), None


def state(obj):
    parts = [rv.status_text(obj)]
    status = rv.field(obj, 'status', {})
    for container in rv.field(status, 'initContainerStatuses', []) + rv.field(status, 'containerStatuses', []):
        waiting = rv.field(rv.field(container, 'state', {}), 'waiting', {})
        if waiting.get('reason'):
            parts.append(waiting['reason'])
    if obj.get('kind') in ('Deployment', 'StatefulSet', 'ReplicaSet'):
        parts.append('Ready %s/%s' % (status.get('readyReplicas', 0), rv.field(obj, 'spec', {}).get('replicas', 1)))
    return ' / '.join(part for part in parts if part != '-') or '-'


def selector_matches(labels, selector):
    for key, value in rv.field(selector, 'matchLabels', {}).items():
        if labels.get(key) != value:
            return False
    for expr in rv.field(selector, 'matchExpressions', []):
        key, operator, values = expr.get('key'), expr.get('operator'), expr.get('values') or []
        if operator == 'In' and (key not in labels or labels[key] not in values): return False
        if operator == 'NotIn' and labels.get(key) in values: return False
        if operator == 'Exists' and key not in labels: return False
        if operator == 'DoesNotExist' and key in labels: return False
        if operator not in ('In', 'NotIn', 'Exists', 'DoesNotExist'): return False
    return True


def topology(snapshot):
    """A relation tree, never a claim of observed traffic or policy reachability."""
    namespace = meta(snapshot['namespace']).get('name')
    objects = snapshot['objects']
    # API aliases/served versions can return the same object; deduplicate by UID.
    unique = {}
    for resource, rows in objects.items():
        for obj in rows or []:
            key = meta(obj).get('uid') or (obj.get('apiVersion'), obj.get('kind'), meta(obj).get('name'))
            unique.setdefault(key, obj)
    all_objects = list(unique.values())
    by_uid = {meta(obj)['uid']: obj for obj in all_objects if meta(obj).get('uid')}
    def api_group(obj):
        version = obj.get('apiVersion', '')
        return version.split('/')[0] if '/' in version else ''

    def native(obj, kind, group=''):
        return obj.get('kind') == kind and api_group(obj) == group

    by_name = {(api_group(obj), obj.get('kind'), meta(obj).get('name')): obj for obj in all_objects}
    pods = [obj for obj in all_objects if native(obj, 'Pod')]
    pod_names = {meta(obj).get('name'): obj for obj in pods}
    children, roots, missing = defaultdict(list), [], []

    def key(obj):
        return meta(obj).get('uid') or (obj.get('apiVersion'), obj.get('kind'), meta(obj).get('name'))

    def label(obj):
        value = str(obj.get('kind', 'Resource')) + '/' + str(meta(obj).get('name', '?'))
        if obj.get('kind') == 'Pod':
            value += ' [%s; node=%s]' % (state(obj), rv.field(obj, 'spec', {}).get('nodeName', 'unassigned'))
        return value

    for obj in all_objects:
        owners = rv.field(meta(obj), 'ownerReferences', [])
        owner = next((ref for ref in owners if ref.get('controller')), owners[0] if owners else None)
        if not owner:
            roots.append(obj)
        elif owner.get('uid') in by_uid:
            children[key(by_uid[owner['uid']])].append(obj)
        else:
            missing.append((owner, obj))
    lines = ['NAMESPACE ' + str(namespace), 'CONFIGURATION TOPOLOGY (not observed traffic)',
             'ownerReferences: UID links | Service: selector candidates / EndpointSlice targets',
             'References marked unavailable can mean RBAC denial, deletion, or missing API data.', '', 'OWNERSHIP']
    visited = set()
    limit = 2500

    def walk(obj, prefix='', path=()):
        identity = key(obj)
        if len(lines) >= limit:
            return
        lines.append(prefix + '+- ' + label(obj) + (' [cycle]' if identity in path else ''))
        visited.add(identity)
        if identity in path:
            return
        if len(path) >= 12:
            lines.append(prefix + '   ... depth limit; see Resources for descendants')
            return
        for child in sorted(children[identity], key=label):
            walk(child, prefix + '   ', path + (identity,))

    for obj in sorted(roots, key=label):
        if children[key(obj)] or obj.get('kind') in ('Pod', 'Deployment', 'ReplicaSet', 'StatefulSet', 'DaemonSet', 'Job', 'CronJob'):
            walk(obj)
    for owner, obj in sorted(missing, key=lambda pair: label(pair[1])):
        lines.append('+- [owner unavailable: %s/%s; UID=%s]' % (owner.get('kind'), owner.get('name'), owner.get('uid', '?')))
        walk(obj, '   ')
    for obj in sorted(all_objects, key=label):
        if key(obj) not in visited and children[key(obj)]:
            walk(obj)

    def ref(kind, name):
        return '%s/%s%s' % (kind, name, '' if ('', kind, name) in by_name else ' [unavailable / not found]')

    lines += ['', 'INGRESS / SERVICE / POD']
    services = sorted((obj for obj in all_objects if native(obj, 'Service')), key=label)
    for ingress in sorted((obj for obj in all_objects if native(obj, 'Ingress', 'networking.k8s.io')), key=label):
        lines.append('+- ' + label(ingress))
        spec = rv.field(ingress, 'spec', {})
        backends = [('default', rv.field(spec, 'defaultBackend', {}))] if spec.get('defaultBackend') else []
        for rule in rv.field(spec, 'rules', []):
            for route in rv.field(rv.field(rule, 'http', {}), 'paths', []):
                backends.append((str(rule.get('host', '*')) + str(route.get('path', '/')), rv.field(route, 'backend', {})))
        for route, backend in backends:
            service = rv.field(backend, 'service', {})
            if service.get('name'):
                lines.append('   +- %s -> %s port=%s' % (route, ref('Service', service['name']), rv.field(service, 'port', {}).get('number', rv.field(service, 'port', {}).get('name', '?'))))
            elif backend.get('resource'):
                lines.append('   +- %s -> resource backend %s' % (route, backend['resource']))
    for service in services:
        lines.append('+- ' + label(service))
        spec = rv.field(service, 'spec', {})
        if spec.get('type') == 'ExternalName':
            lines.append('   +- external DNS: ' + str(spec.get('externalName', '?')))
            continue
        selector = rv.field(spec, 'selector', {})
        candidates = [pod for pod in pods if selector and all(rv.field(meta(pod), 'labels', {}).get(k) == v for k, v in selector.items())]
        for pod in sorted(candidates, key=label):
            lines.append('   +- [selector candidate] ' + label(pod))
        slices = [obj for obj in all_objects if native(obj, 'EndpointSlice', 'discovery.k8s.io') and rv.field(meta(obj), 'labels', {}).get('kubernetes.io/service-name') == meta(service).get('name')]
        for endpoint_slice in sorted(slices, key=label):
            for endpoint in rv.field(endpoint_slice, 'endpoints', []):
                target = rv.field(endpoint, 'targetRef', {})
                pod = pod_names.get(target.get('name')) if target.get('kind') == 'Pod' and target.get('namespace', namespace) == namespace else None
                if pod and target.get('uid') and meta(pod).get('uid') != target['uid']:
                    pod = None
                value = label(pod) if pod else ('%s/%s [unresolved target]' % (target.get('kind'), target.get('name')) if target else ','.join(rv.field(endpoint, 'addresses', [])))
                ready = rv.field(endpoint, 'conditions', {}).get('ready')
                lines.append('   +- [endpoint ready=%s; slice=%s] %s' % ('unknown' if ready is None else ready, meta(endpoint_slice).get('name'), value))
        if not candidates and not slices:
            lines.append('   +- No visible Pod targets (empty selector, no match, or unavailable data).')
    if not services:
        lines.append('No visible Services.')
    lines += ['', 'POD RESOURCE REFERENCES']
    for pod in sorted(pods, key=label):
        spec = rv.field(pod, 'spec', {})
        references = set()
        for volume in rv.field(spec, 'volumes', []):
            for source, kind, field in [('persistentVolumeClaim', 'PersistentVolumeClaim', 'claimName'), ('configMap', 'ConfigMap', 'name'), ('secret', 'Secret', 'secretName')]:
                name = rv.field(volume, source, {}).get(field)
                if name: references.add((kind, name))
            for source in rv.field(rv.field(volume, 'projected', {}), 'sources', []):
                for source_type, kind in [('configMap', 'ConfigMap'), ('secret', 'Secret')]:
                    name = rv.field(source, source_type, {}).get('name')
                    if name: references.add((kind, name))
        for container in rv.field(spec, 'containers', []) + rv.field(spec, 'initContainers', []) + rv.field(spec, 'ephemeralContainers', []):
            for env in rv.field(container, 'env', []):
                for source, kind in [('configMapKeyRef', 'ConfigMap'), ('secretKeyRef', 'Secret')]:
                    name = rv.field(rv.field(env, 'valueFrom', {}), source, {}).get('name')
                    if name: references.add((kind, name))
            for env in rv.field(container, 'envFrom', []):
                for source, kind in [('configMapRef', 'ConfigMap'), ('secretRef', 'Secret')]:
                    name = rv.field(env, source, {}).get('name')
                    if name: references.add((kind, name))
        for secret in rv.field(spec, 'imagePullSecrets', []):
            if secret.get('name'): references.add(('Secret', secret['name']))
        if spec.get('serviceAccountName'):
            references.add(('ServiceAccount', spec['serviceAccountName']))
        if references:
            lines.append('+- ' + label(pod))
            lines += ['   +- ' + ref(kind, name) for kind, name in sorted(references)]
    lines += ['', 'NETWORK POLICY POD SELECTION (not connectivity verdicts)']
    for policy in sorted((obj for obj in all_objects if native(obj, 'NetworkPolicy', 'networking.k8s.io')), key=label):
        selector = rv.field(rv.field(policy, 'spec', {}), 'podSelector', {})
        lines.append('+- ' + label(policy))
        lines += ['   +- [selected] ' + label(pod) for pod in sorted(pods, key=label) if selector_matches(rv.field(meta(pod), 'labels', {}), selector)]
    if len(lines) > limit:
        lines = lines[:limit] + ['... topology limited to %s lines; Resources retains the inventory.' % limit]
    return lines


def render(snapshot, mode, width, table, clean):
    namespace = snapshot['namespace']
    objects, errors = snapshot['objects'], snapshot['errors']
    if table is None:
        table = lambda headers, rows, width, **kwargs: [' | '.join(map(str, row)) for row in [headers] + rows]
    lines = ['Namespace: ' + meta(namespace).get('name', '?') + ' | Snapshot: ' + snapshot['stamp'], '']
    if mode == 'topology':
        lines += topology(snapshot)
    elif mode == 'overview':
        fields = [('State', phase(namespace)), ('UID', meta(namespace).get('uid', '-')),
                  ('Created', meta(namespace).get('creationTimestamp', '-')),
                  ('Deletion timestamp', meta(namespace).get('deletionTimestamp', '-')),
                  ('Spec finalizers', ','.join(rv.field(namespace, 'spec', {}).get('finalizers') or [])),
                  ('Metadata finalizers', ','.join(meta(namespace).get('finalizers') or []))]
        lines += table(['FIELD', 'VALUE'], fields, width, stretch=True)
        for heading, values in [('LABELS', rv.field(meta(namespace), 'labels', {})), ('ANNOTATIONS', rv.field(meta(namespace), 'annotations', {}))]:
            lines += ['', heading] + (table(['KEY', 'VALUE'], sorted(values.items()), width) if values else ['None.'])
        lines += ['', 'CONDITIONS']
        for condition in rv.field(rv.field(namespace, 'status', {}), 'conditions', []):
            lines += ['%s=%s | %s' % (condition.get('type', '?'), condition.get('status', '?'), condition.get('reason', '-')), str(condition.get('message', '-'))]
        lines += ['', 'RESOURCE COUNTS (retained objects; completed Pods/Jobs included)']
        rows = [[resource, 'N/A' if values is None else len(values)] for resource, values in sorted(objects.items())]
        lines += table(['RESOURCE', 'COUNT'], rows, width, stretch=True)
        pods = objects.get('pods')
        counts = Counter(phase(pod) for pod in pods or [])
        lines += ['', 'POD PHASES: ' + ('N/A' if pods is None else ' | '.join('%s=%s' % pair for pair in sorted(counts.items())) or 'No Pods.')]
        lines += ['', 'RESOURCE QUOTAS (reported status.hard / status.used)']
        quotas = objects.get('resourcequotas')
        if quotas is None:
            lines.append('N/A: ResourceQuota list unavailable.')
        elif not quotas:
            lines.append('No ResourceQuota.')
        else:
            rows = []
            for quota in quotas:
                status = rv.field(quota, 'status', {})
                hard, used = rv.field(status, 'hard', {}), rv.field(status, 'used', {})
                rows += [[meta(quota).get('name'), resource, hard.get(resource, 'N/A'), used.get(resource, 'N/A')] for resource in sorted(set(hard) | set(used))]
            lines += table(['QUOTA', 'RESOURCE', 'HARD', 'USED'], rows, width)
        lines += ['', 'LIMIT RANGES']
        limits = objects.get('limitranges')
        if limits is None:
            lines.append('N/A: LimitRange list unavailable.')
        elif not limits:
            lines.append('No LimitRange.')
        else:
            for limit in limits:
                lines.append('LimitRange/' + str(meta(limit).get('name')))
                rows = []
                for rule in rv.field(rv.field(limit, 'spec', {}), 'limits', []):
                    for field in ('min', 'max', 'default', 'defaultRequest', 'maxLimitRequestRatio'):
                        rows += [[rule.get('type', '?'), field + '.' + resource, value] for resource, value in rv.field(rule, field, {}).items()]
                lines += table(['TYPE', 'RULE', 'VALUE'], rows, width)
    elif mode == 'resources':
        lines += ['DISCOVERED NAMESPACED RESOURCES | completed objects included; empty types omitted']
        for resource, rows in sorted(objects.items()):
            if rows == []:
                continue
            lines += ['', resource + ' | Count: ' + ('N/A' if rows is None else str(len(rows)))]
            if rows is None:
                lines.append('Unavailable: ' + errors.get(resource, 'Unknown error'))
            elif rows:
                values = [[meta(obj).get('name', '?'), obj.get('kind', '?'), state(obj), rv.age(obj)] for obj in sorted(rows, key=lambda obj: meta(obj).get('name', ''))]
                lines += table(['NAME', 'KIND', 'STATUS', 'AGE'], values, width, stretch=True, max_widths={'NAME': 60, 'STATUS': 40})
            else:
                lines.append('No resources.')
    if errors:
        lines += ['', 'PARTIAL DATA / QUERY ERRORS'] + [resource + ': ' + str(error) for resource, error in sorted(errors.items())]
    # Wrap text with continuation indentation; table borders already fit exactly.
    output = []
    for line in lines:
        line = clean(line)
        indent = ' ' * min(len(line) - len(line.lstrip()), max(0, width // 3))
        output += textwrap.wrap(line, width, subsequent_indent=indent + ('   ' if '+-' in line else ''), replace_whitespace=False) or ['']
    return output


def demo(name='demo'):
    def obj(kind, name, **fields):
        version = 'apps/v1' if kind in ('Deployment', 'ReplicaSet') else 'networking.k8s.io/v1' if kind == 'Ingress' else 'v1'
        return dict(apiVersion=version, kind=kind, metadata={'name': name, 'namespace': 'demo', 'uid': kind + '-' + name}, **fields)
    deployment = obj('Deployment', 'web')
    replicaset = obj('ReplicaSet', 'web-abc')
    replicaset['metadata']['ownerReferences'] = [{'kind': 'Deployment', 'name': 'web', 'uid': 'Deployment-web', 'controller': True}]
    pod = obj('Pod', 'web-abc-1', status={'phase': 'Running'}, spec={'nodeName': 'worker-01', 'serviceAccountName': 'default', 'volumes': [{'configMap': {'name': 'web-config'}}, {'secret': {'secretName': 'web-secret'}}, {'persistentVolumeClaim': {'claimName': 'web-data'}}]})
    pod['metadata'].update(labels={'app': 'web'}, ownerReferences=[{'kind': 'ReplicaSet', 'name': 'web-abc', 'uid': 'ReplicaSet-web-abc', 'controller': True}])
    service = obj('Service', 'web', spec={'selector': {'app': 'web'}})
    ingress = obj('Ingress', 'web', spec={'rules': [{'host': 'example.invalid', 'http': {'paths': [{'path': '/', 'backend': {'service': {'name': 'web', 'port': {'number': 80}}}}]}}]})
    values = {'pods': [pod], 'deployments.apps': [deployment], 'replicasets.apps': [replicaset], 'services': [service], 'ingresses.networking.k8s.io': [ingress],
              'configmaps': [obj('ConfigMap', 'web-config')], 'secrets': [obj('Secret', 'web-secret')], 'persistentvolumeclaims': [obj('PersistentVolumeClaim', 'web-data')], 'serviceaccounts': [obj('ServiceAccount', 'default')]}
    for rows in values.values():
        for value in rows:
            value['metadata']['namespace'] = name
    namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': name, 'uid': 'namespace-' + name, 'labels': {'environment': 'demo'}}, 'status': {'phase': 'Active'}}
    return dict(namespace=namespace, objects={resource: values.get(resource, []) for resource in COMMON}, errors={}, stamp='DEMO (synthetic data)')
