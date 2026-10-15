"""Creation uses the active tab/discovery type even without any existing objects."""
from concurrent.futures import Future
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from kuba_dm_app.ui import dashboard as kuba_dm
from kuba_dm_app.resources import resource_actions as actions
from kuba_dm_app.resources import resource_views as rv
def api_client(resource, kind, version, namespaced=True, verbs=None):
    client = Mock(base=['kubectl', '--context', 'selected-profile'])
    def call(*args, **kwargs):
        if args[:2] == ('get', '--raw'):
            path = args[2]
            if path == '/apis/' + version.split('/')[0]:
                return {'preferredVersion': {'groupVersion': version}}, None
            return {'resources': [{'name': resource.split('.')[0], 'kind': kind,
                                   'namespaced': namespaced, 'verbs': ['create'] if verbs is None else verbs}]}, None
        return {'apiVersion': version, 'kind': kind,
                'metadata': dict(name='new-object', **({'namespace': 'prod'} if namespaced else {}))}, None
    client.call.side_effect = call
    client.text.return_value = ('created', None)
    return client


class ContextCreateTests(unittest.TestCase):
    def test_selected_type_draft_and_scope_without_template_picker(self):
        cases = [('namespaces', 'Namespace', 'v1', False), ('pods', 'Pod', 'v1', True), ('services', 'Service', 'v1', True),
                 ('widgets.example.io', 'Widget', 'example.io/v1alpha1', True),
                 ('widgets.example.io', 'Widget', 'example.io/v1', False),
                 ('pods.example.io', 'Pod', 'example.io/v1', False),
                 ('statefulsets', 'StatefulSet', 'apps/v1', True)]
        for resource, kind, version, namespaced in cases:
            with self.subTest(resource=resource, scope=namespaced):
                client = api_client(resource, kind, version, namespaced)
                item = dict(resource=resource, name=None, namespace='prod', namespaced=namespaced)
                prompts = ['new-object'] + ([''] if namespaced else []) + ['', 'create']
                with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EDITOR':'editor', 'VISUAL':''}), patch('kuba_dm_app.resources.resource_actions.subprocess.run', return_value=Mock(returncode=0)), patch('kuba_dm_app.resources.resource_actions.profiles.prompt', side_effect=prompts), patch('kuba_dm_app.resources.resource_actions.choose') as choose, patch('builtins.print'):
                    actions.edit_yaml(client, directory, item, new=True)
                    choose.assert_not_called()
                    text = client.call.call_args.kwargs['input_text']
                    self.assertFalse(list((Path(directory)/'.kuba-dm').rglob('*.yaml')))
                    self.assertIn('kind: ' + kind, text)
                    self.assertIn('apiVersion: ' + version, text)
                    self.assertEqual('namespace: "prod"' in text, namespaced)
                    if resource == 'pods': self.assertIn('containers:', text)
                    if resource == 'services': self.assertIn('type: ClusterIP', text)
                    if resource == 'pods.example.io': self.assertNotIn('containers:', text)
                    self.assertEqual(client.text.call_args.args[0], 'create')
                    self.assertEqual('--namespace' in client.text.call_args.args, namespaced)

    def test_discovery_failure_or_unsupported_create_stops_before_editor(self):
        for failure in ('read-only', 'forbidden', 'missing-selection'):
            client = api_client('widgets.example.io', 'Widget', 'example.io/v1', verbs=['get','list'])
            item = {'resource':'widgets.example.io'}
            if failure == 'forbidden': client.call.side_effect = lambda *args: (None, 'Forbidden')
            if failure == 'missing-selection': item = None
            with patch('kuba_dm_app.resources.resource_actions.subprocess.run') as editor, patch('kuba_dm_app.resources.resource_actions.profiles.prompt') as prompt:
                with self.assertRaises(ValueError): actions.edit_yaml(client, '/unused', item, new=True)
                editor.assert_not_called(); prompt.assert_not_called(); client.text.assert_not_called()

    def test_server_dry_run_cannot_change_selected_type_or_create_list(self):
        for kind in ('Service', 'List'):
            client = api_client('pods', 'Pod', 'v1')
            discovery = client.call.side_effect
            client.call.side_effect = lambda *args, **kwargs: discovery(*args, **kwargs) if args[0] == 'get' else ({'kind':kind, 'apiVersion':'v1', 'metadata':{'name':'new-object','namespace':'prod'}}, None)
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EDITOR':'editor','VISUAL':''}), patch('kuba_dm_app.resources.resource_actions.subprocess.run', return_value=Mock(returncode=0)), patch('kuba_dm_app.resources.resource_actions.profiles.prompt', side_effect=['new-object','prod','','']), patch('builtins.print'):
                actions.edit_yaml(client, directory, {'resource':'pods'}, new=True)
                client.text.assert_not_called()

    def test_screen_passes_tab_and_catalog_context_with_empty_lists(self):
        class Pool:
            def __init__(self, **kw): pass
            def submit(self, fn, *args):
                f = Future(); f.set_result(fn(*args)); return f
            def shutdown(self, **kw): pass
        cases = [([ord('Z'),10,ord('n')], 'namespaces'),
                 ([ord('Z'),10,ord('D')], 'namespaces'),
                 ([ord('3'),10,ord('D')], 'pods'),
                 ([ord('3'),10,ord('n')], 'pods'),
                 ([ord('3'),10,kuba_dm.curses.KEY_RIGHT,ord('n')], 'deployments'),
                 ([ord('6'),10,ord('n')], 'services'),
                 ([ord('0'),10,-1,ord('n')], 'widgets.example.io'),
                 ([ord('0'),10,-1,10,-1,ord('n')], 'widgets.example.io')]
        for sequence, resource in cases:
            keys = iter(sequence + [ord('q')])
            class Terminal:
                def timeout(self, value): pass
                def getmaxyx(self): return 40, 140
                def erase(self): pass
                def addnstr(self, *args): pass
                def refresh(self): pass
                def getch(self): return next(keys)
            def empty_data():
                data = kuba_dm.demo()
                for name in kuba_dm.WORKLOAD_KINDS: data[name] = []
                return data
            handler = Mock()
            with ExitStack() as stack:
                stack.enter_context(patch('kuba_dm_app.ui.dashboard.concurrent.futures.ThreadPoolExecutor', Pool))
                stack.enter_context(patch('kuba_dm_app.ui.dashboard.terminal_action', side_effect=lambda screen, fn: fn()))
                for name in ('curs_set', 'set_escdelay', 'mousemask', 'mouseinterval'):
                    stack.enter_context(patch('kuba_dm_app.ui.dashboard.curses.' + name, create=True))
                stack.enter_context(patch('kuba_dm_app.ui.dashboard.sys.stdout'))
                kuba_dm.screen(Terminal(), empty_data, 30, resource_loader=lambda *args: ([], None),
                             discovery_loader=lambda: ([{'name':'widgets.example.io','namespaced':False}], []),
                             action_handler=handler)
            handler.assert_called_once()
            action, item = handler.call_args.args
            self.assertEqual(action, 'drafts' if ord('D') in sequence else 'new'); self.assertEqual(item['resource'], resource)
            self.assertIsNone(item['name']); self.assertIsNone(item['namespace'])
            self.assertEqual(item['namespaced'], resource not in ('widgets.example.io', 'namespaces'))


if __name__ == '__main__': unittest.main()
