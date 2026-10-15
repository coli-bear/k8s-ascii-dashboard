"""Configuration graph model and width-bounded ASCII connection diagrams."""
from collections import defaultdict
import textwrap

from kdash_app.resources import resource_views as rv
MAX_NODES = 160
MAX_EDGES = 400
MAX_LINES = 2500


def meta(obj):
    return rv.field(obj, 'metadata', {})


def group(obj):
    version = obj.get('apiVersion') or ''
    return version.split('/')[0] if '/' in version else ''


def pod_references(pod):
    spec = rv.field(pod, 'spec', {})
    refs = set()
    for volume in rv.field(spec, 'volumes', []):
        for source, kind, field in [('persistentVolumeClaim', 'PersistentVolumeClaim', 'claimName'), ('configMap', 'ConfigMap', 'name'), ('secret', 'Secret', 'secretName')]:
            name = rv.field(volume, source, {}).get(field)
            if name:
                refs.add((kind, name))
        for source in rv.field(rv.field(volume, 'projected', {}), 'sources', []):
            for source_type, kind in [('configMap', 'ConfigMap'), ('secret', 'Secret')]:
                name = rv.field(source, source_type, {}).get('name')
                if name:
                    refs.add((kind, name))
    for key in ('containers', 'initContainers', 'ephemeralContainers'):
        for container in rv.field(spec, key, []):
            for env in rv.field(container, 'env', []):
                for source, kind in [('configMapKeyRef', 'ConfigMap'), ('secretKeyRef', 'Secret')]:
                    name = rv.field(rv.field(env, 'valueFrom', {}), source, {}).get('name')
                    if name:
                        refs.add((kind, name))
            for env in rv.field(container, 'envFrom', []):
                for source, kind in [('configMapRef', 'ConfigMap'), ('secretRef', 'Secret')]:
                    name = rv.field(env, source, {}).get('name')
                    if name:
                        refs.add((kind, name))
    for secret in rv.field(spec, 'imagePullSecrets', []):
        if secret.get('name'):
            refs.add(('Secret', secret['name']))
    if spec.get('serviceAccountName'):
        refs.add(('ServiceAccount', spec['serviceAccountName']))
    return refs


