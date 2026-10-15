from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from kasciidash_app.resources import resource_actions as actions
from tests.test_yaml_storage import fake_client
from tests.test_resource_actions import selected


class DraftTransactionTests(unittest.TestCase):
    def test_resource_names_are_canonical_and_existing_short_path_is_found(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(actions.yaml_directory(root, 'p', 'deployments'), actions.yaml_directory(root, 'p', 'deployments.apps'))
            old = Path(root) / '.kasciidash/p/deployments'; old.mkdir(parents=True)
            (old / 'web.yaml').write_text('fake')
            with patch.object(actions, 'choose', side_effect=actions.profiles.InputCancelled()) as choose:
                with self.assertRaises(actions.profiles.InputCancelled): actions.saved_drafts(fake_client(), root, selected(), 'p')
            self.assertIn('web.yaml', str(choose.call_args))
            self.assertNotEqual(actions.yaml_directory(root, 'p', 'deployments.apps'), actions.yaml_directory(root, 'p', 'deployments.example.io'))

    def test_cleanup_does_not_unlink_new_original_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'web.yaml'; path.write_bytes(b'approved')
            unlink = Path.unlink
            def raced_unlink(p, *args, **kw):
                path.write_bytes(b'new unsent draft')
                return unlink(p, *args, **kw)
            with patch.object(Path, 'unlink', raced_unlink):
                actions.cleanup_draft(path, b'approved')
            self.assertEqual(path.read_bytes(), b'new unsent draft')

    def test_cleanup_changed_missing_and_failure_are_distinct(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'web.yaml';path.write_bytes(b'changed')
            self.assertIn('kept', actions.cleanup_draft(path,b'approved').lower())
            self.assertEqual(path.read_bytes(),b'changed')
            path.unlink()
            self.assertIn('already',actions.cleanup_draft(path,b'approved').lower())
            path.write_bytes(b'approved')
            with patch.object(Path,'unlink',side_effect=PermissionError('fixture')):
                self.assertIn('cleanup',actions.cleanup_draft(path,b'approved').lower())
            self.assertTrue(any(p.read_bytes()==b'approved' for p in Path(root).rglob('*.yaml')))

    def test_all_workload_aliases_both_directions_and_profile_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            for short, full in (('deployments','deployments.apps'), ('replicasets','replicasets.apps'),
                                ('statefulsets','statefulsets.apps'), ('daemonsets','daemonsets.apps'),
                                ('jobs','jobs.batch'), ('cronjobs','cronjobs.batch')):
                for source, destination in ((short,full),(full,short)):
                    path=actions.yaml_directory(root,'prod',source)/'fixture.yaml';path.write_text('fake')
                    with patch.object(actions,'choose',side_effect=actions.profiles.InputCancelled()) as choose:
                        with self.assertRaises(actions.profiles.InputCancelled):
                            actions.saved_drafts(fake_client(),root,selected(destination),'prod')
                    self.assertIn(path, [value for label,value in choose.call_args.args[1]])
                    path.unlink()
                self.assertNotEqual(actions.yaml_directory(root,'prod',short),actions.yaml_directory(root,'other',short))
                self.assertNotEqual(actions.yaml_directory(root,'prod',short),actions.yaml_directory(root,'prod',short+'.example.io'))

    def test_claim_content_changes_are_kept_and_published_collision_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'web.yaml';path.write_bytes(b'approved')
            read=Path.read_bytes
            def reader(p):
                if p.parent.name.startswith('.draft-claim-'):
                    p.write_bytes(b'changed claimed draft')
                    path.write_bytes(b'new public draft')
                return read(p)
            with patch.object(Path,'read_bytes',reader):
                message=actions.cleanup_draft(path,b'approved')
            self.assertIn('kept',message.lower())
            self.assertEqual(path.read_bytes(),b'new public draft')
            self.assertTrue(any(p.read_bytes()==b'changed claimed draft' for p in Path(root).rglob('*.yaml')))

    def test_previous_layout_cleanup_failure_remains_in_picker(self):
        with tempfile.TemporaryDirectory() as root:
            directory=actions.yaml_directory(root,'prod','deployments.apps')
            (directory/'draft').mkdir()
            old=directory/'draft/hash.yaml';old.write_text('legacy')
            with patch.object(actions,'choose',return_value=old),patch.object(actions,'cleanup_draft',return_value='Local cleanup failed'),patch.object(actions,'edit_yaml') as edit,patch('builtins.print'):
                actions.saved_drafts(fake_client(),root,selected(),'prod')
            self.assertTrue(old.exists())
            self.assertEqual((directory/'web.yaml').read_text(),'legacy')
            edit.assert_called_once()
