"""Read-only resource catalog, categories and local filtering helpers."""
import concurrent.futures
import datetime as dt
import re
import shlex

MENUS = [('1', 'Overview'), ('2', 'Nodes'), ('m', 'Monitoring'), ('N', 'Nodes'), ('3', 'Workloads'), ('4', 'Warnings'),
         ('5', 'Errors'), ('6', 'Network'), ('7', 'Configuration'), ('8', 'Storage'),
         ('9', 'Access / RBAC'), ('0', 'API Resources'), ('s', 'Profiles')]
MENU_GROUPS = [('Dashboard', ('1', 'm')), ('Clusters', ('N',)),
               ('Resources', ('3', '6', '7', '8', '9', '0')),
               ('Event', ('4', '5')), ('Settings', ('s',))]


def menu_group(view):
    return next(label for label, keys in MENU_GROUPS if ('1' if view == '2' else view) in keys)


CATEGORIES = {
    '6': [('Services', 'services', True), ('EndpointSlices', 'endpointslices.discovery.k8s.io', True),
          ('Ingresses', 'ingresses.networking.k8s.io', True), ('NetworkPolicies', 'networkpolicies.networking.k8s.io', True)],
    '7': [('ConfigMaps', 'configmaps', True), ('Secrets', 'secrets', True),
          ('ResourceQuotas', 'resourcequotas', True), ('LimitRanges', 'limitranges', True)],
    '8': [('PVCs', 'persistentvolumeclaims', True), ('PVs', 'persistentvolumes', False),
          ('StorageClasses', 'storageclasses.storage.k8s.io', False), ('VolumeAttachments', 'volumeattachments.storage.k8s.io', False)],
    '9': [('ServiceAccounts', 'serviceaccounts', True), ('Roles', 'roles.rbac.authorization.k8s.io', True),
          ('RoleBindings', 'rolebindings.rbac.authorization.k8s.io', True),
          ('ClusterRoles', 'clusterroles.rbac.authorization.k8s.io', False),
          ('ClusterRoleBindings', 'clusterrolebindings.rbac.authorization.k8s.io', False)]}


def field(obj, key, default=None):
    """Kubernetes optional fields may be absent or explicitly null; preserve 0/False."""
    if obj is None:
        return default
    if not isinstance(obj, dict):
        raise TypeError('Expected an object while reading field ' + str(key))
    value = obj.get(key)
    return default if value is None else value


def parse_filter(query):
    filters = {}
    names = []
    for token in shlex.split(query):
        if '=' not in token:
            names.append(token)
            continue
        key, value = token.split('=', 1)
        key = {'namespace': 'ns', 'state': 'status'}.get(key, key)
        if key not in ('name', 'ns', 'status'):
            raise ValueError('Allowed fields: name, ns/namespace, status')
        filters[key] = value.strip()
    if names:
        filters['name'] = ' '.join(names)
    return {key: value for key, value in filters.items() if value}


def filter_items(objects, filters, status):
    if objects is None:
        return None
    def matches(obj):
        metadata = field(obj, 'metadata', {})
        values = {'name': field(metadata, 'name', ''), 'ns': field(metadata, 'namespace', ''), 'status': status(obj) if 'status' in filters else ''}
        return all(value.casefold() in str(values[key]).casefold() for key, value in filters.items())
    return [obj for obj in objects if matches(obj)]


def status_text(obj):
    """Search the resource's reported phase, state, conditions and attachment state."""
    status = field(obj, 'status', {})
    if not isinstance(status, dict):
        return str(status) or '-'
    conditions = field(status, 'conditions', [])
    flags = ','.join('%s=%s' % (field(c, 'type', '?'), field(c, 'status', '?'))
                     for c in conditions if isinstance(c, dict)) if isinstance(conditions, list) else str(conditions)
    parts = [str(status[key]) for key in ('phase', 'state', 'reason') if status.get(key)]
    if 'attached' in status and status['attached'] is not None:
        parts.append('Attached=' + str(status['attached']))
    if flags:
        parts.append(flags)
    return ' '.join(parts) or '-'


