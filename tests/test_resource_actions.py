import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
from kasciidash_app.resources import resource_actions as a
def selected(resource='deployments.apps'):
    return dict(resource=resource,name='web',namespace='prod',namespaced=True,uid='original')


def obj():
    return dict(apiVersion='apps/v1',kind='Deployment',metadata={'name':'web','namespace':'prod','uid':'original','resourceVersion':'5','managedFields':[{}]},spec={'replicas':3,'selector':{'matchLabels':{'app':'web'}}},status={'readyReplicas':3})


def client():
    c=Mock();c.base=['kubectl','--kubeconfig','/profile-a.conf','--context','ctx-a']
    c.call.return_value=(obj(),None);c.text.return_value=('ok',None);c.run.return_value=Mock(returncode=1,stdout='- old\n+ new',stderr='')
    return c


class ActionTests(unittest.TestCase):
    def test_supported_actions_and_cancel_never_mutates(self):
        self.assertEqual(a.available(selected('services')),['delete'])
        self.assertNotIn('restart',a.available(selected('replicasets.apps')))
        c=client()
        with patch('kasciidash_app.resources.resource_actions.profiles.prompt',side_effect=['2','']),patch('builtins.print'):
            a.change(c,selected())
        c.text.assert_not_called()
        self.assertEqual(c.call.call_args.args[:3],('get','deployments.apps','web'))

    def test_scale_restart_delete_scope_and_confirmation(self):
        for answers, expected in [(['1','4','start'],'scale'),(['2','stop'],'scale'),(['3','restart'],'patch'),(['4','delete'],'delete')]:
            c=client()
            with patch('kasciidash_app.resources.resource_actions.profiles.prompt',side_effect=answers),patch('builtins.print'):
                a.change(c,selected())
            args=c.text.call_args.args
            self.assertEqual(args[0],expected)
            if expected != 'delete':
                self.assertIn('--namespace',args);self.assertIn('prod',args)
            if expected=='scale':
                self.assertIn('--current-replicas=3',args)
                self.assertIn('--resource-version=5',args)
            if expected=='delete':
                self.assertIn('--raw=/apis/apps/v1/namespaces/prod/deployments/web',args)
                options=json.loads(c.text.call_args.kwargs['input_text'])
                self.assertEqual(options['preconditions'],{'uid':'original','resourceVersion':'5'})
                self.assertEqual(options['propagationPolicy'],'Background')
                self.assertNotIn('gracePeriodSeconds',options)
                self.assertNotIn('--force',args)
            if expected=='patch':
                operations=json.loads(args[args.index('-p')+1])
                self.assertEqual(operations[:2],[{'op':'test','path':'/metadata/uid','value':'original'},{'op':'test','path':'/metadata/resourceVersion','value':'5'}])
        c=client();other=obj();other['metadata']['uid']='replacement';c.call.return_value=(other,None)
        with self.assertRaises(ValueError),patch('kasciidash_app.resources.resource_actions.profiles.prompt',return_value='4'),patch('builtins.print'):
            a.change(c,selected())
        c.text.assert_not_called()

    def test_edit_strips_server_fields_but_keeps_conflict_version(self):
        original=obj();original['metadata']['annotations']={'keep':'yes','kubectl.kubernetes.io/last-applied-configuration':'old'}
        edited=a.editable_object(original)
        self.assertNotIn('status',edited)
        self.assertNotIn('uid',edited['metadata'])
        self.assertNotIn('managedFields',edited['metadata'])
        self.assertEqual(edited['metadata']['resourceVersion'],'5')
        self.assertEqual(edited['metadata']['annotations'],{'keep':'yes'})
        self.assertIn('uid',original['metadata'])

    def test_new_template_server_validation_and_explicit_create(self):
        for deploy in ('','create'):
            c=client()
            c.call.side_effect=[({'preferredVersion':{'groupVersion':'apps/v1'}},None),
                                ({'resources':[{'name':'deployments','kind':'Deployment','namespaced':True,'verbs':['create']}]},None), (obj(),None)]
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'EDITOR':'editor --wait','VISUAL':''}),patch('kasciidash_app.resources.resource_actions.subprocess.run',return_value=Mock(returncode=0)) as edit,patch('kasciidash_app.resources.resource_actions.profiles.prompt',side_effect=['web','prod','',deploy]),patch('builtins.print'):
                a.edit_yaml(c,Path(directory),selected(),new=True)
                if not deploy:
                    draft=next((Path(directory)/'.kasciidash').rglob('*.yaml'))
                    self.assertEqual(stat.S_IMODE(draft.stat().st_mode),0o600)
                    self.assertIn('kind: Deployment',draft.read_text())
                else:
                    self.assertFalse(list((Path(directory)/'.kasciidash').rglob('*.yaml')))
                self.assertEqual(edit.call_args.args[0][:2],['editor','--wait'])
                self.assertEqual(c.call.call_args.args[:2],('create','--dry-run=server'))
                if deploy:
                    self.assertEqual(c.text.call_args.args[0],'create')
                    self.assertIn('prod',c.text.call_args.args)
                else: c.text.assert_not_called()

    def test_edit_validation_identity_and_diff_failure_block_apply(self):
        for failure in ('validation','identity','diff','cancel','success'):
            c=client();preview=obj()
            if failure=='identity': preview['metadata']['name']='another-object'
            c.call.side_effect=[(obj(),None),(preview,'Forbidden' if failure=='validation' else None)]
            c.text.return_value=('apiVersion: apps/v1\nkind: Deployment\n',None)
            if failure=='diff': c.run.return_value=Mock(returncode=2,stdout='',stderr='diff failed')
            prompts=['',''] if failure in ('validation','identity','cancel') else ['','apply']
            with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'EDITOR':'editor','VISUAL':''}),patch('kasciidash_app.resources.resource_actions.subprocess.run',return_value=Mock(returncode=0)),patch('kasciidash_app.resources.resource_actions.profiles.prompt',side_effect=prompts),patch('builtins.print'):
                if failure=='diff':
                    with self.assertRaises(ValueError): a.edit_yaml(c,Path(directory),selected())
                else: a.edit_yaml(c,Path(directory),selected())
                applies=[call for call in c.text.call_args_list if call.args[0]=='apply']
                self.assertEqual(len(applies),int(failure=='success'))
                self.assertFalse(list((Path(directory)/'.kasciidash').rglob('*.yaml')))

    def test_exec_argv_has_selected_profile_namespace_pod_container_and_no_local_shell(self):
        c=client();pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':'web','namespace':'prod','uid':'original'},'spec':{'containers':[{'name':'app'}]},'status':{'phase':'Running','containerStatuses':[{'name':'app','state':{'running':{}}}]}}
        c.call.return_value=(pod,None)
        for confirm in ('','exec'):
            with patch('kasciidash_app.resources.resource_actions.profiles.prompt',side_effect=['3','sh -c "echo hello; id"',confirm]),patch('kasciidash_app.resources.resource_actions.terminal_exec.run',return_value=0) as run,patch('builtins.print'):
                a.exec_command(c,selected('pods'))
                if confirm:
                    args=run.call_args.args[0]
                    self.assertEqual(args[:len(c.base)],c.base)
                    self.assertEqual(args[-4:],['--','sh','-c','echo hello; id'])
                    self.assertIn('prod',args);self.assertIn('web',args);self.assertIn('app',args)
                    self.assertNotIn('shell',run.call_args.kwargs)
                else: run.assert_not_called()

    def test_all_basic_templates_have_kind_name_and_namespace(self):
        for kind in a.TEMPLATES:
            text=a.template(kind,'example','prod')
            self.assertIn('kind: '+kind,text)
            self.assertIn('name: "example"',text)
            self.assertEqual('namespace: "prod"' in text,kind!='Namespace')


if __name__=='__main__':unittest.main()
