import copy
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch
from kuba_dm_app.ui import dashboard as kuba_dm
from kuba_dm_app.resources import namespace_views as nv
from kuba_dm_app.topology import namespace_topology as graph
from kuba_dm_app.topology import workload_topology as wt
from kuba_dm_app.resources import resource_detail as rd
from kuba_dm_app.resources import resource_views as rv
class WorkloadTopologyTests(unittest.TestCase):
    def fixture(self):
        snapshot=nv.demo()
        deployment=copy.deepcopy(snapshot['objects']['deployments.apps'][0]);deployment['metadata'].update(name='other',uid='other-deploy')
        rs=copy.deepcopy(snapshot['objects']['replicasets.apps'][0]);rs['metadata'].update(name='other-rs',uid='other-rs',ownerReferences=[dict(apiVersion='apps/v1',kind='Deployment',name='other',uid='other-deploy')])
        pod=copy.deepcopy(snapshot['objects']['pods'][0]);pod['metadata'].update(name='other-pod',uid='other-pod',ownerReferences=[dict(apiVersion='apps/v1',kind='ReplicaSet',name='other-rs',uid='other-rs')])
        pod['spec']['volumes'].append({'configMap':{'name':'other-config'}})
        snapshot['objects']['deployments.apps'].append(deployment);snapshot['objects']['replicasets.apps'].append(rs);snapshot['objects']['pods'].append(pod)
        snapshot['objects']['configmaps'].append(dict(apiVersion='v1',kind='ConfigMap',metadata=dict(name='other-config',uid='other-config',namespace='demo')))
        snapshot['objects']['configmaps'][0]['metadata']['ownerReferences']=[dict(apiVersion='apps/v1',kind='Deployment',name='other',uid='other-deploy')]
        endpoints=[dict(conditions=dict(ready=True),targetRef=dict(kind='Pod',name=p['metadata']['name'],uid=p['metadata']['uid'],namespace='demo')) for p in snapshot['objects']['pods']]
        snapshot['objects']['endpointslices.discovery.k8s.io']=[dict(apiVersion='discovery.k8s.io/v1',kind='EndpointSlice',metadata=dict(name='shared-slice',uid='slice',namespace='demo',labels={'kubernetes.io/service-name':'web'}),endpoints=endpoints)]
        return snapshot

    def focus(self,snapshot,obj):
        return wt.focused(snapshot,wt.reference(obj),nv.selector_matches)

    def test_shared_service_config_and_slice_do_not_expand_to_other_workload(self):
        snapshot=self.fixture();before=copy.deepcopy(snapshot)
        selected=self.focus(snapshot,snapshot['objects']['deployments.apps'][0])
        names={o['metadata']['name'] for rows in selected['objects'].values() for o in rows}
        self.assertNotIn('other',names);self.assertNotIn('other-pod',names);self.assertNotIn('other-config',names)
        self.assertIn('web-config',names);self.assertIn('web',names)
        self.assertEqual(len(selected['objects']['endpointslices.discovery.k8s.io'][0]['endpoints']),1)
        model=graph.build(selected,nv.selector_matches)
        self.assertFalse(any(n['name'].startswith('other') for n in model['nodes'].values()))
        self.assertEqual(snapshot,before)

    def test_pod_focus_keeps_owner_chain_but_not_sibling_pods(self):
        snapshot=self.fixture();pod=snapshot['objects']['pods'][0]
        sibling=copy.deepcopy(pod);sibling['metadata'].update(name='sibling',uid='sibling');snapshot['objects']['pods'].append(sibling)
        selected=self.focus(snapshot,pod)
        self.assertEqual([p['metadata']['name'] for p in selected['objects']['pods']],['web-abc-1'])
        self.assertTrue(selected['objects']['deployments.apps']);self.assertTrue(selected['objects']['replicasets.apps'])

    def test_catalog_roots_notebook_and_standalone_pod(self):
        snapshot=self.fixture()
        notebook=dict(apiVersion='kubeflow.org/v1',kind='Notebook',metadata=dict(name='notebook',namespace='demo',uid='notebook'),spec={})
        child=copy.deepcopy(snapshot['objects']['pods'][0]);child['metadata'].update(name='notebook-pod',uid='notebook-pod',ownerReferences=[dict(apiVersion='kubeflow.org/v1',kind='Notebook',name='notebook',uid='notebook')])
        orphan=copy.deepcopy(child);orphan['metadata'].update(name='orphan',uid='orphan',ownerReferences=[])
        snapshot['objects']['notebooks.kubeflow.org']=[notebook];snapshot['objects']['pods'] += [child,orphan]
        roots=wt.Inventory(snapshot).workloads()
        self.assertEqual({(o['kind'],o['metadata']['name']) for o in roots},{('Deployment','web'),('Deployment','other'),('Notebook','notebook'),('Pod','orphan')})

    def test_replaced_focus_uid_missing_scope_and_wrong_endpoint_uid(self):
        snapshot=self.fixture();target=wt.reference(snapshot['objects']['deployments.apps'][0]);target['uid']='old-uid'
        with self.assertRaises(ValueError):wt.focused(snapshot,target,nv.selector_matches)
        target=wt.reference(snapshot['objects']['deployments.apps'][0]);target['namespace']='different'
        with self.assertRaises(ValueError):wt.focused(snapshot,target,nv.selector_matches)
        snapshot['objects']['endpointslices.discovery.k8s.io'][0]['endpoints'][0]['targetRef']['uid']='old-pod'
        selected=self.focus(snapshot,snapshot['objects']['deployments.apps'][0])
        self.assertNotIn('endpointslices.discovery.k8s.io',selected['objects'])
        self.assertTrue(any('selector candidate' in relation for _,_,relation in graph.build(selected,nv.selector_matches)['edges']))

    def test_workload_loader_validates_uid_and_needs_no_namespace_get(self):
        snapshot=self.fixture();obj=snapshot['objects']['deployments.apps'][0]
        item=dict(resource='deployments.apps',name='web',namespace='demo',namespaced=True,uid=obj['metadata']['uid'])
        c=Mock();c.call.side_effect=[(copy.deepcopy(obj),None),(copy.deepcopy(obj),None)]
        with patch('kuba_dm_app.resources.namespace_views.collect_objects',return_value=(copy.deepcopy(snapshot['objects']),{'secrets':'Forbidden fixture'})):
            result,error=nv.load_workload(c,item)
        self.assertIsNone(error);self.assertEqual(result['focus']['uid'],item['uid']);self.assertIn('secrets',result['errors'])
        self.assertTrue(all(call.args[:3]==('get','deployments.apps','web') for call in c.call.call_args_list))
        replacement=copy.deepcopy(obj);replacement['metadata']['uid']='replacement'
        c.call.side_effect=[(obj,None),(replacement,None)]
        with patch('kuba_dm_app.resources.namespace_views.collect_objects',return_value=(copy.deepcopy(snapshot['objects']),{})):
            result,error=nv.load_workload(c,item)
        self.assertIsNone(result);self.assertIn('changed identity',error)
        c.call.side_effect=[(replacement,None)]
        with patch('kuba_dm_app.resources.namespace_views.collect_objects') as collect:
            self.assertIsNone(nv.load_workload(c,item)[0]);collect.assert_not_called()

    def test_both_entry_points_use_same_scope_and_refresh_does_not_retarget(self):
        snapshot=self.fixture();obj=snapshot['objects']['deployments.apps'][0]
        ns=rd.Detail();ns.open(dict(resource='namespaces',name='demo',namespaced=False,uid='namespace-demo'),0);ns.mode='topology';ns.buffers['topology']=(snapshot,None)
        self.assertTrue(ns.topology_list());ns.topology_rows()
        ns.topology_enter();chosen=ns.topology_target
        # Select by UID, independent of the catalog's alphabetic order.
        ns.topology_target=wt.reference(obj)
        ns_text=rd.content(ns.state(),150,kuba_dm.clean,kuba_dm.ascii_table)
        direct=rd.Detail();direct.open(rd.target(dict(name='deployments.apps',namespaced=True),obj),0);direct.mode='topology'
        payload=copy.deepcopy(snapshot);payload['focus']=wt.reference(obj);direct.buffers['topology']=(payload,None)
        self.assertEqual(ns_text,rd.content(direct.state(),150,kuba_dm.clean,kuba_dm.ascii_table))
        ns.buffers['topology']=(copy.deepcopy(snapshot),None)
        ns.buffers['topology'][0]['objects']['deployments.apps'][0]['metadata']['uid']='new-uid'
        self.assertIn('WORKLOAD UNAVAILABLE',' '.join(rd.content(ns.state(),100,kuba_dm.clean,kuba_dm.ascii_table)))
        self.assertTrue(ns.topology_back());self.assertTrue(ns.topology_list())
        ns.close();self.assertIsNone(ns.topology_snapshot)

    def test_focus_is_applied_before_graph_limits_and_is_marked(self):
        snapshot=nv.demo()
        root=snapshot['objects']['deployments.apps'][0]
        snapshot['objects']['configmaps'] += [dict(apiVersion='v1',kind='ConfigMap',metadata=dict(name='unused-'+str(i),uid='unused-'+str(i),namespace='demo')) for i in range(200)]
        selected=self.focus(snapshot,root)
        model=graph.build(selected,nv.selector_matches)
        self.assertTrue(any(n.get('focused') and n['name']=='web' for n in model['nodes'].values()))
        self.assertFalse(any(n['name'].startswith('unused-') for n in model['nodes'].values()))
        self.assertFalse(model['notes'])

    def test_width_partial_data_and_empty_catalog(self):
        snapshot=self.fixture();snapshot['errors']['pods']='Forbidden fixture'
        for width in (36,40,60,84,120,200):
            for target in (None,wt.reference(snapshot['objects']['deployments.apps'][0])):
                lines=wt.render(snapshot,target,width,kuba_dm.ascii_table,kuba_dm.clean,nv.selector_matches)
                self.assertTrue(all(len(line)<=width and line.isascii() for line in lines))
                self.assertIn('Forbidden fixture',' '.join(lines))
        snapshot['objects']={}
        self.assertIn('No visible workloads',' '.join(wt.render(snapshot,None,100,kuba_dm.ascii_table,kuba_dm.clean,nv.selector_matches)))

    def run_keys(self, keys):
        snapshots=[];keys=iter(keys)
        class Pool:
            def __init__(self,**kwargs):pass
            def submit(self,fn,*args):
                f=Future();f.set_result(fn(*args));return f
            def shutdown(self,**kwargs):pass
        class Terminal:
            def timeout(self,value):pass
            def getmaxyx(self):return 45,180
            def erase(self):pass
            def addnstr(self,*args):pass
            def refresh(self):pass
            def getch(self):return next(keys)
        layout=kuba_dm.layout
        def record(data,*args,**kwargs):
            snapshots.append((args[0],kwargs['focus'],copy.deepcopy(data.get('resource_detail'))))
            return layout(data,*args,**kwargs)
        with patch('kuba_dm_app.ui.dashboard.concurrent.futures.ThreadPoolExecutor',Pool),patch('kuba_dm_app.ui.dashboard.layout',record),patch('kuba_dm_app.ui.dashboard.curses.curs_set'),patch('kuba_dm_app.ui.dashboard.curses.set_escdelay',create=True),patch('kuba_dm_app.ui.dashboard.curses.mousemask'),patch('kuba_dm_app.ui.dashboard.curses.mouseinterval'),patch('kuba_dm_app.ui.dashboard.sys.stdout'):
            self.assertEqual(kuba_dm.screen(Terminal(),kuba_dm.demo,30,resource_loader=rv.demo_resource,detail_loader=rd.demo),'quit')
        return snapshots

    def test_keyboard_namespace_list_graph_escape_restores_workload_list(self):
        right=kuba_dm.curses.KEY_RIGHT
        frames=self.run_keys([ord('Z'),10,-1,10,-1,right,-1,right,-1,right,-1,10,-1,27,-1,27,-1,27,-1,ord('q')])
        topology=[state for view,focus,state in frames if state and state['mode']=='topology']
        self.assertTrue(any(s['topology_state']['target'] for s in topology))
        self.assertIsNone(topology[-1]['topology_state']['target'])
        self.assertEqual(frames[-1][:2],('Z','nav'))

    def test_keyboard_workload_detail_topology_escape_returns_resource_list(self):
        right=kuba_dm.curses.KEY_RIGHT
        frames=self.run_keys([ord('3'),10,-1,10,-1,right,-1,right,-1,right,-1,27,-1,ord('q')])
        self.assertTrue(any(s and s['mode']=='topology' and s['target']['resource']=='pods' for _,_,s in frames))
        self.assertEqual(frames[-1][:2],('3','content'));self.assertIsNone(frames[-1][2])

if __name__=='__main__':unittest.main()
