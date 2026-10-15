"""Read-only Prometheus monitoring and terminal-sized ASCII history charts."""
import concurrent.futures
import datetime as dt
import json
import math
import re
import shlex
import time
import textwrap

import profiles

PERIODS = (300, 900, 3600)


def parse_filter(text, tab):
    allowed = ({'node', 'cluster'}, {'ns', 'cluster'}, {'ns', 'service', 'cluster'}, {'ns', 'name', 'cluster'})[tab]
    result = {}
    for token in shlex.split(text):
        key, sep, value = token.partition('=')
        if not sep or key not in allowed or not value:
            raise ValueError('Use ' + ' '.join(k + '=...' for k in sorted(allowed)))
        result[key] = value
    return result


def matcher(label, value):
    # Literal substring search. Quote both the RE2 metacharacters and PromQL string.
    literal = re.sub(r'([\\.^$|?*+()\[\]{}])', r'\\\1', value)
    return label + '=~' + json.dumps('.*' + literal + '.*')


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def samples(series):
    result = []
    for pair in series.get('values', []):
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and number(pair[0]) is not None:
            result.append((float(pair[0]), number(pair[1])))
    return sorted(result)


def latest(series, end, step):
    points = samples(series)
    return points[-1][1] if points and end - points[-1][0] <= step * 1.5 else None


def fmt(value):
    if value is None:
        return 'N/A'
    return '%.2f' % value if abs(value) < 1000 else '%.1fk' % (value / 1000)