def build(snapshot, selector_matches):
    namespace = meta(snapshot['namespace']).get('name')
    objects = {}
    for rows in snapshot['objects'].values():
        for obj in rows or []:
            identity = ('uid', meta(obj)['uid']) if meta(obj).get('uid') else ('object', group(obj), obj.get('kind'), meta(obj).get('namespace'), meta(obj).get('name'))
            objects.setdefault(identity, obj)
    values = sorted(objects.values(), key=lambda o: (o.get('kind', ''), meta(o).get('name', ''), group(o)))
    by_uid = {meta(o)['uid']: o for o in values if meta(o).get('uid')}
    by_name = {(group(o), o.get('kind'), meta(o).get('namespace', namespace), meta(o).get('name')): o for o in values}
    nodes, edges, notes = {}, set(), set()

    def native(obj, kind, api_group=''):
        return obj.get('kind') == kind and group(obj) == api_group

    def add_node(identity, kind, name, ns, missing='', obj=None):
        if identity not in nodes:
            if len(nodes) >= MAX_NODES:
                notes.add('Graph node limit reached (%s); use Resources for the full inventory.' % MAX_NODES)
                return None
            node = dict(kind=kind or 'Resource', name=name or '?', namespace=ns, missing=missing)
            if obj is not None and snapshot.get('focus_uid') and meta(obj).get('uid') == snapshot['focus_uid']:
                node['focused'] = True
            if obj is not None and native(obj, 'Service'):
                node['service_type'] = rv.field(obj, 'spec', {}).get('type', 'ClusterIP')
            if obj is not None and native(obj, 'Pod'):
                node['status'] = 'Terminating' if meta(obj).get('deletionTimestamp') else rv.field(obj, 'status', {}).get('phase', 'Unknown')
                node['ready'] = next((c.get('status', 'Unknown') for c in rv.field(rv.field(obj, 'status', {}), 'conditions', []) if c.get('type') == 'Ready'), 'Unknown')
            nodes[identity] = node
        return identity

    def actual(obj):
        identity = ('uid', meta(obj)['uid']) if meta(obj).get('uid') else ('object', group(obj), obj.get('kind'), meta(obj).get('namespace'), meta(obj).get('name'))
        return add_node(identity, obj.get('kind'), meta(obj).get('name'), meta(obj).get('namespace'), obj=obj)

    def reference(kind, name, api_group='', ns=None, uid=None, strict=False):
        ns = namespace if ns is None else ns
        obj = by_uid.get(uid) if uid else (None if strict else by_name.get((api_group, kind, ns, name)))
        if obj and (obj.get('kind') != kind or meta(obj).get('name') != name or (api_group is not None and group(obj) != api_group) or (not strict and meta(obj).get('namespace', namespace) != ns)):
            obj = None
        if obj:
            return actual(obj)
        current = by_name.get((api_group, kind, ns, name))
        reason = 'UID mismatch' if uid and current and meta(current).get('uid') != uid else 'outside current inventory'
        return add_node(('reference', api_group, kind, ns, name, uid), kind, name, ns, reason)

    def link(source, target, relation):
        if source is None or target is None:
            return
        edge = (source, target, relation)
        if edge not in edges and len(edges) >= MAX_EDGES:
            notes.add('Graph edge limit reached (%s); use TreeView/Resources for more relationships.' % MAX_EDGES)
            return
        edges.add(edge)

    pods = [o for o in values if native(o, 'Pod')]
    services = [o for o in values if native(o, 'Service')]
    for obj in values:
        for owner in rv.field(meta(obj), 'ownerReferences', []):
            version = owner.get('apiVersion') or ''
            owner_group = (version.split('/')[0] if '/' in version else '') if version else None
            parent = reference(owner.get('kind'), owner.get('name'), owner_group, uid=owner.get('uid'), strict=True)
            link(parent, actual(obj), 'owns (UID reference)')
    for ingress in (o for o in values if native(o, 'Ingress', 'networking.k8s.io')):
        spec = rv.field(ingress, 'spec', {})
        backends = [('default', spec['defaultBackend'])] if spec.get('defaultBackend') else []
        for rule in rv.field(spec, 'rules', []):
            for route in rv.field(rv.field(rule, 'http', {}), 'paths', []):
                backends.append((str(rule.get('host') or '*') + str(route.get('path') or '/'), rv.field(route, 'backend', {})))
        for route, backend in backends:
            service = rv.field(backend, 'service', {})
            if service.get('name'):
                port = rv.field(service, 'port', {})
                link(actual(ingress), reference('Service', service['name']), 'backend %s port=%s' % (route, port.get('number', port.get('name', '?'))))
            elif backend.get('resource'):
                target = backend['resource']
                link(actual(ingress), reference(target.get('kind'), target.get('name'), target.get('apiGroup') or ''), 'resource backend ' + route)
    for service in services:
        source = actual(service)
        spec = rv.field(service, 'spec', {})
        if spec.get('type') == 'ExternalName':
            dns = spec.get('externalName') or '?'
            target = add_node(('dns', dns), 'DNS', dns, None)
            link(source, target, 'ExternalName (configuration)')
            continue
        slices = [o for o in values if native(o, 'EndpointSlice', 'discovery.k8s.io') and rv.field(meta(o), 'labels', {}).get('kubernetes.io/service-name') == meta(service).get('name') and meta(o).get('namespace', namespace) == meta(service).get('namespace', namespace)]
        endpoint_pods = set()
        for endpoint_slice in slices:
            slice_id = actual(endpoint_slice)
            link(source, slice_id, 'EndpointSlice association')
            for endpoint in rv.field(endpoint_slice, 'endpoints', []):
                target = rv.field(endpoint, 'targetRef', {})
                ready = rv.field(endpoint, 'conditions', {}).get('ready', 'Unknown')
                if target:
                    version = target.get('apiVersion') or ''
                    api_group = version.split('/')[0] if '/' in version else ''
                    destination = reference(target.get('kind'), target.get('name'), api_group, ns=target.get('namespace', namespace), uid=target.get('uid'))
                    if destination and not nodes[destination]['missing'] and target.get('kind') == 'Pod':
                        endpoint_pods.add(destination)
                    link(slice_id, destination, 'target ready=%s' % ready)
                else:
                    for address in rv.field(endpoint, 'addresses', []):
                        destination = add_node(('address', address), 'Address', address, None)
                        link(slice_id, destination, 'endpoint ready=%s' % ready)
        selector = rv.field(spec, 'selector', {})
        for pod in pods:
            if selector and all(rv.field(meta(pod), 'labels', {}).get(k) == v for k, v in selector.items()):
                destination = actual(pod)
                if destination not in endpoint_pods:
                    link(source, destination, 'selector candidate (not a verified endpoint)')
    for pod in pods:
        for kind, name in sorted(pod_references(pod)):
            link(actual(pod), reference(kind, name), 'references')
    for policy in (o for o in values if native(o, 'NetworkPolicy', 'networking.k8s.io')):
        selector = rv.field(rv.field(policy, 'spec', {}), 'podSelector', {})
        for pod in pods:
            if selector_matches(rv.field(meta(pod), 'labels', {}), selector):
                link(actual(policy), actual(pod), 'selects Pod (not a connectivity verdict)')
    # Keep isolated objects visible, without consuming capacity needed by edges.
    for obj in values:
        actual(obj)
    return dict(nodes=nodes, edges=edges, notes=sorted(notes))


