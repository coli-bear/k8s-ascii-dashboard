import copy
import unittest
from unittest.mock import patch
from kuba_dm_app.ui import dashboard as kuba_dm
from kuba_dm_app.resources import namespace_views as nv
from kuba_dm_app.topology import namespace_topology as graph
from kuba_dm_app.resources import resource_detail as rd
class NamespaceTopologyTests(unittest.TestCase):
    def build(self, snapshot):
        return graph.build(snapshot, nv.selector_matches)

    def fixture(self):
        snapshot=nv.demo()
        pod=snapshot['objects']['pods'][0]
        snapshot['objects']['endpointslices.discovery.k8s.io']=[dict(apiVersion='discovery.k8s.io/v1', kind='EndpointSlice',metadata=dict(name='slice',namespace='demo',uid='slice-uid',labels={'kubernetes.io/service-name':'web'}),endpoints=[dict(conditions=dict(ready=True), targetRef=dict(kind='Pod',name='web-abc-1',namespace='demo',uid=pod['metadata']['uid']))])]
        return snapshot

    def test_tab_order_and_cache_shared_between_tree_and_graph(self):
        self.assertEqual(nv.MODES,('overview','resources','treeview','topology'))
        browser=rd.Detail();browser.open(dict(resource='namespaces',name='demo',uid='uid',namespace=None,namespaced=False,read_only=True),0)
        modes=[]
        for _ in range(6):
            modes.append(browser.mode);browser.switch()
        self.assertEqual(modes,['overview','resources','treeview','topology','yaml','describe'])
        header=' '.join(rd.header(browser.state(),200,kuba_dm.clean))
        self.assertIn('TreeView | Topology',header)
        self.assertIn('RELATION TREE VIEW',' '.join(nv.render(nv.demo(),'treeview',100,kuba_dm.ascii_table,kuba_dm.clean)))
        self.assertIn('WORKLOADS',' '.join(nv.render(nv.demo(),'topology',100,kuba_dm.ascii_table,kuba_dm.clean)))

    def test_network_shared_objects_are_unique_and_relations_are_typed(self):
        snapshot=self.fixture()
        second=copy.deepcopy(snapshot['objects']['services'][0]);second['metadata'].update(name='web-other',uid='other-service')
        snapshot['objects']['services'].append(second)
        result=self.build(snapshot)
        self.assertEqual(sum(n['kind']=='Pod' and not n['missing'] for n in result['nodes'].values()),1)
        relations=[e[2] for e in result['edges']]
        self.assertIn('EndpointSlice association',relations)
        self.assertIn('target ready=True',relations)
        self.assertEqual(relations.count('selector candidate (not a verified endpoint)'),1)
        self.assertIn('references',relations)
        self.assertIn('owns (UID reference)',relations)

    def test_replaced_uid_outside_inventory_and_api_group_collision(self):
        snapshot=self.fixture()
        snapshot['objects']['endpointslices.discovery.k8s.io'][0]['endpoints'][0]['targetRef']['uid']='old-pod-uid'
        pod=snapshot['objects']['pods'][0]
        pod['metadata']['ownerReferences']=[dict(apiVersion='kubeflow.org/v1',kind='Profile',name='team',uid='external-profile')]
        snapshot['objects']['services.example.io']=[dict(apiVersion='example.io/v1',kind='Service',metadata=dict(name='crd-service',namespace='demo',uid='custom-service'),spec=dict(selector={'app':'web'}))]
        result=self.build(snapshot)
        self.assertTrue(any(n['missing']=='UID mismatch' and n['kind']=='Pod' for n in result['nodes'].values()))
        self.assertEqual(sum(n['kind']=='Profile' and n['missing']=='outside current inventory' for n in result['nodes'].values()),1)
        custom=next(key for key,n in result['nodes'].items() if n['name']=='crd-service')
        self.assertFalse(any(source==custom for source,_,_ in result['edges']))
        self.assertTrue(any(r.startswith('selector candidate') for _,_,r in result['edges']))

    def test_no_private_values_in_model_or_graph(self):
        snapshot=self.fixture()
        secret=snapshot['objects']['secrets'][0]
        secret.update(data={'key':'FAKE_PRIVATE_VALUE'},stringData={'key':'FAKE_PRIVATE_VALUE'},binaryData={'key':'FAKE_PRIVATE_VALUE'})
        secret['metadata']['annotations']={'kubectl.kubernetes.io/last-applied-configuration':'FAKE_PRIVATE_VALUE'}
        self.assertNotIn('FAKE_PRIVATE_VALUE',repr(self.build(snapshot)))
        self.assertNotIn('FAKE_PRIVATE_VALUE','\n'.join(graph.render(snapshot,100,nv.selector_matches)))

    def test_width_arrows_and_cycles_and_explicit_limits(self):
        snapshot=self.fixture()
        deployment=snapshot['objects']['deployments.apps'][0]
        deployment['metadata']['ownerReferences']=[dict(apiVersion='apps/v1',kind='ReplicaSet',name='web-abc',uid='ReplicaSet-web-abc')]
        for width in (36,40,60,84,120,220):
            lines=graph.render(snapshot,width,nv.selector_matches)
            self.assertTrue(all(len(line)<=width and line.isascii() for line in lines))
            self.assertIn('->','\n'.join(lines))
            self.assertIn('E01:', '\n'.join(lines))
            self.assertTrue(any('<' in line and '+' in line for line in lines))
        with patch.object(graph,'MAX_NODES',2),patch.object(graph,'MAX_EDGES',1):
            result=self.build(snapshot)
            self.assertLessEqual(len(result['nodes']),2);self.assertLessEqual(len(result['edges']),1)
            self.assertTrue(result['notes'])
        with patch.object(graph,'MAX_LINES',20):
            self.assertIn('Graph display limited',' '.join(graph.render(snapshot,80,nv.selector_matches)))

    def test_horizontal_cards_align_network_pod_owner_and_shared_references(self):
        snapshot=nv.demo()
        snapshot['objects'].pop('ingresses.networking.k8s.io')
        snapshot['objects'].pop('deployments.apps')
        snapshot['objects']['replicasets.apps'][0]['metadata'].pop('ownerReferences')
        snapshot['objects']['pods'][0]['spec']={'volumes':[{'configMap':{'name':'config-'+str(i)}} for i in range(5)]}
        snapshot['objects']['configmaps']=[dict(apiVersion='v1',kind='ConfigMap',metadata=dict(name='config-'+str(i),namespace='demo',uid='config-'+str(i))) for i in range(5)]
        result=self.build(snapshot)
        connected=set(key for edge in result['edges'] for key in edge[:2])
        layout=graph.card_layout(result['nodes'],sorted(connected,key=repr),220)
        by_kind={node['kind']:layout['placements'][key] for key,node in result['nodes'].items() if key in layout['placements']}
        self.assertLess(by_kind['Service']['x'],by_kind['Pod']['x'])
        self.assertLess(by_kind['Pod']['x'],by_kind['ReplicaSet']['x'])
        self.assertLess(by_kind['ReplicaSet']['x'],by_kind['ConfigMap']['x'])
        lines=graph.render(snapshot,220,nv.selector_matches)
        centers=[by_kind[kind]['y']+by_kind[kind]['height']//2 for kind in ('Service','Pod','ReplicaSet')]
        self.assertLessEqual(max(centers)-min(centers),1)
        self.assertTrue(all('['+kind+']' in '\n'.join(lines) for kind in ('Service','Pod','ReplicaSet','ConfigMap')))
        self.assertIn('Type: ClusterIP','\n'.join(lines))
        self.assertIn('Phase: Running','\n'.join(lines))
        self.assertIn('Ready: Unknown','\n'.join(lines))
        self.assertEqual(sum('N001 [ConfigMap]' in line for line in lines),1)

    def test_connection_routes_do_not_cross_card_interiors(self):
        nodes={i:dict(kind=kind,name=str(i)) for i,kind in enumerate(
            ['Service','Ingress','EndpointSlice','Pod','Pod','ReplicaSet','Deployment','ConfigMap','Secret','Secret','PersistentVolumeClaim'])}
        for width in (96,100,127,128,160,240):
            layout=graph.card_layout(nodes,list(nodes),width)
            for source in nodes:
                for target in nodes:
                    path,_=graph.connection_path(layout,source,target,source+target)
                    for (ax,ay),(bx,by) in zip(path,path[1:]):
                        self.assertTrue(ax==bx or ay==by)
                        for y in range(min(ay,by),max(ay,by)+1):
                            for x in range(min(ax,bx),max(ax,bx)+1):
                                self.assertTrue(0<x<width-1 and 0<y<layout['height']-1)
                                if (x,y) in (path[0],path[-1]):
                                    continue
                                self.assertFalse(any(box['x']<=x<box['x']+box['width'] and box['y']<=y<box['y']+box['height'] for box in layout['placements'].values()),(width,source,target,x,y))

    def test_long_card_names_have_full_label_and_narrow_layout_remains_available(self):
        snapshot=nv.demo()
        name='pod-'+'long-name-'*12
        snapshot['objects']['pods'][0]['metadata']['name']=name
        for width in (96,128,200):
            lines=graph.render(snapshot,width,nv.selector_matches)
            self.assertTrue(all(len(line)<=width for line in lines))
            self.assertIn('FULL CARD LABELS',' '.join(lines))
            self.assertIn(name.replace(' ',''),''.join(''.join(lines).split()))
        self.assertIn('COMPONENT 1',' '.join(graph.render(snapshot,60,nv.selector_matches)))

    def test_empty_and_isolated_resources_and_external_name(self):
        snapshot=nv.demo();snapshot['objects']={}
        self.assertIn('No visible resources.',' '.join(graph.render(snapshot,80,nv.selector_matches)))
        snapshot=nv.demo();snapshot['objects']['services'][0]['spec']=dict(type='ExternalName',externalName='example.invalid')
        snapshot['objects']['configmaps'].append(dict(apiVersion='v1',kind='ConfigMap',metadata=dict(name='unused',namespace='demo',uid='unused')))
        result=self.build(snapshot)
        self.assertTrue(any(n['kind']=='DNS' and n['name']=='example.invalid' for n in result['nodes'].values()))
        self.assertIn('NO CONNECTIONS SHOWN',' '.join(graph.render(snapshot,80,nv.selector_matches)))

if __name__=='__main__':unittest.main()
