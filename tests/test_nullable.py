import unittest
from unittest.mock import Mock, patch
from concurrent.futures import Future
from kuba_dm_app.ui import dashboard as kuba_dm
from kuba_dm_app.resources import resource_views as rv
class NullableResponseTests(unittest.TestCase):
    def test_reported_endpointslice_null(self):
        obj = {'metadata': {'name': 'empty', 'namespace': 'default'}, 'endpoints': None, 'ports': None}
        headers, rows = rv.columns('endpointslices.discovery.k8s.io', [obj])
        self.assertEqual(dict(zip(headers, rows[0]))['READY/TOTAL'], '0/0')
        data = kuba_dm.demo()
        data['extras'] = {'endpointslices.discovery.k8s.io': [obj]}
        lines = kuba_dm.content_lines(data, '6', 115, category_tab=1)
        self.assertIn('0/0', '\n'.join(lines))

    def test_nullable_fields_across_categories(self):
        fixtures = {
            'services': {'spec': {'ports': None, 'externalIPs': None}, 'status': {'loadBalancer': None}},
            'endpointslices.discovery.k8s.io': {'metadata': {'name': 's', 'labels': None}, 'ports': None,
                'endpoints': [{'addresses': None, 'conditions': None}, {'addresses': ['10.0.0.1'], 'conditions': {'ready': False}}]},
            'ingresses.networking.k8s.io': {'metadata': {'name': 'i', 'annotations': None}, 'spec': {'rules': None, 'tls': None}, 'status': {'loadBalancer': {'ingress': None}}},
            'networkpolicies.networking.k8s.io': {'spec': {'podSelector': {'matchLabels': None, 'matchExpressions': None}, 'policyTypes': None, 'ingress': None, 'egress': None}},
            'persistentvolumeclaims': {'spec': {'accessModes': None}, 'status': {'capacity': None}},
            'persistentvolumes': {'spec': {'accessModes': None, 'capacity': None, 'claimRef': None}},
            'volumeattachments.storage.k8s.io': {'spec': {'source': None}, 'status': {'attachError': None, 'detachError': None, 'attached': False}},
            'roles.rbac.authorization.k8s.io': {'rules': None},
            'serviceaccounts': {'secrets': None},
            'rolebindings.rbac.authorization.k8s.io': {'subjects': None, 'roleRef': None},
            'widgets.example.io': {'status': {'conditions': None}},
        }
        for name, fields in fixtures.items():
            with self.subTest(resource=name):
                obj = {'metadata': {'name': 'fixture'}, **fields}
                headers, rows = rv.columns(name, [obj])
                self.assertEqual(len(headers), len(rows[0]))
                self.assertNotIn('None', rows[0])
        headers, rows = rv.columns('endpointslices.discovery.k8s.io', [fixtures['endpointslices.discovery.k8s.io']])
        self.assertEqual(dict(zip(headers, rows[0]))['READY/TOTAL'], '1/2')

    def test_null_items_and_secret_maps(self):
        client = Mock()
        client.call.return_value = ({'items': None}, None)
        self.assertEqual(rv.list_resource(client, 'secrets', True), ([], None))
        for kind in ('secrets', 'configmaps'):
            client.call.return_value = ({'items': [{'metadata': {'name': 'empty'}, 'data': None, 'binaryData': None}]}, None)
            objects, error = rv.list_resource(client, kind, True)
            self.assertIsNone(error)
            self.assertEqual(objects[0]['keyCount'], 0)

    def test_workload_optional_status_maps(self):
        pod = {'metadata': {'name': 'p'}, 'spec': {'containers': [], 'initContainers': None, 'overhead': None},
               'status': {'phase': 'Running', 'containerStatuses': None, 'initContainerStatuses': None, 'conditions': None}}
        self.assertEqual(kuba_dm.pod_issue(pod), 'NotReady')
        self.assertEqual(kuba_dm.pod_requests(pod)['cpu'], 0)
        controller = {'kind': 'Deployment', 'metadata': {'name': 'd'}, 'spec': {'replicas': 0},
                      'status': {'conditions': None, 'readyReplicas': None, 'updatedReplicas': None, 'availableReplicas': None}}
        self.assertEqual(kuba_dm.controller_issue(controller), '')

    def test_unknown_resource_group_and_status_shapes(self):
        for status in ('CustomState', ['Ready'], {'conditions': {'custom': True}}, None):
            headers, rows = rv.columns('services.example.io', [{'metadata': {'name': 'custom'}, 'status': status}])
            self.assertIn('API-VERSION', headers)
            self.assertNotIn('CLUSTER-IP', headers)
            self.assertEqual(len(headers), len(rows[0]))
        self.assertEqual(rv.selector({'matchLabels': 'literal-label'}), 'matchLabels=literal-label')
        self.assertEqual(rv.selector({'matchLabels': None, 'matchExpressions': None}, label_selector=True), '<all>')

    def test_render_failure_is_visible_and_clears_after_recovery(self):
        data = kuba_dm.demo()
        data['extras'] = {'services': [{'metadata': {'name': 'bad'}, 'spec': {'externalIPs': 23}}]}
        resource = {'name': 'services', 'namespaced': True}
        self.assertIn('Cannot render services', '\n'.join(kuba_dm.resource_lines(data, resource, 115)))
        self.assertIn('render:services', data['resource_errors'])
        data['extras']['services'] = rv.demo_resource('services', True)[0]
        self.assertIn('CLUSTER-IP', '\n'.join(kuba_dm.resource_lines(data, resource, 115)))
        self.assertNotIn('render:services', data['resource_errors'])

    def test_bad_response_and_failed_loader_do_not_break_tab_navigation(self):
        class Pool:
            def __init__(self, **kwargs): pass
            def submit(self, fn, *args):
                future = Future()
                try: future.set_result(fn(*args))
                except Exception as exc: future.set_exception(exc)
                return future
            def shutdown(self, **kwargs): pass
        class Terminal:
            def __init__(self):
                self.keys = iter([ord('6'), 10, -1, kuba_dm.curses.KEY_RIGHT, -1, kuba_dm.curses.KEY_RIGHT, -1, ord('q')])
                self.output = []
            def timeout(self, value): pass
            def getmaxyx(self): return 45, 160
            def erase(self): pass
            def addnstr(self, *args): self.output.append(str(args[2]))
            def refresh(self): pass
            def getch(self): return next(self.keys)
        def load(name, scope):
            if name == 'services':
                return [{'metadata': {'name': 'bad'}, 'spec': {'externalIPs': 23}}], None
            if name.startswith('endpointslices'):
                raise TypeError('fixture malformed response')
            return rv.demo_resource(name, scope)
        terminal = Terminal()
        with patch('kuba_dm_app.ui.dashboard.curses.curs_set'), patch('kuba_dm_app.ui.dashboard.curses.set_escdelay', create=True), \
             patch('kuba_dm_app.ui.dashboard.curses.mousemask'), patch('kuba_dm_app.ui.dashboard.curses.mouseinterval'), patch('kuba_dm_app.ui.dashboard.sys.stdout'), \
             patch('kuba_dm_app.ui.dashboard.concurrent.futures.ThreadPoolExecutor', Pool):
            self.assertEqual(kuba_dm.screen(terminal, kuba_dm.demo, 30, resource_loader=load), 'quit')
        output = '\n'.join(terminal.output)
        self.assertIn('Cannot render services', output)
        self.assertIn('Resource collection failed', output)
        self.assertIn('web.demo.invalid', output)


if __name__ == '__main__':
    unittest.main()
