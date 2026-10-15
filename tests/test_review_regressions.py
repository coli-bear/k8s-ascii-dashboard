"""Operational races and isolation, using synthetic data only."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from kdash_app.ui import dashboard as kdash
from kdash_app.monitoring import prometheus as m
from kdash_app.config import profiles
from kdash_app.resources import resource_actions as a
from kdash_app.resources import resource_detail as rd
from kdash_app.resources import resource_views as rv
from tests.test_resource_actions import client, obj, selected


class MutationRaceTests(unittest.TestCase):
    def test_restart_preserves_template_metadata_and_is_conditional(self):
        for annotations in (None, {}, {'existing/key': 'keep'}):
            c = client()
            original = obj()
            metadata = {'labels': {'app': 'web'}}
            if annotations is not None:
                metadata['annotations'] = copy.deepcopy(annotations)
            original['spec']['template'] = {'metadata': metadata, 'spec': {'containers': []}}
            c.call.return_value = original, None
            with patch.object(a, 'choose', return_value='restart'), patch.object(a, 'confirmed', return_value=True), patch('builtins.print'):
                a.change(c, selected())
            args = c.text.call_args.args
            operations = json.loads(args[args.index('-p') + 1])
            patched = copy.deepcopy(original)
            for operation in operations:
                parts = [v.replace('~1', '/').replace('~0', '~') for v in operation['path'].split('/')[1:]]
                parent = patched
                for part in parts[:-1]:
                    parent = parent[part]
                if operation['op'] == 'test':
                    self.assertEqual(parent[parts[-1]], operation['value'])
                else:
                    parent[parts[-1]] = operation['value']
            self.assertEqual(patched['spec']['template']['metadata']['labels'], {'app': 'web'})
            self.assertIn('kubectl.kubernetes.io/restartedAt', patched['spec']['template']['metadata']['annotations'])
            if annotations:
                self.assertEqual(patched['spec']['template']['metadata']['annotations']['existing/key'], 'keep')

    def test_delete_core_and_custom_cluster_scoped_paths(self):
        for item, original, expected in [
            ({'resource': 'nodes', 'name': 'worker', 'namespaced': False}, {'apiVersion': 'v1', 'metadata': {'name': 'worker', 'uid': 'fake-node', 'resourceVersion': '7'}}, '/api/v1/nodes/worker'),
            ({'resource': 'widgets.example.io', 'name': 'sample', 'namespaced': False}, {'apiVersion': 'example.io/v1alpha1', 'metadata': {'name': 'sample', 'uid': 'fake-widget', 'resourceVersion': '8'}}, '/apis/example.io/v1alpha1/widgets/sample')]:
            c = client()
            c.call.return_value = original, None
            with patch.object(a, 'choose', return_value='delete'), patch.object(a, 'confirmed', return_value=True), patch('builtins.print'):
                a.change(c, item)
            self.assertIn('--raw=' + expected, c.text.call_args.args)
            self.assertEqual(json.loads(c.text.call_args.kwargs['input_text'])['preconditions']['uid'], original['metadata']['uid'])

    def test_replacement_and_update_during_confirmation_are_rejected(self):
        for action, menu in [('start', '1'), ('stop', '2'), ('restart', '3'), ('delete', '4')]:
            for replacement in (True, False):
                with self.subTest(action=action, replacement=replacement):
                    c = client()
                    state = obj()
                    mutated = []

                    def confirm(word):
                        if replacement:
                            state['metadata']['uid'] = 'replacement'
                        state['metadata']['resourceVersion'] = '6'
                        return True

                    def server(*args, **kwargs):
                        # Model server-side preconditions, not a second client GET.
                        if args[0] == 'delete' and any(v.startswith('--raw=') for v in args):
                            pre = json.loads(kwargs['input_text']).get('preconditions', {})
                            ok = pre.get('uid') == state['metadata']['uid'] and pre.get('resourceVersion') == state['metadata']['resourceVersion']
                        elif args[0] == 'scale':
                            ok = '--resource-version=' + state['metadata']['resourceVersion'] in args
                        elif args[0] == 'patch':
                            operations = json.loads(args[args.index('-p') + 1])
                            tests = {v['path']: v['value'] for v in operations if v['op'] == 'test'}
                            ok = tests.get('/metadata/uid') == state['metadata']['uid'] and tests.get('/metadata/resourceVersion') == state['metadata']['resourceVersion']
                        else:
                            ok = False
                            mutated.append(copy.deepcopy(state))
                            return 'unsafe mutation', None
                        if not ok:
                            return '', 'Conflict: precondition failed'
                        mutated.append(copy.deepcopy(state))
                        return 'ok', None

                    c.text.side_effect = server
                    with patch.object(a, 'choose', return_value=action), patch.object(a, 'confirmed', side_effect=confirm), patch.object(profiles, 'prompt', return_value='4'), patch('builtins.print'):
                        with self.assertRaisesRegex(ValueError, 'Refresh|refresh'):
                            a.change(c, selected())
                    self.assertEqual(mutated, [])
                    self.assertEqual(c.call.call_count, 1)

    def test_missing_identity_fails_closed(self):
        for key in ('uid', 'resourceVersion'):
            c = client()
            value = obj()
            value['metadata'].pop(key)
            c.call.return_value = value, None
            with patch.object(a, 'choose', return_value='delete'), patch.object(a, 'confirmed', return_value=True), patch('builtins.print'):
                with self.assertRaises(ValueError):
                    a.change(c, selected())
            c.text.assert_not_called()


class YamlSnapshotTests(unittest.TestCase):
    def test_approved_payload_survives_external_draft_changes(self):
        replacements = ['kind: Namespace\nmetadata:\n  name: changed\n',
                        'kind: Deployment\nmetadata:\n  name: changed\n  namespace: other\n',
                        'kind: Pod\n---\nkind: Namespace\n']
        for new in (True, False):
            for replacement in replacements:
                with self.subTest(new=new, replacement=replacement), tempfile.TemporaryDirectory() as directory:
                    c = client()
                    validated = []
                    submitted = []
                    diffed = []

                    def read_payload(args, kwargs):
                        filename = args[args.index('-f') + 1]
                        return kwargs.get('input_text') if filename == '-' else Path(filename).read_text()

                    def call(*args, **kwargs):
                        if '--dry-run=server' in args:
                            validated.append(read_payload(args, kwargs))
                            return obj(), None
                        if args[:2] == ('get', '--raw'):
                            if args[-1] == '/apis/apps':
                                return {'preferredVersion': {'groupVersion': 'apps/v1'}}, None
                            return {'resources': [{'name': 'deployments', 'kind': 'Deployment', 'namespaced': True, 'verbs': ['create']}]}, None
                        return obj(), None

                    def text(*args, **kwargs):
                        if '--dry-run=client' in args:
                            return json.dumps(a.editable_object(obj())), None
                        submitted.append(read_payload(args, kwargs))
                        return 'ok', None

                    def run(*args, **kwargs):
                        diffed.append(read_payload(args, kwargs))
                        return Mock(returncode=1, stdout='', stderr='')

                    edited_paths = []
                    def editor(argv):
                        edited_paths.append(Path(argv[-1]))
                        return Mock(returncode=0)

                    def prompt(question):
                        if question.startswith('New resource'):
                            return 'web'
                        if question.startswith('Namespace'):
                            return 'prod'
                        if question.startswith('Type '):
                            edited_paths[-1].write_text(replacement)
                            return 'create' if new else 'apply'
                        return ''

                    c.call.side_effect, c.text.side_effect, c.run.side_effect = call, text, run
                    with patch.dict(os.environ, {'VISUAL': '', 'EDITOR': 'fixture-editor'}), patch.object(a.subprocess, 'run', side_effect=editor), patch.object(profiles, 'prompt', side_effect=prompt), patch('builtins.print'):
                        a.edit_yaml(c, directory, selected(), new=new)
                    self.assertEqual(submitted, validated)
                    self.assertEqual(len(submitted), 1)
                    if not new:
                        self.assertEqual(diffed, validated)
                    self.assertNotEqual(submitted[0], replacement)

    def test_client_json_call_forwards_stdin(self):
        c = kdash.Client('/synthetic.conf', None, 1)
        with patch.object(c, 'text', return_value=('{}', None)) as text:
            self.assertEqual(c.call('create', '-f', '-', input_text='fixture'), ({}, None))
        text.assert_called_once_with('create', '-f', '-', input_text='fixture')


class ClusterIsolationTests(unittest.TestCase):
    def reader(self, families, tab, filters=None):
        calls = []

        def query(config, expr, timeout, **kwargs):
            calls.append(expr)
            found = sorted({label for source, labels in families.items() if source in expr for label in labels})
            exact = re.search(r'cluster=("(?:[^"\\]|\\.)*")', expr)
            if exact:
                label = json.loads(exact.group(1))
                found = [v for v in found if v == label]
            if expr.startswith('count by(cluster)'):
                result = [{'metric': {'cluster': label} if label else {}, 'value': [1000, '1']} for label in found]
            elif expr.startswith('node_uname_info'):
                result = []
            elif expr.startswith('max(timestamp'):
                result = []
            else:
                result = [{'metric': {'cluster': label, 'instance': 'shared:9100', 'namespace': 'same', 'pod': 'same'}, 'values': [[1000, '1']]} for label in found]
            return {'data': {'result': result}}

        with patch.object(m.time, 'time', return_value=1000):
            snapshot = m.Reader({}, query=query)(tab, filters or {}, 300)
        return snapshot, calls

    def test_cluster_exact_match_and_safe_empty_match(self):
        snap, calls = self.reader({'node_cpu_seconds_total': ['prod-east', 'prod-west']}, 0, {'cluster': 'prod'})
        self.assertTrue(all(not card['series'] for card in snap['cards']))
        snap, calls = self.reader({'node_cpu_seconds_total': ['prod', 'prod-east', 'prod-west']}, 0, {'cluster': 'prod'})
        self.assertFalse(snap['errors'])
        self.assertEqual({s['metric']['cluster'] for card in snap['cards'] for s in card['series']}, {'prod'})
        self.assertTrue(all('cluster="prod"' in card['query'] for card in snap['cards']))

    def test_all_used_source_families_are_checked_over_history(self):
        for tab, families in [
            (0, {'node_memory_MemAvailable_bytes': ['a', 'b']}),
            (1, {'container_memory_working_set_bytes': ['a', 'b']}),
            (1, {'container_cpu_usage_seconds_total': ['a'], 'kube_pod_container_resource_requests': ['b']}),
            (3, {'container_cpu_cfs_throttled_periods_total': ['a', 'b']}),
            (2, {'container_network_transmit_bytes_total': ['a', 'b']}),
            (2, {'istio_requests_total': ['a', 'b']})]:
            with self.subTest(tab=tab, families=families):
                snap, calls = self.reader(families, tab)
                self.assertTrue(snap['errors'])
                self.assertEqual(snap['cards'], [])
                self.assertTrue(any('count_over_time' in expr and '[600s]' in expr for expr in calls))

    def test_selected_cluster_scopes_istio_and_network_fallback(self):
        for families in ({'istio_requests_total': ['prod', 'prod-east']},
                         {'container_network_receive_bytes_total': ['prod', 'prod-east'], 'container_network_transmit_bytes_total': ['prod', 'prod-west']}):
            snap, calls = self.reader(families, 2, {'cluster': 'prod'})
            self.assertFalse(snap['errors'])
            self.assertTrue(all('cluster="prod"' in card['query'] for card in snap['cards']))
            self.assertEqual({s['metric']['cluster'] for card in snap['cards'] for s in card['series']}, {'prod'})

    def test_inferred_cluster_is_pinned_and_retained_in_grouping(self):
        snap, calls = self.reader({'container_cpu_usage_seconds_total': ['prod']}, 1)
        self.assertFalse(snap['errors'])
        self.assertTrue(all('cluster="prod"' in card['query'] for card in snap['cards']))
        self.assertTrue(all('by(cluster,' in card['query'] for card in snap['cards']))


class PodAndMetadataTests(unittest.TestCase):
    def pod(self, phase='Pending', reason=None, init=False):
        value = {'metadata': {'name': 'fixture', 'namespace': 'test'}, 'status': {'phase': phase, 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        if reason:
            key = 'initContainerStatuses' if init else 'containerStatuses'
            value['status'][key] = [{'state': {'waiting': {'reason': reason}}}]
        return value

    def test_pending_and_running_reasons_filter_and_count(self):
        for phase, reason, init in [('Pending', 'CrashLoopBackOff', True), ('Pending', 'ImagePullBackOff', False), ('Pending', 'ErrImagePull', True), ('Running', 'CrashLoopBackOff', False)]:
            with self.subTest(phase=phase, reason=reason):
                pod = self.pod(phase, reason, init)
                self.assertEqual(kdash.pod_issue(pod), reason)
                data = {'pods': [pod]}
                self.assertEqual(len(kdash.filtered_workloads(data, 'pods', {'status': reason})['pods']), 1)
                self.assertIn(reason + ': 1', '\n'.join(kdash.workload_summary(data, 'pods')))
                self.assertIn(phase + ': 1', '\n'.join(kdash.workload_summary(data, 'pods')))

    def test_normal_init_terminal_and_terminating(self):
        self.assertEqual(kdash.pod_issue(self.pod()), 'Pending')
        pod = self.pod('Running')
        pod['status']['initContainerStatuses'] = [{'state': {'terminated': {'exitCode': 0, 'reason': 'Completed'}}}]
        self.assertEqual(kdash.pod_issue(pod), '')
        self.assertEqual(kdash.pod_issue(self.pod('Succeeded')), '')
        self.assertEqual(kdash.pod_issue(self.pod('Failed')), 'Failed')
        pod['metadata']['deletionTimestamp'] = 'fixture'
        self.assertEqual(kdash.pod_issue(pod), 'Terminating')

    def test_initializing_app_does_not_hide_failed_init_container(self):
        pod = self.pod('Pending', 'CrashLoopBackOff', init=True)
        pod['status']['containerStatuses'] = [{'state': {'waiting': {'reason': 'PodInitializing'}}}]
        self.assertEqual(kdash.pod_issue(pod), 'CrashLoopBackOff')
        pod = self.pod('Pending', 'ImagePullBackOff')
        pod['status']['containerStatuses'].insert(0, {'state': {'waiting': {'reason': 'ContainerCreating'}}})
        self.assertEqual(kdash.pod_issue(pod), 'ImagePullBackOff')

    def test_sensitive_metadata_is_excluded_in_category_and_api_lists(self):
        for resource, kind in [('secrets', 'Secret'), ('secret', 'Secret'), ('configmaps', 'ConfigMap')]:
            c = Mock()
            value = {'apiVersion': 'v1', 'kind': kind, 'metadata': {'name': 'fixture', 'namespace': 'test', 'uid': 'fake-uid', 'resourceVersion': '7', 'annotations': {'kubectl.kubernetes.io/last-applied-configuration': '{"data":{"key":"FAKE_SENSITIVE"}}', 'custom/payload': 'FAKE_SENSITIVE'}, 'managedFields': [{'payload': 'FAKE_SENSITIVE'}]}, 'data': {'key': 'FAKE_SENSITIVE'}, 'binaryData': {'key2': 'FAKE_SENSITIVE'}, 'stringData': {'key3': 'FAKE_SENSITIVE'}}
            c.call.return_value = {'items': [value]}, None
            listed, error = rv.list_resource(c, resource, True)
            self.assertIsNone(error)
            self.assertNotIn('FAKE_SENSITIVE', json.dumps(listed))
            self.assertEqual(listed[0]['metadata']['uid'], 'fake-uid')
            self.assertEqual(value['data']['key'], 'FAKE_SENSITIVE')

    def test_sensitive_yaml_detail_is_fetched_only_on_explicit_request(self):
        c = Mock()
        c.text.return_value = 'data:\n  key: FAKE_DETAIL_PAYLOAD\n', None
        item = {'resource': 'secrets', 'name': 'fixture', 'namespace': 'test', 'namespaced': True}
        output, error = rd.load(c, item, 'yaml')
        self.assertIsNone(error)
        self.assertIn('FAKE_DETAIL_PAYLOAD', output)
        c.text.assert_called_once_with('get', 'secrets', 'fixture', '--namespace', 'test', '-o', 'yaml')


if __name__ == '__main__':
    unittest.main()
