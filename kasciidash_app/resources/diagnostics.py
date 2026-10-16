"""Read-only incident evidence. Never render free-form API messages or specs."""
import datetime as dt

def failure(error):
    value = str(error).lower()
    if 'forbidden' in value or 'unauthorized' in value:
        return 'permission denied'
    if 'timeout' in value or 'timed out' in value:
        return 'timeout'
    return 'API data unavailable'


RESOURCES = {'pods': 'Pod', 'deployments': 'Deployment', 'deployments.apps': 'Deployment'}


def meta(obj):
    return (obj or {}).get('metadata') or {}


def supports(item):
    return not item.get('event_context') and item.get('resource') in RESOURCES


def load(client, item):
    """Use the existing timeout-bound client and Detail's generation isolation."""
    args = ('get', item['resource'], item['name'], '--namespace', item['namespace'], '-o', 'json')
    obj, error = client.call(*args)
    if error:
        return 'Diagnostics unavailable: target query failed (' + failure(error) + ').', None
    def valid(value):
        return (isinstance(value, dict) and meta(value).get('name') == item['name']
                and meta(value).get('namespace') == item['namespace'] and meta(value).get('uid')
                and (not item.get('uid') or meta(value)['uid'] == item['uid'])
                and value.get('kind') == RESOURCES[item['resource']])
    if not valid(obj):
        return 'Diagnostics unavailable: target identity changed. Refresh the list.', None
    objects, sources = {}, []
    resources = ['events', 'persistentvolumeclaims', 'resourcequotas']
    if obj['kind'] == 'Deployment':
        resources += ['replicasets.apps', 'pods']
    else:
        objects['pods'] = [obj]
    for resource in resources:
        value, error = client.call('get', resource, '--namespace', item['namespace'], '-o', 'json')
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        available = not error and isinstance(value, dict) and isinstance(value.get('items'), list)
        objects[resource] = [row for row in value['items'] if meta(row).get('namespace') == item['namespace']] if available else None
        sources.append('%s: %s | queried %s' % (resource, 'collected' if available else 'unavailable (' + failure(error) + ')' , stamp))
    current, error = client.call(*args)
    if error or not valid(current) or meta(current)['uid'] != meta(obj)['uid'] or meta(current).get('generation') != meta(obj).get('generation'):
        return 'Diagnostics unavailable: target changed during collection. Refresh.', None
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    return report(current, objects, sources, stamp), None


def owned(obj, parent):
    return any(ref.get('uid') == meta(parent).get('uid') and ref.get('kind') == parent.get('kind')
               and ref.get('name') == meta(parent).get('name') and ref.get('controller') is True
               for ref in meta(obj).get('ownerReferences') or [])


