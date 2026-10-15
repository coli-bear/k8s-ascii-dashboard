"""Resolve immutable Event references to the original Kubernetes object."""
import copy
import json
import re


def reference(row):
    if not isinstance(row, dict):
        return None
    # Collection errors can supply an explicit structured reference. Never parse
    # a kubectl error message into an object name or infer a plural from Kind.
    raw = row.get('raw')
    raw = raw if isinstance(raw, dict) else {}
    value = row.get('reference') or raw.get('involvedObject') or raw.get('regarding')
    return copy.deepcopy(value) if isinstance(value, dict) and value else None


def unavailable(row):
    ref = reference(row)
    if not ref:
        return 'No object reference. Collection/discovery errors may have no target; open Resources to inspect objects.'
    missing = [key for key in ('apiVersion', 'kind', 'name', 'uid') if not ref.get(key)]
    if missing:
        return 'Original resource cannot be identified: reference lacks ' + ', '.join(missing) + '.'
    return ''


def resolve(client, item):
    row = item['event_row']
    problem = unavailable(row)
    if problem:
        raise ValueError(problem)
    ref = reference(row)
    version, kind, name = ref['apiVersion'], ref['kind'], ref['name']
    if not isinstance(version, str) or not re.fullmatch(r'(?:[a-z0-9][a-z0-9.-]*/)?v[0-9][a-z0-9]*', version):
        raise ValueError('Invalid Event target apiVersion.')
    if not isinstance(kind, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', kind):
        raise ValueError('Invalid Event target kind.')
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', name):
        raise ValueError('Invalid Event target name.')
    path = ('/apis/' if '/' in version else '/api/') + version
    catalog, error = client.call('get', '--raw', path)
    if error:
        raise ValueError('Cannot discover Event target API ' + version + ': ' + error)
    candidates = [entry for entry in (catalog or {}).get('resources') or []
                  if entry.get('kind') == kind and '/' not in entry.get('name', '')
                  and 'get' in (entry.get('verbs') or []) and isinstance(entry.get('namespaced'), bool)
                  and re.fullmatch(r'[a-z0-9][a-z0-9.-]*', entry.get('name', ''))]
    if len(candidates) != 1:
        raise ValueError('Event target Kind %s cannot be resolved unambiguously in %s.' % (kind, version))
    entry = candidates[0]
    namespace = ref.get('namespace') or ((row.get('raw') or {}).get('metadata') or {}).get('namespace')
    if entry['namespaced'] and (not isinstance(namespace, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', namespace)):
        raise ValueError('Namespace is missing or invalid in the Event target.')
    group = version.split('/')[0] if '/' in version else ''
    return dict(resource=entry['name'] + ('.' + group if group else ''), name=name,
                namespaced=entry['namespaced'], namespace=namespace if entry['namespaced'] else None,
                uid=ref['uid'], expected_kind=kind, expected_group=group)


def read_object(client, target):
    scope = ['--namespace', target['namespace']] if target['namespaced'] else []
    value, error = client.call('get', target['resource'], target['name'], *scope, '-o', 'json')
    if error:
        if re.search(r'\bNotFound\b', str(error)):
            raise ValueError('TARGET NOT FOUND\nStatus: the referenced resource does not currently exist.\nAPI response: ' + str(error))
        raise ValueError(error + '\nCannot read the original Event target. Check permissions/existence in Resources; the original Event JSON is retained.')
    meta = (value or {}).get('metadata') or {}
    version = (value or {}).get('apiVersion', '')
    group = version.split('/')[0] if '/' in version else ''
    if meta.get('name') != target['name'] or (target['namespaced'] and meta.get('namespace') != target['namespace']):
        raise ValueError('Returned object does not match the Event target.')
    if meta.get('uid') != target['uid']:
        raise ValueError('The Event target was replaced (UID mismatch). Open the current object from Resources; the original Event is retained.')
    if value.get('kind') != target['expected_kind'] or group != target['expected_group']:
        raise ValueError('Returned object API group/Kind does not match the Event target.')
    return value


def load(client, item, mode):
    try:
        target = resolve(client, item)
        obj = read_object(client, target)
        scope = ['--namespace', target['namespace']] if target['namespaced'] else []
        if mode == 'yaml':
            # Serialize the verified GET snapshot, not a second unchecked GET by
            # name that might already refer to a replacement with a different UID.
            return client.text('create', '--dry-run=client', '--validate=false', '-f', '-', *scope,
                               '-o', 'yaml', input_text=json.dumps(obj))
        if mode != 'describe':
            return None, 'Unsupported Event target detail mode.'
        manifest = dict(apiVersion=obj['apiVersion'], kind=obj['kind'], metadata={'name': target['name']})
        if target['namespaced']:
            manifest['metadata']['namespace'] = target['namespace']
        output, error = client.text('describe', '-f', '-', *scope, input_text=json.dumps(manifest))
        if error:
            return None, error
        # describe fetches by name internally. Discard its output if the Event's
        # UID is no longer current when the command finishes.
        read_object(client, target)
        return output, None
    except (ValueError, OSError, TypeError, AttributeError, KeyError) as exc:
        return None, str(exc)
