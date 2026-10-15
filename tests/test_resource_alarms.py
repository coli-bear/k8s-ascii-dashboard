import copy
import unittest
from kasciidash_app.ui import dashboard
from kasciidash_app.resources import resource_detail as detail
from kasciidash_app.resources import alarms


def fixture():
    data=dashboard.demo()
    def obj(kind,name,uid,namespace='prod',**spec):
        return {'apiVersion':'apps/v1' if kind in ('Deployment','ReplicaSet') else 'v1','kind':kind,'metadata':{'name':name,'namespace':namespace,'uid':uid,'labels':{'app':'web'}},'spec':spec}
    deployment=obj('Deployment','web','d')
    rs=obj('ReplicaSet','web-rs','r');rs['metadata']['ownerReferences']=[{'uid':'d'}]
    pod=obj('Pod','web-pod','p',nodeName='node1');pod['metadata']['ownerReferences']=[{'uid':'r'}]
    pod['status']={'phase':'Running'}
    service=obj('Service','web','s',selector={'app':'web'})
    warning={'type':'Warning','reason':'BackOff','message':'fake','count':500,'metadata':{'uid':'warning1','namespace':'prod'},'involvedObject':{'apiVersion':'v1','kind':'Pod','namespace':'prod','name':'web-pod','uid':'p'}}
    error=copy.deepcopy(warning);error['type']='Error';error['metadata']['uid']='error1';error['reason']='Failed'
    data.update(pods=[pod],deployments=[deployment],replicasets=[rs],statefulsets=[],events=[warning,warning,error],errors=['Generic collection failure'],extras={'services':[service]},resource_errors={})
    return data,pod,deployment,service


class ResourceAlarmTests(unittest.TestCase):
    def test_counts_include_owner_and_service_pods_without_duplicate_occurrences(self):
        data,pod,deployment,service=fixture();index=alarms.index(data)
        for obj in (pod,deployment,service):
            self.assertEqual(index.count(obj),2)
            self.assertEqual(len(index.rows(obj,'warnings')),1)
            self.assertEqual(len(index.rows(obj,'errors')),1)
        self.assertEqual(alarms.index(data),index)

    def test_namespace_replaced_uid_and_selectorless_service_are_isolated(self):
        data,pod,deployment,service=fixture()
        replacement=copy.deepcopy(pod);replacement['metadata']['uid']='new'
        self.assertEqual(alarms.index(data).count(replacement),0)
        no_selector=copy.deepcopy(service);no_selector['spec']={}
        self.assertEqual(alarms.index(data).count(no_selector),0)
        ns={'kind':'Namespace','metadata':{'name':'other','uid':'n'}}
        self.assertEqual(alarms.index(data).count(ns),0)
        data['events']=None
        self.assertEqual(alarms.index(data).label(pod),'N/A')

    def test_detail_tabs_and_list_alarm_columns(self):
        data,pod,deployment,service=fixture()
        browser=detail.Detail();browser.open(detail.target({'name':'pods','namespaced':True},pod),0)
        self.assertIn('warnings',browser.modes());self.assertIn('errors',browser.modes())
        self.assertIn('ALARMS','\n'.join(dashboard.workload_lines(data,140,'pods')))
        self.assertIn('ALARMS','\n'.join(dashboard.resource_lines(data,{'name':'services','namespaced':True},140)))

    def test_structured_collection_errors_are_scoped_and_duplicates_collapsed(self):
        data,pod,deployment,service=fixture()
        ref={'apiVersion':'v1','kind':'Pod','namespace':'prod','name':'web-pod','uid':'p'}
        error={'reference':ref,'reason':'Forbidden','message':'fake denied'}
        data['errors']=[error,error,'Unscoped failure']
        self.assertEqual(alarms.index(data).count(pod),3)
        self.assertEqual(len(alarms.index(data).rows(service,'errors')),2)
        self.assertEqual(len(alarms.index(data).rows(deployment,'errors')),2)

    def test_alarm_tabs_use_snapshot_without_detail_api_calls(self):
        from unittest.mock import Mock
        data,pod,_,_=fixture();browser=detail.Detail()
        browser.open(detail.target({'name':'pods','namespaced':True},pod),0)
        pool=Mock();loader=Mock()
        for mode in ('warnings','errors'):
            browser.mode=mode;browser.poll(pool,loader,data)
            self.assertFalse(browser.state()['loading'])
            content=detail.content(browser.state(),120,str,dashboard.ascii_table)
            self.assertIn(mode.upper(),'\n'.join(content))
        loader.assert_not_called();pool.submit.assert_not_called()

    def test_alarm_cache_updates_when_snapshot_event_list_changes(self):
        data,pod,_,_=fixture();first=alarms.index(data)
        data['events']=[]
        self.assertIsNot(alarms.index(data),first)
        self.assertEqual(alarms.index(data).count(pod),0)

    def test_unavailable_child_lists_do_not_report_zero_as_complete(self):
        data,_,deployment,service=fixture();data['pods']=None
        self.assertEqual(alarms.index(data).label(service),'N/A')
        self.assertEqual(alarms.index(data).label(deployment),'N/A')
