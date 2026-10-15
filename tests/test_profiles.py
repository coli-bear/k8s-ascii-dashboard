import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from kasciidash_app.ui import dashboard as kasciidash
from kasciidash_app.config import profiles
CONFIG = {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'ctx',
          'clusters': [{'name': 'cluster', 'cluster': {'server': 'https://cluster.invalid'}}],
          'contexts': [{'name': 'ctx', 'context': {'cluster': 'cluster', 'user': 'user'}}],
          'users': [{'name': 'user', 'user': {'client-certificate-data': 'fixture-only'}}]}


class ProfileTests(unittest.TestCase):
    def test_last_profile_survives_restart_rename_and_delete(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store = profiles.Store(root)
            self.assertIsNone(store.preferred())
            first = store.register(root / 'first', 'first', CONFIG)
            second = store.register(root / 'second', 'second', CONFIG)
            self.assertEqual(store.preferred()['id'], first['id'])
            store.remember(second['id'])
            store.update(second['id'], alias='renamed')
            loaded = profiles.Store(root)
            self.assertEqual(loaded.preferred()['alias'], 'renamed')
            with patch.object(loaded, 'save') as save:
                loaded.remember(second['id'])
                save.assert_not_called()
            loaded.remove(second['id'])
            self.assertEqual(profiles.Store(root).preferred()['id'], first['id'])
            with self.assertRaises(ValueError): loaded.remember('missing')
            loaded.data['last_profile_id'] = 'deleted-id'
            loaded.save()
            self.assertEqual(profiles.Store(root).preferred()['id'], first['id'])

    def test_remember_conflict_preserves_registry_and_local_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store = profiles.Store(root)
            first = store.register(root / 'first', 'first', CONFIG)
            second = store.register(root / 'second', 'second', CONFIG)
            store.remember(first['id'])
            other = profiles.Store(root)
            store.update(first['id'], alias='changed')
            with self.assertRaisesRegex(ValueError, 'another process'):
                other.remember(second['id'])
            self.assertEqual(other.preferred()['id'], first['id'])
            self.assertEqual(profiles.Store(root).preferred()['alias'], 'changed')

    def test_bootstrap_order_persistence_delete_and_corrupt_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / '.kube').mkdir()
            for name in ('z.conf', 'a.conf'): (root / '.kube' / name).write_text('fixture')
            store = profiles.Store(root)
            self.assertEqual([p['alias'] for p in store.items], ['a', 'z'])
            first = store.items[0]
            store.update(first['id'], alias='production', context='ctx', prometheus={'endpoint': 'https://prom.invalid/base/'})
            loaded = profiles.Store(root)
            self.assertEqual(loaded.items[0]['prometheus']['endpoint'], 'https://prom.invalid/base')
            self.assertEqual(stat.S_IMODE(loaded.path.stat().st_mode), 0o600)
            loaded.remove(first['id'])
            self.assertTrue((root / '.kube' / 'a.conf').is_file())
            self.assertEqual([p['alias'] for p in profiles.Store(root).items], ['z'])
            loaded.path.write_text('{broken')
            with self.assertRaisesRegex(ValueError, 'preserved'): profiles.Store(root)
            self.assertEqual(loaded.path.read_text(), '{broken')

    def test_import_copy_alias_validation_and_write_conflicts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'original.yaml'; source.write_text('unchanged')
            store = profiles.Store(root)
            item = store.register(source, 'prod', CONFIG)
            target = store.config_path(item)
            self.assertTrue(target.is_relative_to(root.resolve() / '.kube'))
            self.assertEqual(target.suffix, '.conf')
            self.assertEqual(json.loads(target.read_text()), CONFIG)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(source.read_text(), 'unchanged')
            with self.assertRaisesRegex(ValueError, 'unique'): store.register(source, 'PROD', CONFIG)
            with self.assertRaisesRegex(ValueError, 'existing context'): store.register(source, 'other', CONFIG, 'missing')
            other = profiles.Store(root)
            store.update(item['id'], alias='updated')
            with self.assertRaisesRegex(ValueError, 'another process'): other.update(item['id'], alias='lost')
            self.assertEqual(other.items[0]['alias'], 'prod')
            self.assertEqual(profiles.Store(root).items[0]['alias'], 'updated')
            with self.assertRaises(ValueError): store.config_path({'kubeconfig': '../outside'})

    def test_prometheus_url_auth_response_and_redirect_validation(self):
        good = {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'value': [123, '1']}]}}
        with patch('kasciidash_app.config.profiles.urllib.request.build_opener') as build, patch.dict(os.environ, {'TEST_PROM_TOKEN': 'private-fixture'}):
            response = build.return_value.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps(good).encode()
            checked = profiles.check_prometheus({'endpoint': 'https://prom.invalid/prefix', 'token_env': 'TEST_PROM_TOKEN'})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://prom.invalid/prefix/api/v1/query?query=vector%281%29&timeout=8s')
            self.assertEqual(request.get_header('Authorization'), 'Bearer private-fixture')
            self.assertIn('verified_at', checked)
            self.assertNotIn('private-fixture', json.dumps(checked))
            for payload in (b'<html>login</html>', b'[]', b'null', b'{"status":"success"}',
                            b'{"status":"success","data":{"resultType":"vector","result":[]}}'):
                response.read.return_value = payload
                with self.assertRaises(ValueError): profiles.check_prometheus({'endpoint': 'http://prom.invalid'})
        for endpoint in ('ftp://host', 'https://user:secret@host', 'https://host?token=secret', 'http://host:bad', 'http://host/ bad'):
            with self.assertRaises(ValueError): profiles.prometheus_config({'endpoint': endpoint})
        with self.assertRaises(ValueError): profiles.prometheus_config({'endpoint': 'http://host', 'token': 'secret'})
        with self.assertRaisesRegex(ValueError, 'not set'):
            profiles.check_prometheus({'endpoint': 'http://prom.invalid', 'token_env': 'KASCIIDASH_TEST_MISSING_TOKEN'})
        self.assertIsNone(profiles.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://other.invalid'))

    def test_discovery_has_all_tcp_nodeports_and_tolerates_nullable_fields(self):
        client = Mock()
        client.call.side_effect = [({'items': [
            {'metadata': {'namespace': 'monitoring', 'name': 'prometheus'}, 'spec': {'type': 'NodePort', 'ports': [{'port': 9090, 'nodePort': 30900}]}},
            {'metadata': {'name': 'other'}, 'spec': {'type': 'NodePort', 'ports': [{'port': 8080, 'nodePort': 30800}]}},
            {'metadata': {'name': 'dns'}, 'spec': {'ports': [{'port': 53, 'nodePort': 30053, 'protocol': 'UDP'}]}},
            {'metadata': {'name': 'prometheus-internal'}, 'spec': {'clusterIP': '10.1.2.3', 'ports': [{'port': 9090}]}},
            {'metadata': None, 'spec': None, 'status': None}]}, None),
            ({'items': [{'status': {'addresses': [{'type': 'InternalIP', 'address': 'fd00::1'}]}}, {'status': None}]}, None)]
        candidates, addresses, errors = profiles.service_candidates(client)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0]['endpoint'], 'http://[fd00::1]:30900')
        self.assertIn('http://[fd00::1]:30800', [c['endpoint'] for c in candidates])
        self.assertFalse(errors)
        client.call.side_effect = [(None, 'Forbidden')]
        self.assertEqual(profiles.service_candidates(client), ([], [], ['Forbidden']))

    def test_registration_uses_flatten_and_json_import_is_checked_before_save(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'input.conf'; source.write_text('fixture')
            store = profiles.Store(root); client = Mock(); client.call.return_value = (CONFIG, None)
            with patch('builtins.input', side_effect=[str(source), 'demo']), patch('kasciidash_app.config.profiles.clear'), contextlib.redirect_stdout(io.StringIO()):
                item = profiles.register_prompt(store, lambda path, context: client)
            client.call.assert_called_once_with('config', 'view', '--raw', '--flatten', '-o', 'json')
            settings_file = root / 'prom.json'; settings_file.write_text('{"endpoint":"http://prom.invalid:9090"}')
            with patch('builtins.input', side_effect=['j', str(settings_file)]), patch('kasciidash_app.config.profiles.clear'), \
                 patch('kasciidash_app.config.profiles.check_prometheus', side_effect=ValueError('offline')), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'offline'):
                    profiles.connection_prompt(store, item, lambda p, c: client, 8)
            self.assertFalse(store.items[0]['prometheus'])
            with patch('builtins.input', side_effect=['j', str(settings_file), '']), patch('kasciidash_app.config.profiles.clear'), \
                 patch('kasciidash_app.config.profiles.check_prometheus', return_value={'endpoint': 'http://prom.invalid:9090', 'verified_at': 'fixture'}), contextlib.redirect_stdout(io.StringIO()):
                profiles.connection_prompt(store, item, lambda p, c: client, 8)
            self.assertEqual(profiles.Store(root).items[0]['prometheus']['endpoint'], 'http://prom.invalid:9090')

    def test_service_first_selects_nodeport_without_url_or_auth_questions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store = profiles.Store(root)
            profile = store.register(root / 'config', 'aifactory', CONFIG)
            profile = store.update(profile['id'], prometheus={'endpoint': 'http://old.invalid', 'token_env': 'OLD_TOKEN'})
            client = Mock()
            client.call.side_effect = [({'items': [{'metadata': {'namespace': 'monitoring', 'name': 'prometheus'},
                'spec': {'type': 'NodePort', 'ports': [{'name': 'web', 'port': 9090, 'targetPort': 9090, 'nodePort': 30900},
                                                      {'name': 'other', 'port': 9091, 'nodePort': 30901}]}}]}, None),
                ({'items': [{'status': {'addresses': [{'type': 'InternalIP', 'address': ip}]}} for ip in ('10.10.12.131', '10.10.12.135')]}, None)]
            factory = Mock(return_value=client)
            output = io.StringIO()
            with patch('builtins.input', side_effect=['2', '2', '', '']) as read, patch('kasciidash_app.config.profiles.clear'), \
                 patch('kasciidash_app.config.profiles.check_prometheus', side_effect=lambda config, timeout: dict(config, verified_at='fixture')) as check, \
                 contextlib.redirect_stdout(output):
                profiles.connection_prompt(store, profile, factory, 8)
            factory.assert_called_once_with(store.config_path(profile), 'ctx')
            self.assertEqual(check.call_args.args[0], {'endpoint': 'http://10.10.12.135:30901'})
            self.assertEqual(store.items[0]['prometheus']['endpoint'], 'http://10.10.12.135:30901')
            self.assertNotIn('Auth:', str(read.call_args_list))
            self.assertNotIn('Custom CA', str(read.call_args_list))
            self.assertIn('Step 1/3', output.getvalue())
            self.assertIn('Service port: 9091', output.getvalue())
            self.assertNotIn('OLD_TOKEN', str(check.call_args))

    def test_escape_cancels_service_setup_and_invalid_manual_url_stays_in_picker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store = profiles.Store(root)
            profile = store.register(root / 'config', 'aifactory', CONFIG)
            client = Mock(); client.call.return_value = ({'items': []}, None)
            for answers in (['1\x1b'], ['u', '10.10.12.135', '', '']):
                with self.subTest(answers=answers), patch('builtins.input', side_effect=answers) as read, \
                     patch('kasciidash_app.config.profiles.clear'), patch('kasciidash_app.config.profiles.check_prometheus') as check, contextlib.redirect_stdout(io.StringIO()):
                    profiles.connection_prompt(store, profile, lambda path, context: client, 8)
                check.assert_not_called()
                self.assertNotIn('Auth:', str(read.call_args_list))
                self.assertFalse(store.items[0]['prometheus'])

    def test_main_auto_first_and_profile_switch_have_separate_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store = profiles.Store(root)
            first = store.register(root / 'first', 'first', CONFIG)
            second = store.register(root / 'second', 'second', CONFIG)
            first = store.update(first['id'], prometheus={'endpoint': 'http://first.invalid'})
            second = store.update(second['id'], prometheus={'endpoint': 'http://second.invalid'})
            clients, snapshots = [], []
            def factory(path, context, timeout):
                clients.append((path, context)); return Mock()
            def collect(client, info): snapshots.append(info.copy()); return kasciidash.demo()
            count = 0
            def wrapper(fn, loader, *args):
                nonlocal count
                loader(); count += 1
                return 'configs' if count == 1 else 'quit'
            with patch.object(kasciidash, 'BASE', root), patch('sys.argv', ['kasciidash']), \
                 patch('kasciidash_app.ui.dashboard.shutil.which', return_value='/kubectl'), patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stdout.isatty', return_value=True), patch('kasciidash_app.ui.dashboard.Client', factory), \
                 patch('kasciidash_app.ui.dashboard.identity', return_value={'cluster': 'cluster', 'context': 'ctx', 'server': 'https://fixture'}), \
                 patch('kasciidash_app.ui.dashboard.collect', collect), patch('kasciidash_app.ui.dashboard.curses.wrapper', wrapper), \
                 patch('kasciidash_app.config.profiles.manage', return_value=second) as manage, patch('kasciidash_app.config.profiles.clear'):
                self.assertEqual(kasciidash.main(), 0)
            self.assertEqual([i['alias'] for i in snapshots], ['first', 'second'])
            self.assertEqual([i['prometheus']['endpoint'] for i in snapshots], ['http://first.invalid', 'http://second.invalid'])
            self.assertEqual([p for p, c in clients], [store.config_path(first), store.config_path(second)])
            manage.assert_called_once()
            self.assertEqual(profiles.Store(root).preferred()['id'], second['id'])
            snapshots.clear()
            with patch.object(kasciidash, 'BASE', root), patch('sys.argv', ['kasciidash']), \
                 patch('kasciidash_app.ui.dashboard.shutil.which', return_value='/kubectl'), patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stdout.isatty', return_value=True), patch('kasciidash_app.ui.dashboard.Client', factory), \
                 patch('kasciidash_app.ui.dashboard.identity', return_value={'cluster': 'cluster', 'context': 'ctx', 'server': 'https://fixture'}), \
                 patch('kasciidash_app.ui.dashboard.collect', collect), patch('kasciidash_app.ui.dashboard.curses.wrapper', side_effect=lambda fn, loader, *a: (loader(), 'quit')[1]), \
                 patch('kasciidash_app.config.profiles.manage') as manage, patch('kasciidash_app.config.profiles.clear'):
                self.assertEqual(kasciidash.main(), 0)
                manage.assert_not_called()
            self.assertEqual([i['alias'] for i in snapshots], ['second'])

    def test_single_connection_is_automatic_and_multiple_show_cluster_account(self):
        with patch('builtins.input') as read:
            self.assertEqual(profiles.choose_connection(CONFIG)['name'], 'ctx')
            read.assert_not_called()
        config = dict(CONFIG, contexts=CONFIG['contexts'] + [
            {'name': 'other', 'context': {'cluster': 'other-cluster', 'user': 'operator'}}])
        output = io.StringIO()
        with patch('builtins.input', return_value='2'), patch('kasciidash_app.config.profiles.clear'), contextlib.redirect_stdout(output):
            self.assertEqual(profiles.choose_connection(config)['name'], 'other')
        self.assertIn('Cluster: other-cluster | Account: operator', output.getvalue())
        self.assertNotIn('Choose context', output.getvalue())

    def test_profile_list_shows_conf_file_and_rename_needs_no_cluster_call(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store = profiles.Store(root)
            item = store.register(root / 'aifactory-admin.conf', 'aifactory', CONFIG)
            client_factory = Mock()
            output = io.StringIO()
            with patch('builtins.input', side_effect=['e', '1', 'production', '']) as read, \
                 patch('kasciidash_app.config.profiles.clear'), contextlib.redirect_stdout(output):
                selected = profiles.manage(store, item, client_factory)
            self.assertEqual(selected['alias'], 'production')
            client_factory.assert_not_called()
            self.assertIn('CONF FILE', output.getvalue())
            self.assertIn(Path(item['kubeconfig']).name, output.getvalue())
            self.assertNotIn('CONTEXT', output.getvalue())
            self.assertNotIn('context', str(read.call_args_list))

    def test_context_change_clears_previous_prometheus_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store = profiles.Store(root)
            item = store.register(root / 'original', 'prod', CONFIG)
            item = store.update(item['id'], prometheus={'endpoint': 'http://old-cluster.invalid'})
            config = dict(CONFIG, contexts=CONFIG['contexts'] + [{'name': 'other', 'context': {'cluster': 'other-cluster'}}])
            client = Mock(); client.call.return_value = (config, None)
            with patch('builtins.input', side_effect=['k', '1', '2', '']), patch('kasciidash_app.config.profiles.clear'), contextlib.redirect_stdout(io.StringIO()):
                selected = profiles.manage(store, item, lambda p, c: client)
            self.assertEqual(selected['context'], 'other')
            self.assertFalse(selected['prometheus']['endpoint'])

    def test_manager_edit_delete_and_no_profile_registration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'sample'; source.write_text('fixture')
            store = profiles.Store(root)
            client = Mock(); client.call.return_value = (CONFIG, None)
            with patch('builtins.input', side_effect=['a', str(source), 'sample']), patch('kasciidash_app.config.profiles.clear'), contextlib.redirect_stdout(io.StringIO()):
                selected = profiles.manage(store, None, lambda p, c: client)
            self.assertEqual(selected['alias'], 'sample')
            with patch('builtins.input', side_effect=['e', '1', 'renamed', '']), patch('kasciidash_app.config.profiles.clear'), contextlib.redirect_stdout(io.StringIO()):
                selected = profiles.manage(store, selected, lambda p, c: client)
            self.assertEqual(selected['alias'], 'renamed')
            with patch('builtins.input', side_effect=['d', '1', 'y', 'q']), patch('kasciidash_app.config.profiles.clear'), contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(profiles.manage(store, selected, lambda p, c: client))
            self.assertFalse(profiles.Store(root).items)
            self.assertTrue(store.config_path(selected).is_file())


if __name__ == '__main__': unittest.main()
