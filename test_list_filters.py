import copy
import unittest
from concurrent.futures import Future
from unittest.mock import patch

import kdash
import resource_views as rv


class ListFilterTests(unittest.TestCase):
    def test_every_category_and_api_list_filters_rows_and_counts(self):
        for view, tabs in rv.CATEGORIES.items():
            for tab, (_, name, scope) in enumerate(tabs):
                with self.subTest(resource=name):
                    obj = rv.demo_resource(name, scope)[0][0]
                    first, second = copy.deepcopy(obj), copy.deepcopy(obj)
                    first['metadata'] = {'name': 'keep-web', 'namespace': 'production'}
                    second['metadata'] = {'name': 'drop-web', 'namespace': 'staging'}
                    data = dict(kdash.demo(), extras={name: [first, second]})
                    filters = rv.parse_filter('name=WEB ns=PRODUCTION')
                    resource = {'name': name, 'namespaced': scope}
                    for selected_view, api_resource in ((view, None), ('0', resource)):
                        body = '\n'.join(kdash.content_lines(data, selected_view, 140,
                            filters=filters, category_tab=tab, api_resource=api_resource))
                        self.assertIn('keep-web', body)
                        self.assertNotIn('drop-web', body)
                        header = '\n'.join(kdash.resource_header(data, selected_view, 140,
                            category_tab=tab, api_resource=api_resource, filters=filters))
                        self.assertIn('Matches: 1 / Total: 2', header)
                    self.assertEqual(kdash.resource_lines(data, resource, 140, {'name': 'absent'}),
                                     ['No matching resources.'])
                    self.assertIn('drop-web', '\n'.join(kdash.resource_lines(data, resource, 140)))

    def test_resource_status_and_unavailable_data(self):
        objects = [
            {'metadata': {'name': 'bound'}, 'status': {'phase': 'Bound'}},
            {'metadata': {'name': 'pending'}, 'status': {'phase': 'Pending'}},
            {'metadata': {'name': 'condition'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'False'}]}},
            {'metadata': {'name': 'attachment'}, 'status': {'attached': False}},
            {'metadata': {'name': 'no-status'}, 'status': None},
        ]
        for query, expected in [('status=pending', 'pending'), ('status=Ready=False', 'condition'),
                                ('status=Attached=False', 'attachment')]:
            matches = rv.filter_items(objects, rv.parse_filter(query), rv.status_text)
            self.assertEqual([o['metadata']['name'] for o in matches], [expected])
        data = dict(kdash.demo(), extras={'services': None}, resource_errors={'services': 'Forbidden'})
        self.assertIn('Matches: N/A / Total: N/A', '\n'.join(kdash.resource_header(data, '6', 140, filters={'name': 'x'})))
        self.assertIn('Forbidden', '\n'.join(kdash.content_lines(data, '6', 140, filters={'name': 'x'})))

    def test_keyboard_apply_clear_cancel_tabs_and_catalog_isolation(self):
        class Pool:
            def __init__(self, **kwargs): pass
            def submit(self, fn, *args):
                future = Future(); future.set_result(fn(*args)); return future
            def shutdown(self, **kwargs): pass
        keys = iter(
            [ord('6'), 10, ord('/')] + list(map(ord, 'ns=demo name=absent')) +
            [10, kdash.curses.KEY_RIGHT, kdash.curses.KEY_LEFT, ord('/'), 21] +
            list(map(ord, 'invalid=value')) + [10, 27, -1, ord('x'), ord('0'), 10, ord('/')] +
            list(map(ord, 'widgets')) + [10, 10, -1, ord('/')] +
            list(map(ord, 'name=absent status=active')) + [10, ord('x'), 27, -1, ord('q')])
        class Terminal:
            def timeout(self, value): pass
            def getmaxyx(self): return 45, 160
            def erase(self): pass
            def addnstr(self, *args): pass
            def refresh(self): pass
            def getch(self): return next(keys)
        states = []
        layout = kdash.layout
        def record(*args, **kwargs):
            result = layout(*args, **kwargs)
            states.append((args[1], kwargs.copy(), '\n'.join(result[0])))
            return result
        with patch('kdash.concurrent.futures.ThreadPoolExecutor', Pool), patch('kdash.layout', record), \
             patch('kdash.curses.curs_set'), patch('kdash.curses.set_escdelay', create=True), \
             patch('kdash.curses.mousemask'), patch('kdash.curses.mouseinterval'), patch('kdash.sys.stdout'):
            self.assertEqual(kdash.screen(Terminal(), kdash.demo, 30,
                resource_loader=rv.demo_resource, discovery_loader=rv.demo_catalog), 'quit')
        filtered_services = [(k, body) for v, k, body in states if v == '6' and k['category_tab'] == 0 and k['filters']]
        self.assertGreaterEqual(len(filtered_services), 3)  # apply, return from tab, cancel draft
        self.assertTrue(all(k['filters'] == {'ns': 'demo', 'name': 'absent'} for k, _ in filtered_services))
        self.assertTrue(any('No matching resources.' in body for _, body in filtered_services))
        self.assertTrue(any(v == '6' and k['category_tab'] == 1 and not k['filters'] for v, k, _ in states))
        self.assertTrue(any(v == '6' and not k['filters'] and 'demo-services' in body for v, k, body in states))
        api = [(k, body) for v, k, body in states if v == '0' and k['api_resource']]
        self.assertTrue(any(k['filters'] == {'name': 'absent', 'status': 'active'} and 'No matching resources.' in body for k, body in api))
        self.assertTrue(all(k['catalog_query'] == 'widgets' for k, _ in api))
        self.assertTrue(any(not k['filters'] and 'demo-widgets' in body for k, body in api))
        self.assertTrue(any(v == '0' and k['api_resource'] is None and k['catalog_query'] == 'widgets' for v, k, _ in states))


if __name__ == '__main__':
    unittest.main()
