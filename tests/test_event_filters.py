import copy
from concurrent.futures import Future
import unittest
from unittest.mock import patch

from kdash_app.ui import dashboard as kdash
from kdash_app.resources import event_views as events


def data_set():
    data = kdash.demo()
    data['events'] = [dict(type='Warning', metadata=dict(uid=uid, namespace=ns),
                           involvedObject=dict(kind='Pod', name=name, namespace=ns), reason=reason,
                           message='fixture', lastTimestamp='2026-10-15T00:00:00Z')
                      for uid, ns, name, reason in [('a','prod','web','FailedScheduling'),
                                                    ('b','dev','web','FailedScheduling'),
                                                    ('c','prod','worker','BackOff')]]
    data['errors'] = [dict(message='Forbidden fixture', reason='Forbidden',
                           reference=dict(kind='Pod', name='web', namespace='prod')),
                      'Timeout without a resource reference']
    data['resource_errors'] = {}
    return data


class EventFilterTests(unittest.TestCase):
    def test_multiple_fields_aliases_quotes_and_case(self):
        filters = events.parse_filter('namespace=PROD obj=pod/WEB reason="failedScheduling"')
        rows = events.records(data_set(), '4', filters)
        self.assertEqual([r['id'] for r in rows], ['a'])
        self.assertEqual(len(events.records(data_set(), '4', {})), 3)
        self.assertEqual(events.parse_filter('ns= reason='), {})
        for query in ('status=failed', 'web', 'reason="unterminated'):
            with self.assertRaises(ValueError): events.parse_filter(query)

    def test_errors_use_reference_and_reason_or_message(self):
        rows = events.records(data_set(), '5', events.parse_filter('ns=prod object=web reason=forbidden'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['target'], 'Pod/web')
        self.assertEqual(len(events.records(data_set(), '5', {'reason':'timeout'})), 1)
        self.assertEqual(events.records(data_set(), '5', {'ns':'unknown'}), [])

    def test_empty_match_and_counts(self):
        data = data_set(); data['event_filters'] = {'4': {'reason':'missing'}}
        self.assertIn('No matching', '\n'.join(events.content(data, '4', 120, str)))
        header = '\n'.join(events.header(data, '4', 120, str))
        self.assertIn('Total: 3 | Matches: 0', header)
        self.assertIn('reason=missing', header)

    def test_keyboard_filter_select_detail_and_clear_in_both_menus(self):
        class Pool:
            def __init__(self, **kw): pass
            def submit(self, fn, *args):
                future = Future(); future.set_result(fn(*args)); return future
            def shutdown(self, **kw): pass
        for view in ('4', '5'):
            with self.subTest(view=view):
                query = 'namespace=prod object=web reason=' + ('scheduling' if view == '4' else 'forbidden')
                keys = iter([ord(view),10,ord('/'),*map(ord,query),10,-1,10,-1,27,-1,ord('x'),-1,ord('q')])
                class Terminal:
                    def timeout(self, value): pass
                    def getmaxyx(self): return 40, 150
                    def erase(self): pass
                    def addnstr(self, *args): pass
                    def refresh(self): pass
                    def getch(self): return next(keys)
                snapshots = []; original = kdash.layout
                def layout(data, *args, **kw):
                    if args[0] == view:
                        snapshots.append(copy.deepcopy(data))
                    return original(data, *args, **kw)
                with patch.object(kdash.concurrent.futures, 'ThreadPoolExecutor', Pool), patch.object(kdash, 'layout', layout), patch.object(kdash.curses, 'curs_set'), patch.object(kdash.curses, 'set_escdelay', create=True), patch.object(kdash.curses, 'mousemask'), patch.object(kdash.curses, 'mouseinterval'), patch.object(kdash.sys, 'stdout'):
                    self.assertEqual(kdash.screen(Terminal(), data_set, 30), 'quit')
                filtered = [d for d in snapshots if d['event_filters'][view]]
                self.assertTrue(filtered)
                self.assertTrue(all(len(events.records(d, view)) == 1 for d in filtered))
                details = [d['event_state']['detail'] for d in filtered if d['event_state']['detail']]
                self.assertTrue(details)
                self.assertEqual(details[0]['target'], 'Pod/web')
                self.assertFalse(snapshots[-1]['event_filters'][view])
