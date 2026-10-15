import copy
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch
from kdash_app.ui import dashboard as kdash
from kdash_app.resources import node_views as nv
from kdash_app.resources import resource_detail as rd
from kdash_app.resources import resource_views as rv
class NodeTests(unittest.TestCase):
    def test_status_partial_data_conditions_taints_and_scoped_calls(self):
        node=copy.deepcopy(kdash.demo()['nodes'][0])
        node['metadata']['labels']={'node-role.kubernetes.io/control-plane':'','zone':'west'}
        node['spec']={'unschedulable':True,'taints':[{'key':'node-role.kubernetes.io/control-plane','effect':'NoSchedule'}]}
        node['status']['conditions'].append({'type':'DiskPressure','status':'True','reason':'DiskFull','message':'diagnostic '*30,'lastTransitionTime':'2026-10-14T00:00:00Z'})
        node['status']['nodeInfo']={'osImage':'Example Linux','kubeletVersion':'v1.30.0','containerRuntimeVersion':'containerd://example'}
        client=Mock()
        def call(*args):
            if args[:2]==('get','nodes'): return node,None
            if 'pods' in args: return {'items':kdash.demo()['pods'][:1]},None
            if 'metrics.k8s.io' in args[-1]: return None,'metrics Forbidden'
            return None,'disk Forbidden'
        client.call.side_effect=call
        target=rd.target({'name':'nodes','namespaced':False,'read_only':True},node)
        text,error=rd.load(client,target,'status')
        self.assertIsNone(error)
        for phrase in ('Ready / Cordoned / DiskPressure','Last transition: 2026','NoSchedule','zone=west','Example Linux','total=8','CPU: N/A','PARTIAL DATA','metrics Forbidden','disk Forbidden','demo/web-abc'):
            self.assertIn(phrase,text)
        self.assertTrue(any('--field-selector' in c.args and 'spec.nodeName=worker-01' in c.args for c in client.call.call_args_list))
        self.assertTrue(all('--namespace' not in c.args for c in client.call.call_args_list))

    def test_missing_node_stops_additional_queries_and_pod_denial_is_not_zero(self):
        client=Mock();client.call.return_value=(None,'NotFound')
        self.assertEqual(nv.load(client,{'name':'missing'}),(None,'NotFound'))
        self.assertEqual(client.call.call_count,1)
        text=nv.status_text({'metadata':{'name':'n'},'spec':None,'status':None},None,None,None,['pods: Forbidden'])
        self.assertIn('State: Unknown',text)
        self.assertIn('N/A: Pod list unavailable',text)
        self.assertNotIn('Total: 0',text)

    def test_filters_marker_and_responsive_list(self):
        data=kdash.demo()
        data['nodes'][1]['spec']={'unschedulable':True}
        data['resource_state']={'index':1}
        self.assertEqual(len(nv.matches(data,{'status':'Cordoned'})),1)
        self.assertEqual(len(nv.matches(data,{'name':'worker-01'})),1)
        for width in (60,84,100,140,230):
            for height in (18,25,40):
                lines,*_=kdash.layout(data,'N',width,height=height,focus='content')
                self.assertTrue(all(len(line)==width for line in lines))
                self.assertIn('| > ', '\n'.join(lines))
        data['nodes']=None
        self.assertIn('unavailable',' '.join(nv.list_lines(data,100,{},kdash.ascii_table)))
        self.assertIn('N/A',' '.join(nv.header(data,100,{},kdash.clean)))

    def test_node_detail_tab_cycle_and_read_only_actions(self):
        item=dict(resource='nodes',name='node-1',namespaced=False,namespace=None,read_only=True)
        browser=rd.Detail();browser.open(item,3)
        self.assertEqual(browser.mode,'status')
        browser.switch();self.assertEqual(browser.mode,'yaml')
        browser.switch();self.assertEqual(browser.mode,'describe')
        browser.switch();self.assertEqual(browser.mode,'status')
        browser.switch(-1);self.assertEqual(browser.mode,'describe')
        text=' '.join(rd.header(browser.state(),100,kdash.clean))
        self.assertIn('Status',text);self.assertIn('READ ONLY',text)
        self.assertNotIn('edit YAML',text);self.assertNotIn('Logs',text)

    def test_keyboard_nodes_navigation_filter_details_refresh_and_return(self):
        class Pool:
            def __init__(self,**kw):pass
            def submit(self,fn,*args):
                f=Future();f.set_result(fn(*args));return f
            def shutdown(self,**kw):pass
        keys=iter([kdash.curses.KEY_DOWN,kdash.curses.KEY_DOWN,10,kdash.curses.KEY_DOWN,10,-1,ord('r'),-1,kdash.curses.KEY_RIGHT,-1,kdash.curses.KEY_RIGHT,-1,ord('a'),ord('e'),ord('n'),ord('!'),27,-1,ord('/'),*map(ord,'name=worker-01'),10,10,-1,27,-1,27,-1,ord('q')])
        class Terminal:
            def timeout(self,v):pass
            def getmaxyx(self):return 42,160
            def erase(self):pass
            def addnstr(self,*a):pass
            def refresh(self):pass
            def getch(self):return next(keys)
        calls=[];states=[];action=Mock();resource_loader=Mock()
        def load(item,mode):
            calls.append((copy.deepcopy(item),mode));return rd.demo(item,mode)
        original=kdash.layout
        def layout(data,*args,**kwargs):
            states.append((args[0],kwargs['focus'],kwargs['selected'],kwargs['nav_group'],copy.deepcopy(data.get('resource_detail'))))
            return original(data,*args,**kwargs)
        with patch('kdash_app.ui.dashboard.concurrent.futures.ThreadPoolExecutor',Pool),patch('kdash_app.ui.dashboard.layout',layout),patch('kdash_app.ui.dashboard.curses.curs_set'),patch('kdash_app.ui.dashboard.curses.set_escdelay',create=True),patch('kdash_app.ui.dashboard.curses.mousemask'),patch('kdash_app.ui.dashboard.curses.mouseinterval'),patch('kdash_app.ui.dashboard.sys.stdout'):
            self.assertEqual(kdash.screen(Terminal(),kdash.demo,30,resource_loader=resource_loader,detail_loader=load,action_handler=action),'quit')
        self.assertEqual([mode for _,mode in calls],['status','status','yaml','describe','status'])
        self.assertEqual([item['name'] for item,_ in calls],['worker-02']*4+['worker-01'])
        self.assertTrue(all(item['namespaced'] is False for item,_ in calls))
        self.assertTrue(any(s[:4]==('N','content','N','Clusters') for s in states))
        self.assertEqual(states[-1][:4],('N','nav','N','Clusters'))
        action.assert_not_called();resource_loader.assert_not_called()


if __name__=='__main__':unittest.main()
