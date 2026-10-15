"""Interactive, explicitly confirmed Kubernetes operations outside curses."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from urllib.parse import quote

from kasciidash_app.config import profiles
from kasciidash_app.resources import resource_detail as detail
from kasciidash_app.resources import event_resources
from kasciidash_app.terminal import exec_session as terminal_exec
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


def yaml_component(value):
    # Aliases are user-defined; encode separators without alias collisions.
    text = str(value or 'unregistered')
    return quote(text, safe='-_') if text not in ('.', '..') else text.replace('.', '%2E')


def yaml_directory(base, profile_alias, resource):
    root = Path(base) / '.kasciidash'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root
    for value in (profile_alias, resource):
        path = path / yaml_component(value)
        if path.is_symlink():
            raise ValueError('YAML storage directory must not be a symbolic link: ' + str(path))
        path.mkdir(exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    return path


def saved_drafts(client, base, item, profile_alias='unregistered'):
    directory = yaml_directory(base, profile_alias, item['resource'])
    paths = sorted(p for pattern in ('*.yaml', 'draft/*.yaml') for p in directory.glob(pattern)
                   if p.is_file() and not p.is_symlink() and not p.parent.is_symlink())
    if not paths:
        print('No saved drafts for this profile and resource. Press n to create one.')
        return
    path = choose('Saved new YAML drafts | ' + safe(profile_alias) + ' / ' + safe(item['resource']),
                  [(str(p.relative_to(directory)), p) for p in paths])
    if path.parent != directory:
        # Earlier versions stored new drafts under resource/draft/<hash>.yaml.
        payload = path.read_bytes()
        parsed, error = client.call('create', '--dry-run=client', '--validate=false', '-f', '-', '-o', 'json', input_text=payload.decode('utf-8'))
        selected_type = creation_type(client, item)
        if error or not isinstance(parsed, dict):
            raise ValueError(error or 'Cannot read legacy draft YAML.')
        api, kind, namespace, name = object_key(parsed)
        if (api, kind) != (selected_type['apiVersion'], selected_type['kind']) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', str(name or '')):
            raise ValueError('Legacy draft must contain one object of the selected resource type.')
        target = directory / (yaml_component(name) + '.yaml')
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ValueError('A draft with this name already exists; resume ' + target.name + ' first.') from exc
        with os.fdopen(fd, 'wb') as output:
            output.write(payload)
        if not path.is_symlink() and path.read_bytes() == payload:
            path.unlink()
        path = target
    edit_yaml(client, base, item, new=True, profile_alias=profile_alias, draft_path=path)


def edit_yaml(client, base, item, new=False, profile_alias='unregistered', draft_path=None):
    # Existing resource edits are disposable, including cancellation and errors.
    if not new:
        with tempfile.TemporaryDirectory(prefix='kasciidash-edit-') as directory:
            return _edit_yaml(client, base, item, new, profile_alias,
                              Path(directory) / 'edit.yaml')
    return _edit_yaml(client, base, item, new, profile_alias, draft_path)


def _edit_yaml(client, base, item, new, profile_alias, path=None):
    original = None
    if new:
        selected_type = creation_type(client, item)
        kind = selected_type['kind']
        print('Create %s | API: %s | Scope: %s' % (kind, selected_type['apiVersion'],
              'Namespace' if selected_type['namespaced'] else 'Cluster'))
        print('The YAML template follows the selected resource tab.')
        directory = yaml_directory(base, profile_alias, item['resource'])
        if path is not None:
            path = Path(path)
            if path.parent != directory or path.is_symlink() or not path.is_file():
                raise ValueError('Choose a saved draft from the current profile and resource.')
            name = path.stem
            selected_scope = []
            expected = (selected_type['apiVersion'], kind, None, name)
            content = None
        else:
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
    if new and path is None:
        path = directory / (yaml_component(name) + '.yaml')
        if path.exists() or path.is_symlink():
            print('A draft with this name already exists. Use D to resume it; nothing was overwritten.')
            return
    if content is not None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(content)
    editor = shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi')
    print('Draft: ' + str(path))
    print('Editor: %s | vi: i to edit, Esc then :wq to save and close.' % shlex.join(editor))
    print('New drafts are kept until deployment succeeds.' if new else 'Temporary edit; this file is removed when editing ends.')
    profiles.prompt('Enter to open the editor; Esc cancels: ')
    while True:
        result = subprocess.run(editor + [str(path)])
        os.chmod(path, 0o600)
        if result.returncode:
            raise ValueError('Editor exited unsuccessfully.' + (' Draft preserved at ' + str(path) if new else ' Temporary edit will be removed.'))
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
        resumed = new and content is None
        actual = object_key(preview) if not error else None
        identity_matches = actual == expected if new else True
        if resumed and actual:
            identity_matches = actual[:2] == expected[:2] and actual[3] == expected[3]
        if not error and new and not identity_matches:
            error = 'New YAML must preserve the selected resource type, name and namespace. Open the matching resource tab to create another type.'
        if not error and original is not None and object_key(preview) != object_key(original):
            error = 'Edit must preserve apiVersion, kind, name and namespace. Use New YAML for a different object.'
        if error:
            print('Validation failed: ' + safe(error))
            if profiles.prompt('e: edit again | Enter/Esc: return: ').strip().lower() == 'e':
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
        choice = profiles.prompt('Type "%s" to deploy, "e" to edit again, Enter/Esc to return: ' % verb).strip()
        if choice == 'e':
            continue
        if choice != verb:
            print('Cancelled; new draft preserved.' if new else 'Cancelled; temporary edit removed on return.')
            return
        output, error = client.text(*args, input_text=payload)
        print(safe(output or ''))
        if error:
            raise ValueError(error + ('\nDraft preserved: ' + str(path) if new else '\nTemporary edit will be removed.'))
        print('Deployment request accepted.')
        if new:
            # Do not delete a draft changed externally after its snapshot was approved.
            if not path.is_symlink() and path.read_bytes() == payload.encode('utf-8'):
                path.unlink()
                print('Submitted draft removed.')
            else:
                print('Draft changed after approval; the newer draft was kept: ' + str(path))
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
    container = containers[0] if len(containers) == 1 else choose('Choose a running container', [(c, c) for c in containers])
    print('Container: ' + safe(container))
    print('\nOPEN CONTAINER TERMINAL')
    print('  [1] sh    - /bin/sh (default)')
    print('  [2] bash  - /bin/bash')
    print('  [3] command - enter a command and arguments')
    while True:
        mode = profiles.prompt('Choose 1/2/3 or sh/bash [Enter: sh; Esc: cancel]: ').strip().lower()
        if mode in ('', '1', 'sh', '/bin/sh'):
            argv, shell = ['/bin/sh', '-i'], True
            break
        if mode in ('2', 'bash', '/bin/bash'):
            argv, shell = ['/bin/bash', '-i'], True
            break
        if mode in ('3', 'command'):
            command = profiles.prompt('Command (example: sh -c "id; pwd"; Enter/Esc cancels): ').strip()
            if not command:
                raise profiles.InputCancelled()
            argv, shell = shlex.split(command), False
            if not argv or not argv[0]:
                raise ValueError('Command executable cannot be empty.')
            break
        print('Choose sh, bash, or 3 for a custom command.')
    namespace = (obj.get('metadata') or {}).get('namespace')
    pod = (obj.get('metadata') or {}).get('name')
    print('\nNamespace: ' + safe(namespace))
    print('Pod:       ' + safe(pod))
    print('Container: ' + safe(container))
    print('Command:   ' + safe(shlex.join(argv)))
    if shell:
        while True:
            answer = profiles.prompt('Enter: connect to shell | Esc: cancel: ').strip()
            if answer in ('', 'exec'):
                break
            print('The shell is already selected. Press Enter to connect, or Esc to cancel.')
    elif not confirmed('exec'):
        print('Cancelled; no command was sent.')
        return
    print('Checking the selected Pod and container...', flush=True)
    current = read_object(client, dict(resource='pods', name=pod, namespace=namespace, namespaced=True,
                                       uid=(obj.get('metadata') or {}).get('uid')))
    still_running = {c.get('name') for key in ('containerStatuses', 'initContainerStatuses', 'ephemeralContainerStatuses')
                     for c in (current.get('status') or {}).get(key) or [] if (c.get('state') or {}).get('running') is not None}
    if container not in still_running:
        raise ValueError('The selected container is no longer running. Refresh and choose a running container.')
    profiles.clear()
    print('KAsciiDash | CONTAINER TERMINAL')
    print('Namespace: %s\nPod: %s\nContainer: %s' % tuple(safe(v) for v in (namespace, pod, container)))
    print('Command: ' + safe(shlex.join(argv)))
    print('Exit: exit or Ctrl+D | Interrupt: Ctrl+C | Disconnect: Ctrl+]')
    print('Connecting... (shell startup: up to 20 seconds)\n' if shell else 'Starting command...\n', flush=True)
    args = ['exec', '--namespace', namespace, pod, '-c', container, '-it', '--', *argv]
    code = terminal_exec.run(terminal_exec.exec_base(client.base) + args, first_output_timeout=20 if shell else None)
    if code is not None:
        print('\nTerminal closed | exit code: %s' % code)
        if code:
            print('If the connection failed, inspect the output above; try sh if bash is unavailable.')


def perform(client, base, info, action, item):
    profiles.clear()
    print('KAsciiDash | Resource operations')
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
            edit_yaml(client, base, item, new=True, profile_alias=info.get('alias') or 'unregistered')
        elif action == 'drafts' and item:
            saved_drafts(client, base, item, profile_alias=info.get('alias') or 'unregistered')
        elif action == 'edit' and item:
            edit_yaml(client, base, item, profile_alias=info.get('alias') or 'unregistered')
        elif action == 'actions' and item:
            change(client, item)
        elif action == 'exec' and item:
            exec_command(client, item)
    except (profiles.InputCancelled, EOFError, KeyboardInterrupt):
        return
    except (ValueError, OSError, TypeError, AttributeError, KeyError, subprocess.TimeoutExpired) as exc:
        print('KAsciiDash: ' + safe(exc))
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