def plan(tab, filters, instances=None, service_metrics=False):
    scope = ['cluster=' + json.dumps(filters['cluster'])] if 'cluster' in filters else []
    common = scope + ([matcher('namespace', filters['ns'])] if filters.get('ns') else [])
    common += [matcher('pod', filters['name'])] if filters.get('name') else []
    def metric(name, extra=(), node=False):
        labels = list(scope if node else common) + list(extra)
        if node and instances is not None:
            labels.append('instance=~' + json.dumps('|'.join(re.escape(v) for v in instances) if instances else 'a^'))
        return name + ('{' + ','.join(labels) + '}' if labels else '')
    def spec(key, title, unit, query, source, *other_sources):
        return dict(key=key, title=title, unit=unit, query=query, source=source, sources=(source,) + other_sources)
    specs, notes = [], []
    if tab == 0:
        cpu = metric('node_cpu_seconds_total', ['mode="idle"'], True)
        mem = metric('node_memory_MemTotal_bytes', node=True)
        avail = metric('node_memory_MemAvailable_bytes', node=True)
        specs += [spec('cpu', 'CPU used', 'cores', 'sum by(cluster,instance)(1 - max by(cluster,instance,cpu)(rate(' + cpu + '[2m])))', cpu),
                  spec('memory', 'Memory used', 'GiB', '(max by(cluster,instance)(' + mem + ') - max by(cluster,instance)(' + avail + ')) / 1073741824', mem, avail)]
        disk = ['device=~"(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)"']
        net = ['device!~"lo|veth.*|cali.*|cilium.*|docker.*|br.*|tunl.*|flannel.*|kube.*|vxlan.*"']
        for key, title, source, extra, divisor, unit in [
            ('read', 'Disk read', 'node_disk_read_bytes_total', disk, 1048576, 'MiB/s'),
            ('write', 'Disk write', 'node_disk_written_bytes_total', disk, 1048576, 'MiB/s'),
            ('iops_r', 'Disk read IOPS', 'node_disk_reads_completed_total', disk, 1, 'ops/s'),
            ('iops_w', 'Disk write IOPS', 'node_disk_writes_completed_total', disk, 1, 'ops/s'),
            ('rx', 'Host network RX', 'node_network_receive_bytes_total', net, 1048576, 'MiB/s'),
            ('tx', 'Host network TX', 'node_network_transmit_bytes_total', net, 1048576, 'MiB/s')]:
            m = metric(source, extra, True)
            specs.append(spec(key, title, unit, 'sum by(cluster,instance)(max by(cluster,instance,device)(rate(' + m + '[2m]))) / ' + str(divisor), m))
        notes = ['Disk: physical sd/vd/xvd/nvme/mmc devices only; logical devices/partitions excluded.',
                 'Network: host interfaces excluding loopback/pod bridges/tunnels; bonded links can overlap.']
    elif tab in (1, 3):
        group = 'cluster,namespace' if tab == 1 else 'cluster,namespace,pod'
        cpu = metric('container_cpu_usage_seconds_total', ['container!=""', 'container!="POD"'])
        mem = metric('container_memory_working_set_bytes', ['container!=""', 'container!="POD"'])
        specs = [spec('cpu', 'Container CPU used', 'cores', 'sum by(' + group + ')(max by(cluster,namespace,pod,container)(rate(' + cpu + '[2m])))', cpu),
                 spec('memory', 'Container memory working set', 'GiB', 'sum by(' + group + ')(max by(cluster,namespace,pod,container)(' + mem + ')) / 1073741824', mem)]
        if tab == 1:
            for resource, unit, divisor in [('cpu', 'cores', 1), ('memory', 'GiB', 1073741824)]:
                for typ in ('requests', 'limits'):
                    m = metric('kube_pod_container_resource_' + typ, ['resource="' + resource + '"'])
                    specs.append(spec(resource + '_' + typ, resource.title() + ' declared ' + typ, unit,
                                      'sum by(cluster,namespace)(max by(cluster,namespace,pod,container)(' + m + ')) / ' + str(divisor), m))
            notes = ['Requests/limits: declared regular-container values (not scheduler effective allocation).',
                     'Init containers, Pod overhead and ResourceQuota accounting are not included.']
        else:
            throttled = metric('container_cpu_cfs_throttled_periods_total', ['container!=""', 'container!="POD"'])
            periods = metric('container_cpu_cfs_periods_total', ['container!=""', 'container!="POD"'])
            specs.append(spec('throttle', 'CPU throttled periods (weighted)', '%',
                              '100 * sum by(cluster)(max by(cluster,namespace,pod,container)(rate(' + throttled + '[2m]))) / sum by(cluster)(max by(cluster,namespace,pod,container)(rate(' + periods + '[2m])))', periods, throttled))
            m = metric('kube_pod_container_status_restarts_total')
            specs.append(spec('restarts', 'Restarts in preceding 5m', 'count', 'sum by(cluster,namespace,pod)(max by(cluster,namespace,pod,container)(increase(' + m + '[5m])))', m))
            for phase in ('Pending', 'Failed'):
                m = metric('kube_pod_status_phase', ['phase="' + phase + '"'])
                specs.append(spec(phase.lower(), phase + ' Pods', 'pods', 'max by(cluster,namespace,pod)(' + m + ')', m))
            m = metric('kube_pod_container_status_last_terminated_reason', ['reason="OOMKilled"'])
            specs.append(spec('oom', 'Last termination was OOMKilled', 'containers', 'sum by(cluster,namespace,pod)(max by(cluster,namespace,pod,container)(' + m + '))', m))
            notes = ['OOM is the count of containers whose last termination was OOMKilled, not new events.',
                     'Tables show top 20 Pods; charts aggregate all returned Pods. Restarts use a rolling 5m increase.',
                     'Throttling is the weighted ratio of throttled CFS periods, not CPU time lost.']
    else:
        if service_metrics:
            labels = list(scope) + ['reporter="destination"']
            if filters.get('ns'):
                labels.append(matcher('destination_service_namespace', filters['ns']))
            if filters.get('service'):
                labels.append(matcher('destination_service_name', filters['service']))
            m = 'istio_requests_total{' + ','.join(labels) + '}'
            group = 'cluster,destination_service_namespace,destination_service_name'
            specs = [spec('requests', 'Istio destination requests', 'req/s', 'sum by(' + group + ')(rate(' + m + '[2m]))', m)]
            errors = 'istio_requests_total{' + ','.join(labels + ['response_code=~"5.."']) + '}'
            specs.append(spec('errors', 'Istio destination HTTP 5xx', 'req/s', 'sum by(' + group + ')(rate(' + errors + '[2m]))', errors))
            notes = ['Source: Istio destination reporter; only observed mesh traffic is included.']
        else:
            notes = ['SERVICE TELEMETRY UNAVAILABLE: istio_requests_total (destination reporter) is absent.',
                     'Below is Pod network traffic by namespace, NOT traffic attributed to Kubernetes Services.',
                     'Service-level HTTP rates require a mesh/ingress exporter; no service mapping is inferred.']
            if filters.get('service'):
                return [], notes + ['Remove service= filter to see namespace Pod network traffic.']
            for key, verb in [('rx', 'receive'), ('tx', 'transmit')]:
                m = metric('container_network_' + verb + '_bytes_total', ['pod!=""'])
                specs.append(spec(key, 'Pod network ' + key.upper(), 'MiB/s',
                                  'sum by(cluster,namespace)(max by(cluster,namespace,pod,interface)(rate(' + m + '[2m]))) / 1048576', m))
    return specs, notes


