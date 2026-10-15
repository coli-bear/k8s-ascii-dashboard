import unittest
import copy
import kdash
import resource_views as rv
import resource_detail as rd
from test_event_targets import client, event_item

class TabLabelsTests(unittest.TestCase):
    def test_all_menu_and_detail_tabs_have_labels_and_separators(self):
        data = kdash.demo()
        headers = [kdash.dashboard_tabs('1', 180), kdash.dashboard_tabs('m', 180),
                   kdash.workload_header(data, 'pods', 180)]
        headers += [kdash.resource_header(data, view, 180) for view in rv.CATEGORIES]
        items = [dict(resource=r, namespaced=n, namespace='demo' if n else None, name='example', uid='fixture')
                 for r, n in [('pods', True), ('nodes', False), ('namespaces', False), ('services', True)]]
        for item in items:
            browser = rd.Detail(); browser.open(item, 0)
            headers.append(rd.header(browser.state(), 180, kdash.clean))
        browser = rd.Detail(); browser.open_event(event_item()['event_row'], '4', 0)
        headers.append(rd.header(browser.state(), 180, kdash.clean))
        for header in headers:
            with self.subTest(header=header[0]):
                self.assertTrue(header[0].startswith('Tabs: '))
                self.assertIn(' | ', header[0])
                self.assertTrue(header[1].startswith('Keys: '))

    def test_missing_event_target_explains_historical_reference(self):
        from event_resources import load
        c = client()
        original = c.call.side_effect
        c.call.side_effect = lambda *args: original(*args) if args[1] == '--raw' else (None, 'Error from server (NotFound): pods "web" not found')
        for mode in ('yaml', 'describe'):
            output, error = load(c, event_item(), mode)
            state = dict(target=event_item(), mode=mode, loading=False, output=output, error=error)
            lines = rd.content(state, 100, kdash.clean)
            text = ' '.join(lines)
            self.assertIn('TARGET NOT FOUND', text)
            self.assertNotIn('QUERY ERROR', text)
            self.assertIn('Event JSON', text)
            self.assertIn('not an archived', text)
        c.text.assert_not_called()

    def test_forbidden_is_not_reported_as_deleted(self):
        state = dict(target=event_item(), mode='yaml', loading=False, output=None, error='Forbidden fixture')
        self.assertIn('QUERY ERROR', ' '.join(rd.content(state, 80, kdash.clean)))
        self.assertNotIn('TARGET NOT FOUND', ' '.join(rd.content(state, 80, kdash.clean)))

    def test_wrapped_headers_and_event_error_respect_width(self):
        browser = rd.Detail(); browser.open_event(event_item()['event_row'], '4', 0)
        for width in (36, 50, 80, 140):
            self.assertTrue(all(len(line) <= width for line in rd.header(browser.state(), width, kdash.clean)))

if __name__ == '__main__': unittest.main()