def report(obj, objects, sources, stamp):
    lines = ['INCIDENT DIAGNOSTICS | READ ONLY', 'Source: kubectl get JSON | target queried ' + stamp,
             'Snapshot: separate API reads, not atomic; r refreshes. Retained events may be old.',
             'Evidence is incomplete when a field/source is unavailable; no finding does not mean healthy.',
             'Logs are fetched only when you select the existing Logs tab.', ''] + sources + ['']
    def finding(symptom, evidence, explanation, next_step):
        lines.extend(['Symptom: ' + symptom, 'Evidence: ' + evidence, 'Possible explanation: ' + explanation, 'Next check: ' + next_step, ''])
    pods = objects.get('pods')
    related = [obj]
    if obj.get('kind') == 'Deployment':
        status, spec = obj.get('status') or {}, obj.get('spec') or {}
        fields = ['updatedReplicas', 'readyReplicas', 'availableReplicas', 'replicas', 'observedGeneration']
        evidence = 'desired=%s; generation=%s; ' % (spec.get('replicas', 'uncollected'), meta(obj).get('generation', 'uncollected')) + '; '.join('%s=%s' % (key, status.get(key, 'uncollected')) for key in fields)
        generation, observed, desired = meta(obj).get('generation'), status.get('observedGeneration'), spec.get('replicas')
        if generation is None or observed is None or observed < generation:
            finding('Deployment status stale or uncollected', evidence, 'Controller may not have observed the current spec.', 'Refresh and inspect controller conditions and related ReplicaSet events.')
        elif desired is None or any(status.get(key) is None for key in fields[:4]):
            finding('Deployment completion unknown', evidence, 'Required counters are uncollected; absent values are not assumed zero.', 'Refresh and check Deployment status.')
        elif any(status[key] != desired for key in ('updatedReplicas', 'readyReplicas', 'availableReplicas', 'replicas')):
            finding('Deployment rollout incomplete', evidence, 'Rollout may still be progressing, paused, or blocked; counters alone do not prove a failure.', 'Inspect owned Pods, readiness, ReplicaSet events and quota usage.')
        else:
            lines += ['Deployment counters match desired replicas at collection time.', evidence, '']
        if not status.get('conditions'):
            lines += ['Deployment conditions: uncollected']
        for condition in status.get('conditions') or []:
            if condition.get('type') in ('Available', 'Progressing', 'ReplicaFailure'):
                lines += ['Deployment condition %s=%s; reason=%s' % (condition.get('type'), condition.get('status', 'uncollected'), safe_reason(condition.get('reason')))]
        replicasets = [rs for rs in objects.get('replicasets.apps') or [] if owned(rs, obj)]
        related += replicasets
        pods = [pod for pod in pods or [] if any(owned(pod, rs) for rs in replicasets)]
        if objects.get('pods') is None or objects.get('replicasets.apps') is None:
            lines += ['Pod relationship evidence incomplete: Pod or ReplicaSet inventory unavailable.']
    for pod in pods or []:
        related.append(pod)
        status, spec = pod.get('status') or {}, pod.get('spec') or {}
        label = 'Pod ' + str(meta(pod).get('name', '?'))
        lines += [label + ': phase=%s; node=%s' % (status.get('phase', 'uncollected'), spec.get('nodeName', 'unassigned/uncollected'))]
        scheduled = next((condition for condition in status.get('conditions') or [] if condition.get('type') == 'PodScheduled'), {})
        lines += [label + ': PodScheduled=' + str(scheduled.get('status', 'uncollected'))]
        for condition in status.get('conditions') or []:
            if condition.get('type') == 'PodScheduled' and condition.get('status') == 'False':
                finding(label + ' not scheduled', 'PodScheduled=False; reason=' + safe_reason(condition.get('reason')), 'Scheduling constraints, capacity, storage or admission may be involved. Pending alone does not identify a cause.', 'Inspect scheduler events, requests, affinity/taints and PVC binding.')
        for key in ('initContainerStatuses', 'containerStatuses'):
            if key not in status:
                lines += [label + ' ' + key + ': uncollected']
            for container in status.get(key) or []:
                prefix = label + ' / ' + key + ' / ' + str(container.get('name', '?'))
                waiting = (container.get('state') or {}).get('waiting')
                if waiting is not None:
                    finding(prefix + ' waiting', 'state.waiting.reason=' + safe_reason(waiting.get('reason')), 'Image retrieval, configuration, initialization or restart backoff may block startup.', 'Inspect related event reasons; verify image access and referenced configuration without displaying Secret values.')
                restarts = container.get('restartCount')
                lines += [prefix + ': cumulative restartCount=' + str(restarts if restarts is not None else 'uncollected')]
                if isinstance(restarts, int) and restarts > 1:
                    finding(prefix + ' repeated restarts recorded', 'cumulative restartCount=%s (not a recent-time rate)' % restarts, 'Past container failures or probes may explain restarts; frequency requires multiple observations.', 'Select Logs explicitly; compare observations and inspect termination reason/probes.')
                terminated = (container.get('lastState') or {}).get('terminated') or (container.get('state') or {}).get('terminated')
                if terminated:
                    reason = safe_reason(terminated.get('reason'))
                    lines += [prefix + ': termination reason=%s; exitCode=%s; finishedAt=%s' % (reason, terminated.get('exitCode', 'uncollected'), terminated.get('finishedAt', 'uncollected'))]
                    if reason == 'OOMKilled':
                        finding(prefix + ' OOM termination observed', 'termination.reason=OOMKilled', 'Memory limit or node pressure may be involved; this does not establish a memory leak.', 'Compare limits, usage history and node memory conditions.')
        claims = {((v.get('persistentVolumeClaim') or {}).get('claimName')) for v in spec.get('volumes') or []} - {None}
        collected_claims = {meta(pvc).get('name') for pvc in objects.get('persistentvolumeclaims') or []}
        for claim in sorted(claims - collected_claims):
            lines += ['Referenced PVC %s: %s' % (claim, 'uncollected' if objects.get('persistentvolumeclaims') is None else 'not found in collected inventory')]
        for pvc in objects.get('persistentvolumeclaims') or []:
            if meta(pvc).get('name') in claims:
                lines += ['Referenced PVC %s: phase=%s' % (meta(pvc).get('name'), (pvc.get('status') or {}).get('phase', 'uncollected'))]
    uids = {meta(value).get('uid') for value in related} - {None}
    for event in objects.get('events') or []:
        ref = event.get('involvedObject') or event.get('regarding') or {}
        if ref.get('uid') in uids:
            lines += ['Related retained event: reason=%s; time=%s; count=%s (message withheld)' % (safe_reason(event.get('reason')), event.get('eventTime') or event.get('lastTimestamp') or 'uncollected', event.get('count', 'uncollected'))]
    for quota in objects.get('resourcequotas') or []:
        status = quota.get('status') or {}
        for key in sorted(set(status.get('hard') or {}) | set(status.get('used') or {})):
            lines += ['Namespace quota %s / %s: used=%s; hard=%s (namespace scope; not proof of this incident)' % (meta(quota).get('name'), key, (status.get('used') or {}).get(key, 'uncollected'), (status.get('hard') or {}).get(key, 'uncollected'))]
    return '\n'.join(lines)


def safe_reason(value):
    # API reason codes only; do not expose arbitrary messages or credentials.
    known = {'Unschedulable', 'SchedulingGated', 'ImagePullBackOff', 'ErrImagePull', 'CrashLoopBackOff', 'ContainerCreating', 'CreateContainerConfigError', 'CreateContainerError', 'PodInitializing', 'OOMKilled', 'Error', 'Completed', 'ProgressDeadlineExceeded', 'NewReplicaSetAvailable', 'MinimumReplicasUnavailable', 'MinimumReplicasAvailable', 'FailedCreate', 'FailedScheduling', 'FailedMount', 'BackOff', 'Failed', 'ReplicaSetUpdated', 'ReplicaSetCreateError', 'DeploymentPaused'}
    return value if value in known else 'other/uncollected (raw reason withheld)'
