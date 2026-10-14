import unittest
import kdash
import monitoring as m


def snapshot():
    cards = []
    for i, title in enumerate(('CPU used', 'Memory used', 'Disk read', 'Disk write', 'Disk read IOPS', 'Disk write IOPS', 'Host network RX', 'Host network TX')):
        cards.append(dict(key=str(i), title=title, unit='cores', source='test', error='', warnings=[], age=1,
                          series=[{'metric': {'instance': '10.0.0.%s:9100' % j, 'namespace': 'production', 'pod': 'a-long-workload-name-' + str(j)},
                                   'values': [[1000, str(j+1)]]} for j in range(i % 3 + 1)]))
    return dict(start=700, end=1000, step=5, cards=cards, notes=[], errors=[], names={})


class ResponsiveMonitoringTests(unittest.TestCase):
    def test_wide_tables_share_a_row_and_narrow_tables_stack(self):
        data = dict(kdash.demo(), prometheus={'endpoint': 'http://test'}, monitoring=snapshot())
        wide = m.render(data, 0, 300, kdash.ascii_table)
        tables = wide[wide.index('METRIC TABLES')+1:]
        self.assertTrue(any(all(title in line for title in ('CPU USED', 'MEMORY USED', 'DISK READ', 'DISK WRITE')) for line in tables))
        narrow = m.render(data, 0, 60, kdash.ascii_table)
        tables_narrow = narrow[narrow.index('METRIC TABLES')+1:]
        self.assertFalse(any('CPU USED' in line and 'MEMORY USED' in line for line in tables_narrow))
        for card in data['monitoring']['cards']:
            self.assertIn(card['title'].upper(), '\n'.join(tables))
            self.assertIn(card['title'].upper(), '\n'.join(tables_narrow))
        self.assertLess(len(wide), len(narrow))

    def test_grid_keeps_tall_rows_and_partial_last_row(self):
        blocks = [['A'], ['B', 'B2', 'B3', 'B4'], ['C'], ['D', 'D2']]
        lines = m.grid(blocks, 32, 9, lambda item, size: item)
        self.assertTrue(any('A' in line and 'B' in line and 'C' in line for line in lines))
        self.assertIn('B4', '\n'.join(lines))
        self.assertIn('D2', '\n'.join(lines))
        self.assertTrue(all(len(line) <= 32 for line in lines))

    def test_all_tabs_width_boundaries_and_stretched_borders(self):
        data = dict(kdash.demo(), prometheus={'endpoint': 'http://test'}, monitoring=snapshot())
        for tab in range(4):
            for width in (40, 60, 83, 84, 85, 99, 100, 129, 130, 150, 200, 300, 420):
                body = m.render(data, tab, width, kdash.ascii_table)
                start = body.index('METRIC TABLES')
                self.assertTrue(all(len(line) <= width for line in body[start:]))
                lines, *_ = kdash.layout(data, 'm', width, height=40, monitoring_tab=tab)
                self.assertTrue(all(len(line) == width and line.isascii() for line in lines))
        table = kdash.ascii_table(['NODE', 'CORES'], [['n1', '0.1']], 60, stretch=True)
        self.assertTrue(all(len(line) == 60 for line in table))


if __name__ == '__main__': unittest.main()
