"""Event-to-resource navigation uses synthetic API responses only."""
import copy
import json
import tempfile
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch

from kdash_app.resources import event_resources as er
from kdash_app.resources import event_views as ev
from kdash_app.ui import dashboard as kdash
from kdash_app.resources import resource_actions as actions
from kdash_app.resources import resource_detail as rd
def row(kind='Pod', version='v1', name='web', namespace='prod'):
    ref = {'apiVersion': version, 'kind': kind, 'name': name, 'uid': 'event-target-uid', 'resourceVersion': '2'}
    if namespace is not None:
        ref['namespace'] = namespace
    return {'id': 'event-uid', 'stamp': '2026-10-15T00:00:00Z', 'namespace': namespace or 'default',
            'target': kind + '/' + name, 'reason': 'Failed', 'count': 3, 'message': 'Original event message',
            'raw': {'metadata': {'namespace': 'prod'}, 'involvedObject': ref}}


def event_item(record=None, view='4'):
    browser = rd.Detail()
    browser.open_event(record or row(), view, 6)
    return browser.item


def client(kind='Pod', version='v1', plural='pods', namespaced=True):
    c = Mock()
    c.base = ['kubectl', '--kubeconfig', '/synthetic.conf', '--context', 'synthetic']
    c.current = {'apiVersion': version, 'kind': kind, 'metadata': {'name': 'web', 'uid': 'event-target-uid', 'resourceVersion': '8'}, 'status': {'phase': 'Pending'}}
    if namespaced:
        c.current['metadata']['namespace'] = 'prod'

    def call(*args, **kwargs):
        if args[:2] == ('get', '--raw'):
            return {'resources': [{'name': plural, 'kind': kind, 'namespaced': namespaced, 'verbs': ['get']},
                                  {'name': plural + '/status', 'kind': kind, 'namespaced': namespaced, 'verbs': ['get']}]}, None
        return copy.deepcopy(c.current), None

    c.call.side_effect = call
    c.text.return_value = 'VERIFIED RESOURCE OUTPUT', None
    return c


