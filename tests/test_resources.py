import unittest
from unittest.mock import Mock, patch
from concurrent.futures import Future
from kdash_app.ui import dashboard as kdash
from kdash_app.resources import resource_views as rv
class ResourceTests(unittest.TestCase):
    def test_filters_and_counts(self):
        data = kdash.demo()
        query = rv.parse_filter('name=WEB namespace=DEMO status=running')
        filtered = kdash.filtered_workloads(data, 'pods', query)
        self.assertEqual([p['metadata']['name'] for p in filtered['pods']], ['web-abc'])
        self.assertEqual(len(kdash.filtered_workloads(data, 'pods', {'status': 'healthy'})['pods']), 1)
        self.assertEqual(len(kdash.filtered_workloads(data, 'pods', {'status': 'issues'})['pods']), 1)
        self.assertEqual(rv.parse_filter('status="Ready 0/2"'), {'status': 'Ready 0/2'})
        self.assertEqual(rv.parse_filter('web'), {'name': 'web'})
        with self.assertRaises(ValueError): rv.parse_filter('invalid=x')
        with self.assertRaises(ValueError): rv.parse_filter('name="unterminated')
        self.assertIsNone(rv.filter_items(None, {'name': 'x'}, lambda o: ''))
        header = '\n'.join(kdash.workload_header(data, 'pods', 115, query))
        self.assertIn('Matches: 1 / Total: 2', header)
        self.assertIn('Total: 1 | Healthy: 1 | Issues: 0', header)
        self.assertNotIn('batch-pending', '\n'.join(kdash.content_lines(data, '3', 115, filters=query)))

    def test_scope_and_secret_contents(self):
        client = Mock()
        client.call.return_value = ({'items': [{'kind': 'Secret', 'metadata': {'name': 's'}, 'type': 'Opaque', 'data': {'password': 'REDACTED_FIXTURE'}}]}, None)
        items, error = rv.list_resource(client, 'secrets', True)
        self.assertIsNone(error)
        self.assertEqual(items[0]['keyCount'], 1)
        self.assertNotIn('data', items[0])
        self.assertNotIn('REDACTED_FIXTURE', str(items))
        client.call.assert_called_with('get', 'secrets', '-A', '-o', 'json')
        rv.list_resource(client, 'persistentvolumes', False)
        client.call.assert_called_with('get', 'persistentvolumes', '-o', 'json')
        client.call.return_value = (None, 'Forbidden')
        self.assertEqual(rv.list_resource(client, 'widgets.example.io', True), (None, 'Forbidden'))
        client.reset_mock()
        self.assertIsNotNone(rv.list_resource(client, '--help', True)[1])
        client.call.assert_not_called()

    def test_partial_discovery_and_custom_resource(self):
        class Client:
            def text(self, *args):
                if '--namespaced=true' in args:
                    return 'pods\nwidgets.example.io\n--bad\nwidgets.example.io\n', 'partial discovery'
                return 'nodes\npersistentvolumes\n', None
        catalog, errors = rv.discover(Client())
        self.assertEqual(len(errors), 1)
        self.assertEqual([r['name'] for r in catalog], ['nodes', 'persistentvolumes', 'pods', 'widgets.example.io'])
        self.assertFalse(catalog[0]['namespaced'])
        self.assertTrue(catalog[-1]['namespaced'])
        headers, rows = rv.columns('widgets.example.io', [{'metadata': {'name': 'test', 'namespace': 'demo'},
            'kind': 'Widget', 'apiVersion': 'example.io/v1', 'status': {'conditions': [{'type': 'Ready', 'status': 'False'}]}}])
        self.assertIn('Ready=False', rows[0])

    def test_resource_specific_fields_and_resizing(self):
        data = kdash.demo(); data['extras'] = {}
        data['catalog'], _ = rv.demo_catalog(); data['catalog_loaded'] = True
        for view, tabs in rv.CATEGORIES.items():
            for index, (_, name, scope) in enumerate(tabs):
                objects, _ = rv.demo_resource(name, scope)
                data['extras'][name] = objects
                headers, rows = rv.columns(name, objects)
                self.assertTrue(all(len(row) == len(headers) for row in rows))
                for width in (40, 59, 84, 100, 140):
                    lines, *_ = kdash.layout(data, view, width, height=30, category_tab=index)
                    self.assertLessEqual(len(lines), 30)
                    self.assertTrue(all(len(line) == width for line in lines))
        headers, rows = rv.columns('endpointslices.discovery.k8s.io', [{'metadata': {'name': 'slice'}, 'addressType': 'IPv4',
            'endpoints': [{'addresses': ['10.1.1.1'], 'conditions': {'ready': True}}, {'addresses': ['10.1.1.2'], 'conditions': {'ready': False}}]}])
        self.assertEqual(rows[0][4], '1/2')
        denied = dict(data, extras={'services': None}, resource_errors={'services': 'services: Forbidden'})
        self.assertIn('Forbidden', '\n'.join(kdash.resource_lines(denied, {'name': 'services', 'namespaced': True}, 115)))

    def test_filter_modal_and_api_selection(self):
        class Pool:
            def __init__(self, **kwargs): pass
            def submit(self, fn, *args):
                f = Future(); f.set_result(fn(*args)); return f
            def shutdown(self, **kwargs): pass
        class Terminal:
            def __init__(self):
                keys = [ord('3'), 10, ord('/')] + list(map(ord, 'name=web ns=demo status=running')) + [10, ord('x'),
                    ord('0'), 10, ord('/')] + list(map(ord, 'widgets')) + [10, 10, 27, -1, 27, -1, ord('6'), 10, kdash.curses.KEY_RIGHT, ord('q')]
                self.keys = iter(keys)
            def timeout(self, v): pass
            def getmaxyx(self): return 45, 160
            def erase(self): pass
            def addnstr(self, *args): pass
            def refresh(self): pass
            def getch(self): return next(self.keys)
        states, loads = [], []
        layout = kdash.layout
        def record(*args, **kwargs):
            states.append((args[1], kwargs.copy()))
            return layout(*args, **kwargs)
        def resource_load(name, scope):
            loads.append((name, scope)); return rv.demo_resource(name, scope)
        with patch('kdash_app.ui.dashboard.curses.curs_set'), patch('kdash_app.ui.dashboard.curses.set_escdelay', create=True), patch('kdash_app.ui.dashboard.curses.mousemask'), \
             patch('kdash_app.ui.dashboard.curses.mouseinterval'), patch('kdash_app.ui.dashboard.sys.stdout'), \
             patch('kdash_app.ui.dashboard.concurrent.futures.ThreadPoolExecutor', Pool), patch('kdash_app.ui.dashboard.layout', record):
            self.assertEqual(kdash.screen(Terminal(), kdash.demo, 30, resource_loader=resource_load, discovery_loader=rv.demo_catalog), 'quit')
        self.assertTrue(any(v == '3' and k['filters'] == {'name': 'web', 'ns': 'demo', 'status': 'running'} for v, k in states))
        self.assertTrue(any(v == '0' and k['api_resource'] and k['api_resource']['name'] == 'widgets.demo.example' for v, k in states))
        self.assertIn(('widgets.demo.example', True), loads)
        self.assertIn(('services', True), loads)
        self.assertIn(('endpointslices.discovery.k8s.io', True), loads)
        self.assertTrue(any(v == '0' and k['focus'] == 'nav' and k['api_resource'] is None for v, k in states))


if __name__ == '__main__':
    unittest.main()