class Reader:
    def __init__(self, config, timeout=8, query=None):
        self.config, self.timeout = config, timeout
        self.query = query or profiles.prometheus_query

    def __call__(self, tab, filters, period):
        end = int(time.time())
        step = max(5, period // 60)
        snapshot = dict(end=end, start=end-period, step=step, cards=[], notes=[], errors=[], names={})
        def query(expr, ranged=False):
            kwargs = dict(start=end-period, end=end, step=step) if ranged else {}
            result = self.query(self.config, expr, self.timeout, **kwargs)
            return result['data']['result'], result.get('warnings') or []
        try:
            # Probe every operand over the graph history plus the longest rate/
            # increase lookback, before any expression drops series labels.
            horizon = period + 300
            scoped = dict(filters)
            service_metrics = False
            if tab == 2:
                labels = ['reporter="destination"']
                if 'cluster' in scoped:
                    labels.append('cluster=' + json.dumps(scoped['cluster']))
                if scoped.get('ns'):
                    labels.append(matcher('destination_service_namespace', scoped['ns']))
                if scoped.get('service'):
                    labels.append(matcher('destination_service_name', scoped['service']))
                source = 'istio_requests_total{' + ','.join(labels) + '}'
                found, warnings = query('count by(cluster)(count_over_time(' + source + '[' + str(horizon) + 's]))')
                snapshot['notes'] += warnings
                service_metrics = bool(found)
            specs, notes = plan(tab, scoped, service_metrics=service_metrics)
            sources = list(dict.fromkeys(source for spec in specs for source in spec['sources']))
            if tab == 0:
                selector = '{cluster=' + json.dumps(scoped['cluster']) + '}' if 'cluster' in scoped else ''
                sources.append('node_uname_info' + selector)
            if sources:
                guard = 'count by(cluster)(' + ' or '.join('count_over_time(' + source + '[' + str(horizon) + 's])' for source in sources) + ')'
                clusters, warnings = query(guard)
                snapshot['notes'] += warnings
                labels = {s.get('metric', {}).get('cluster', '') for s in clusters}
                if len(labels) > 1 or ('cluster' in scoped and labels - {scoped['cluster']}):
                    snapshot['errors'] = ['Multiple cluster labels or an inconsistent scope detected. Use / cluster=<exact-label> before aggregating: ' + ', '.join(sorted(label or '(unlabeled)' for label in labels))]
                    return snapshot
                if 'cluster' not in scoped:
                    # Pin the inferred label even when it is empty. Newly arriving
                    # clusters between this guard and chart queries stay excluded.
                    scoped['cluster'] = next(iter(labels), '')
                if not labels:
                    snapshot['notes'].append('No matching source samples in this cluster/range; check the exact cluster label and exporters.')
                elif labels == {''}:
                    snapshot['notes'].append('Sources have no cluster label. Separate unlabeled clusters cannot be distinguished; use a dedicated endpoint or add cluster labels.')
            instances = None
            if tab == 0:
                selector = '{cluster=' + json.dumps(scoped.get('cluster', '')) + '}'
                nodes, warnings = query('node_uname_info' + selector)
                snapshot['notes'] += warnings
                snapshot['names'] = {s['metric'].get('instance', ''): s['metric'].get('nodename', '') for s in nodes}
                if filters.get('node'):
                    instances = [i for i, name in snapshot['names'].items() if filters['node'].lower() in (i + ' ' + name).lower()]
                    if not instances:
                        snapshot['notes'].append('No node matches node=' + filters['node'])
            specs, notes = plan(tab, scoped, instances, service_metrics)
            snapshot['notes'] += notes
            def fetch(spec):
                card = dict(spec, series=[], error='', warnings=[], age=None)
                try:
                    card['series'], card['warnings'] = query(spec['query'], True)
                    fresh, warnings = query('max(timestamp(' + spec['source'] + '))')
                    card['warnings'] += warnings
                    stamp = number(fresh[0].get('value', [None, None])[1]) if fresh else None
                    card['age'] = max(0, end-stamp) if stamp is not None else None
                except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
                    card['error'] = str(exc)
                return card
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                snapshot['cards'] = list(pool.map(fetch, specs))
        except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
            snapshot['errors'].append(str(exc))
        return snapshot


def aggregate(series, start, end, step):
    # Missing samples remain gaps. Never carry old values across an outage.
    buckets = {}
    for item in series:
        for stamp, value in samples(item):
            if value is not None and start <= stamp <= end:
                buckets.setdefault(stamp, []).append(value)
    return [(stamp, sum(values)) for stamp, values in sorted(buckets.items())]


def chart(card, snap, width):
    width = max(24, width)
    inner = width - 4
    points = aggregate(card['series'], snap['start'], snap['end'], snap['step'])
    current = sum(v for s in card['series'] if (v := latest(s, snap['end'], snap['step'])) is not None) if any(latest(s, snap['end'], snap['step']) is not None for s in card['series']) else None
    status = 'ERROR' if card['error'] else 'N/A' if current is None else 'STALE' if card['age'] is None or card['age'] > 120 else 'LIVE'
    lines = [card['title'], fmt(current) + ' ' + card['unit'] + ' | ' + status]
    plot_width, height = max(4, inner - 9), 4
    peak = max([v for _, v in points] or [0])
    floor = min([v for _, v in points] or [0])
    grid = [[' '] * plot_width for _ in range(height)]
    for stamp, value in points:
        x = min(plot_width-1, int((stamp-snap['start']) / (snap['end']-snap['start']) * (plot_width-1)))
        y = height-1 - min(height-1, max(0, round((value-floor) / (peak-floor) * (height-1)))) if peak > floor else height-1
        grid[y][x] = '*'
    for i, row in enumerate(grid):
        lines.append((fmt(peak) if i == 0 else fmt(floor) if i == height-1 else '').rjust(7) + ' |' + ''.join(row))
    lines += [' ' * 8 + '+' + '-' * plot_width, '-%sm%snow' % ((snap['end']-snap['start'])//60, ' ' * max(1, inner-10)),
              'Newest sample age: ' + ('N/A' if card['age'] is None else '%ss' % int(card['age']))]
    border = '+' + '-' * (width-2) + '+'
    return [border] + ['| ' + line[:inner].ljust(inner) + ' |' for line in lines] + [border]


def grid(items, width, min_width, render_item):
    """Pack cards into as many readable columns as the current terminal allows."""
    if not items:
        return []
    gap = 2
    columns = max(1, min(len(items), (width + gap) // (min_width + gap)))
    base, remainder = divmod(width - gap * (columns - 1), columns)
    widths = [base + (i < remainder) for i in range(columns)]
    output = []
    for start in range(0, len(items), columns):
        blocks = [render_item(item, widths[i]) for i, item in enumerate(items[start:start+columns])]
        if output:
            output.append('')
        # Unequal table heights must not discard rows from taller neighbours.
        for y in range(max(map(len, blocks))):
            output.append((' ' * gap).join((block[y] if y < len(block) else '').ljust(widths[i])
                                          for i, block in enumerate(blocks)))
    return output


def render(data, tab, width, table):
    prom = data.get('prometheus') or {}
    if not prom.get('endpoint'):
        return ['PROMETHEUS NOT CONFIGURED', 'Settings / Profiles -> Prometheus: select a Service and port.']
    state = data.get('monitor_state') or {}
    snap = data.get('monitoring')
    output = ['PROMETHEUS | ' + prom['endpoint'],
              'Scope: this Prometheus endpoint | Filter: ' + (state.get('filter') or '(none)'),
              'Refresh: %ss | Range: %sm | %s' % (state.get('interval', 5), state.get('period', 300)//60, 'PAUSED' if state.get('paused') else 'LOADING' if state.get('loading') else 'AUTO'),
              'Keys: t: time range | p: pause/resume | r: refresh | /: filter | x: clear',
              'Filters: ' + ('node=... cluster=...', 'ns=... cluster=...', 'ns=... service=... cluster=...', 'ns=... name=... cluster=...')[tab]]
    if not snap:
        return output + ['', 'Loading Prometheus time series...']
    output += ['Last query: ' + dt.datetime.fromtimestamp(snap['end']).strftime('%H:%M:%S'),
               'Chart: rates over trailing 2m | Y axis: auto min/max | STALE: newest source sample >120s.', ''] + snap['notes'] + snap['errors']
    cards = snap['cards']
    output += [''] + grid(cards, width, 48, lambda card, size: chart(card, snap, size))
    for card in cards:
        if card['error']:
            output += [card['title'] + ': QUERY ERROR - ' + card['error']]
        elif not card['series']:
            output += [card['title'] + ': NO SERIES (exporter/metric/filter): ' + card['source']]
        output += [card['title'] + ': ' + str(w) for w in card['warnings']]
    ranked = []
    for card in cards:
        rows = []
        for series in card['series']:
            labels = series.get('metric') or {}
            if tab == 0:
                instance = labels.get('instance', '?')
                name = (snap['names'].get(instance) or instance) + ' (' + instance + ')'
            else:
                name = '/'.join(labels.get(k, '') for k in ('namespace', 'pod', 'destination_service_namespace', 'destination_service_name') if labels.get(k)) or '(scope total)'
            rows.append((name, latest(series, snap['end'], snap['step'])))
        rows.sort(key=lambda r: -(r[1] if r[1] is not None else -math.inf))
        if rows:
            ranked.append((card, rows))
    def ranked_table(item, size):
        card, rows = item
        title = textwrap.wrap(card['title'].upper(), size)
        labels = ('NODE', 'NAMESPACE', 'NAMESPACE / SERVICE', 'NAMESPACE / POD')
        return title + [str(len(rows)) + ' series | top 20'] + table(
            [labels[tab], card['unit'].upper()], [[name, fmt(val)] for name, val in rows[:20]],
            size, stretch=True)
    output += ['', 'METRIC TABLES', ''] + grid(ranked, width, (42, 40, 48, 64)[tab], ranked_table)
    return output
