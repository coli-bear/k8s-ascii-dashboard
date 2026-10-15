from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from kasciidash_app.resources import resource_actions as actions
from tests.test_resource_actions import client, obj, selected


def fake_client():
    c = client()
    def call(*args, **kwargs):
        if args[:2] == ('get', '--raw'):
            if args[-1] == '/apis/apps':
                return {'preferredVersion': {'groupVersion': 'apps/v1'}}, None
            return {'resources': [{'name': 'deployments', 'kind': 'Deployment', 'namespaced': True, 'verbs': ['create']}]}, None
        return obj(), None
    c.call.side_effect = call
    c.text.side_effect = lambda *args, **kw: ('kind: Deployment\n', None) if '--dry-run=client' in args else ('created', None)
    return c


class YamlStorageTests(unittest.TestCase):
    def test_only_unsubmitted_new_drafts_persist(self):
        for new in (False, True):
            for outcome in ('cancel', 'success', 'failure', 'editor-failure', 'escape'):
                with self.subTest(new=new, outcome=outcome), tempfile.TemporaryDirectory() as folder:
                    c = fake_client(); paths = []
                    if outcome == 'failure':
                        c.text.side_effect = lambda *a, **kw: ('kind: Deployment\n', None) if '--dry-run=client' in a else ('', 'fixture failure')
                    def editor(argv):
                        paths.append(Path(argv[-1]))
                        self.assertEqual(stat.S_IMODE(paths[-1].stat().st_mode), 0o600)
                        return Mock(returncode=1 if outcome == 'editor-failure' else 0)
                    def prompt(question):
                        if question.startswith('New resource'): return 'web'
                        if question.startswith('Namespace'): return 'prod'
                        if outcome == 'escape': raise actions.profiles.InputCancelled()
                        return ('create' if new else 'apply') if question.startswith('Type ') and outcome != 'cancel' else ''
                    with patch.object(actions.subprocess, 'run', side_effect=editor), patch.object(actions.profiles, 'prompt', side_effect=prompt), patch('builtins.print'):
                        if outcome in ('failure', 'editor-failure', 'escape'):
                            with self.assertRaises((ValueError, actions.profiles.InputCancelled)):
                                actions.edit_yaml(c, folder, selected(), new=new, profile_alias='prod/team')
                        else:
                            actions.edit_yaml(c, folder, selected(), new=new, profile_alias='prod/team')
                    stored = list((Path(folder) / '.kasciidash').rglob('*.yaml'))
                    self.assertEqual(len(stored), int(new and outcome != 'success'))
                    if stored:
                        self.assertEqual(stored[0], Path(folder) / '.kasciidash/prod%2Fteam/deployments.apps/web.yaml')
                    if not new and paths: self.assertFalse(paths[0].exists())

    def test_resume_and_successful_deletion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = actions.yaml_directory(folder, 'prod', 'deployments.apps') / 'web.yaml'
            path.write_text('saved: draft\n')
            c = fake_client()
            with patch.object(actions, 'choose', return_value=path), patch.object(actions.subprocess, 'run', return_value=Mock(returncode=0)), patch.object(actions.profiles, 'prompt', side_effect=['', 'create']), patch('builtins.print'):
                actions.saved_drafts(c, folder, selected(), 'prod')
            self.assertFalse(path.exists())
            self.assertEqual(c.text.call_args.kwargs['input_text'], 'saved: draft\n')
            self.assertEqual(c.text.call_args.args[0], 'create')
            self.assertNotIn('--namespace', c.text.call_args.args)

    def test_resume_rejects_changed_kind_name_and_multi_document(self):
        for preview in (dict(kind='List', items=[obj(), obj()]), dict(obj(), kind='Pod'), dict(obj(), metadata={'name': 'changed'})):
            with self.subTest(preview=preview), tempfile.TemporaryDirectory() as folder:
                path = actions.yaml_directory(folder, 'prod', 'deployments.apps') / 'web.yaml'
                path.write_text('saved')
                c = fake_client(); discovery = c.call.side_effect
                c.call.side_effect = lambda *args, **kw: (preview, None) if '--dry-run=server' in args else discovery(*args, **kw)
                with patch.object(actions.subprocess, 'run', return_value=Mock(returncode=0)), patch.object(actions.profiles, 'prompt', side_effect=['', '']), patch('builtins.print'):
                    actions.edit_yaml(c, folder, selected(), new=True, profile_alias='prod', draft_path=path)
                c.text.assert_not_called()
                self.assertTrue(path.exists())

    def test_existing_new_draft_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = actions.yaml_directory(folder, 'prod', 'deployments.apps') / 'web.yaml'
            path.write_text('keep me')
            with patch.object(actions.profiles, 'prompt', side_effect=['web','prod']), patch.object(actions.subprocess, 'run') as editor, patch('builtins.print'):
                actions.edit_yaml(fake_client(), folder, selected(), new=True, profile_alias='prod')
            editor.assert_not_called()
            self.assertEqual(path.read_text(), 'keep me')

    def test_external_changes_after_approval_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = actions.yaml_directory(folder, 'prod', 'deployments.apps') / 'web.yaml'
            path.write_text('approved')
            def prompt(question):
                if question.startswith('Type '):
                    path.write_text('newer draft')
                    return 'create'
                return ''
            c = fake_client()
            with patch.object(actions.subprocess, 'run', return_value=Mock(returncode=0)), patch.object(actions.profiles, 'prompt', side_effect=prompt), patch('builtins.print'):
                actions.edit_yaml(c, folder, selected(), new=True, profile_alias='prod', draft_path=path)
            self.assertEqual(c.text.call_args.kwargs['input_text'], 'approved')
            self.assertEqual(path.read_text(), 'newer draft')

    def test_perform_passes_profile_alias(self):
        for action, method in (('edit', 'edit_yaml'), ('new', 'edit_yaml'), ('drafts', 'saved_drafts')):
            with patch.object(actions, method) as run, patch.object(actions.profiles, 'clear'), patch.object(actions.profiles, 'prompt', return_value=''), patch('builtins.print'):
                actions.perform(client(), '/synthetic', {'alias': 'prod'}, action, selected())
            self.assertEqual(run.call_args.kwargs['profile_alias'], 'prod')

    def test_legacy_hashed_new_draft_can_be_resumed(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = actions.yaml_directory(folder, 'prod', 'deployments.apps')
            (directory / 'draft').mkdir()
            old = directory / 'draft/legacy-hash.yaml'
            old.write_text('old draft')
            c = fake_client()
            with patch.object(actions, 'choose', return_value=old), patch.object(actions.subprocess, 'run', return_value=Mock(returncode=0)), patch.object(actions.profiles, 'prompt', side_effect=['', '']), patch('builtins.print'):
                actions.saved_drafts(c, folder, selected(), 'prod')
            self.assertEqual((directory / 'web.yaml').read_text(), 'old draft')
            self.assertFalse(old.exists())
            c.text.assert_not_called()

    def test_profile_and_resource_draft_list_is_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = actions.yaml_directory(folder, 'prod', 'deployments.apps') / 'web.yaml'
            path.write_text('prod draft')
            with patch.object(actions, 'choose') as choose, patch('builtins.print'):
                actions.saved_drafts(fake_client(), folder, selected(), 'other')
                actions.saved_drafts(fake_client(), folder, selected('pods'), 'prod')
            choose.assert_not_called()
