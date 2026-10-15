import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from kasciidash_app.config import profiles
from tests.test_profile_registry_contract import REGISTRY


class RegistryResolutionTests(unittest.TestCase):
    def fixtures(self, root):
        parent=Path(root)/'.kasciidash';parent.mkdir()
        canonical=parent/'profiles.json';old=parent/'previous_app.config.profiles.json'
        canonical.write_text(json.dumps(REGISTRY))
        value=copy.deepcopy(REGISTRY);value['profiles'][0]['alias']='old-fixture'
        old.write_text(json.dumps(value))
        return canonical,old

    def test_explicit_canonical_choice_backs_up_every_original(self):
        with tempfile.TemporaryDirectory() as root:
            canonical,old=self.fixtures(root);originals=[p.read_bytes() for p in (canonical,old)]
            store=profiles.Store(root,resolver=lambda entries: entries[0][0])
            self.assertEqual(store.preferred()['alias'],'fixture')
            self.assertEqual(canonical.read_bytes(),originals[0])
            backups=[p.read_bytes() for p in (canonical.parent/'registry-backups').iterdir()]
            self.assertTrue(all(value in backups for value in originals))
            self.assertFalse(old.exists())
            self.assertEqual(profiles.Store(root).preferred()['alias'],'fixture')

    def test_explicit_legacy_choice_does_not_lose_canonical(self):
        with tempfile.TemporaryDirectory() as root:
            canonical,old=self.fixtures(root);previous=canonical.read_bytes()
            store=profiles.Store(root,resolver=lambda entries: entries[1][0])
            self.assertEqual(store.preferred()['alias'],'old-fixture')
            self.assertTrue(any(p.read_bytes()==previous for p in (canonical.parent/'registry-backups').iterdir()))

    def test_cancel_or_backup_failure_preserves_active_files(self):
        for fail in (False,True):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as root:
                canonical,old=self.fixtures(root);before=[p.read_bytes() for p in (canonical,old)]
                def resolve(entries):
                    if not fail:raise profiles.InputCancelled()
                    return entries[1][0]
                with patch.object(profiles,'atomic_write',side_effect=OSError('fixture')):
                    with self.assertRaises((profiles.InputCancelled,OSError)):
                        profiles.Store(root,resolver=resolve)
                self.assertEqual([p.read_bytes() for p in (canonical,old)],before)

    def test_prompt_has_no_implicit_choice(self):
        with tempfile.TemporaryDirectory() as root:
            canonical,old=self.fixtures(root)
            entries=[(canonical,REGISTRY),(old,REGISTRY)]
            with patch.object(profiles,'prompt',side_effect=['','2']),patch.object(profiles,'clear'),patch('builtins.print'):
                self.assertEqual(profiles.choose_registry(entries),old)
            with patch.object(profiles,'prompt',return_value='q'),patch.object(profiles,'clear'),patch('builtins.print'):
                with self.assertRaises(profiles.InputCancelled):profiles.choose_registry(entries)

    def test_registry_changed_during_selection_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            canonical,old=self.fixtures(root)
            def resolve(entries):
                changed=copy.deepcopy(REGISTRY);changed['profiles'][0]['alias']='external-change'
                canonical.write_text(json.dumps(changed))
                return entries[1][0]
            with self.assertRaisesRegex(ValueError,'changed during selection'):
                profiles.Store(root,resolver=resolve)
            self.assertEqual(json.loads(canonical.read_text())['profiles'][0]['alias'],'external-change')
            self.assertTrue(old.exists())

    def test_main_interactive_recovers_and_once_never_prompts(self):
        import contextlib,io
        from unittest.mock import Mock
        from kasciidash_app.ui import dashboard
        for once in (True,False):
            with self.subTest(once=once),tempfile.TemporaryDirectory() as root:
                canonical,old=self.fixtures(root)
                kube=Path(root)/'.kube';kube.mkdir();(kube/'fixture.conf').write_text('synthetic only')
                argv=['kasciidash']+(['--once'] if once else [])
                with patch.object(dashboard,'BASE',Path(root)),patch('sys.argv',argv),patch.object(dashboard.shutil,'which',return_value='/synthetic/kubectl'),patch('sys.stdin.isatty',return_value=True),patch('sys.stdout.isatty',return_value=True),patch.object(dashboard,'Client',return_value=Mock()),patch.object(dashboard,'identity',return_value={'cluster':'fixture','context':'fixture-context','server':'https://fixture.invalid'}),patch.object(dashboard.curses,'wrapper',return_value='quit'),patch.object(profiles,'clear'),patch.object(profiles,'choose_registry',side_effect=lambda entries:entries[0][0]) as choose,contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(dashboard.main(),1 if once else 0)
                self.assertEqual(choose.call_count,int(not once))
                self.assertEqual(old.exists(),once)
