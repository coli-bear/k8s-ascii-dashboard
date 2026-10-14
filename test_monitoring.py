import json
import unittest
from concurrent.futures import Future
from unittest.mock import patch
import kdash
import monitoring as m
import profiles


class MonitoringTests(unittest.TestCase):
    def test_filter_literals_and_tab_scope(self):
        filters = m.parse_filter('ns=prod name="a.*|b" cluster=west', 3)
        specs, _ = m.plan(3, filters)
        self.assertIn('namespace=~".*prod.*"', specs[0]['query'])
        self.assertIn(m.matcher('pod', 'a.*|b'), specs[0]['query'])
        self.assertEqual(json.loads(m.matcher('pod', 'a.*|b').split('=~')[1]), r'.*a\.\*\|b.*')
        with self.assertRaises(ValueError): m.parse_filter('service=web', 0)
        specs, _ = m.plan(0, {'node': 'missing'}, [])
        self.assertIn('instance=~"a^"', specs[0]['query'])

    def test_missing_service_metrics_never_infer_service_traffic(self):
        specs, notes = m.plan(2, {})
        self.assertIn('NOT traffic attributed', ' '.join(notes))
        self.assertTrue(all('container!=' not in c['query'] for c in specs))
        self.assertEqual(m.plan(2, {'service': 'web'})[0], [])
        specs, _ = m.plan(2, {'service': 'web', 'ns': 'prod'}, service_metrics=True)
        self.assertIn('reporter="destination"', specs[0]['query'])
        self.assertIn('destination_service_name=~', specs[0]['query'])

    def test_gaps_nan_zero_and_expired_samples(self):
        series = [{'values': [[0, '0'], [5, 'NaN'], [15, '2'], [20, '+Inf']]}]
        self.assertEqual(m.aggregate(series, 0, 20, 5), [(0., 0.), (15., 2.)])
        self.assertIsNone(m.latest(series[0], 20, 5))
        self.assertIsNone(m.latest({'values': [[0, '2']]}, 20, 5))
        self.assertEqual(m.latest({'values': [[20, '0']]}, 20, 5), 0)

    def test_reader_cluster_guard_and_query_failure(self):
        def multi(*args, **kwargs):
            return {'data': {'result': [{'metric': {'cluster': c}} for c in ('east', 'west')]}}
        snap = m.Reader({}, query=multi)(0, {}, 300)
        self.assertIn('Multiple cluster', snap['errors'][0])
        self.assertEqual(snap['cards'], [])
        def broken(*args, **kwargs): raise ValueError('HTTP 403')
        self.assertIn('HTTP 403', m.Reader({}, query=broken)(1, {}, 300)['errors'][0])

    def test_reader_partial_failure_freshness_and_range(self):
        calls = []
        def query(config, expr, timeout, **kwargs):
            calls.append((expr, kwargs))
            if 'istio_requests' in expr: result = []
            elif expr.startswith('count by'): result = [{'metric': {}, 'value': [1000, '4']}]
            elif 'transmit' in expr: raise ValueError('query denied')
            elif expr.startswith('max(timestamp'): result = [{'value': [1000, '950']}]
            else: result = [{'metric': {'namespace': 'prod'}, 'values': [[1000, '0']]}]
            return {'data': {'result': result}}
        with patch('monitoring.time.time', return_value=1000):
            snap = m.Reader({}, query=query)(2, {}, 300)
        self.assertEqual(snap['cards'][0]['age'], 50)
        self.assertEqual(snap['cards'][1]['error'], 'query denied')
        self.assertTrue(any(kw == dict(start=700, end=1000, step=5) for _, kw in calls))

    def test_http_range_parameters_and_empty_matrix(self):
        with patch('profiles.urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"status":"success","data":{"resultType":"matrix","result":[]}}'
            result = profiles.prometheus_query({'endpoint': 'https://prom.invalid/prefix'}, 'sum(up)', start=0, end=60, step=5)
            self.assertEqual(result['data']['result'], [])
            url = build.return_value.open.call_args.args[0].full_url
            self.assertIn('/prefix/api/v1/query_range?', url)
            self.assertIn('start=0&end=60&step=5', url)

    def test_ascii_layout_all_sizes_and_stale_status(self):
        card = dict(key='cpu', title='CPU used', unit='cores', source='cpu', series=[{'metric': {'instance': 'n:9100'}, 'values': [[1000, '2']]}], error='', warnings=[], age=200)
        snap = dict(start=700, end=1000, step=5, cards=[card], notes=[], errors=[], names={})
        data = dict(kdash.demo(), prometheus={'endpoint': 'http://test'}, monitoring=snap)
        self.assertIn('STALE', '\n'.join(m.render(data, 0, 140, kdash.ascii_table)))
        for width in (40, 60, 84, 100, 140, 240):
            for height in (18, 35, 60):
                lines, *_ = kdash.layout(data, 'm', width, height=height)
                self.assertLessEqual(len(lines), height)
                self.assertTrue(all(len(line) == width and line.isascii() for line in lines))

    def test_background_tab_result_isolation_pause_and_range(self):
        pending = Future()
        calls, frames = [], []
        marker = dict(end=1000, cards=[], notes=['OLD TAB RESULT'], errors=[], names={})
        def load_metrics(tab, filters, period): pass
        class Pool:
            def __init__(self, **kwargs): pass
            def submit(self, fn, *args):
                if fn is load_metrics:
                    calls.append(args)
                    return pending if len(calls) == 1 else Future()
                f = Future(); f.set_result(fn(*args)); return f
            def shutdown(self, **kwargs): pass
        keys = iter([ord('m'), 10, kdash.curses.KEY_RIGHT, -1, ord('p'), ord('t'), ord('q')])
        class Terminal:
            def timeout(self, v): pass
            def getmaxyx(self): return 40, 160
            def erase(self): pass
            def addnstr(self, *a): pass
            def refresh(self): pass
            def getch(self):
                key = next(keys)
                if key == -1: pending.set_result(marker)
                return key
        real_layout = kdash.layout
        def layout(data, *args, **kwargs):
            if args[0] == 'm': frames.append((kwargs['monitoring_tab'], data.get('monitoring'), dict(data['monitor_state'])))
            return real_layout(data, *args, **kwargs)
        with patch('kdash.concurrent.futures.ThreadPoolExecutor', Pool), patch('kdash.layout', layout), patch('kdash.curses.curs_set'), patch('kdash.curses.set_escdelay', create=True), patch('kdash.curses.mousemask'), patch('kdash.curses.mouseinterval'), patch('kdash.sys.stdout'):
            kdash.screen(Terminal(), lambda: dict(kdash.demo(), prometheus={'endpoint': 'http://test'}), 30, monitoring_loader=load_metrics)
        self.assertEqual([c[0] for c in calls], [0, 1])
        self.assertTrue(all(snap is None for tab, snap, _ in frames if tab == 1))
        self.assertTrue(any(state['paused'] for _, _, state in frames))
        self.assertEqual(frames[-1][2]['period'], 900)


if __name__ == '__main__': unittest.main()
