import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kasciidash_app.resources import resource_actions as actions
from tests.test_resource_actions import obj, selected, client


class EditPreconditionTests(unittest.TestCase):
    def run_edit(self, edit, preview=None, confirmation='apply', submit_error=None):
        c = client(); original = obj(); payloads = []
        def call(*args, **kwargs):
            if args[0] == 'get': return original, None
            if '--dry-run=client' in args:
                return json.loads(kwargs['input_text']), None
            payloads.append(kwargs['input_text'])
            return (preview or json.loads(kwargs['input_text'])), None
        def text(*args, **kwargs):
            if '--dry-run=client' in args: return json.dumps(actions.editable_object(original)), None
            payloads.append(kwargs['input_text']); return ('', submit_error)
        def editor(argv):
            p = Path(argv[-1]); data = json.loads(p.read_text()); edit(data); p.write_text(json.dumps(data))
            return Mock(returncode=0)
        c.call.side_effect = call; c.text.side_effect = text
        with tempfile.TemporaryDirectory() as root, patch.object(actions.subprocess, 'run', side_effect=editor), patch.object(actions.profiles, 'prompt', side_effect=['', confirmation, '']), patch('builtins.print'):
            try: actions.edit_yaml(c, root, selected())
            except ValueError: pass
        return c, payloads

    def test_deleted_tokens_restored_and_final_replace_is_conditional(self):
        def edit(data):
            data['metadata'].pop('uid', None); data['metadata'].pop('resourceVersion', None)
        c, payloads = self.run_edit(edit)
        submits = [v for v in c.text.call_args_list if '--dry-run=client' not in v.args]
        self.assertEqual(len(submits), 1)
        self.assertEqual(submits[0].args[0], 'replace')
        data = json.loads(submits[0].kwargs['input_text'])
        self.assertEqual(data['metadata']['uid'], 'original')
        self.assertEqual(data['metadata']['resourceVersion'], '5')
        self.assertEqual(len(set(payloads)), 1)
        c.run.assert_not_called()
        self.assertNotIn('--force', submits[0].args)

    def test_changed_tokens_or_replaced_preview_are_never_submitted(self):
        for key in ('uid', 'resourceVersion'):
            c, _ = self.run_edit(lambda data: data['metadata'].__setitem__(key, 'changed'))
            self.assertFalse([v for v in c.text.call_args_list if '--dry-run=client' not in v.args])
        replacement = copy.deepcopy(obj()); replacement['metadata']['uid'] = 'replaced'
        c, _ = self.run_edit(lambda data: None, replacement)
        self.assertFalse([v for v in c.text.call_args_list if '--dry-run=client' not in v.args])

    def test_cancel_and_server_conflict_do_not_retry(self):
        c, _ = self.run_edit(lambda data: None, confirmation='')
        self.assertFalse([v for v in c.text.call_args_list if '--dry-run=client' not in v.args])
        c, _ = self.run_edit(lambda data: None, submit_error='Conflict')
        self.assertEqual(len([v for v in c.text.call_args_list if v.args[0] == 'replace']), 1)

    def test_concurrent_version_and_replace_after_confirmation(self):
        changed=copy.deepcopy(obj());changed['metadata']['resourceVersion']='6'
        c,_=self.run_edit(lambda data: None,preview=changed)
        self.assertFalse([v for v in c.text.call_args_list if '--dry-run=client' not in v.args])
        c,payloads=self.run_edit(lambda data: data['spec'].__setitem__('replicas',4),submit_error='Conflict: replaced after confirmation')
        sent=[v for v in c.text.call_args_list if v.args[0]=='replace']
        self.assertEqual(len(sent),1)
        value=json.loads(sent[0].kwargs['input_text'])
        self.assertEqual(value['metadata']['uid'],'original')
        self.assertEqual(value['metadata']['resourceVersion'],'5')
        self.assertEqual(value['spec']['replicas'],4)

    def test_server_rejects_changes_made_while_user_confirms(self):
        import contextlib
        import io
        for replacement in ('same', 'uid', 'version'):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as root:
                c=client(); initial=obj(); server=copy.deepcopy(initial); rejected=[]
                def check(payload):
                    meta=json.loads(payload)['metadata']
                    return all(meta.get(k)==server['metadata'][k] for k in ('uid','resourceVersion'))
                def call(*args,**kw):
                    if args[0]=='get': return copy.deepcopy(initial),None
                    value=json.loads(kw['input_text'])
                    return (value,None) if check(kw['input_text']) else (None,'Conflict')
                def text(*args,**kw):
                    if '--dry-run=client' in args: return json.dumps(actions.editable_object(initial)),None
                    rejected.append(not check(kw['input_text']))
                    return ('accepted',None) if not rejected[-1] else ('','Conflict')
                def confirm(question):
                    if question.startswith('Type '):
                        if replacement=='uid': server['metadata']['uid']='new-object'
                        if replacement=='version': server['metadata']['resourceVersion']='6'
                        return 'apply'
                    return ''
                c.call.side_effect=call;c.text.side_effect=text;output=io.StringIO()
                with patch.object(actions.subprocess,'run',return_value=Mock(returncode=0)),patch.object(actions.profiles,'prompt',side_effect=confirm),contextlib.redirect_stdout(output):
                    if replacement=='same': actions.edit_yaml(c,root,selected())
                    else:
                        with self.assertRaisesRegex(ValueError,'Refresh'):
                            actions.edit_yaml(c,root,selected())
                self.assertEqual(rejected,[replacement!='same'])
                self.assertEqual('Deployment request accepted.' in output.getvalue(),replacement=='same')