def discover(client):
    catalog, errors = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {scope: pool.submit(client.text, 'api-resources', '--verbs=list',
                   '--namespaced=' + str(scope).lower(), '--cached=false', '-o', 'name') for scope in (True, False)}
        for scope, future in futures.items():
            output, error = future.result()
            if error:
                errors.append(('namespaced' if scope else 'cluster') + ': ' + error)
            # kubectl can return partial discovery data together with a nonzero exit.
            for name in (output or '').splitlines():
                name = name.strip()
                if re.fullmatch(r'[a-z0-9][a-z0-9.-]*', name):
                    catalog[name] = {'name': name, 'namespaced': scope}
    return [catalog[name] for name in sorted(catalog)], errors


def list_resource(client, resource, namespaced):
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', resource):
        return None, 'Invalid resource name'
    args = ['get', resource] + (['-A'] if namespaced else []) + ['-o', 'json']
    value, error = client.call(*args)
    if error:
        return None, error
    # Only retain fields needed by views; Secret/ConfigMap contents are excluded.
    items = []
    for obj in field(value or {}, 'items', []):
        item = {key: obj[key] for key in ('apiVersion', 'kind', 'metadata', 'spec', 'status', 'type', 'roleRef', 'subjects', 'rules', 'provisioner', 'reclaimPolicy', 'volumeBindingMode', 'endpoints', 'addressType', 'ports', 'secrets') if key in obj}
        # Identify core Secrets/ConfigMaps even when selected through discovery.
        sensitive = obj.get('apiVersion') == 'v1' and obj.get('kind') in ('Secret', 'ConfigMap')
        if sensitive or resource.rstrip('.') in ('configmaps', 'secrets'):
            metadata = field(obj, 'metadata', {})
            item['metadata'] = {key: metadata[key] for key in
                                ('name', 'namespace', 'uid', 'resourceVersion', 'creationTimestamp', 'deletionTimestamp')
                                if key in metadata}
            item['keyCount'] = len(field(obj, 'data', {})) + (len(field(obj, 'binaryData', {})) if obj.get('kind') == 'ConfigMap' else 0)
        items.append(item)
    return items, None


def selector(value, label_selector=False):
    if not value:
        return '<all>'
    labels = field(value, 'matchLabels', {}) if label_selector else value
    parts = ['%s=%s' % pair for pair in sorted(labels.items())]
    if label_selector:
        parts += [str(expr) for expr in field(value, 'matchExpressions', [])]
    return ','.join(parts) or '<all>'


