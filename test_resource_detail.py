import copy
import json
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch

import kdash
import resource_detail as rd
import resource_views as rv


class DetailTests(unittest.TestCase):
    def test_yaml_describe_exact_identity_and_stdin_manifest(self):
        client = Mock()
        client.text.return_value = ('text', None)
        item = dict(resource='services', name='web', namespace='prod', namespaced=True)
        rd.load(client, item, 'yaml')
        self.assertEqual(client.text.call_args.args, ('get', 'services', 'web', '--namespace', 'prod', '-o', 'yaml'))
        client.call.return_value = ({'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'web', 'namespace': 'prod'}, 'data': {'private': 'never copied to describe stdin'}}, None)
        rd.load(client, item, 'describe')
        self.assertEqual(client.text.call_args.args, ('describe', '-f', '-', '--namespace', 'prod'))
        manifest = json.loads(client.text.call_args.kwargs['input_text'])
        self.assertEqual(set(manifest), {'apiVersion','kind','metadata'})
        client.call.return_value = (None, 'NotFound')
        client.text.reset_mock()
        self.assertEqual(rd.load(client,item,'describe'), (None,'NotFound'))
        client.text.assert_not_called()
        item = dict(resource='widgets.example.org', name='one', namespace=None, namespaced=False)
        rd.load(client,item,'yaml')
        self.assertNotIn('--namespace', client.text.call_args.args)

    def test_logs_containers_limits_and_previous_and_selector(self):
        client = Mock();client.text.return_value = ('partial','container waiting')
        item = dict(resource='pods', name='web', namespace='prod', namespaced=True)
        self.assertEqual(rd.load(client,item,'logs_previous'), ('partial','container waiting'))
        args = client.text.call_args.args
        for flag in ('--all-containers=true','--prefix=true','--tail=200','--limit-bytes=262144','--previous=true'):
            self.assertIn(flag,args)
        item['resource']='deployments.apps'
        client.call.return_value=({'spec':{'selector':{'matchLabels':{'app':'web'},'matchExpressions':[{'key':'tier','operator':'In','values':['front','api']}]}}},None)
        rd.load(client,item,'logs')
        self.assertIn('app=web,tier in (front,api)',client.text.call_args.args)
        client.call.return_value=({'spec':{'selector':{}}},None)
        client.text.reset_mock()
        self.assertIn('empty',rd.load(client,item,'logs')[1])
        client.text.assert_not_called()

    def test_inflight_result_is_discarded_after_close_and_new_object(self):
        queue=[]
        class Pool:
            def submit(self,*args):
                f=Future();queue.append(f);return f
        browser=rd.Detail();pool=Pool()
        first=dict(resource='pods',name='one',namespace='a',namespaced=True)
        browser.open(first,9);browser.poll(pool,lambda *a:None)
        queue[0].set_running_or_notify_cancel()
        browser.close();browser.open(dict(first,name='two'),0)
        queue[0].set_result(('OLD CONTENT',None));browser.poll(pool,lambda *a:None)
        self.assertTrue(browser.state()['loading'])
        self.assertNotIn('OLD CONTENT',str(browser.state()))
        queue[1].set_result(('NEW CONTENT',None));browser.poll(pool,lambda *a:None)
        self.assertEqual(browser.state()['output'],'NEW CONTENT')
        browser.switch();self.assertEqual(browser.mode,'describe')
        browser.switch();self.assertEqual(browser.mode,'logs')
        browser.previous();self.assertEqual(browser.mode,'logs_previous')
        browser.switch(-1);self.assertEqual(browser.mode,'describe')

    def test_filtered_selection_sort_and_refresh_identity(self):
        data=kdash.demo()
        first=copy.deepcopy(data['pods'][0]);first['metadata']={'name':'same','namespace':'a','uid':'1'}
        second=copy.deepcopy(first);second['metadata']={'name':'same','namespace':'b','uid':'2'}
        data['pods']=[second,first]
        resource,rows=kdash.resource_selection(data,'3','pods',None,{})
        self.assertEqual([r['metadata']['namespace'] for r in rows],['a','b'])
        browser=rd.Selection();key=('3','pods');browser.reconcile(key,rows);browser.move(key,rows,1)
        browser.reconcile(key,list(reversed(rows)))
        self.assertEqual(browser.indices[key],0)
        _,filtered=kdash.resource_selection(data,'3','pods',None,{'ns':'a'})
        browser.reconcile(key,filtered)
        self.assertEqual(rd.target(resource,filtered[0])['namespace'],'a')
        browser.reconcile(key,[]);self.assertEqual(browser.indices[key],0)

    def test_detail_layout_esc_and_tabs_do_not_switch_resource_category(self):
        calls,frames=[],[]
        class Pool:
            def __init__(self,**kwargs):pass
            def submit(self,fn,*args):
                f=Future();f.set_result(fn(*args));return f
            def shutdown(self,**kwargs):pass
        keys=iter([ord('3'),10,kdash.curses.KEY_DOWN,10,-1,kdash.curses.KEY_RIGHT,-1,kdash.curses.KEY_RIGHT,-1,ord('p'),-1,27,-1,ord('6'),10,-1,10,-1,kdash.curses.KEY_RIGHT,-1,27,-1,ord('q')])
        class Terminal:
            def timeout(self,v):pass
            def getmaxyx(self):return 45,150
            def erase(self):pass
            def addnstr(self,*a):pass
            def refresh(self):pass
            def getch(self):return next(keys)
        original=kdash.layout
        def layout(data,*args,**kwargs):
            frames.append((args[0],kwargs['category_tab'],copy.deepcopy(data.get('resource_detail'))))
            result=original(data,*args,**kwargs)
            self.assertTrue(all(len(line)==149 for line in result[0]))
            return result
        def loader(item,mode):
            calls.append((item,mode));return rd.demo(item,mode)
        with patch('kdash.concurrent.futures.ThreadPoolExecutor',Pool),patch('kdash.layout',layout),patch('kdash.curses.curs_set'),patch('kdash.curses.set_escdelay',create=True),patch('kdash.curses.mousemask'),patch('kdash.curses.mouseinterval'),patch('kdash.sys.stdout'):
            self.assertEqual(kdash.screen(Terminal(),kdash.demo,30,resource_loader=rv.demo_resource,detail_loader=loader),'quit')
        self.assertEqual([mode for item,mode in calls],['yaml','describe','logs','logs_previous','yaml','describe'])
        self.assertEqual(calls[0][0]['name'],'web-abc')
        self.assertTrue(all(tab==0 for view,tab,state in frames if view=='6'))
        self.assertTrue(any(view=='6' and state is None for view,tab,state in frames))

    def test_long_lines_partial_errors_controls_and_small_layouts(self):
        state=dict(target=dict(resource='pods',name='web',namespace='prod',namespaced=True),mode='logs',loading=False,error='waiting',output='x'*300+'\n\x1b[31mend')
        text='\n'.join(rd.content(state,40,kdash.clean))
        self.assertIn('PARTIAL OUTPUT',text);self.assertNotIn('\x1b',text)
        self.assertEqual(text.count('x'),300)
        for width in (60,84,100,150):
            data=dict(kdash.demo(),resource_detail=state)
            lines,*_=kdash.layout(data,'3',width,height=25)
            self.assertTrue(all(len(line)==width for line in lines))


if __name__=='__main__': unittest.main()
