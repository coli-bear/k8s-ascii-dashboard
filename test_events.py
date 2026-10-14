import copy
import unittest
from concurrent.futures import Future
from unittest.mock import patch

import event_views as ev
import kdash


def data_set(count=35):
    data = kdash.demo()
    data['events'] = [dict(metadata={'uid': str(i), 'namespace': 'example', 'name': 'event-' + str(i)}, type='Warning', reason='FailedScheduling',
                           involvedObject={'kind': 'Pod', 'name': 'very-long-pod-' + str(i), 'namespace': 'example'},
                           lastTimestamp='2026-10-14T12:00:%02dZ' % i, count=i+1,
                           message='Long explanation ' * 30 + 'unique ending ' + str(i)) for i in range(count)]
    data['errors'] = ['nodes: ' + 'forbidden ' * 60, 'pods: access denied']
    data['resource_errors'] = {'services': 'Service request failed'}
    data['catalog_errors'] = ['API discovery timed out']
    return data


class EventTests(unittest.TestCase):
    def test_table_single_line_rows_ellipsis_and_full_details(self):
        data = data_set()
        for view in ('4', '5'):
            rows = ev.records(data, view)
            for width in (36, 60, 84, 109, 110, 150, 240):
                header, body = ev.table(data, view, width, kdash.clean)
                self.assertEqual(len(body), 2 * len(rows))
                self.assertTrue(all(len(line) == width for line in header + body))
                self.assertIn('...', '\n'.join(body))
            data['event_state'] = {'index': 0, 'detail': copy.deepcopy(rows[0])}
            lines = ev.content(data, view, 45, kdash.clean)
            self.assertTrue(all(len(line) <= 45 for line in lines))
            joined = ' '.join(lines)
            self.assertIn('MESSAGE', joined)
            self.assertIn('unique ending 34' if view == '4' else 'forbidden', joined)
            if view == '4': self.assertIn('FULL EVENT JSON', joined)
            data.pop('event_state')

    def test_selection_survives_reorder_details_are_snapshot_and_deleted_rows_clamp(self):
        data = data_set(4)
        browser = ev.Browser()
        rows = ev.records(data, '4')
        browser.move('4', rows, 2)
        uid = browser.ids['4']
        browser.open('4', rows, 7)
        data['events'][1]['message'] = 'changed after opening'
        data['events'][1]['lastTimestamp'] = '2027-01-01T00:00:00Z'
        rows = ev.records(data, '4')
        browser.reconcile('4', rows)
        self.assertEqual(browser.ids['4'], uid)
        self.assertEqual(browser.indices['4'], 0)
        self.assertNotIn('changed after opening', str(browser.detail))
        self.assertEqual(browser.saved_offset, 7)
        browser.reconcile('4', [])
        self.assertEqual(browser.indices['4'], 0)
        self.assertIsNone(browser.ids['4'])
        data['errors'] = ['same', 'same']
        rows = ev.records(data, '5')
        browser.move('5', rows, 1); browser.reconcile('5', rows)
        self.assertEqual(browser.indices['5'], 1)

    def test_empty_unavailable_nullable_events_and_series_timestamp(self):
        data = data_set(0)
        self.assertIn('No retained', ev.content(data, '4', 60, kdash.clean)[0])
        data['events'] = None
        self.assertIn('unavailable', ev.content(data, '4', 60, kdash.clean)[0])
        self.assertIn('N/A', ev.header(data, '4', 60, kdash.clean)[0])
        data['events'] = [None, {'type': 'Normal'}, {'type': 'Warning', 'metadata': None, 'involvedObject': None, 'message': None, 'series': {'lastObservedTime': '2026-10-14T12:00:00Z', 'count': 9}}]
        row = ev.records(data, '4')[0]
        self.assertEqual(row['count'], 9)
        self.assertEqual(row['stamp'], '2026-10-14T12:00:00Z')
        ev.table(data, '4', 36, kdash.clean)

    def test_selected_row_visible_after_page_and_resize(self):
        data = data_set()
        data['event_state'] = {'index': 30, 'detail': None}
        for width in (60, 100, 160):
            for height in (18, 25, 40):
                lines, _, visible, offset = kdash.layout(data, '4', width, height=height, focus='content')
                self.assertTrue(all(len(line) == width for line in lines))
                self.assertIn('| > ', '\n'.join(lines))
                self.assertLessEqual(offset, 60)
                self.assertGreater(offset + visible, 60)

    def test_keyboard_select_open_scroll_escape_page_and_errors(self):
        class Pool:
            def __init__(self, **kwargs): pass
            def submit(self, fn, *args):
                f=Future();f.set_result(fn(*args));return f
            def shutdown(self, **kwargs): pass
        keys = iter([ord('4'), 10, kdash.curses.KEY_DOWN, 10, ord('d'), 27, -1,
                     ord('d'), 10, 27, -1, 27, -1, ord('5'), 10, kdash.curses.KEY_DOWN, 10, 27, -1, ord('q')])
        class Terminal:
            def timeout(self, value): pass
            def getmaxyx(self): return 35, 150
            def erase(self): pass
            def addnstr(self, *args): pass
            def refresh(self): pass
            def getch(self): return next(keys)
        snapshots=[]
        original=kdash.layout
        def layout(data, *args, **kwargs):
            if args[0] in ('4','5'): snapshots.append((args[0], kwargs['focus'], copy.deepcopy(data.get('event_state'))))
            return original(data,*args,**kwargs)
        with patch('kdash.concurrent.futures.ThreadPoolExecutor',Pool), patch('kdash.layout',layout), patch('kdash.curses.curs_set'), patch('kdash.curses.set_escdelay',create=True), patch('kdash.curses.mousemask'), patch('kdash.curses.mouseinterval'), patch('kdash.sys.stdout'):
            self.assertEqual(kdash.screen(Terminal(),data_set,30),'quit')
        self.assertTrue(any(v=='4' and s['index']==1 and s['detail'] for v,f,s in snapshots))
        self.assertTrue(any(v=='4' and s['index']==1 and not s['detail'] and f=='content' for v,f,s in snapshots))
        self.assertTrue(any(v=='4' and s['index']>1 and s['detail'] for v,f,s in snapshots))
        self.assertTrue(any(v=='4' and f=='nav' and not s['detail'] for v,f,s in snapshots))
        self.assertTrue(any(v=='5' and s['index']==1 and s['detail'] for v,f,s in snapshots))


if __name__ == '__main__': unittest.main()
