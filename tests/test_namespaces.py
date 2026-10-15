import copy
import json
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch

from kasciidash_app.ui import dashboard as kasciidash
from kasciidash_app.resources import namespace_views as nv
from kasciidash_app.resources import resource_detail as rd
from kasciidash_app.resources import resource_views as rv
class NamespaceTests(unittest.TestCase):
    def client(self, denied=(), discovery_error=None):
        snapshot = nv.demo()
        c = Mock()
        c.text.return_value = 'pods\nwidgets.example.io\ninvalid/path\n', discovery_error

        def call(*args):
            if args[:2] == ('get', 'namespaces'):
                return copy.deepcopy(snapshot['namespace']), None
            resource = args[1]
            if resource in denied:
                return None, 'Forbidden fixture'
            rows = copy.deepcopy(snapshot['objects'].get(resource, []))
            if resource == 'widgets.example.io':
                rows = [{'apiVersion': 'example.io/v1', 'kind': 'Widget', 'metadata': {'name': 'custom', 'namespace': 'demo', 'uid': 'custom'}}]
            if resource in ('secrets', 'configmaps'):
                for obj in rows:
                    obj['data'] = {'key': 'FAKE_SENSITIVE'}
                    obj['metadata']['annotations'] = {'kubectl.kubernetes.io/last-applied-configuration': 'FAKE_SENSITIVE'}
            # Simulate an unexpected response containing another namespace.
            rows += [{'kind': 'Pod', 'metadata': {'name': 'foreign', 'namespace': 'other'}}]
            return {'items': rows}, None

        c.call.side_effect = call
        return c

    def test_scoped_collection_discovery_and_sensitive_data(self):
        c = self.client(denied=('jobs.batch',), discovery_error='Partial discovery')
        snapshot, error = rd.load(c, {'resource': 'namespaces', 'name': 'demo', 'namespace': None, 'namespaced': False, 'uid': 'namespace-demo'}, 'overview')
        self.assertIsNone(error)
        self.assertIn('widgets.example.io', snapshot['objects'])
        self.assertNotIn('invalid/path', snapshot['objects'])
        self.assertIsNone(snapshot['objects']['jobs.batch'])
        self.assertIn('jobs.batch', snapshot['errors'])
        self.assertIn('discovery', snapshot['errors'])
        self.assertNotIn('FAKE_SENSITIVE', json.dumps(snapshot))
        self.assertNotIn('foreign', json.dumps(snapshot))
        for call in c.call.call_args_list:
            self.assertNotIn('-A', call.args)
            if call.args[1] != 'namespaces':
                self.assertIn('--namespace', call.args)
                self.assertEqual(call.args[call.args.index('--namespace') + 1], 'demo')

    def test_missing_replaced_namespace_and_mid_collection_replacement(self):
        c = self.client()
        c.call.side_effect = None
        c.call.return_value = None, 'NotFound fixture'
        self.assertEqual(nv.load(c, {'name': 'gone'}), (None, 'NotFound fixture'))
        c.text.assert_not_called()
        c = self.client()
        value = nv.demo()['namespace']
        with patch.object(c, 'call', return_value=(value, None)):
            self.assertIn('replaced', nv.load(c, {'name': 'demo', 'uid': 'old-uid'})[1])
        c = self.client()
        original = c.call.side_effect
        reads = []

        def call(*args):
            value, error = original(*args)
            if args[1] == 'namespaces':
                reads.append(1)
                if len(reads) == 2:
                    value['metadata']['uid'] = 'replacement'
            return value, error

        c.call.side_effect = call
        self.assertIn('changed identity', nv.load(c, {'name': 'demo'})[1])

    def test_list_filters_partial_data_counts_and_all_widths(self):
        data = kasciidash.demo()
        data['extras'] = {'namespaces': rv.demo_resource('namespaces', False)[0]}
        data['resource_state'] = {'index': 1}
        self.assertEqual([obj['metadata']['name'] for obj in nv.matches(data, {'status': 'terminating'})], ['retiring'])
        self.assertEqual([obj['metadata']['name'] for obj in nv.matches(data, {'name': 'demo'})], ['demo'])
        self.assertEqual(nv.matches(data, {'name': 'demo', 'ns': 'default'}), [])
        for width in (40, 60, 84, 100, 160, 220):
            for height in (18, 30, 55):
                lines, *_ = kasciidash.layout(data, 'Z', width, height=height, focus='content')
                self.assertLessEqual(len(lines), height)
                self.assertTrue(all(len(line) == width and line.isascii() for line in lines))
                self.assertIn('| > ', '\n'.join(lines))
        data['extras']['namespaces'] = None
        data['resource_errors'] = {'namespaces': 'Forbidden fixture'}
        self.assertIn('Forbidden', ' '.join(nv.list_lines(data, 100, {}, kasciidash.ascii_table)))

    def test_namespace_tabs_share_snapshot_and_refresh_invalidates_all_four(self):
        item = dict(resource='namespaces', name='demo', namespaced=False, namespace=None, read_only=True)
        browser = rd.Detail()
        browser.open(item, 8)
        pool = Mock()
        future = Future()
        pool.submit.return_value = future
        loader = Mock()
        browser.poll(pool, loader)
        future.set_result((nv.demo(), None))
        browser.poll(pool, loader)
        for mode in ('overview', 'resources', 'treeview', 'topology', 'yaml', 'describe', 'warnings', 'errors'):
            self.assertEqual(browser.mode, mode)
            if mode in nv.MODES:
                self.assertFalse(browser.state()['loading'])
                browser.poll(pool, loader)
            browser.switch()
        self.assertEqual(browser.mode, 'overview')
        self.assertEqual(pool.submit.call_count, 1)
        browser.refresh()
        self.assertTrue(all(mode not in browser.buffers for mode in nv.MODES))

    def test_overview_resources_and_topology_render_responsive(self):
        snapshot = nv.demo()
        snapshot['objects']['jobs.batch'] = None
        snapshot['errors']['jobs.batch'] = 'Forbidden fixture'
        snapshot['objects']['resourcequotas'] = [{'metadata': {'name': 'quota'}, 'status': {'hard': {'requests.cpu': '4'}, 'used': {'requests.cpu': '1'}}}]
        snapshot['objects']['limitranges'] = [{'metadata': {'name': 'limits'}, 'spec': {'limits': [{'type': 'Container', 'min': {'cpu': '100m'}, 'max': {'memory': '1Gi'}}]}}]
        for mode in nv.MODES:
            for width in (40, 60, 84, 100, 160, 220):
                lines = nv.render(snapshot, mode, width, kasciidash.ascii_table, kasciidash.clean)
                self.assertTrue(all(len(line) <= width and line.isascii() for line in lines))
                self.assertIn('Forbidden', '\n'.join(lines))
        overview = '\n'.join(nv.render(snapshot, 'overview', 140, kasciidash.ascii_table, kasciidash.clean))
        self.assertIn('jobs.batch', overview)
        self.assertIn('N/A', overview)
        self.assertIn('requests.cpu', overview)
        self.assertIn('min.cpu', overview)
        topology = '\n'.join(nv.treeview(snapshot))
        for value in ('[Deployment] web', '[ReplicaSet] web-abc', '[Pod] web-abc-1', '[Service] web', '[Ingress] web', '[ConfigMap] web-config', '[Secret] web-secret', '[PersistentVolumeClaim] web-data'):
            self.assertIn(value, topology)
        self.assertIn('selector candidate', topology)

    def test_owner_uid_selectorless_service_endpoint_and_cycles(self):
        snapshot = nv.demo()
        replicaset = snapshot['objects']['replicasets.apps'][0]
        replicaset['metadata']['ownerReferences'][0]['uid'] = 'old-deployment'
        service = snapshot['objects']['services'][0]
        service['spec']['selector'] = {}
        snapshot['objects']['endpointslices.discovery.k8s.io'] = [{'apiVersion': 'discovery.k8s.io/v1', 'kind': 'EndpointSlice', 'metadata': {'name': 'slice', 'namespace': 'demo', 'labels': {'kubernetes.io/service-name': 'web'}}, 'endpoints': [{'addresses': ['10.0.0.1'], 'targetRef': {'kind': 'Pod', 'name': 'web-abc-1', 'namespace': 'demo', 'uid': 'wrong-uid'}, 'conditions': {'ready': False}}]}]
        text = '\n'.join(nv.treeview(snapshot))
        self.assertIn('owner unavailable', text)
        self.assertIn('UID=old-deployment', text)
        self.assertNotIn('[selector candidate]', text)
        self.assertIn('unresolved target', text)
        self.assertIn('ready=False', text)
        deployment = snapshot['objects']['deployments.apps'][0]
        deployment['metadata']['ownerReferences'] = [{'uid': 'ReplicaSet-web-abc'}]
        replicaset['metadata']['ownerReferences'][0]['uid'] = 'Deployment-web'
        self.assertIn('[cycle]', '\n'.join(nv.treeview(snapshot)))

    def test_selector_expressions_external_name_and_policy_are_not_traffic(self):
        self.assertTrue(nv.selector_matches({}, {'matchExpressions': [{'key': 'app', 'operator': 'NotIn', 'values': ['web']}]}))
        self.assertFalse(nv.selector_matches({}, {'matchExpressions': [{'key': 'app', 'operator': 'Exists'}]}))
        snapshot = nv.demo()
        snapshot['objects']['services'][0]['spec'] = {'type': 'ExternalName', 'externalName': 'example.invalid'}
        snapshot['objects']['networkpolicies.networking.k8s.io'] = [{'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': {'name': 'all'}, 'spec': {'podSelector': {}}}]
        text = '\n'.join(nv.treeview(snapshot))
        self.assertIn('external DNS: example.invalid', text)
        self.assertIn('not connectivity verdicts', text)
        self.assertIn('[selected] [Pod] web-abc-1', text)

    def test_projected_env_image_pull_references_and_crd_kind_collision(self):
        snapshot = nv.demo()
        pod = snapshot['objects']['pods'][0]
        pod['spec']['volumes'].append({'projected': {'sources': [{'secret': {'name': 'projected'}}, {'configMap': {'name': 'projected-config'}}]}})
        pod['spec']['containers'] = [{'env': [{'valueFrom': {'secretKeyRef': {'name': 'environment', 'key': 'not-shown'}}}], 'envFrom': [{'configMapRef': {'name': 'environment-config'}}]}]
        pod['spec']['imagePullSecrets'] = [{'name': 'registry'}]
        snapshot['objects']['services.example.io'] = [{'apiVersion': 'example.io/v1', 'kind': 'Service', 'metadata': {'name': 'crd-service', 'namespace': 'demo', 'uid': 'crd'}, 'spec': {'selector': {'app': 'web'}}}]
        text = '\n'.join(nv.treeview(snapshot))
        for value in ('[Secret] projected', '[ConfigMap] projected-config', '[Secret] environment', '[ConfigMap] environment-config', '[Secret] registry'):
            self.assertIn(value, text)
        self.assertNotIn('not-shown', text)
        self.assertNotIn('+- [Service] crd-service', text)

    def test_pod_state_and_container_issue_are_readable_without_changing_identity(self):
        snapshot = nv.demo()
        pod = snapshot['objects']['pods'][0]
        pod['spec']['containers'] = [{'name': 'app'}]
        pod['status'].update(conditions=[{'type': 'Initialized', 'status': 'True'}, {'type': 'Ready', 'status': 'False'}],
                             containerStatuses=[{'name': 'app', 'ready': False, 'state': {'waiting': {'reason': 'CrashLoopBackOff'}}}])
        lines = nv.treeview(snapshot)
        self.assertTrue(all('Status:' not in line for line in lines if '[Pod] web-abc-1' in line))
        text = '\n'.join(lines)
        self.assertIn('Status: Running | Ready: False | Containers: 0/1', text)
        self.assertIn('Node: worker-01', text)
        self.assertIn('Container issue: app | CrashLoopBackOff', text)
        self.assertIn('Conditions: Initialized=True | Ready=False', text)

    def test_service_tree_exposes_slice_and_avoids_duplicate_pod_details(self):
        snapshot = nv.demo()
        pod = snapshot['objects']['pods'][0]
        snapshot['objects']['endpointslices.discovery.k8s.io'] = [
            {'apiVersion': 'discovery.k8s.io/v1', 'kind': 'EndpointSlice',
             'metadata': {'name': 'web-slice', 'uid': 'slice-uid', 'labels': {'kubernetes.io/service-name': 'web'}},
             'endpoints': [{'conditions': {'ready': True}, 'targetRef': {'kind': 'Pod', 'name': 'web-abc-1', 'uid': pod['metadata']['uid']}}]}]
        text = '\n'.join(nv.treeview(snapshot)).split('INGRESS / SERVICE / POD')[1].split('POD RESOURCE REFERENCES')[0]
        self.assertIn('[EndpointSlice] web-slice', text)
        self.assertIn('Endpoint: ready=True', text)
        self.assertIn('selector candidates: 1', text)
        self.assertEqual(text.count('[Pod] web-abc-1'), 1)
        self.assertEqual(text.count('Status: Running'), 1)
        # Missing/stale target UIDs remain unresolved; selector matches are not
        # silently promoted to actual endpoints.
        snapshot['objects']['endpointslices.discovery.k8s.io'][0]['endpoints'][0]['targetRef']['uid'] = 'old-uid'
        text = '\n'.join(nv.treeview(snapshot))
        self.assertIn('unresolved target', text)
        self.assertIn('Selector candidates without a resolved endpoint', text)

    def test_topology_display_limit_is_explicit(self):
        snapshot = nv.demo()
        prototype = snapshot['objects']['pods'][0]
        snapshot['objects']['pods'] = []
        for i in range(2600):
            pod = copy.deepcopy(prototype)
            pod['metadata']['uid'] = 'pod-' + str(i)
            pod['metadata']['name'] = 'pod-' + str(i)
            snapshot['objects']['pods'].append(pod)
        lines = nv.treeview(snapshot)
        self.assertLessEqual(len(lines), 2501)
        self.assertIn('topology limited', lines[-1])

    def test_keyboard_namespace_entry_filter_tabs_refresh_esc_and_no_actions(self):
        class Pool:
            def __init__(self, **kwargs): pass
            def submit(self, fn, *args):
                future = Future()
                future.set_result(fn(*args))
                return future
            def shutdown(self, **kwargs): pass
        keys = iter([ord('Z'), 10, -1, ord('/'), *map(ord, 'name=demo'), 10, 10, -1,
                     kasciidash.curses.KEY_RIGHT, -1, kasciidash.curses.KEY_RIGHT, -1, kasciidash.curses.KEY_RIGHT, -1,
                     ord('r'), -1, ord('a'), ord('e'), ord('!'), 27, -1, 27, -1, ord('q')])
        class Terminal:
            def timeout(self, value): pass
            def getmaxyx(self): return 40, 160
            def erase(self): pass
            def addnstr(self, *args): pass
            def refresh(self): pass
            def getch(self): return next(keys)
        loads, frames = [], []
        action = Mock()
        layout = kasciidash.layout
        def detail(item, mode):
            loads.append((item, mode))
            return rd.demo(item, mode)
        def record(data, *args, **kwargs):
            frames.append((args[0], kwargs['focus'], copy.deepcopy(data.get('resource_detail'))))
            return layout(data, *args, **kwargs)
        with patch('kasciidash_app.ui.dashboard.concurrent.futures.ThreadPoolExecutor', Pool), patch('kasciidash_app.ui.dashboard.layout', record), patch('kasciidash_app.ui.dashboard.curses.curs_set'), patch('kasciidash_app.ui.dashboard.curses.set_escdelay', create=True), patch('kasciidash_app.ui.dashboard.curses.mousemask'), patch('kasciidash_app.ui.dashboard.curses.mouseinterval'), patch('kasciidash_app.ui.dashboard.sys.stdout'):
            self.assertEqual(kasciidash.screen(Terminal(), kasciidash.demo, 30, resource_loader=rv.demo_resource, detail_loader=detail, action_handler=action), 'quit')
        self.assertEqual([mode for _, mode in loads], ['overview', 'topology'])
        self.assertTrue(all(item['name'] == 'demo' and item['read_only'] for item, _ in loads))
        self.assertEqual(frames[-1][:2], ('Z', 'nav'))
        action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
