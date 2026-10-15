"""Local cluster profiles and read-only Prometheus connection setup."""
import base64
import codecs
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import sys
import select
import termios
import unicodedata
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


def safe(value):
    return ''.join(c if 32 <= ord(c) < 127 else '?' for c in str(value))


def clear():
    import sys
    if sys.stdout.isatty():
        sys.stdout.write('\x1b[2J\x1b[H')
        sys.stdout.flush()


class InputCancelled(Exception):
    pass


def prompt(message):
    """Single-line terminal entry with immediate Escape cancellation."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        value = input(message)
        if '\x1b' in value:
            raise InputCancelled()
        return value
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    mode = termios.tcgetattr(fd)
    mode[3] &= ~(termios.ICANON | termios.ECHO)
    mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    chars = []
    def erase(char):
        width = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        sys.stdout.write('\b \b' * width)
    sys.stdout.write(message); sys.stdout.flush()
    try:
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        while True:
            raw = os.read(fd, 1)
            if not raw or raw == b'\x04':
                raise EOFError()
            if raw == b'\x1b':
                # Ignore arrow/function-key sequences; a standalone Escape cancels.
                if select.select([fd], [], [], .04)[0]:
                    lead = os.read(fd, 1)
                    if lead in (b'[', b'O'):
                        for _ in range(16):
                            if not select.select([fd], [], [], .04)[0]: break
                            byte = os.read(fd, 1)
                            if byte and 64 <= byte[0] <= 126: break
                        continue
                sys.stdout.write('\n'); sys.stdout.flush()
                raise InputCancelled()
            if raw in (b'\n', b'\r'):
                sys.stdout.write('\n'); sys.stdout.flush()
                return ''.join(chars)
            if raw in (b'\x7f', b'\x08'):
                if chars: erase(chars.pop())
                decoder.reset()
            elif raw == b'\x15':
                while chars: erase(chars.pop())
                decoder.reset()
            else:
                decoded = decoder.decode(raw)
                for char in decoded:
                    if char.isprintable() and len(chars) < 2048:
                        chars.append(char); sys.stdout.write(char)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prometheus_config(value):
    if not isinstance(value, dict):
        raise ValueError('Prometheus settings must be a JSON object.')
    allowed = ('endpoint', 'token_env', 'username', 'password_env', 'ca_file')
    if set(value) - set(allowed) - {'verified_at'}:
        raise ValueError('Allowed Prometheus fields: ' + ', '.join(allowed) + '; store credentials in environment variables.')
    result = {key: value.get(key, '') for key in allowed}
    if not all(isinstance(v, str) for v in result.values()):
        raise ValueError('Prometheus settings must contain text values.')
    endpoint = result['endpoint'].strip().rstrip('/')
    if endpoint:
        parts = urllib.parse.urlsplit(endpoint)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('Use an http(s) base URL without embedded credentials, query or fragment.')
        if any(c.isspace() or ord(c) < 32 for c in endpoint):
            raise ValueError('Invalid endpoint URL.')
        _ = parts.port
    result['endpoint'] = endpoint
    for key in ('token_env', 'password_env'):
        if result[key] and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', result[key]):
            raise ValueError('Invalid environment variable name: ' + key)
    if result['token_env'] and (result['username'] or result['password_env']):
        raise ValueError('Choose bearer token OR basic authentication.')
    if bool(result['username']) != bool(result['password_env']):
        raise ValueError('Basic authentication requires username and password_env.')
    verified = value.get('verified_at')
    if verified is not None and not isinstance(verified, str):
        raise ValueError('verified_at must be text or null.')
    result['verified_at'] = verified
    return result


class Store:
    def __init__(self, base):
        self.base = Path(base).resolve()
        self.path = self.base / '.kasciidash' / 'kasciidash_app.config.profiles.json'
        self.original = self.path.read_text(encoding='utf-8') if self.path.exists() else None
        if self.original is None:
            self.data = {'version': 1, 'profiles': [
                {'id': uuid.uuid4().hex, 'alias': path.stem,
                 'kubeconfig': str(path.relative_to(self.base)), 'context': None, 'prometheus': {}}
                for path in sorted((self.base / '.kube').glob('*.conf')) if path.is_file()]}
            self.save()
        else:
            try:
                self.data = json.loads(self.original)
                if not isinstance(self.data, dict) or self.data.get('version') != 1 or not isinstance(self.data.get('profiles'), list):
                    raise ValueError('Unsupported profile registry format.')
                ids, aliases = set(), set()
                for item in self.items:
                    if not isinstance(item, dict):
                        raise ValueError('Invalid profile entry.')
                    for key in ('id', 'alias', 'kubeconfig'):
                        if not isinstance(item.get(key), str) or not item[key].strip():
                            raise ValueError('Missing profile ' + key)
                    if item['id'] in ids or item['alias'].casefold() in aliases:
                        raise ValueError('Duplicate profile ID or alias.')
                    ids.add(item['id']); aliases.add(item['alias'].casefold())
                    if item.get('context') is not None and not isinstance(item['context'], str):
                        raise ValueError('Invalid context.')
                    self.config_path(item)
                    item['prometheus'] = prometheus_config(item.get('prometheus', {}))
            except (ValueError, TypeError) as exc:
                raise ValueError('Cannot load profiles.json; existing file was preserved: ' + str(exc)) from exc

    @property
    def items(self):
        return self.data['profiles']

    def preferred(self):
        """Last successfully opened profile, or the first registered profile."""
        return next((p for p in self.items if p['id'] == self.data.get('last_profile_id')),
                    self.items[0] if self.items else None)

    def remember(self, profile_id):
        if not any(p['id'] == profile_id for p in self.items):
            raise ValueError('Profile no longer registered. Reload Profiles before connecting.')
        if self.data.get('last_profile_id') == profile_id:
            return
        previous = self.data.get('last_profile_id')
        self.data['last_profile_id'] = profile_id
        try:
            self.save()
        except Exception:
            if previous is None:
                self.data.pop('last_profile_id', None)
            else:
                self.data['last_profile_id'] = previous
            raise

    def config_path(self, item):
        path = (self.base / item['kubeconfig']).resolve()
        if not path.is_relative_to((self.base / '.kube').resolve()):
            raise ValueError('Registered kubeconfig must be inside .kube.')
        return path

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_suffix('.lock')
        with open(lock_path, 'a', encoding='utf-8') as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.path.read_text(encoding='utf-8') if self.path.exists() else None
            if current != self.original:
                raise ValueError('Profiles changed in another process. Restart KAsciiDash to reload them.')
            encoded = json.dumps(self.data, ensure_ascii=False, indent=2) + '\n'
            atomic_write(self.path, encoded)
            self.original = encoded

    def update(self, profile_id, **changes):
        index = next(i for i, p in enumerate(self.items) if p['id'] == profile_id)
        old = self.items[index]
        updated = dict(old, **changes)
        alias = updated['alias'].strip()
        if not alias or any(p['id'] != profile_id and p['alias'].casefold() == alias.casefold() for p in self.items):
            raise ValueError('Alias must be nonempty and unique.')
        updated['alias'] = alias
        updated['prometheus'] = prometheus_config(updated.get('prometheus', {}))
        self.items[index] = updated
        try:
            self.save()
        except Exception:
            self.items[index] = old
            raise
        return updated

    def remove(self, profile_id):
        old = list(self.items)
        self.data['profiles'] = [p for p in old if p['id'] != profile_id]
        try:
            self.save()
        except Exception:
            self.data['profiles'] = old
            raise

    def register(self, source, alias, config, context=None):
        # config is kubectl's flattened JSON; embedded certs survive moving directories.
        contexts = [c.get('name') for c in config.get('contexts') or [] if isinstance(c, dict)]
        context = context or config.get('current-context') or (contexts[0] if len(contexts) == 1 else None)
        if not contexts or context not in contexts:
            raise ValueError('Choose an existing context before registering this kubeconfig.')
        alias = alias.strip()
        if not alias or any(p['alias'].casefold() == alias.casefold() for p in self.items):
            raise ValueError('Alias must be nonempty and unique.')
        profile_id = uuid.uuid4().hex
        stem = re.sub(r'[^A-Za-z0-9_.-]', '-', Path(source).stem).strip('.-') or 'cluster'
        relative = '.kube/%s-%s.conf' % (stem, profile_id[:8])
        path = self.base / relative
        atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        item = {'id': profile_id, 'alias': alias, 'kubeconfig': relative, 'context': context, 'prometheus': {}}
        self.items.append(item)
        try:
            self.save()
        except Exception:
            self.items.pop()
            path.unlink()
            raise
        return item


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def prometheus_query(config, query, timeout=8, start=None, end=None, step=None):
    config = prometheus_config(config)
    if not config['endpoint']:
        raise ValueError('Prometheus endpoint is empty.')
    headers = {'Accept': 'application/json'}
    env = config['token_env'] or config['password_env']
    if env:
        secret = os.environ.get(env)
        if not secret:
            raise ValueError('Environment variable is not set: ' + env)
        if '\n' in secret or '\r' in secret:
            raise ValueError('Authentication value contains a line break.')
        headers['Authorization'] = ('Bearer ' + secret if config['token_env'] else
                                    'Basic ' + base64.b64encode((config['username'] + ':' + secret).encode()).decode())
    context = ssl.create_default_context(cafile=config['ca_file'] or None)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    params = {'query': query, 'timeout': str(timeout) + 's'}
    route = '/api/v1/query'
    if start is not None:
        route += '_range'
        params.update(start=start, end=end, step=step)
    request = urllib.request.Request(config['endpoint'] + route + '?' + urllib.parse.urlencode(params), headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError('Prometheus response is too large.')
        value = json.loads(payload)
        data = value.get('data') if isinstance(value, dict) else None
        result = data.get('result') if isinstance(data, dict) else None
        expected = 'matrix' if start is not None else 'vector'
        if not (isinstance(value, dict) and value.get('status') == 'success' and
                isinstance(data, dict) and data.get('resultType') == expected and isinstance(result, list)):
            raise ValueError('Endpoint did not return a valid Prometheus query result.')
    except urllib.error.HTTPError as exc:
        raise ValueError('Prometheus HTTP %s; check endpoint and authentication.' % exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError('Prometheus connection failed: ' + str(exc.reason if hasattr(exc, 'reason') else exc)) from exc
    except (json.JSONDecodeError, AttributeError, TypeError, IndexError) as exc:
        raise ValueError('Endpoint did not return a valid Prometheus JSON response.') from exc
    return value


def check_prometheus(config, timeout=8):
    value = prometheus_query(config, 'vector(1)', timeout)
    result = value['data']['result']
    try:
        valid = bool(result) and result[0].get('value', [None, None])[1] == '1'
    except (AttributeError, TypeError, IndexError):
        valid = False
    if not valid:
        raise ValueError('Endpoint did not return the expected Prometheus query result.')
    return dict(prometheus_config(config), verified_at=dt.datetime.now(dt.timezone.utc).isoformat())


def service_candidates(client):
    services, error = client.call('get', 'services', '-A', '-o', 'json')
    if error:
        return [], [], [error]
    nodes, node_error = client.call('get', 'nodes', '-o', 'json')
    addresses = []
    for kind in ('ExternalIP', 'InternalIP'):
        for node in (nodes or {}).get('items') or []:
            for address in (node.get('status') or {}).get('addresses') or []:
                if address.get('type') == kind and address.get('address') and address['address'] not in addresses:
                    addresses.append(address['address'])
    candidates = []
    for svc in (services or {}).get('items') or []:
        meta, spec, status = svc.get('metadata') or {}, svc.get('spec') or {}, svc.get('status') or {}
        label = '%s/%s' % (meta.get('namespace', 'default'), meta.get('name', '?'))
        likely = 'prometheus' in (label + ' ' + json.dumps(meta.get('labels') or {})).lower()
        for port in spec.get('ports') or []:
            if port.get('protocol', 'TCP') != 'TCP':
                continue
            if not port.get('nodePort') and spec.get('type') != 'LoadBalancer' and not likely:
                continue
            scheme = 'https' if 'https' in str(port.get('name', '')).lower() else 'http'
            hosts = [e.get('ip') or e.get('hostname') for e in (status.get('loadBalancer') or {}).get('ingress') or []]
            host = next((h for h in hosts if h), None)
            target_port = port.get('port')
            if not host and port.get('nodePort'):
                host = addresses[0] if addresses else None
                target_port = port['nodePort']
            elif not host:
                host = spec.get('clusterIP')
            host = '[%s]' % host if host and ':' in host else host
            endpoint = '%s://%s:%s' % (scheme, host, target_port) if host and host != 'None' and target_port else ''
            candidates.append({'service': label, 'type': spec.get('type', 'ClusterIP'), 'port': port.get('port'),
                               'node_port': port.get('nodePort'), 'endpoint': endpoint, 'likely': likely,
                               'port_name': port.get('name') or '-', 'target_port': port.get('targetPort', port.get('port')),
                               'load_balancers': [h for h in hosts if h], 'scheme': scheme})
    return sorted(candidates, key=lambda c: (not c['likely'], {'NodePort': 0, 'LoadBalancer': 1}.get(c['type'], 2), c['service'], c['port'] or 0)), addresses, [node_error] if node_error else []


def select_index(items, title, label, reader=None, question='Number | n/p page | Enter cancel: '):
    read = reader or input
    page = 0
    while True:
        clear(); print(title + '\n' + '-' * 78)
        if not items:
            print('No entries.'); read('Enter to return: '); return None
        for index in range(page * 12, min(len(items), (page + 1) * 12)):
            print('[%s] %s' % (index + 1, safe(label(items[index]))))
        choice = read(question).strip()
        if not choice: return None
        if choice == 'n': page = min((len(items) - 1) // 12, page + 1)
        elif choice == 'p': page = max(0, page - 1)
        elif choice.isdigit() and 1 <= int(choice) <= len(items): return items[int(choice) - 1]


def choose_connection(config):
    connections = config.get('contexts') or []
    if not connections:
        raise ValueError('This conf file contains no cluster connection settings.')
    if len(connections) == 1:
        return connections[0]
    servers = {entry['name']: (entry.get('cluster') or {}).get('server', '-') for entry in config.get('clusters') or []}
    def label(entry):
        connection = entry.get('context') or {}
        cluster = connection.get('cluster', '-')
        return 'Cluster: %s | Account: %s | Namespace: %s | API: %s | Name: %s' % (
            cluster, connection.get('user', '-'), connection.get('namespace') or 'default',
            servers.get(cluster, '-'), entry.get('name', '-'))
    return select_index(connections,
        'This conf file contains multiple cluster/account connections.\nChoose the connection you want to use.',
        label, reader=prompt, question='Connection number (example: 1) | n/p page | Enter/Esc cancel: ')


def register_prompt(store, client_factory):
    clear(); print('KAsciiDash | Register profile\n' + '-' * 78)
    raw = input('Kubeconfig path (Enter cancel): ').strip()
    if not raw: return None
    source = Path(raw).expanduser().resolve()
    if not source.is_file(): raise ValueError('Kubeconfig file not found.')
    existing = next((p for p in store.items if store.config_path(p) == source), None)
    if existing: return existing
    client = client_factory(source, None)
    config, error = client.call('config', 'view', '--raw', '--flatten', '-o', 'json')
    if error or not isinstance(config, dict): raise ValueError('Cannot import kubeconfig: ' + (error or 'Invalid config'))
    contexts = config.get('contexts') or []
    context = config.get('current-context') or (contexts[0].get('name') if len(contexts) == 1 else None)
    if not context:
        selected = choose_connection(config)
        if not selected: return None
        context = selected['name']
    alias = input('Alias [%s]: ' % safe(source.stem)).strip() or source.stem
    return store.register(source, alias, config, context)


def endpoint_url(scheme, host, port):
    host = '[%s]' % host if ':' in host and not host.startswith('[') else host
    return '%s://%s:%s' % (scheme, host, port)


def service_endpoint(candidate, addresses):
    options = []
    for host in candidate.get('load_balancers', []):
        options.append(('LoadBalancer ' + host, endpoint_url(candidate['scheme'], host, candidate['port'])))
    if candidate.get('node_port'):
        for host in addresses:
            options.append(('Node ' + host, endpoint_url(candidate['scheme'], host, candidate['node_port'])))
    if not options and candidate.get('endpoint'):
        options.append(('Cluster IP (requires a route from this Mac)', candidate['endpoint']))
    if not options:
        print('No address available. Use advanced URL input or check node-list permissions.')
        prompt('Enter to return: ')
        return None
    if len(options) == 1:
        return options[0][1]
    selected = select_index(options, 'Step 2/3 - Choose node / address | ' + safe(candidate['service']),
                            lambda item: item[0] + ' -> ' + item[1], reader=prompt)
    return selected[1] if selected else None


def configure_auth(settings):
    print('Choose none if this endpoint does not require additional authentication.')
    print('Bearer/basic options use an environment variable name, not the secret value.')
    auth = prompt('Auth: n none / b bearer env / p basic env [n]: ').strip() or 'n'
    if auth not in ('n', 'b', 'p'): raise ValueError('Unknown authentication mode.')
    settings.update(token_env='', username='', password_env='')
    if auth == 'b':
        settings['token_env'] = prompt('Bearer token environment variable name: ').strip()
        if not settings['token_env']: raise ValueError('Bearer token environment variable name is required.')
    elif auth == 'p':
        settings['username'] = prompt('Username: ').strip()
        settings['password_env'] = prompt('Password environment variable name: ').strip()
    settings['ca_file'] = prompt('Custom CA file path (Enter system trust): ').strip()
    if settings['ca_file']: settings['ca_file'] = str(Path(settings['ca_file']).expanduser().resolve())
    return settings


def connection_prompt(store, profile, client_factory, timeout):
    try:
        return _connection_prompt(store, profile, client_factory, timeout)
    except InputCancelled:
        return None


def _connection_prompt(store, profile, client_factory, timeout):
    old = profile.get('prometheus') or {}
    client = client_factory(store.config_path(profile), profile.get('context'))
    print('Loading Services and node addresses for ' + safe(profile['alias']) + '...')
    candidates, addresses, errors = service_candidates(client)
    page = 0
    while True:
        clear(); print('KAsciiDash | Prometheus / ' + safe(profile['alias']) + '\n' + '-' * 78)
        print('Config file: ' + safe(Path(profile['kubeconfig']).name))
        print('Saved connection: ' + safe(old.get('endpoint') or '(not configured)'))
        print('Step 1/3 - Choose a Service port. Enter its number (example: 1).')
        print('No IP or URL required. * marks likely Prometheus Services. Esc: cancel.\n')
        for index in range(page * 10, min(len(candidates), (page + 1) * 10)):
            candidate = candidates[index]
            print('[%s] %s%s | %s' % (index + 1, '* ' if candidate['likely'] else '',
                                    safe(candidate['service']), candidate['type']))
            print('    Service port: %s (%s) -> targetPort: %s | NodePort: %s' %
                  (candidate['port'], safe(candidate['port_name']), safe(candidate['target_port']), candidate['node_port'] or '-'))
        if not candidates:
            print('No connection candidates found in this profile.')
        for error in errors:
            print('Discovery: ' + safe(error))
        print('\nNumber: select Service port | n/p: page | r: reload')
        print('Advanced: u URL | j JSON | t test saved | x disconnect')
        choice = prompt('Service port number (Enter/q back): ').strip().lower()
        if not choice or choice == 'q': return
        if choice in ('n', 'p'):
            page = min(max(0, (len(candidates) - 1) // 10), max(0, page + (1 if choice == 'n' else -1)))
            continue
        if choice == 'r':
            candidates, addresses, errors = service_candidates(client)
            page = 0
            continue
        if choice == 'x': store.update(profile['id'], prometheus={}); return
        if choice == 't':
            settings = old
        elif choice == 'j':
            raw = prompt('Prometheus JSON path (Enter cancel): ').strip()
            if not raw: continue
            source = Path(raw).expanduser().resolve()
            settings = prometheus_config(json.loads(source.read_text(encoding='utf-8')))
            if settings['ca_file']:
                settings['ca_file'] = str((source.parent / Path(settings['ca_file']).expanduser()).resolve())
        elif choice == 'u' or choice.isdigit() and 1 <= int(choice) <= len(candidates):
            candidate = None
            if choice == 'u':
                endpoint = prompt('Full URL, e.g. http://node-ip:nodePort (Enter cancel): ').strip()
            else:
                candidate = candidates[int(choice) - 1]
                endpoint = service_endpoint(candidate, addresses)
            if not endpoint: continue
            try:
                endpoint = prometheus_config({'endpoint': endpoint})['endpoint']
            except ValueError:
                print('Enter a full URL such as http://10.0.0.10:30900, or choose a Service number instead.')
                prompt('Enter to return to Services: ')
                continue
            # Credentials belong to the saved endpoint, never to a newly selected Service.
            settings = dict(old) if endpoint == old.get('endpoint') else {'endpoint': endpoint}
            settings['endpoint'] = endpoint
            while True:
                print('\nStep 3/3 - Connection address: ' + safe(settings['endpoint']))
                print('The address is ready. Press Enter to test and save; Esc to cancel.')
                action = prompt('Enter: connect | a: advanced authentication | h: HTTP/HTTPS | q: back: ').strip().lower()
                if action == 'q': break
                if action == 'a':
                    configure_auth(settings)
                elif action == 'h':
                    parts = urllib.parse.urlsplit(settings['endpoint'])
                    settings = {'endpoint': urllib.parse.urlunsplit(parts._replace(scheme='https' if parts.scheme == 'http' else 'http'))}
                elif not action:
                    break
                else:
                    print('Choose Enter, a, h or q.')
            if action == 'q': continue
        else:
            print('Choose a listed Service port or an advanced option.')
            continue
        print('Checking Prometheus API...')
        checked = check_prometheus(settings, timeout)
        store.update(profile['id'], prometheus=checked)
        print('Connection verified and saved for ' + safe(profile['alias']) + '.')
        prompt('Enter to return: ')
        return


def manage(store, current, client_factory, timeout=8):
    """Return a selected profile or None on quit; usable without cluster connectivity."""
    while True:
        clear(); print('KAsciiDash | Settings / Profiles\n' + '-' * 78)
        width = min(120, max(60, shutil.get_terminal_size((120, 40)).columns))
        alias_width, file_width = (20, 32) if width >= 90 else (15, 20)
        widths = [4, alias_width, file_width, width - 17 - alias_width - file_width]
        border = '+' + '+'.join('-' * (w + 2) for w in widths) + '+'
        def row(values):
            cells = []
            for value, size in zip(values, widths):
                value = safe(value)
                cells.append((value if len(value) <= size else value[:size - 3] + '...').ljust(size))
            return '| ' + ' | '.join(cells) + ' |'
        print(border)
        print(row(['#', 'ALIAS', 'CONF FILE', 'PROMETHEUS']))
        print(border)
        for index, item in enumerate(store.items, 1):
            print(row([('*' if current and current['id'] == item['id'] else '') + str(index), item['alias'],
                       Path(item['kubeconfig']).name,
                       (item.get('prometheus') or {}).get('endpoint') or '(not configured)']))
        print(border)
        if not store.items: print('No profiles. Use a to register a kubeconfig.')
        print('\na: register conf file | v: details | e: rename | p: Prometheus | d: delete')
        print('Advanced: k selects a different cluster/account inside a multi-connection conf file.')
        choice = input('Number: connect | Enter: return/connect first | q: quit: ').strip().lower()
        try:
            if choice == 'q': return None
            if not choice:
                if current:
                    found = next((p for p in store.items if p['id'] == current['id']), None)
                    if found: return found
                if store.items: return store.items[0]
            elif choice.isdigit() and 1 <= int(choice) <= len(store.items): return store.items[int(choice) - 1]
            elif choice == 'a':
                registered = register_prompt(store, client_factory)
                if registered and len(store.items) == 1: return registered
            elif choice in ('v', 'e', 'k', 'p', 'd'):
                selected = select_index(store.items, 'Choose a registered conf file by number.',
                    lambda p: p['alias'] + ' | ' + Path(p['kubeconfig']).name)
                if not selected: continue
                if choice == 'v':
                    clear(); print('KAsciiDash | Profile details\n' + '-' * 78)
                    print('Display name: ' + safe(selected['alias']))
                    print('Conf file: ' + safe(store.config_path(selected)))
                    print('Connection inside this file: ' + safe(selected.get('context') or 'Use the default connection saved in the file'))
                    print('A connection identifies the cluster/account within the conf file.')
                    print('Prometheus URL: ' + safe((selected.get('prometheus') or {}).get('endpoint') or '(not configured)'))
                    print('Last connection check: ' + safe((selected.get('prometheus') or {}).get('verified_at') or '(not checked)'))
                    input('Enter to return: ')
                elif choice == 'e':
                    alias = input('New display name [%s] (Enter keeps current): ' % safe(selected['alias'])).strip() or selected['alias']
                    store.update(selected['id'], alias=alias)
                elif choice == 'k':
                    config, error = client_factory(store.config_path(selected), None).call('config', 'view', '-o', 'json')
                    if error: raise ValueError(error)
                    context = choose_connection(config or {})
                    if len((config or {}).get('contexts') or []) == 1:
                        print('This conf file has only one connection. No selection is needed.')
                        input('Enter to return: ')
                    elif context and context['name'] != selected.get('context'):
                        store.update(selected['id'], context=context['name'], prometheus={})
                elif choice == 'p': connection_prompt(store, selected, client_factory, timeout)
                elif input('Delete profile registration? Kubeconfig file is kept. [y/N]: ').strip().lower() == 'y':
                    store.remove(selected['id'])
        except InputCancelled:
            continue
        except (ValueError, OSError) as exc:
            print('KAsciiDash: ' + safe(exc)); input('Enter to continue: ')