def age(obj):
    try:
        timestamp = field(field(obj, 'metadata', {}), 'creationTimestamp', '')
        if not isinstance(timestamp, str):
            return '-'
        stamp = dt.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        seconds = max(0, int((dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()))
        return '%sd' % (seconds // 86400) if seconds >= 86400 else '%sh' % (seconds // 3600) if seconds >= 3600 else '%sm' % (seconds // 60)
    except (KeyError, ValueError, TypeError):
        return '-'


def columns(resource, objects):
    # A CRD can share a plural name with a built-in resource. Match the API group too.
    builtins = {name: name.split('.')[0] for tabs in CATEGORIES.values() for _, name, _ in tabs}
    base = builtins.get(resource)
    headers = ['NAMESPACE', 'NAME']
    tails = {
        'services': ['TYPE', 'CLUSTER-IP', 'EXTERNAL-IP', 'PORTS', 'SELECTOR'],
        'endpointslices': ['SERVICE', 'ADDRESS-TYPE', 'READY/TOTAL', 'ADDRESSES', 'PORTS'],
        'ingresses': ['CLASS', 'HOSTS', 'ADDRESS', 'TLS'],
        'networkpolicies': ['POD-SELECTOR', 'POLICY-TYPES', 'INGRESS-RULES', 'EGRESS-RULES'],
        'configmaps': ['KEYS', 'AGE'], 'secrets': ['TYPE', 'KEYS', 'AGE'],
        'resourcequotas': ['HARD', 'USED'], 'limitranges': ['LIMITS'],
        'persistentvolumeclaims': ['PHASE', 'VOLUME', 'CAPACITY', 'ACCESS', 'CLASS'],
        'persistentvolumes': ['PHASE', 'CAPACITY', 'ACCESS', 'CLAIM', 'CLASS'],
        'storageclasses': ['PROVISIONER', 'RECLAIM', 'BINDING'],
        'volumeattachments': ['NODE', 'PV', 'ATTACHED', 'ERROR'],
        'serviceaccounts': ['SECRETS', 'AGE'], 'roles': ['RULES', 'AGE'], 'clusterroles': ['RULES', 'AGE'],
        'rolebindings': ['ROLE', 'SUBJECTS'], 'clusterrolebindings': ['ROLE', 'SUBJECTS']}
    headers += tails.get(base, ['KIND', 'API-VERSION', 'STATUS', 'AGE'])
    rows = []
    for obj in sorted(objects, key=lambda o: (field(field(o, 'metadata', {}), 'namespace', ''), field(field(o, 'metadata', {}), 'name', ''))):
        metadata, spec, status = field(obj, 'metadata', {}), field(obj, 'spec', {}), field(obj, 'status', {})
        row = [field(metadata, 'namespace', '-'), field(metadata, 'name', '-')]
        if base == 'services':
            addresses = field(spec, 'externalIPs', []) + [entry.get('ip') or field(entry, 'hostname', '-') for entry in field(field(status, 'loadBalancer', {}), 'ingress', [])]
            external = spec.get('externalName') or ','.join(addresses) or '-'
            ports = ','.join('%s%s/%s->%s' % (field(port, 'port', '-'), ':' + str(port['nodePort']) if port.get('nodePort') else '', field(port, 'protocol', 'TCP'), field(port, 'targetPort', field(port, 'port', '-'))) for port in field(spec, 'ports', []))
            values = [field(spec, 'type', 'ClusterIP'), field(spec, 'clusterIP', '-'), external, ports or '-', selector(spec.get('selector'))]
        elif base == 'endpointslices':
            endpoints = field(obj, 'endpoints', [])
            # Fields outside spec are injected by list_resource below.
            values = [field(field(metadata, 'labels', {}), 'kubernetes.io/service-name', '-'), field(obj, 'addressType', '-'),
                      '%s/%s' % (sum(field(e, 'conditions', {}).get('ready') is not False for e in endpoints), len(endpoints)),
                      ','.join(a for e in endpoints for a in field(e, 'addresses', [])) or '-',
                      ','.join('%s/%s' % (field(p, 'port', '-'), field(p, 'protocol', 'TCP')) for p in field(obj, 'ports', [])) or '-']
        elif base == 'ingresses':
            values = [spec.get('ingressClassName') or field(field(metadata, 'annotations', {}), 'kubernetes.io/ingress.class', '-'),
                      ','.join(field(rule, 'host', '*') for rule in field(spec, 'rules', [])) or '*',
                      ','.join(e.get('ip') or field(e, 'hostname', '-') for e in field(field(status, 'loadBalancer', {}), 'ingress', [])) or '-',
                      str(len(field(spec, 'tls', [])))]
        elif base == 'networkpolicies':
            values = [selector(spec.get('podSelector'), label_selector=True), ','.join(field(spec, 'policyTypes', ['Ingress'] + (['Egress'] if 'egress' in spec else []))),
                      str(len(field(spec, 'ingress', []))), str(len(field(spec, 'egress', [])))]
        elif base in ('configmaps', 'secrets'):
            values = ([field(obj, 'type', 'Opaque')] if base == 'secrets' else []) + [str(field(obj, 'keyCount', 0)), age(obj)]
        elif base == 'resourcequotas':
            values = [str(field(status, 'hard', field(spec, 'hard', {}))), str(field(status, 'used', {}))]
        elif base == 'limitranges':
            values = [str(field(spec, 'limits', []))]
        elif base == 'persistentvolumeclaims':
            values = [field(status, 'phase', 'Unknown'), field(spec, 'volumeName', '-'), field(field(status, 'capacity', {}), 'storage', '-'),
                      ','.join(field(spec, 'accessModes', [])) or '-', field(spec, 'storageClassName', '-')]
        elif base == 'persistentvolumes':
            claim = field(spec, 'claimRef', {})
            values = [field(status, 'phase', 'Unknown'), field(field(spec, 'capacity', {}), 'storage', '-'),
                      ','.join(field(spec, 'accessModes', [])) or '-',
                      '/'.join(filter(None, (claim.get('namespace'), claim.get('name')))) or '-', field(spec, 'storageClassName', '-')]
        elif base == 'storageclasses':
            values = [field(obj, 'provisioner', '-'), field(obj, 'reclaimPolicy', 'Delete'), field(obj, 'volumeBindingMode', 'Immediate')]
        elif base == 'volumeattachments':
            values = [field(spec, 'nodeName', '-'), field(field(spec, 'source', {}), 'persistentVolumeName', '-'), str(field(status, 'attached', False)),
                      field(status, 'attachError', {}).get('message') or field(status, 'detachError', {}).get('message') or '-']
        elif base in ('roles', 'clusterroles', 'serviceaccounts'):
            values = [str(len(field(obj, 'secrets', []) if base == 'serviceaccounts' else field(obj, 'rules', []))), age(obj)]
        elif base in ('rolebindings', 'clusterrolebindings'):
            ref = field(obj, 'roleRef', {})
            values = [field(ref, 'kind', '-') + '/' + field(ref, 'name', '-'), ','.join(field(subject, 'kind', '-') + ':' + (field(subject, 'namespace', '') + '/' if subject.get('namespace') else '') + field(subject, 'name', '-') for subject in field(obj, 'subjects', [])) or '-']
        else:
            values = [field(obj, 'kind', '-'), field(obj, 'apiVersion', '-'), status_text(obj), age(obj)]
        rows.append(row + list(map(str, values)))
    return headers, rows


def demo_catalog():
    resources = {resource: scope for tabs in CATEGORIES.values() for _, resource, scope in tabs}
    resources.update({'pods': True, 'deployments.apps': True, 'widgets.demo.example': True, 'nodes': False})
    return [{'name': name, 'namespaced': resources[name]} for name in sorted(resources)], []


def demo_resource(resource, namespaced):
    base = resource.split('.')[0]
    obj = {'apiVersion': 'v1', 'kind': base, 'metadata': {'name': 'demo-' + base, **({'namespace': 'demo'} if namespaced else {})}}
    if base == 'services':
        obj.update(kind='Service', spec={'type': 'ClusterIP', 'clusterIP': '10.96.0.10', 'ports': [{'port': 80, 'targetPort': 8080}], 'selector': {'app': 'web'}})
    elif base == 'networkpolicies':
        obj['spec'] = {'podSelector': {'matchLabels': {'app': 'web'}}, 'policyTypes': ['Ingress'], 'ingress': []}
    elif base == 'ingresses':
        obj['spec'] = {'ingressClassName': 'nginx', 'rules': [{'host': 'web.demo.invalid'}]}
    elif base == 'endpointslices':
        obj.update(addressType='IPv4', endpoints=[{'addresses': ['10.0.0.20'], 'conditions': {'ready': True}}], ports=[{'port': 8080}])
    elif base in ('configmaps', 'secrets'):
        obj.update(keyCount=2, type='Opaque')
    else:
        obj['status'] = {'phase': 'Active'}
    return [obj], None
