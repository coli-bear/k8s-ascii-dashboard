import concurrent.futures
import copy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from kasciidash_app.config import profiles


REGISTRY = {'version': 1, 'last_profile_id': 'fixture-id', 'profiles': [
    {'id': 'fixture-id', 'alias': 'fixture', 'kubeconfig': '.kube/fixture.conf',
     'context': 'fixture-context', 'prometheus': {'endpoint': 'http://prom.invalid'}}]}


class RegistryContractTests(unittest.TestCase):
    def fixture(self, root, name, value=REGISTRY):
        path = Path(root) / '.kasciidash' / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value) if not isinstance(value, str) else value)
        return path

    def test_canonical_contract_and_preferred_profile(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.fixture(root, 'profiles.json')
            store = profiles.Store(root)
            self.assertEqual(store.path, path.resolve())
            self.assertEqual(store.preferred()['id'], 'fixture-id')
            self.assertEqual(store.preferred()['prometheus']['endpoint'], 'http://prom.invalid')

    def test_legacy_only_and_identical_files_migrate_preserving_data(self):
        for canonical in (False, True):
            with self.subTest(canonical=canonical), tempfile.TemporaryDirectory() as root:
                old = self.fixture(root, 'kasciidash_app.config.profiles.json')
                if canonical: self.fixture(root, 'profiles.json')
                store = profiles.Store(root)
                new = Path(root) / '.kasciidash/profiles.json'
                self.assertTrue(new.is_file())
                self.assertEqual(json.loads(new.read_text()), REGISTRY)
                self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o600)
                self.assertFalse(old.exists())
                self.assertTrue(list((new.parent / 'registry-backups').iterdir()))
                store.update('fixture-id', alias='renamed')
                self.assertEqual(profiles.Store(root).preferred()['alias'], 'renamed')

    def test_conflict_and_corruption_preserve_all_inputs(self):
        changed = copy.deepcopy(REGISTRY); changed['profiles'][0]['alias'] = 'other'
        for legacy in (changed, '{broken'):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as root:
                first = self.fixture(root, 'profiles.json')
                second = self.fixture(root, 'kasciidash_app.config.profiles.json', legacy)
                originals = [p.read_bytes() for p in (first, second)]
                with self.assertRaises(ValueError): profiles.Store(root)
                self.assertEqual([p.read_bytes() for p in (first, second)], originals)
        with tempfile.TemporaryDirectory() as root:
            path = self.fixture(root, 'profiles.json', '{broken')
            with self.assertRaises(ValueError): profiles.Store(root)
            self.assertEqual(path.read_text(), '{broken')

    def test_migration_write_failure_and_concurrent_start(self):
        with tempfile.TemporaryDirectory() as root:
            old = self.fixture(root, 'kasciidash_app.config.profiles.json')
            with patch.object(profiles, 'atomic_write', side_effect=OSError('fixture write failure')):
                with self.assertRaises(OSError): profiles.Store(root)
            self.assertTrue(old.exists())
            self.assertFalse((old.parent / 'profiles.json').exists())
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                stores = list(pool.map(lambda _: profiles.Store(root), range(2)))
            self.assertEqual([s.preferred()['id'] for s in stores], ['fixture-id'] * 2)
            stores[0].update('fixture-id', alias='updated')
            with self.assertRaisesRegex(ValueError, 'another process'):
                stores[1].update('fixture-id', alias='stale')

    def test_multiple_legacy_files_and_invalid_legacy_do_not_bootstrap(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.fixture(root, 'first_app.config.profiles.json')
            second = self.fixture(root, 'second_app.config.profiles.json')
            store = profiles.Store(root)
            self.assertEqual(store.preferred()['context'], 'fixture-context')
            self.assertFalse(first.exists()); self.assertFalse(second.exists())
        with tempfile.TemporaryDirectory() as root:
            first = self.fixture(root, 'first_app.config.profiles.json', '{broken')
            with self.assertRaises(ValueError): profiles.Store(root)
            self.assertTrue(first.exists())
            self.assertFalse((first.parent / 'profiles.json').exists())

    def test_backup_failure_leaves_recoverable_registries_and_save_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as root:
            old = self.fixture(root, 'kasciidash_app.config.profiles.json')
            with patch.object(Path, 'rename', side_effect=PermissionError('fixture')):
                with self.assertRaises(PermissionError): profiles.Store(root)
            self.assertTrue(old.exists())
            self.assertEqual(json.loads((old.parent/'profiles.json').read_text()), REGISTRY)
            store = profiles.Store(root)
            with patch.object(profiles,'atomic_write',side_effect=OSError('fixture')):
                with self.assertRaises(OSError): store.update('fixture-id',alias='changed')
            self.assertEqual(store.preferred()['alias'],'fixture')
            self.assertEqual(profiles.Store(root).preferred()['alias'],'fixture')
