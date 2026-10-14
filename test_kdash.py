import unittest
from unittest.mock import patch, Mock
from pathlib import Path
import subprocess
import kdash


class DashboardTests(unittest.TestCase):
    def test_quantities(self):
        for raw, expected in [('250m', .25), ('123456789n', .123456789), ('2Gi', 2 * 1024 ** 3), ('4e3', 4000), ('1G', 1e9)]:
            self.assertAlmostEqual(kdash.quantity(raw), expected)
        self.assertIsNone(kdash.quantity(None))

    def test_init_sidecar_and_overhead_requests(self):
        def c(cpu, **kw):
            return dict(resources={'requests': {'cpu': cpu}}, **kw)
        pod = {'spec': {'containers': [c('500m')], 'initContainers': [c('200m', restartPolicy='Always'), c('2')],
                        'overhead': {'cpu': '100m'}}}
        self.assertAlmostEqual(kdash.pod_requests(pod)['cpu'], 2.3)
        pod['spec']['resources'] = {'requests': {'cpu': '3'}}
        self.assertAlmostEqual(kdash.pod_requests(pod)['cpu'], 3.1)

    def test_pending_failed_and_notready(self):
        self.assertEqual(kdash.pod_issue({'status': {'phase': 'Pending'}}), 'Pending')
        self.assertEqual(kdash.pod_issue({'status': {'phase': 'Failed', 'reason': 'Evicted'}}), 'Evicted')
        self.assertEqual(kdash.pod_issue({'status': {'phase': 'Succeeded'}}), '')
        self.assertEqual(kdash.pod_issue({'status': {'phase': 'Running'}}), 'NotReady')
        obj = {'kind': 'Deployment', 'spec': {'replicas': 2}, 'status': {'readyReplicas': 2, 'updatedReplicas': 1}}
        self.assertEqual(kdash.controller_issue(obj), 'Rollout/Unavailable')

    def test_layout_resize_scroll_and_remote_control_characters(self):
        data = kdash.demo()
        data['cluster'] = 'bad\x1b[2J\ncluster'
        for width in (40, 59, 84, 100, 120, 160):
            for view in '12345':
                for height in (17, 23, 39):
                    lines, total, available, offset = kdash.layout(data, view, width, 999, height)
                    self.assertLessEqual(len(lines), height)
                    self.assertTrue(all(len(line) == width for line in lines))
                    self.assertTrue(all(all(32 <= ord(c) < 127 for c in line) for line in lines))
                    self.assertLessEqual(offset, max(0, total - available))
        lines, *_ = kdash.layout(data, '1', 120, 10, 39)
        self.assertIn('NAVIGATION', '\n'.join(lines))
        self.assertIn('Cluster:', lines[2])

    def test_partial_failure_never_reports_zero_for_denied_lists(self):
        class Fake:
            def call(self, *args):
                if args[1] == 'nodes':
                    return {'items': kdash.demo()['nodes']}, None
                return None, 'Forbidden'
        data = kdash.collect(Fake(), {'cluster': 'c', 'context': 'ctx', 'server': 's'})
        self.assertIsNone(data['pods'])
        self.assertIn('Pods Running: N/A', kdash.summary(data)[0])
        self.assertIn('N/A', '\n'.join(kdash.node_lines(data)))
        self.assertEqual(len(data['errors']), 8)

    def test_mac_mouse_reports_and_preservation_of_other_keys(self):
        for report, expected in (('[<65;60;20M', 1), ('[<64;60;20M', -1),
                                 ('[<81;60;20M', 1), ('[<0;60;20M', 0)):
            terminal = Mock()
            terminal.getch.side_effect = list(map(ord, report))
            self.assertEqual(kdash.wheel_direction(terminal, 27), expected)
            terminal.timeout.assert_called_with(40)
        terminal = Mock()
        terminal.getch.return_value = ord('q')
        with patch('kdash.curses.ungetch') as push:
            self.assertIsNone(kdash.wheel_direction(terminal, 27))
            push.assert_called_once_with(ord('q'))

    def test_cached_layout_matches_uncached_layout(self):
        data = kdash.demo()
        for view in '12345':
            for width in (80, 120, 160):
                inner = width - (25 if width >= 85 else 0) - 4
                content = kdash.content_lines(data, view, inner)
                self.assertEqual(kdash.layout(data, view, width, 5, 30),
                                 kdash.layout(data, view, width, 5, 30, content=content))

    def test_workload_tabs_counts_and_pinned_summary(self):
        data = kdash.demo()
        text = '\n'.join(kdash.workload_summary(data, 'pods'))
        self.assertIn('Total: 2 | Healthy: 1 | Issues: 1', text)
        self.assertIn('Pending: 1', text)
        data['pods'] = None
        self.assertIn('Total: N/A', '\n'.join(kdash.workload_summary(data, 'pods')))
        data = kdash.demo()
        for kind in kdash.WORKLOAD_KINDS:
            lines = kdash.content_lines(data, '3', 115, kind)
            self.assertTrue(lines[0].startswith(kind.upper()))
            self.assertFalse(any(line.startswith(k.upper() + ' |') for k in kdash.WORKLOAD_KINDS if k != kind for line in lines))
        data['pods'] *= 20
        lines, *_ = kdash.layout(data, '3', 140, 30, 35, focus='content')
        output = '\n'.join(lines)
        self.assertIn('[Pods]  Deployments', output)
        self.assertIn('Total: 40', output)

    def test_enter_escape_navigation_and_left_right_tabs(self):
        from concurrent.futures import Future
        class Pool:
            def __init__(self, **kwargs): pass
            def submit(self, fn):
                future = Future(); future.set_result(fn()); return future
            def shutdown(self, **kwargs): pass
        class Terminal:
            def __init__(self):
                self.keys = iter([kdash.curses.KEY_DOWN, kdash.curses.KEY_DOWN, kdash.curses.KEY_DOWN, kdash.curses.KEY_DOWN, 10,
                    kdash.curses.KEY_RIGHT, kdash.curses.KEY_RIGHT, kdash.curses.KEY_LEFT,
                    27, -1, kdash.curses.KEY_DOWN, 10, ord('q')])
            def timeout(self, value): pass
            def getmaxyx(self): return 35, 140
            def erase(self): pass
            def addnstr(self, *args): pass
            def refresh(self): pass
            def getch(self): return next(self.keys)
        states = []
        layout = kdash.layout
        def record(*args, **kwargs):
            states.append((args[1], kwargs['focus'], kwargs['selected'], kwargs['workload_kind']))
            return layout(*args, **kwargs)
        with patch('kdash.curses.curs_set'), patch('kdash.curses.set_escdelay', create=True), \
             patch('kdash.curses.mousemask'), patch('kdash.curses.mouseinterval'), \
             patch('kdash.sys.stdout'), patch('kdash.concurrent.futures.ThreadPoolExecutor', Pool), \
             patch('kdash.layout', record):
            self.assertEqual(kdash.screen(Terminal(), kdash.demo, 30), 'quit')
        self.assertIn(('1', 'nav', '3', 'pods'), states)
        self.assertIn(('3', 'content', '3', 'pods'), states)
        self.assertIn(('3', 'content', '3', 'deployments'), states)
        self.assertIn(('3', 'content', '3', 'replicasets'), states)
        self.assertIn(('3', 'nav', '3', 'deployments'), states)
        self.assertEqual(states[-1], ('6', 'content', '6', 'deployments'))

    @patch('kdash.subprocess.run')
    def test_config_context_isolation_and_timeout(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout='{"items":[]}', stderr='')
        client = kdash.Client(Path('/tmp/test config.conf'), 'dev', 8)
        client.call('get', 'nodes', '-o', 'json')
        args = run.call_args.args[0]
        self.assertEqual(args[1:3], ['--kubeconfig', '/tmp/test config.conf'])
        self.assertIn('--context', args)
        self.assertEqual(run.call_args.kwargs['timeout'], 13)
        run.side_effect = subprocess.TimeoutExpired(args, 13)
        self.assertIsNone(client.call('get', 'nodes')[0])


if __name__ == '__main__':
    unittest.main()