class EventTargetTests(unittest.TestCase):
    def test_discovery_maps_kind_to_plural_group_and_api_scope(self):
        for kind, version, plural, namespaced in [('Pod', 'v1', 'pods', True), ('Policy', 'example.io/v1alpha1', 'policies', True), ('Node', 'v1', 'nodes', False)]:
            with self.subTest(kind=kind):
                c = client(kind, version, plural, namespaced)
                item = event_item(row(kind, version, namespace='prod'))
                target = er.resolve(c, item)
                group = version.split('/')[0] if '/' in version else ''
                self.assertEqual(target['resource'], plural + ('.' + group if group else ''))
                self.assertEqual(target['namespaced'], namespaced)
                self.assertEqual(target['namespace'], 'prod' if namespaced else None)
                path = ('/apis/' if '/' in version else '/api/') + version
                c.call.assert_called_once_with('get', '--raw', path)

    def test_regarding_and_namespace_fallback_use_raw_reference(self):
        record = row(namespace=None)
        record['raw']['regarding'] = record['raw'].pop('involvedObject')
        self.assertEqual(er.resolve(client(), event_item(record))['namespace'], 'prod')
        data = kdash.demo()
        data['events'] = [dict(record['raw'], type='Warning', note='fixture')]
        self.assertEqual(ev.records(data, '4')[0]['target'], 'Pod/web')

    def test_incomplete_ambiguous_invalid_references_fail_without_guessing(self):
        for missing in ('apiVersion', 'kind', 'name', 'uid'):
            record = row()
            record['raw']['involvedObject'].pop(missing)
            c = client()
            with self.assertRaises(ValueError):
                er.resolve(c, event_item(record))
            c.call.assert_not_called()
        c = client()
        c.call.return_value = {'resources': [{'name': name, 'kind': 'Pod', 'namespaced': True, 'verbs': ['get']} for name in ('pods', 'otherpods')]}, None
        c.call.side_effect = None
        with self.assertRaisesRegex(ValueError, 'unambiguously'):
            er.resolve(c, event_item())
        record = row(version='../../v1')
        c = client()
        with self.assertRaises(ValueError):
            er.resolve(c, event_item(record))
        c.call.assert_not_called()

    def test_yaml_uses_single_verified_snapshot_and_ignores_event_resource_version(self):
        c = client()
        output, error = rd.load(c, event_item(), 'yaml')
        self.assertIsNone(error)
        self.assertEqual(output, 'VERIFIED RESOURCE OUTPUT')
        c.text.assert_called_once()
        self.assertEqual(c.text.call_args.args[:4], ('create', '--dry-run=client', '--validate=false', '-f'))
        payload = json.loads(c.text.call_args.kwargs['input_text'])
        self.assertEqual(payload['metadata']['uid'], 'event-target-uid')
        self.assertEqual(payload['metadata']['resourceVersion'], '8')
        reads = [call for call in c.call.call_args_list if call.args[1] == 'pods']
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].args, ('get', 'pods', 'web', '--namespace', 'prod', '-o', 'json'))

    def test_get_denied_deleted_and_replaced_never_show_replacement_yaml(self):
        for error in ('Forbidden fixture', 'NotFound fixture', None):
            c = client()
            original = c.call.side_effect
            def call(*args):
                if args[1] == '--raw': return original(*args)
                if error: return None, error
                return dict(c.current, metadata=dict(c.current['metadata'], uid='replacement')), None
            c.call.side_effect = call
            output, problem = rd.load(c, event_item(), 'yaml')
            self.assertIsNone(output)
            self.assertIn(error or 'UID mismatch', problem)
            c.text.assert_not_called()

    def test_describe_manifest_is_minimal_and_result_uid_checked_after_command(self):
        for replaced in (False, True):
            c = client()
            c.current['data'] = {'key': 'FAKE_PRIVATE_DATA'}
            def describe(*args, **kwargs):
                if replaced:
                    c.current['metadata']['uid'] = 'replacement'
                return 'DESCRIBE RESULT', None
            c.text.side_effect = describe
            output, error = rd.load(c, event_item(), 'describe')
            manifest = json.loads(c.text.call_args.kwargs['input_text'])
            self.assertEqual(manifest, {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'web', 'namespace': 'prod'}})
            self.assertEqual(c.text.call_args.args, ('describe', '-f', '-', '--namespace', 'prod'))
            self.assertNotIn('FAKE_PRIVATE_DATA', c.text.call_args.kwargs['input_text'])
            if replaced:
                self.assertIsNone(output)
                self.assertIn('UID mismatch', error)
            else:
                self.assertEqual(output, 'DESCRIBE RESULT')
                self.assertIsNone(error)

    def test_edit_uses_resolved_target_and_replacement_blocks_editor(self):
        c = client('Policy', 'example.io/v1', 'policies')
        item = event_item(row('Policy', 'example.io/v1'))
        with tempfile.TemporaryDirectory() as directory, patch('kdash_app.resources.resource_actions.profiles.clear'), patch('kdash_app.resources.resource_actions.profiles.prompt', return_value=''), patch('builtins.print'), patch.object(actions, 'edit_yaml') as edit:
            actions.perform(c, directory, {}, 'edit', item)
            target = edit.call_args.args[2]
            self.assertEqual((target['resource'], target['namespace'], target['uid']), ('policies.example.io', 'prod', 'event-target-uid'))
        c.current['metadata']['uid'] = 'replacement'
        with tempfile.TemporaryDirectory() as directory, patch('kdash_app.resources.resource_actions.profiles.clear'), patch('kdash_app.resources.resource_actions.profiles.prompt', return_value=''), patch('builtins.print'), patch('kdash_app.resources.resource_actions.subprocess.run') as editor:
            actions.perform(c, directory, {}, 'edit', item)
            editor.assert_not_called()
            c.text.assert_not_called()

    def test_event_snapshot_tabs_and_missing_target_error_details(self):
        browser = rd.Detail()
        record = row()
        browser.open_event(record, '4', 6)
        record['message'] = 'changed after opening'
        pool = Mock()
        browser.poll(pool, Mock())
        pool.submit.assert_not_called()
        for mode in ('event', 'describe', 'yaml', 'event'):
            self.assertEqual(browser.mode, mode)
            browser.switch()
        self.assertIn('Original event message', ' '.join(rd.content(dict(browser.state(), mode='event'), 80, kdash.clean)))
        browser.refresh(all_modes=True)
        self.assertIn('event', browser.buffers)
        browser.open_event({'message': 'Forbidden fixture', 'source': 'API', 'stamp': '-'}, '5', 0)
        browser.switch()
        self.assertEqual(browser.mode, 'event')
        header = ' '.join(rd.header(browser.state(), 140, kdash.clean))
        self.assertIn('No object reference', header)
        self.assertNotIn('edit target YAML', header)

    def test_error_with_explicit_object_reference_supports_target_tabs(self):
        data = kdash.demo()
        data['errors'] = [{'message': 'Object query failed', 'reference': row()['raw']['involvedObject']}]
        data['resource_errors'] = {}
        records = ev.records(data, '5')
        browser = rd.Detail()
        browser.open_event(records[0], '5', 0)
        browser.switch()
        self.assertEqual(browser.mode, 'describe')
        self.assertEqual(er.resolve(client(), browser.item)['resource'], 'pods')

    def test_event_context_cannot_trigger_other_operations_or_edit_without_reference(self):
        for action in ('new', 'actions', 'exec'):
            c = client()
            with patch('kdash_app.resources.resource_actions.profiles.clear'), patch('kdash_app.resources.resource_actions.profiles.prompt', return_value=''), patch('builtins.print'), patch('kdash_app.resources.resource_actions.edit_yaml') as edit:
                actions.perform(c, '/unused', {}, action, event_item())
                c.call.assert_not_called()
                edit.assert_not_called()
        c = client()
        item = event_item({'source': 'Kubernetes', 'message': 'Forbidden fixture', 'stamp': '-'}, '5')
        with patch('kdash_app.resources.resource_actions.profiles.clear'), patch('kdash_app.resources.resource_actions.profiles.prompt', return_value=''), patch('builtins.print'), patch('kdash_app.resources.resource_actions.edit_yaml') as edit:
            actions.perform(c, '/unused', {}, 'edit', item)
            c.call.assert_not_called()
            edit.assert_not_called()

    def test_event_target_layout_is_ascii_and_responsive(self):
        data = kdash.demo()
        browser = rd.Detail()
        browser.open_event(row(), '4', 0)
        for mode in ('event', 'describe', 'yaml'):
            browser.mode = mode
            if mode != 'event': browser.buffers[mode] = ('synthetic output\n' * 100, None)
            data['resource_detail'] = browser.state()
            data['event_state'] = {'detail': browser.item['event_row'], 'index': 0}
            for width in (40, 60, 80, 100, 160, 240):
                for height in (18, 30, 50):
                    lines, *_ = kdash.layout(data, '4', width, height=height, focus='content')
                    self.assertTrue(all(len(line) == width and line.isascii() for line in lines))

    def test_keyboard_target_tabs_edit_and_escape_keep_event_row(self):
        class Pool:
            def __init__(self, **kwargs): pass
            def submit(self, fn, *args):
                future = Future(); future.set_result(fn(*args)); return future
            def shutdown(self, **kwargs): pass
        keys = iter([ord('4'), 10, 10, kdash.curses.KEY_RIGHT, -1, kdash.curses.KEY_RIGHT, -1, ord('e'), -1, 27, -1, 27, -1, ord('q')])
        class Terminal:
            def timeout(self, value): pass
            def getmaxyx(self): return 40, 160
            def erase(self): pass
            def addnstr(self, *args): pass
            def refresh(self): pass
            def getch(self): return next(keys)
        loads, frames = [], []
        action = Mock()
        real_layout = kdash.layout
        def load(item, mode):
            loads.append(mode)
            return 'TARGET: ' + mode, None
        def record(data, *args, **kwargs):
            frames.append((args[0], kwargs['focus'], copy.deepcopy(data.get('event_state')), copy.deepcopy(data.get('resource_detail'))))
            return real_layout(data, *args, **kwargs)
        with patch('kdash_app.ui.dashboard.concurrent.futures.ThreadPoolExecutor', Pool), patch('kdash_app.ui.dashboard.layout', record), patch('kdash_app.ui.dashboard.curses.curs_set'), patch('kdash_app.ui.dashboard.curses.set_escdelay', create=True), patch('kdash_app.ui.dashboard.curses.mousemask'), patch('kdash_app.ui.dashboard.curses.mouseinterval'), patch('kdash_app.ui.dashboard.sys.stdout'), patch('kdash_app.ui.dashboard.terminal_action', side_effect=lambda terminal, callback: callback()):
            self.assertEqual(kdash.screen(Terminal(), kdash.demo, 30, detail_loader=load, action_handler=action), 'quit')
        self.assertEqual(loads, ['describe', 'yaml', 'yaml'])
        action.assert_called_once()
        self.assertEqual(action.call_args.args[0], 'edit')
        self.assertTrue(action.call_args.args[1]['event_context'])
        self.assertEqual(frames[-1][:2], ('4', 'nav'))
        self.assertIsNone(frames[-1][2]['detail'])
        self.assertTrue(any(frame[3] and frame[3]['mode'] == 'describe' for frame in frames))


if __name__ == '__main__':
    unittest.main()
