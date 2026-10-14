import unittest
from concurrent.futures import Future
from unittest.mock import patch
import kdash
import resource_views as rv


class NavigationTests(unittest.TestCase):
    def test_direct_submenus_monitoring_tabs_and_group_boundaries(self):
        class Pool:
            def __init__(self, **kwargs): pass
            def submit(self, fn, *args):
                f = Future(); f.set_result(fn(*args)); return f
            def shutdown(self, **kwargs): pass
        down, right, left = kdash.curses.KEY_DOWN, kdash.curses.KEY_RIGHT, kdash.curses.KEY_LEFT
        keys = iter([kdash.curses.KEY_UP, down, ord('k'), ord('j'), 10, right, 27, -1, down, 10, right, right, right, right, left,
                     27, -1, down, down, down, 10, right, 27, -1,
                     down, down, down, down, down, down, 10, 27, -1,
                     27, -1, *([kdash.curses.KEY_UP] * 6), 10, ord('q')])
        class Terminal:
            def timeout(self, value): pass
            def getmaxyx(self): return 40, 160
            def erase(self): pass
            def addnstr(self, *args): pass
            def refresh(self): pass
            def getch(self): return next(keys)
        states, output = [], []
        layout = kdash.layout
        def record(*args, **kwargs):
            states.append((args[1], kwargs['focus'], kwargs['nav_group'], kwargs['selected'], kwargs['monitoring_tab'], kwargs['category_tab']))
            result = layout(*args, **kwargs); output.append('\n'.join(result[0])); return result
        with patch('kdash.concurrent.futures.ThreadPoolExecutor', Pool), patch('kdash.layout', record), \
             patch('kdash.curses.curs_set'), patch('kdash.curses.set_escdelay', create=True), \
             patch('kdash.curses.mousemask'), patch('kdash.curses.mouseinterval'), patch('kdash.sys.stdout'):
            self.assertEqual(kdash.screen(Terminal(), kdash.demo, 30,
                resource_loader=rv.demo_resource, discovery_loader=rv.demo_catalog), 'quit')
        self.assertEqual(states[0][:4], ('1', 'nav', 'Dashboard', '1'))
        self.assertEqual([state[3] for state in states[:5]], ['1', 's', '1', 's', '1'])
        self.assertEqual([state[2] for state in states[:5]], ['Dashboard', 'Settings', 'Dashboard', 'Settings', 'Dashboard'])
        self.assertTrue(any(s[:4] == ('1', 'nav', 'Dashboard', '1') for s in states))
        self.assertTrue(any(s[:4] == ('2', 'content', 'Dashboard', '1') for s in states))
        self.assertEqual([s[4] for s in states if s[0] == 'm' and s[1] == 'content'], [0, 1, 2, 3, 0, 3])
        self.assertTrue(any(s[:4] == ('m', 'nav', 'Dashboard', 'm') for s in states))
        self.assertTrue(any(s[:4] == ('5', 'content', 'Event', '5') for s in states))
        self.assertEqual(states[-1][:4], ('6', 'content', 'Resources', '6'))
        self.assertTrue(all(s[1] != 'group' for s in states))
        self.assertTrue(all('Groups' not in text and 'Esc groups' not in text for text in output))
        self.assertEqual(states[-1][5], 1)  # tab survives a visit to another group
        self.assertTrue(any('PROMETHEUS NOT CONFIGURED' in text for text in output))

    def test_group_order_visible_cursor_and_layout_sizes(self):
        self.assertEqual([label for label, _ in rv.MENU_GROUPS], ['Dashboard', 'Clusters', 'Resources', 'Event', 'Settings'])
        self.assertEqual(dict(rv.MENU_GROUPS)['Dashboard'], ('1', 'm'))
        data = kdash.demo()
        for group, keys in rv.MENU_GROUPS:
            for focus in ('nav', 'content'):
                for selected in keys:
                    rows, cursor = kdash.navigation_lines(group, selected, focus)
                    label = {'7': 'Config', '9': 'Access / RBAC', '0': 'API Resource'}.get(selected, dict(rv.MENUS)[selected])
                    self.assertIn('- ' + label, rows[cursor])
                    self.assertIn(' [Dashboard]', rows)
                    self.assertIn(' [Resources]', rows)
                    self.assertIn(' [Event]', rows)
                    for width in (59, 84, 100, 160):
                        for height in (17, 23, 40):
                            lines, *_ = kdash.layout(data, selected, width, height=height,
                                focus=focus, selected=selected, nav_group=group)
                            self.assertLessEqual(len(lines), height)
                            self.assertTrue(all(len(line) == width for line in lines))
                            if focus == 'nav' and width >= 85:
                                self.assertIn(label, '\n'.join(lines))
        for tab in range(4):
            lines, *_ = kdash.layout(data, 'm', 160, height=40, monitoring_tab=tab)
            text = '\n'.join(lines)
            self.assertIn('[' + kdash.MONITORING_TABS[tab][0] + ']', text)
            self.assertIn('PROMETHEUS NOT CONFIGURED', text)


if __name__ == '__main__': unittest.main()
