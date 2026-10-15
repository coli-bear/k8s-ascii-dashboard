import unittest
from kasciidash_app.ui import dashboard
from kasciidash_app.resources import resource_detail


class NavigationPerformanceTests(unittest.TestCase):
    def test_idle_and_cursor_changes_reuse_row_selection(self):
        cache=dashboard.RowCache();data=dashboard.demo();calls=[]
        def select():calls.append(1);return dashboard.resource_selection(data,'3','pods',None,{})
        for _ in range(25):cache.get(('3','pods'),data,select)
        self.assertEqual(len(calls),1)
        cache.get(('3','pods'),dashboard.demo(),select)
        self.assertEqual(len(calls),2)

    def test_cached_render_moves_only_marker_and_tracks_selected_line(self):
        data=dashboard.demo();data['resource_state']={'index':0}
        lines=dashboard.CursorContent(dashboard.workload_lines(data,140,'pods'),len(data['pods']))
        first=list(lines);lines.select(1)
        self.assertEqual(sum(a!=b for a,b in zip(first,lines)),2)
        self.assertTrue(lines[lines.selected_line].startswith('| > '))

    def test_screen_cursor_and_idle_do_not_reformat_or_resort_full_list(self):
        from concurrent.futures import Future
        from unittest.mock import patch
        class Pool:
            def __init__(self,**kw): pass
            def submit(self,fn,*args):
                future=Future();future.set_result(fn(*args));return future
            def shutdown(self,**kw):pass
        data=dashboard.demo();template=data['pods'][0]
        data['pods']=[dict(template,metadata=dict(template['metadata'],name='pod-%03d'%i,uid='pod-%d'%i)) for i in range(100)]
        keys=iter([ord('3'),10,-1]+[key for _ in range(20) for key in (dashboard.curses.KEY_DOWN,-1)]+[ord('q')])
        class Terminal:
            def timeout(self,v):pass
            def getmaxyx(self):return 45,180
            def erase(self):pass
            def addnstr(self,*args):pass
            def refresh(self):pass
            def getch(self):return next(keys)
        with patch.object(dashboard.concurrent.futures,'ThreadPoolExecutor',Pool),patch.object(dashboard.curses,'curs_set'),patch.object(dashboard.curses,'set_escdelay',create=True),patch.object(dashboard.curses,'mousemask'),patch.object(dashboard.curses,'mouseinterval'),patch.object(dashboard.sys,'stdout'),patch.object(dashboard,'workload_lines',wraps=dashboard.workload_lines) as render,patch.object(dashboard,'resource_selection',wraps=dashboard.resource_selection) as select:
            self.assertEqual(dashboard.screen(Terminal(),lambda:data,30),'quit')
        self.assertLessEqual(render.call_count,2)
        self.assertLessEqual(select.call_count,3)

    def test_new_detail_does_not_wait_for_old_running_request(self):
        from concurrent.futures import Future
        class Pool:
            def __init__(self):self.jobs=[]
            def submit(self,*args):
                future=Future();future.set_running_or_notify_cancel();self.jobs.append(future);return future
        browser=resource_detail.Detail();pool=Pool()
        item={'resource':'pods','name':'old','namespace':'prod','namespaced':True}
        browser.open(item,0);browser.poll(pool,lambda *args:None)
        browser.close();browser.open(dict(item,name='new'),0);browser.poll(pool,lambda *args:None)
        self.assertEqual(len(pool.jobs),2)
        pool.jobs[0].set_result(('stale',None));browser.poll(pool,lambda *args:None)
        self.assertTrue(browser.state()['loading'])
        pool.jobs[1].set_result(('current',None));browser.poll(pool,lambda *args:None)
        self.assertEqual(browser.state()['output'],'current')
