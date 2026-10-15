"""Interactive, explicitly confirmed Kubernetes operations outside curses."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import uuid

import profiles
import resource_detail as detail
import event_resources

SCALE = {'deployments', 'deployments.apps', 'statefulsets', 'statefulsets.apps', 'replicasets', 'replicasets.apps'}
RESTART = {'deployments', 'deployments.apps', 'statefulsets', 'statefulsets.apps', 'daemonsets', 'daemonsets.apps'}
TEMPLATES = ('Pod', 'ReplicaSet', 'StatefulSet', 'DaemonSet', 'Deployment', 'Service', 'ConfigMap', 'Secret', 'PersistentVolumeClaim', 'Job', 'Namespace', 'Ingress')


def safe(text):
    return ''.join(c if c.isprintable() or c in '\n\t' else '?' for c in str(text))


def scope(item):
    return ['--namespace', item['namespace']] if item and item.get('namespaced') else []


def choose(title, options):
    print(title)
    for i, (label, _) in enumerate(options, 1):
        print('[%s] %s' % (i, safe(label)))
    while True:
        value = profiles.prompt('Number (Enter/Esc cancels): ').strip()
        if not value:
            raise profiles.InputCancelled()
        if value.isdigit() and 1 <= int(value) <= len(options):
            return options[int(value)-1][1]
        print('Enter one of the numbers shown above.')


def available(item):
    if not item:
        return []
    resource = item['resource']
    return (['start', 'stop'] if resource in SCALE else []) + (['restart'] if resource in RESTART else []) + ['delete']


def read_object(client, item):
    if item.get('expected_kind'):
        return event_resources.read_object(client, item)
    value, error = client.call('get', item['resource'], item['name'], *scope(item), '-o', 'json')
    if error:
        raise ValueError(error)
    meta = (value or {}).get('metadata') or {}
    if meta.get('name') != item['name'] or (item.get('namespaced') and meta.get('namespace') != item['namespace']):
        raise ValueError('Returned object does not match the selected row.')
    if item.get('uid') and meta.get('uid') != item['uid']:
        raise ValueError('The selected object was replaced. Refresh the list before changing it.')
    return value


def show_command(client, args):
    print('\nCommand: ' + safe(shlex.join(client.base + list(args))))


def confirmed(word):
    return profiles.prompt('Type "%s" to execute (Enter/Esc cancels): ' % word).strip() == word


def delete_path(item, obj):
    """Use the fetched API version and canonical plural, including custom APIs."""
    version = obj.get('apiVersion', '')
    plural = item['resource'].split('.')[0]
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', plural) or not re.fullmatch(r'(?:[a-z0-9][a-z0-9.-]*/)?v[a-z0-9]+', version):
        raise ValueError('Cannot resolve the selected resource API path.')
    path = ('/apis/' if '/' in version else '/api/') + version
    for value in [item.get('namespace') if item.get('namespaced') else None, item['name']]:
        if value is not None and not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', value):
            raise ValueError('Invalid selected resource identity.')
    if item.get('namespaced'):
        path += '/namespaces/' + item['namespace']
    return path + '/' + plural + '/' + item['name']


def change(client, item):
    descriptions = {'start': 'Start / scale to a positive replica count', 'stop': 'Stop / scale replicas to zero',
                    'restart': 'Rolling restart', 'delete': 'Delete resource'}
    action = choose('Choose an operation', [(descriptions[a], a) for a in available(item)])
    obj = read_object(client, item)
    meta = obj.get('metadata') or {}
    uid, version = meta.get('uid'), meta.get('resourceVersion')
    if not uid or not version:
        raise ValueError('Missing UID/resourceVersion. Refresh the list before changing it.')
    input_text = None
    if action in ('start', 'stop'):
        current = (obj.get('spec') or {}).get('replicas', 1)
        desired = 0
        if action == 'start':
            answer = profiles.prompt('Desired replicas (positive integer; Enter cancels): ').strip()
            if not answer:
                raise profiles.InputCancelled()
            if not answer.isdigit() or int(answer) < 1:
                raise ValueError('Replicas must be a positive integer.')
            desired = int(answer)
        print('Replicas: %s -> %s' % (current, desired))
        if action == 'stop':
            print('This scales the workload to zero. An autoscaler/operator may restore replicas.')
        # Scale inherits the parent object's resourceVersion; the API update is
        # conditional on that version even if the name/replica count is reused.
        args = ['scale', item['resource'], item['name'], *scope(item), '--replicas=' + str(desired), '--current-replicas=' + str(current), '--resource-version=' + version]
    elif action == 'restart':
        if (obj.get('spec') or {}).get('paused'):
            raise ValueError('Resume the paused workload before restarting it.')
        operations = [{'op': 'test', 'path': '/metadata/uid', 'value': uid},
                      {'op': 'test', 'path': '/metadata/resourceVersion', 'value': version}]
        template_meta = ((obj.get('spec') or {}).get('template') or {}).get('metadata')
        if template_meta is None:
            operations.append({'op': 'add', 'path': '/spec/template/metadata', 'value': {}})
        if not (template_meta or {}).get('annotations'):
            operations.append({'op': 'add', 'path': '/spec/template/metadata/annotations', 'value': {}})
        operations.append({'op': 'add', 'path': '/spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt',
                           'value': dt.datetime.now(dt.timezone.utc).isoformat()})
        args = ['patch', item['resource'], item['name'], *scope(item), '--type=json', '-p', json.dumps(operations)]
        print('The workload controller will replace Pods using its configured update strategy.')
    else:
        # Ordinary kubectl delete ignores the selected UID/resourceVersion.
        # --raw with stdin passes DeleteOptions directly using kubectl's auth.
        args = ['delete', '--raw=' + delete_path(item, obj), '-f', '-']
        input_text = json.dumps({'apiVersion': 'v1', 'kind': 'DeleteOptions',
                                'preconditions': {'uid': uid, 'resourceVersion': version},
                                'propagationPolicy': 'Background'})
        print('This deletes the selected resource. Controller-managed Pods may be recreated.')
        if item['resource'] in ('namespaces', 'persistentvolumeclaims', 'persistentvolumes'):
            print('Deletion may remove contained resources or stored data, depending on reclaim policy.')
    show_command(client, args)
    print('Preconditions: UID %s | resourceVersion %s' % (safe(uid), safe(version)))
    if not confirmed(action):
        print('Cancelled; no change sent.')
        return
    output, error = client.text(*args, input_text=input_text)
    print(safe(output or ''))
    if error:
        raise ValueError(error + '\nChange was not confirmed as accepted. Refresh the list and select the current object before retrying.')
    print('Request accepted. Refresh the dashboard to observe completion.')


def creation_type(client, item):
    """Resolve the selected resource against this cluster, including empty CRD lists."""
    resource = (item or {}).get('resource', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', resource):
        raise ValueError('Select a resource tab or an API Resource before creating YAML.')
    plural, _, group = resource.partition('.')
    if not group and plural in ('deployments', 'replicasets', 'statefulsets', 'daemonsets'):
        group = 'apps'
    if not group and plural in ('jobs', 'cronjobs'):
        group = 'batch'
    if group:
        api_group, error = client.call('get', '--raw', '/apis/' + group)
        if error:
            raise ValueError('Cannot discover selected resource: ' + error)
        preferred = ((api_group or {}).get('preferredVersion') or {}).get('groupVersion')
        versions = list(dict.fromkeys([preferred] + [v.get('groupVersion') for v in (api_group or {}).get('versions') or []]))
    else:
        versions = ['v1']
    errors = []
    for version in filter(None, versions):
        if not re.fullmatch(re.escape(group) + r'/[a-z0-9]+' if group else r'v1', version):
            continue
        listing, error = client.call('get', '--raw', ('/apis/' if group else '/api/') + version)
        if error:
            errors.append(error)
            continue
        entry = next((r for r in (listing or {}).get('resources') or [] if r.get('name') == plural), None)
        if not entry:
            continue
        if 'create' not in (entry.get('verbs') or []):
            raise ValueError(resource + ' does not support creation.')
        if not entry.get('kind') or not isinstance(entry.get('namespaced'), bool):
            raise ValueError('Incomplete API discovery for ' + resource)
        return dict(kind=entry['kind'], apiVersion=version, namespaced=entry['namespaced'])
    raise ValueError('Cannot discover selected resource ' + resource + (': ' + '; '.join(errors) if errors else ''))


def template(kind, name, namespace, api_version=None, namespaced=None):
    meta = 'metadata:\n  name: ' + json.dumps(name) + '\n'
    include_namespace = namespaced if namespaced is not None else kind != 'Namespace'
    if include_namespace:
        meta += '  namespace: ' + json.dumps(namespace) + '\n'
    bodies = {
        'Deployment': ('apps/v1', 'spec:\n  replicas: 1\n  selector:\n    matchLabels:\n      app: ' + json.dumps(name) + '\n  template:\n    metadata:\n      labels:\n        app: ' + json.dumps(name) + '\n    spec:\n      containers:\n        - name: app\n          image: nginx:stable\n          ports:\n            - containerPort: 80\n          resources:\n            requests:\n              cpu: 100m\n              memory: 64Mi\n            limits:\n              cpu: 500m\n              memory: 256Mi\n'),
        'Service': ('v1', 'spec:\n  type: ClusterIP\n  selector:\n    app: ' + json.dumps(name) + '\n  ports:\n    - name: http\n      port: 80\n      targetPort: 80\n'),
        'ConfigMap': ('v1', 'data:\n  example.conf: |\n    # Replace with your configuration\n'),
        'Secret': ('v1', 'type: Opaque\nstringData:\n  example-key: REPLACE_ME\n'),
        'PersistentVolumeClaim': ('v1', '# Set storageClassName if your cluster has no default StorageClass.\nspec:\n  accessModes:\n    - ReadWriteOnce\n  resources:\n    requests:\n      storage: 1Gi\n'),
        'Job': ('batch/v1', 'spec:\n  backoffLimit: 1\n  template:\n    spec:\n      restartPolicy: Never\n      containers:\n        - name: job\n          image: busybox:stable\n          command: ["sh", "-c", "echo hello"]\n'),
        'Namespace': ('v1', ''),
        'Ingress': ('networking.k8s.io/v1', '# Set ingressClassName and replace host/service for your cluster.\nspec:\n  rules:\n    - host: example.invalid\n      http:\n        paths:\n          - path: /\n            pathType: Prefix\n            backend:\n              service:\n                name: REPLACE_ME\n                port:\n                  number: 80\n')}
    pod_spec = 'spec:\n  containers:\n    - name: app\n      image: nginx:stable\n      ports:\n        - containerPort: 80\n      resources:\n        requests:\n          cpu: 100m\n          memory: 64Mi\n        limits:\n          cpu: 500m\n          memory: 256Mi\n'
    bodies['Pod'] = ('v1', pod_spec)
    bodies['ReplicaSet'] = bodies['Deployment']
    bodies['StatefulSet'] = ('apps/v1', bodies['Deployment'][1].replace('spec:\n', 'spec:\n  serviceName: "REPLACE_WITH_HEADLESS_SERVICE"\n', 1))
    bodies['DaemonSet'] = ('apps/v1', bodies['Deployment'][1].replace('  replicas: 1\n', '', 1))
    version, body = bodies.get(kind, (api_version, '# Add the required fields for this resource before deploying.\n'))
    # A custom API may reuse a built-in Kind; never insert a different group's spec.
    if api_version and (api_version.split('/')[0] if '/' in api_version else '') != (version.split('/')[0] if '/' in version else ''):
        body = '# Add the required fields for this resource before deploying.\n'
    version = api_version or version
    return 'apiVersion: ' + version + '\nkind: ' + kind + '\n' + meta + body


def editable_object(obj):
    obj = copy.deepcopy(obj)
    obj.pop('status', None)
    meta = obj.setdefault('metadata', {})
    for key in ('uid', 'creationTimestamp', 'generation', 'managedFields', 'selfLink', 'deletionTimestamp', 'deletionGracePeriodSeconds'):
        meta.pop(key, None)
    # Keep resourceVersion so a concurrent update can fail instead of being overwritten.
    annotations = meta.get('annotations') or {}
    annotations.pop('kubectl.kubernetes.io/last-applied-configuration', None)
    if annotations:
        meta['annotations'] = annotations
    else:
        meta.pop('annotations', None)
    return obj


def object_key(obj):
    meta = obj.get('metadata') or {}
    return obj.get('apiVersion'), obj.get('kind'), meta.get('namespace'), meta.get('name')


def edit_yaml(client, base, item, new=False):
    original = None
    if new:
        selected_type = creation_type(client, item)
        kind = selected_type['kind']
        print('Create %s | API: %s | Scope: %s' % (kind, selected_type['apiVersion'],
              'Namespace' if selected_type['namespaced'] else 'Cluster'))
        print('The YAML template follows the selected resource tab.')
        name = profiles.prompt('New resource name (Enter cancels): ').strip()
        if not name:
            raise profiles.InputCancelled()
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', name):
            raise ValueError('Use a Kubernetes lowercase resource name.')
        namespace = ''
        if selected_type['namespaced']:
            default = (item or {}).get('namespace') or 'default'
            namespace = profiles.prompt('Namespace [Enter: %s]: ' % default).strip() or default
            if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', namespace):
                raise ValueError('Invalid namespace.')
        selected_scope = ['--namespace', namespace] if namespace else []
        content = template(kind, name, namespace, selected_type['apiVersion'], selected_type['namespaced'])
        if '# Add the required fields' in content:
            print('This API uses a minimal template. Add its required fields in the editor.')
            show_command(client, ['explain', item['resource'], '--api-version=' + selected_type['apiVersion']])
        expected = (selected_type['apiVersion'], kind, namespace or None, name)
    else:
        original = read_object(client, item)
        content, error = client.text('create', '--dry-run=client', '--validate=false', '-f', '-', *scope(item), '-o', 'yaml', input_text=json.dumps(editable_object(original)))
        if error:
            raise ValueError(error)
        selected_scope = scope(item)
    drafts = Path(base) / '.kdash' / 'drafts'
    drafts.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = drafts / (uuid.uuid4().hex[:12] + '.yaml')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(content)
    editor = shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi')
    print('Draft: ' + str(path))
    print('Editor: %s | vi: i to edit, Esc then :wq to save and close.' % shlex.join(editor))
    print('The draft is retained on disk after success, cancellation or failure (mode 600).')
    profiles.prompt('Enter to open the editor; Esc cancels: ')
    while True:
        result = subprocess.run(editor + [str(path)])
        os.chmod(path, 0o600)
        if result.returncode:
            raise ValueError('Editor exited unsuccessfully; draft preserved at ' + str(path))
        # Keep exactly these bytes for every subprocess. The retained draft may
        # be edited externally while the user reviews the approval prompt.
        payload = path.read_bytes().decode('utf-8')
        print('\nYAML PREVIEW\n' + safe(payload))
        print('Only the previewed snapshot will be submitted; later draft edits require another review.')
        verb = 'create' if new else 'apply'
        print('\nChecking YAML with a server dry-run...')
        preview, error = client.call(verb, '--dry-run=server', '-f', '-', *selected_scope, '-o', 'json', input_text=payload)
        if not error and not isinstance(preview, dict):
            error = 'Server dry-run returned an invalid object.'
        if not error and new and object_key(preview) != expected:
            error = 'New YAML must preserve the selected resource type, name and namespace. Open the matching resource tab to create another type.'
        if not error and original is not None and object_key(preview) != object_key(original):
            error = 'Edit must preserve apiVersion, kind, name and namespace. Use New YAML for a different object.'
        if error:
            print('Validation failed: ' + safe(error))
            if profiles.prompt('e: edit again | Enter/Esc: keep draft and return: ').strip().lower() == 'e':
                continue
            return
        objects = (preview.get('items') or []) if preview.get('kind') == 'List' else [preview]
        print('Validated targets:')
        for obj in objects:
            print('  ' + safe('/'.join(str(v or '-') for v in object_key(obj))))
        if not new:
            print('\nDIFF (current -> edited)')
            diff = client.run('diff', '-f', '-', *selected_scope, input_text=payload)
            print(safe(diff.stdout or '(No differences)'))
            if diff.returncode not in (0, 1):
                raise ValueError(diff.stderr or 'kubectl diff failed; no apply was sent.')
        args = [verb, '-f', '-', *selected_scope]
        show_command(client, args)
        choice = profiles.prompt('Type "%s" to deploy, "e" to edit again, Enter/Esc to keep draft and return: ' % verb).strip()
        if choice == 'e':
            continue
        if choice != verb:
            print('Cancelled; draft preserved.')
            return
        output, error = client.text(*args, input_text=payload)
        print(safe(output or ''))
        if error:
            raise ValueError(error + '\nDraft preserved: ' + str(path))
        print('Deployment request accepted. Draft: ' + str(path))
        return


def exec_command(client, item):
    if item['resource'] not in detail.LOG_RESOURCES:
        raise ValueError('Exec is supported from Pod/workload details.')
    obj = read_object(client, item)
    if item['resource'] != 'pods':
        selector = detail.selector_text((obj.get('spec') or {}).get('selector') or {})
        pods, error = client.call('get', 'pods', *scope(item), '--selector', selector, '-o', 'json')
        if error:
            raise ValueError(error)
        choices = [p for p in (pods or {}).get('items') or [] if (p.get('status') or {}).get('phase') == 'Running']
        if not choices:
            raise ValueError('No Running Pods match this workload selector.')
        obj = choose('Choose a Running Pod', [((p.get('metadata') or {}).get('name', '?'), p) for p in choices])
    spec, status = obj.get('spec') or {}, obj.get('status') or {}
    running = {c.get('name') for key in ('containerStatuses', 'initContainerStatuses', 'ephemeralContainerStatuses')
               for c in status.get(key) or [] if (c.get('state') or {}).get('running') is not None}
    containers = [c['name'] for key in ('containers', 'initContainers', 'ephemeralContainers') for c in spec.get(key) or [] if c.get('name') in running]
    if not containers:
        raise ValueError('No running containers are available for exec.')
    container = choose('Choose a container', [(c, c) for c in containers])
    command = profiles.prompt('Command [Enter: /bin/sh]; quote arguments as needed: ').strip() or '/bin/sh'
    argv = shlex.split(command)
    if not argv:
        raise ValueError('Command is empty.')
    namespace = (obj.get('metadata') or {}).get('namespace')
    pod = (obj.get('metadata') or {}).get('name')
    args = ['exec', '--namespace', namespace, pod, '-c', container, '-it', '--', *argv]
    print('Exec target: %s/%s | container: %s' % (namespace, pod, container))
    show_command(client, args)
    if confirmed('exec'):
        result = subprocess.run(client.base + args)
        print('Exec exited with code %s.' % result.returncode)


def perform(client, base, info, action, item):
    profiles.clear()
    print('KDASH | Resource operations')
    print('Profile: %s | Cluster: %s | API: %s' % tuple(safe(info.get(k, '-')) for k in ('alias', 'cluster', 'server')))
    try:
        if item and item.get('event_context'):
            if action != 'edit':
                raise ValueError('Event details support target YAML editing only.')
            print('Resolving the original Event target...')
            item = event_resources.resolve(client, item)
        if action == 'new' and item:
            print('Create resource: ' + safe(item['resource']))
        elif item:
            print('Target: ' + safe(item['resource'] + '/' + item['name']) + ' | Namespace: ' + safe(item.get('namespace') or '<cluster>'))
        print('Esc cancels at any prompt.\n')
        if action == 'new':
            edit_yaml(client, base, item, new=True)
        elif action == 'edit' and item:
            edit_yaml(client, base, item)
        elif action == 'actions' and item:
            change(client, item)
        elif action == 'exec' and item:
            exec_command(client, item)
    except (profiles.InputCancelled, EOFError, KeyboardInterrupt):
        return
    except (ValueError, OSError, TypeError, AttributeError, KeyError, subprocess.TimeoutExpired) as exc:
        print('KDASH: ' + safe(exc))
    try:
        profiles.prompt('\nEnter to return to dashboard: ')
    except (profiles.InputCancelled, EOFError, KeyboardInterrupt):
        pass


def demo_action(action, item):
    print('DEMO mode: deployment, editing, scale/restart/delete and exec are disabled.')
    try:
        profiles.prompt('Enter to return: ')
    except (profiles.InputCancelled, EOFError, KeyboardInterrupt):
        pass