def render_rails(snapshot, width, selector_matches):
    def bounded(lines):
        wrapped = [part for line in lines for part in textwrap.wrap(line, width, replace_whitespace=False) or ['']]
        if len(wrapped) > MAX_LINES:
            notice = textwrap.wrap('Graph display limited to %s lines; use TreeView/Resources.' % MAX_LINES, width)
            return wrapped[:max(0, MAX_LINES - len(notice))] + notice
        return wrapped
    graph = build(snapshot, selector_matches)
    nodes, edges = graph['nodes'], graph['edges']
    priority = {'Ingress': 0, 'Profile': 1, 'Deployment': 2, 'StatefulSet': 2, 'DaemonSet': 2, 'CronJob': 2, 'Job': 3, 'ReplicaSet': 3, 'Service': 4, 'EndpointSlice': 5, 'NetworkPolicy': 6, 'Pod': 7, 'ConfigMap': 8, 'Secret': 9, 'PersistentVolumeClaim': 10, 'ServiceAccount': 11, 'DNS': 12}
    ordered = sorted(nodes, key=lambda key: (priority.get(nodes[key]['kind'], 2), nodes[key]['kind'], nodes[key]['name'], repr(key)))
    ids = {key: 'N%03d' % (i + 1) for i, key in enumerate(ordered)}
    adjacency = defaultdict(set)
    for source, target, _ in edges:
        adjacency[source].add(target)
        adjacency[target].add(source)
    output = ['CONNECTION TOPOLOGY (configuration, not observed traffic)',
              'Legend: > sends a relation; < receives it; x = crossing, not a junction.',
              'Boxes identify objects; E labels explain connection direction and type.',
              'Missing references mean outside inventory or UID mismatch, not confirmed deletion.']
    if not nodes:
        return bounded(output + ['No visible resources.'])
    visited, components, isolated = set(), [], []
    for key in ordered:
        if key in visited:
            continue
        if not adjacency[key]:
            isolated.append(key)
            visited.add(key)
            continue
        pending, component = [key], []
        visited.add(key)
        while pending:
            current = pending.pop()
            component.append(current)
            for neighbor in sorted(adjacency[current], key=ids.get, reverse=True):
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        components.append(sorted(component, key=ids.get))
    box_width = min(62, max(20, width // 2))
    lanes = max(1, min(12, (width - box_width - 2) // 4))
    for component_number, component in enumerate(components, 1):
        members = set(component)
        links = sorted((e for e in edges if e[0] in members), key=lambda e: (ids[e[0]], ids[e[1]], e[2]))
        # Split wide diagrams by edge lanes rather than clipping connections.
        for start in range(0, len(links), lanes):
            if len(output) >= MAX_LINES:
                return bounded(output + ['Graph display limited to %s lines; use TreeView/Resources.' % MAX_LINES])
            panel = links[start:start + lanes]
            output += ['', 'COMPONENT %s | CONNECTIONS %s-%s/%s' % (component_number, start + 1, start + len(panel), len(links))]
            if len(links) > lanes:
                output.append('Edge panels share the same object IDs; boxes repeat across panels only.')
            canvas, ports = [], {}
            canvas_width = box_width + 2 + len(panel) * 4
            def add_line(text):
                canvas.append(list(text.ljust(canvas_width)))
            for key in component:
                node = nodes[key]
                caption = ('* ' if node.get('focused') else '') + '%s [%s] %s' % (ids[key], node['kind'], node['name'])
                text = textwrap.wrap(caption, box_width - 4) or ['?']
                if node.get('status'):
                    text.append('Phase: ' + node['status'])
                if node['missing']:
                    text += textwrap.wrap('Reference: ' + node['missing'], box_width - 4)
                if len(text) < 2:
                    text.append('')
                add_line('+' + '-' * (box_width - 2) + '+')
                ports[key] = len(canvas)
                for line in text:
                    add_line('| ' + line.ljust(box_width - 4) + ' |')
                add_line('+' + '-' * (box_width - 2) + '+')
                add_line('')
            def put(y, x, char):
                old = canvas[y][x]
                canvas[y][x] = 'x' if {old, char} == {'-', '|'} or old == 'x' else char
            for i, (source, target, relation) in enumerate(panel):
                x = box_width + 2 + i * 4
                a, b = ports[source], ports[target] + 1
                for y in range(min(a, b) + 1, max(a, b)):
                    put(y, x, '|')
                for y in (a, b):
                    for column in range(box_width, x):
                        put(y, column, '-')
                    put(y, x, '+')
                canvas[a][box_width - 1] = '>'
                canvas[b][box_width - 1] = '<'
            lane_header = [' '] * canvas_width
            for i in range(len(panel)):
                x = box_width + 2 + i * 4
                for j, char in enumerate('E%02d' % (start + i + 1)):
                    lane_header[x + j] = char
            output.append(''.join(lane_header).rstrip())
            output += [''.join(row).rstrip() for row in canvas]
            output += ['E%02d: %s -> %s | %s' % (start + i + 1, ids[source], ids[target], relation)
                       for i, (source, target, relation) in enumerate(panel)]
    if isolated:
        output += ['', 'NO CONNECTIONS SHOWN (unlinked in collected data or omitted by limits)']
        output += ['%s [%s] %s' % (ids[key], nodes[key]['kind'], nodes[key]['name']) for key in isolated]
    output += graph['notes']
    if len(output) > MAX_LINES:
        output = output[:MAX_LINES] + ['Graph display limited to %s lines; use TreeView/Resources.' % MAX_LINES]
    return bounded(output)


def card_layout(nodes, component, width):
    """Center compact cards in role columns and reserve routing corridors."""
    columns = 4 if width >= 128 else 3
    gap, margin, row_gap = 8, 4, 3
    card_width = min(48, (width - 2 * margin - gap * (columns - 1)) // columns)
    left = (width - (columns * card_width + gap * (columns - 1))) // 2
    network = {'Ingress', 'Service', 'EndpointSlice', 'Address', 'DNS'}
    references = {'ConfigMap', 'Secret', 'PersistentVolumeClaim', 'ServiceAccount', 'ResourceQuota', 'LimitRange'}
    def column(key):
        kind = nodes[key]['kind']
        if kind in network:
            return 0
        if kind == 'Pod':
            return 1
        return 3 if columns == 4 and kind in references else 2
    groups = [[] for _ in range(columns)]
    for key in component:
        groups[column(key)].append(key)
    heights = {}
    for key in component:
        node = nodes[key]
        name_rows = min(2, max(1, len(textwrap.wrap(node['name'], card_width - 4))))
        has_info = bool(node.get('missing') or node.get('status') or node.get('service_type'))
        heights[key] = 3 + name_rows + has_info + ('ready' in node)
    totals = [sum(heights[key] for key in keys) + max(0, len(keys) - 1) * row_gap for keys in groups]
    tallest = max(totals)
    placements = {}
    for col, keys in enumerate(groups):
        y = 4 + (tallest - totals[col]) // 2
        for key in keys:
            placements[key] = dict(x=left + col * (card_width + gap), y=y,
                                   width=card_width, height=heights[key], column=col)
            y += heights[key] + row_gap
    height = tallest + 8
    occupied_rows = {y for box in placements.values() for y in range(box['y'], box['y'] + box['height'])}
    free_rows = [y for y in range(2, height - 1) if y not in occupied_rows]
    return dict(placements=placements, columns=columns, width=width, height=height,
                card_width=card_width, left=left, gap=gap, free_rows=free_rows)


def connection_path(layout, source, target, index):
    """Route through column gutters and shared row gaps, never through cards."""
    placements = layout['placements']
    a, b = placements[source], placements[target]
    ay = a['y'] + min(a['height'] - 2, a['height'] // 2 + 1)
    by = b['y'] + b['height'] // 2
    if source == target and ay == by:
        by -= 1
    def gutter(box, right):
        if right:
            low = box['x'] + box['width']
            count = layout['gap'] if box['column'] < layout['columns'] - 1 else layout['width'] - 1 - low
            return low + index % max(1, count)
        count = layout['gap'] if box['column'] else box['x'] - 1
        return box['x'] - 1 - index % max(1, count)
    if a['column'] == b['column']:
        ax, bx = a['x'] + a['width'] - 1, b['x'] + b['width'] - 1
        rail = gutter(a, True)
        return [(ax, ay), (rail, ay), (rail, by), (bx, by)], '<'
    right = a['column'] < b['column']
    ax = a['x'] + a['width'] - 1 if right else a['x']
    bx = b['x'] if right else b['x'] + b['width'] - 1
    if abs(a['column'] - b['column']) == 1:
        rail = gutter(a if right else b, True)
        return [(ax, ay), (rail, ay), (rail, by), (bx, by)], '>' if right else '<'
    ga, gb = gutter(a, right), gutter(b, not right)
    rows = sorted(layout['free_rows'], key=lambda y: (abs(y - (ay + by) / 2), y))
    corridor = rows[index % min(3, len(rows))]
    return [(ax, ay), (ga, ay), (ga, corridor), (gb, corridor), (gb, by), (bx, by)], '>' if right else '<'


def render_cards(snapshot, width, selector_matches):
    graph = build(snapshot, selector_matches)
    nodes, edges = graph['nodes'], graph['edges']
    ordered = sorted(nodes, key=lambda key: (nodes[key]['kind'], nodes[key]['name'], repr(key)))
    ids = {key: 'N%03d' % (i + 1) for i, key in enumerate(ordered)}
    adjacency = defaultdict(set)
    for source, target, _ in edges:
        adjacency[source].add(target)
        adjacency[target].add(source)
    components, visited, isolated = [], set(), []
    for key in ordered:
        if key in visited:
            continue
        if not adjacency[key]:
            isolated.append(key)
            visited.add(key)
            continue
        pending, members = [key], []
        visited.add(key)
        while pending:
            current = pending.pop()
            members.append(current)
            for neighbor in sorted(adjacency[current], key=ids.get):
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        components.append(sorted(members, key=ids.get))
    output = ['CONNECTION TOPOLOGY | CARD MAP (configuration, not observed traffic)',
              'Layout: NETWORK | PODS | OWNERS | REFERENCES (last columns merge on smaller screens).',
              'Arrows point to the target. x = crossing; shared routing is clarified by the E connection list.',
              'Reference-only cards are outside inventory or have a different UID, not confirmed deleted.']
    for number, component in enumerate(components, 1):
        if len(output) >= MAX_LINES:
            break
        layout = card_layout(nodes, component, width)
        canvas = [[' '] * width for _ in range(layout['height'])]
        placements = layout['placements']
        def write(x, y, value):
            for i, char in enumerate(value):
                canvas[y][x + i] = char
        write(0, 0, '+' + '-' * (width - 2) + '+')
        write(2, 0, (' Namespace: %s ' % meta(snapshot['namespace']).get('name', '?'))[:width - 4])
        write(0, layout['height'] - 1, '+' + '-' * (width - 2) + '+')
        for y in range(1, layout['height'] - 1):
            canvas[y][0] = canvas[y][-1] = '|'
        headings = ['NETWORK', 'PODS', 'OWNERS', 'REFERENCES'] if layout['columns'] == 4 else ['NETWORK', 'PODS', 'OWNERS / REFERENCES']
        for col, heading in enumerate(headings):
            write(layout['left'] + col * (layout['card_width'] + layout['gap']), 1, heading)
        truncated = []
        def shorten(value, size):
            value = str(value)
            return value if len(value) <= size else value[:size - 3] + '...'
        for key in component:
            node, box = nodes[key], placements[key]
            x, y, size = box['x'], box['y'], box['width'] - 4
            title = ('* ' if node.get('focused') else '') + ids[key] + ' [' + node['kind'] + ']'
            name = textwrap.wrap(node['name'], size) or ['?']
            if len(name) > 2 or len(title) > size:
                truncated.append(key)
            name = name[:2]
            if len(textwrap.wrap(node['name'], size)) > 2:
                name[-1] = name[-1][:size - 3] + '...'
            info = ('Ref: ' + node['missing']) if node['missing'] else 'Phase: ' + node['status'] if node.get('status') else 'Type: ' + node['service_type'] if node.get('service_type') else ''
            rows = [shorten(title, size)] + name
            if info:
                rows.append(shorten(info, size))
            if 'ready' in node:
                rows.append('Ready: ' + str(node['ready']))
            write(x, y, '+' + '-' * (box['width'] - 2) + '+')
            for i, row in enumerate(rows):
                write(x, y + i + 1, '| ' + row.ljust(size) + ' |')
            write(x, y + box['height'] - 1, '+' + '-' * (box['width'] - 2) + '+')
        links = sorted((edge for edge in edges if edge[0] in placements), key=lambda e: (ids[e[0]], ids[e[1]], e[2]))
        incoming, outgoing = {}, set()
        for i, (source, target, _) in enumerate(links):
            path, arrow = connection_path(layout, source, target, i)
            simple = []
            for point in path:
                if simple and simple[-1] == point:
                    continue
                while len(simple) >= 2 and ((simple[-2][0] == simple[-1][0] == point[0]) or (simple[-2][1] == simple[-1][1] == point[1])):
                    simple.pop()
                simple.append(point)
            path = simple
            for (ax, ay), (bx, by) in zip(path, path[1:]):
                if ax == bx and ay == by:
                    continue
                char = '-' if ay == by else '|'
                for y in range(min(ay, by), max(ay, by) + 1):
                    for x in range(min(ax, bx), max(ax, bx) + 1):
                        old = canvas[y][x]
                        canvas[y][x] = 'x' if {old, char} == {'-', '|'} or old == 'x' else char if old != '+' else '+'
            for x, y in path[1:-1]:
                canvas[y][x] = '+'
            outgoing.add(path[0])
            incoming[path[-1]] = arrow
        for x, y in outgoing:
            canvas[y][x] = '+'
        for (x, y), arrow in incoming.items():
            canvas[y][x] = arrow
        output += ['', 'GROUP %s | Namespace: %s | Nodes: %s | Connections: %s' % (number, meta(snapshot['namespace']).get('name', '?'), len(component), len(links))]
        output += [''.join(row).rstrip() for row in canvas]
        output += ['E%02d: %s -> %s | %s' % (i + 1, ids[source], ids[target], relation) for i, (source, target, relation) in enumerate(links)]
        if truncated:
            output += ['FULL CARD LABELS'] + ['%s [%s] %s' % (ids[key], nodes[key]['kind'], nodes[key]['name']) for key in truncated]
    if isolated:
        output += ['', 'NO CONNECTIONS SHOWN (unlinked in collected data or omitted by limits)']
        output += ['%s [%s] %s' % (ids[key], nodes[key]['kind'], nodes[key]['name']) for key in isolated]
    if not nodes:
        output.append('No visible resources.')
    output += graph['notes']
    wrapped = [part for line in output for part in textwrap.wrap(line, width, replace_whitespace=False) or ['']]
    if len(wrapped) > MAX_LINES:
        notice = textwrap.wrap('Graph display limited to %s lines; use TreeView/Resources.' % MAX_LINES, width)
        return wrapped[:max(0, MAX_LINES - len(notice))] + notice
    return wrapped


def render(snapshot, width, selector_matches):
    # Preserve a readable graph on terminals too narrow for horizontal cards.
    return render_cards(snapshot, width, selector_matches) if width >= 96 else render_rails(snapshot, width, selector_matches)
