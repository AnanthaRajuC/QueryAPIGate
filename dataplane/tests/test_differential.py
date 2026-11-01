"""The gate (ADR 0028, section 5): the same generated calls, sent to the Python server directly and through the data
plane in front of it, must come back the same - status, headers, body - and leave the same history. Any difference
fails it, a refusal included.

One gunicorn serves both: the direct call and the call the data plane forwards or serves meet the same store, the
same caches and the same databases, so the only thing that can differ is the data plane. Keys of every kind (plain,
masked, table-restricted, both, by name, IP-restricted, rotated, expired, with their own row and time limits) call
saved queries shaped like the benchmarks' and many others, with valid, invalid and hostile parameters, paging and
timeouts; partway through, grants and limits change, a key is revoked and a query gets a new version.

The proxied run does it all again behind two trusted proxies (QUERYAPIGATE_TRUST_PROXY=2) with HSTS on, each call with
X-Forwarded-For and X-Forwarded-Proto of every shape - plain, IPv6, quoted, empty items, ports, too few values - so
the client address that IP-restricted keys are judged by, and the scheme HSTS depends on, are found as ProxyFix finds
them (ADR 0030, section 5).

    cargo build --release --manifest-path dataplane/Cargo.toml
    QUERYAPIGATE_DATAPLANE_TEST_POSTGRES=127.0.0.1:55435:postgres:pw:postgres \\
    QUERYAPIGATE_DATAPLANE_TEST_MYSQL=127.0.0.1:55436:root:pw:shop \\
    .venv/bin/python dataplane/tests/test_differential.py

DIFFERENTIAL_CALLS (default 1500 per database) and DIFFERENTIAL_SEED (default 28) set the run; a failure prints the
seed and the call, so it can be replayed exactly.
"""
import json
import os
import random
import sys
import tempfile
import time
import unittest
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_forwarding import IGNORED, Pair, binary, request  # noqa: E402
from test_queries import details  # noqa: E402

ADMIN = 'differential-admin-key'
CALLS = int(os.environ.get('DIFFERENTIAL_CALLS', '1500'))
SEED = int(os.environ.get('DIFFERENTIAL_SEED', '28'))
COMPARED_HEADERS = IGNORED | {'server'}
TIMING = ('executed_at', 'duration_ms', 'serialization_ms')
HOSTILE = ["O'Brien", "' OR '1'='1", '\\', "\\'", '%', '_', '%s', ':id', '{id}', '--', '/*', 'é😀', '汉字', '',
           ' ', 'NULL', chr(0xbf) + "'", 'x' * 300, '1; DROP TABLE customer', '‮', '\x00', '1e400', 'nan']

FIRST = ["Jane", "John", "Zoë", "D'Arcy", "Łukasz", "李", "Ana María", "Ohm"]
LAST = ["Smith", "O'Brien", "Müller", "García", "Nguyễn", "Ivanov", "Brown", "Østergaard"]


def rows():
    """The same data for both databases: NULLs, quotes, Unicode, money, dates."""
    rng = random.Random(7)
    customers = []
    for i in range(1, 61):
        customers.append((i, FIRST[i % 8], LAST[(i * 3) % 8], None if i % 9 == 0 else f'user{i}@example.com',
                          f'+44 7700 9{i:05d}', ['Leeds', 'Pune', 'Lyon', None][i % 4],
                          f'{rng.randint(-500, 9000)}.{i % 100:02d}',
                          ['GC', 'BC'][i % 2], f'2020-0{1 + i % 9}-{1 + i % 28:02d} 1{i % 10}:0{i % 6}:3{i % 10}',
                          f'19{60 + i % 40}-0{1 + i % 9}-{1 + i % 28:02d}', i % 3 != 0, None if i % 5 else f'note {i}',
                          f's3cret-{i}', json.dumps({'lang': ['en', 'fr', 'hi'][i % 3], 'n': i, 'tags': ['a', 'é']}),
                          bytes([i % 256, 0xff, 0x41])))
    orders = [(o, 1 + (o * 7) % 60, f'2024-{1 + o % 12:02d}-{1 + o % 28:02d} 0{o % 10}:00:00',
               None if o % 4 == 0 else o % 10, f'{(o * 37) % 900}.{o % 100:02d}', ['new', 'paid', 'shipped'][o % 3])
              for o in range(1, 241)]
    lines = [(o, n, 1 + (o * n) % 40, 1 + (o + n) % 9, f'{(o * n) % 300}.5', None if (o + n) % 6 == 0 else
              f'2024-{1 + o % 12:02d}-{1 + n % 28:02d}') for o in range(1, 241) for n in range(1, 1 + o % 4)]
    items = [(i, f'Item {i} "{LAST[i % 8]}"', f'{i * 3}.99', f'data-{i}' * (i % 3)) for i in range(1, 41)]
    history = [(1 + (h * 11) % 60, f'2024-0{1 + h % 9}-{1 + h % 28:02d} 12:00:00', f'{h * 3}.25', f'pay {h}')
               for h in range(1, 121)]
    return customers, orders, lines, items, history


def load(dialect, settings):
    customers, orders, lines, items, history = rows()
    if dialect == 'postgres':
        import psycopg2
        conn = psycopg2.connect(host=settings['host'], port=settings['port'], user=settings['user'],
                                password=settings['password'], dbname=settings['database'])
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute('DROP SCHEMA IF EXISTS billing CASCADE; CREATE SCHEMA billing; '
                    'DROP TABLE IF EXISTS customer, orders, order_line, item CASCADE')
        cur.execute('CREATE TABLE customer (id int PRIMARY KEY, first text, last text, email text, phone text, '
                    'city text, balance numeric(12,2), credit char(2), since timestamp, born date, active boolean, '
                    'notes text, secret text, prefs jsonb, photo bytea)')
        cur.execute('CREATE TABLE orders (id int PRIMARY KEY, customer_id int, entry_d timestamp, carrier_id int, '
                    'total numeric(10,2), status varchar(10))')
        cur.execute('CREATE TABLE order_line (order_id int, number int, item_id int, quantity int, amount numeric, '
                    'delivery_d date, PRIMARY KEY (order_id, number))')
        cur.execute('CREATE TABLE item (id int PRIMARY KEY, name text, price numeric, data text, tags int[])')
        cur.execute('CREATE TABLE billing.history (customer_id int, h_date timestamp, amount numeric, data text)')
        cur.executemany('INSERT INTO customer VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', customers)
        cur.executemany('INSERT INTO orders VALUES (%s,%s,%s,%s,%s,%s)', orders)
        cur.executemany('INSERT INTO order_line VALUES (%s,%s,%s,%s,%s,%s)', lines)
        cur.executemany('INSERT INTO item VALUES (%s,%s,%s,%s,%s)', [(*i, [i[0], i[0] * 2]) for i in items])
        cur.executemany('INSERT INTO billing.history VALUES (%s,%s,%s,%s)', history)
    else:
        import mysql.connector
        conn = mysql.connector.connect(**settings, autocommit=True)
        cur = conn.cursor()
        for statement in ('DROP DATABASE IF EXISTS billing', 'CREATE DATABASE billing',
                          'DROP TABLE IF EXISTS customer, orders, order_line, item',
                          'CREATE TABLE customer (id int PRIMARY KEY, first varchar(40), last varchar(40), email '
                          'varchar(80), phone varchar(30), city varchar(30), balance decimal(12,2), credit char(2), '
                          'since datetime, born date, active tinyint(1), notes text, secret varchar(40), prefs json, '
                          'photo varbinary(10)) CHARACTER SET utf8mb4',
                          'CREATE TABLE orders (id int PRIMARY KEY, customer_id int, entry_d datetime, carrier_id int, '
                          'total decimal(10,2), status varchar(10))',
                          'CREATE TABLE order_line (order_id int, number int, item_id int, quantity int, amount '
                          'decimal(10,1), delivery_d date, PRIMARY KEY (order_id, number))',
                          'CREATE TABLE item (id int PRIMARY KEY, name varchar(80), price decimal(10,2), data text) '
                          'CHARACTER SET utf8mb4',
                          'CREATE TABLE billing.history (customer_id int, h_date datetime, amount decimal(10,2), '
                          'data varchar(40))'):
            cur.execute(statement)
        cur.executemany('INSERT INTO customer VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', customers)
        cur.executemany('INSERT INTO orders VALUES (%s,%s,%s,%s,%s,%s)', orders)
        cur.executemany('INSERT INTO order_line VALUES (%s,%s,%s,%s,%s,%s)', lines)
        cur.executemany('INSERT INTO item VALUES (%s,%s,%s,%s)', items)
        cur.executemany('INSERT INTO billing.history VALUES (%s,%s,%s,%s)', history)
    conn.close()


def queries(dialect):
    """name -> (sql, parameters): the benchmarks' shapes, and the features around them."""
    pg = dialect == 'postgres'
    json_lang = "prefs->>'lang'" if pg else "JSON_UNQUOTE(JSON_EXTRACT(prefs, '$.lang'))"
    year = 'EXTRACT(YEAR FROM born)' if pg else 'YEAR(born)'
    found = {
        'customer': ('SELECT id, first, last, email, phone, balance, credit, since FROM customer WHERE id = :id',
                     {'id': 'int'}),
        'by_last': ('SELECT id, first, last, email FROM customer WHERE last = :last ORDER BY first, id',
                    {'last': 'str'}),
        'order_status': ('SELECT c.first, c.last, c.balance, o.id, o.entry_d, o.carrier_id FROM customer c '
                         'JOIN orders o ON o.customer_id = c.id WHERE c.id = :id AND o.id = (SELECT MAX(o2.id) '
                         'FROM orders o2 WHERE o2.customer_id = c.id)', {'id': 'int'}),
        'order_lines': ('SELECT c.last, o.id, o.entry_d, ol.number, i.name, ol.quantity, ol.amount, ol.delivery_d '
                        'FROM customer c JOIN orders o ON o.customer_id = c.id '
                        'JOIN order_line ol ON ol.order_id = o.id '
                        'JOIN item i ON i.id = ol.item_id WHERE c.id = :id ORDER BY o.id, ol.number', {'id': 'int'}),
        'payments': ('SELECT c.last, h.h_date, h.amount, h.data FROM customer c JOIN billing.history h '
                     'ON h.customer_id = c.id WHERE c.id = :id ORDER BY h.h_date, h.amount', {'id': 'int'}),
        'low_stock': ('SELECT COUNT(DISTINCT item_id) AS low FROM order_line WHERE quantity < :q', {'q': 'int'}),
        'everyone': ('SELECT * FROM customer ORDER BY id', {}),
        'top_n': ('SELECT id, last FROM customer ORDER BY id LIMIT :n OFFSET 3', {'n': 'int'}),
        'search': ('SELECT id, last, city FROM customer WHERE last LIKE :pattern ORDER BY id', {'pattern': 'str'}),
        'rich': ('SELECT id, balance, active FROM customer WHERE balance > :min AND active = :active ORDER BY id',
                 {'min': 'float', 'active': 'bool'}),
        'by_credit': ('SELECT id, credit FROM customer WHERE credit = :credit ORDER BY id',
                      {'credit': {'type': 'str', 'enum': ['GC', 'BC'], 'default': 'GC'}}),
        'in_city': ('SELECT id, city FROM customer WHERE (:city IS NULL OR city = :city) ORDER BY id',
                    {'city': {'type': 'str', 'required': False}}),
        'stats': ('SELECT credit, COUNT(*) AS n, SUM(balance) AS total, AVG(balance) AS mean, MIN(born) AS oldest '
                  'FROM customer GROUP BY credit ORDER BY credit', {}),
        'ranked': ('SELECT id, balance, ROW_NUMBER() OVER (ORDER BY balance DESC, id) AS place FROM customer '
                   'ORDER BY place', {}),
        'with_cte': ('WITH big AS (SELECT customer_id, SUM(total) AS spent FROM orders GROUP BY customer_id) '
                     'SELECT c.id, c.last, big.spent FROM big JOIN customer c ON c.id = big.customer_id '
                     'ORDER BY big.spent DESC, c.id', {}),
        'langs': (f'SELECT id, {json_lang} AS lang, prefs FROM customer WHERE id <= :id ORDER BY id', {'id': 'int'}),
        'years': (f'SELECT {year} AS y, COUNT(*) AS n FROM customer GROUP BY {year} ORDER BY y', {}),
        'photos': ('SELECT id, photo, notes FROM customer WHERE id BETWEEN :a AND :b ORDER BY id',
                   {'a': 'int', 'b': {'type': 'int', 'default': 5}}),
        'items': ('SELECT * FROM item WHERE price < :p ORDER BY id', {'p': 'float'}),
        'legacy': ('SELECT id, last FROM customer WHERE id = {id}', {'id': 'int'}),
        'spenders': ('SELECT * FROM (SELECT customer_id, SUM(total) AS spent FROM orders GROUP BY customer_id) s '
                     'JOIN customer c ON c.id = s.customer_id ORDER BY s.spent DESC, c.id', {}),
        'secretive': ('SELECT id, secret, notes, email FROM customer ORDER BY id', {}),
    }
    if pg:
        found['whole_table'] = ('TABLE item', {})
    else:
        found['tables'] = ('SHOW TABLES', {})
    return found


KEYS = {  # name -> grants
    'plain': {'connections': ['shop']},
    'masked': {'connections': ['shop'], 'mask': [{'column': 'customer.email', 'action': 'hash'},
                                                 {'column': 'customer.phone', 'action': 'mask'},
                                                 {'column': 'customer.secret', 'action': 'drop'},
                                                 {'column': 'customer.balance', 'action': 'hash'},
                                                 {'column': '*.notes', 'action': 'null'}]},
    'restricted': {'connections': ['shop'], 'allowed_tables': ['customer', 'orders']},
    'both': {'connections': ['shop'], 'allowed_tables': ['customer', 'orders', 'order_line', 'item',
                                                          'billing.history'],
             'mask': [{'column': 'customer.email', 'action': 'mask'}]},
    'by_name': {'connections': [], 'queries': ['customer', 'everyone']},
    'office': {'connections': ['shop'], 'allowed_ips': ['127.0.0.0/8']},
    'elsewhere': {'connections': ['shop'], 'allowed_ips': ['10.0.0.0/8']},
    'expired': {'connections': ['shop'], 'expires_at': '2020-01-01'},
    'rotated': {'connections': ['shop']},
    'v6': {'connections': ['shop'], 'allowed_ips': ['2001:db8::/32']},
    # a key's own limits (ADR 0011): served with the plan's, the rows changed partway through
    'capped': {'connections': ['shop'], 'max_rows': 3},
    'timed': {'connections': ['shop'], 'query_timeout': 2},
}
# Behind two proxies the client is the second value from the right: plainly written ones, mostly, and every shape
# Python could read some other way (the data plane forwards those)
FORWARDED_FOR = ['10.1.2.3, 198.51.100.1', '203.0.113.7, 10.1.2.3, 198.51.100.1', '127.0.0.5, 198.51.100.2',
                 '2001:db8::5, 10.0.0.2', '10.0.0.2, 2001:db8::5', '192.0.2.44, 127.0.0.1', ' 10.1.2.3 ,\t127.0.0.9',
                 '10.1.2.3', '1.1.1.1, 2.2.2.2, 3.3.3.3, 4.4.4.4']
ODD_FORWARDED_FOR = ['2001:DB8::5, 1.1.1.1', '::ffff:10.0.0.1, 1.1.1.1', '10.0.0.1:443, 1.1.1.1',
                     '[2001:db8::5], 1.1.1.1', '"10.0.0.1", 1.1.1.1', 'unknown, 1.1.1.1', '', ',', '10.0.0.1,,1.1.1.1',
                     '10.0.0.1, 1.1.1.1,', 'garbage', '010.0.0.1, 1.1.1.1', '10.0.0.1 10.0.0.2, 1.1.1.1']
FORWARDED_PROTO = ['https, http', 'http, https', 'https, https', 'http, http', 'https', ' https , http']
ODD_FORWARDED_PROTO = ['wss, http', 'HTTPS, http', '"https", http', 'https, http,', '', 'ftp, https']
# A host werkzeug can't encode as IDNA is a 400: the client's own Host, or behind proxies the one ProxyFix takes
FORWARDED_HOST = ['api.example.com, gateway.internal', 'api.example.com:8443, lb', 'a b, c', ', ', 'x:99999, y',
                  '[::1]:80, lb', 'é.example, lb', '..., lb', 'api.example.com']
HOSTS = ['a..b', '...', 'api.example.com', 'x' * 64 + '.com', 'a.b.:8080', '[::1]:5000']


def values(spec, rng):
    """A value for a parameter, mostly valid, sometimes not."""
    kind = spec if isinstance(spec, str) else spec.get('type')
    roll = rng.random()
    if roll < 0.08:
        return rng.choice(HOSTILE)
    if roll < 0.12:
        return rng.choice(['x', '', '1.5', 'true', '-1', '99999999999999999999'])
    if kind == 'int':
        return str(rng.choice([1, 2, 3, 5, 9, 17, 30, 45, 59, 60, 61, 0, -3, 1000]))
    if kind == 'float':
        return rng.choice(['0', '100.5', '-1000', '5000', '1e3', '0.001'])
    if kind == 'bool':
        return rng.choice(['true', 'false', '1', '0', 'yes', 'no', 'TRUE'])
    return rng.choice(LAST + ['%e%', 'O%', '_r%', 'GC', 'BC', 'Leeds', 'Pune', 'nowhere'])


class DifferentialTests(unittest.TestCase):
    dialect = None
    env = None
    proxied = False

    @classmethod
    def setUpClass(cls):
        cls.settings = details(cls.env)
        load(cls.dialect, cls.settings)
        cls.tmp = tempfile.TemporaryDirectory()
        proxies = {'QUERYAPIGATE_TRUST_PROXY': '2', 'QUERYAPIGATE_HSTS_MAX_AGE': '600'} if cls.proxied else {}
        cls.pair = Pair(Path(cls.tmp.name), {'QUERYAPIGATE_API_KEY': ADMIN, 'QUERYAPIGATE_MASK_KEY': 'diff-mask',
                                             'QUERYAPIGATE_MAX_PAGE_SIZE': '50', **proxies})
        cls.python = ('127.0.0.1', cls.pair.python_port)
        cls.admin = {'X-API-Key': ADMIN}
        status, _h, body = cls.api('POST', '/api/v1/connections', {'name': 'shop', 'db': cls.dialect, **cls.settings,
                                                                    'active': True})
        assert status == 201, body
        cls.queries = queries(cls.dialect)
        for name, (sql, parameters) in cls.queries.items():
            status, _h, body = cls.api('POST', '/api/v1/queries', {'name': name, 'connection_name': 'shop',
                                                                    'description': name, 'sql': sql,
                                                                    'parameters': parameters, 'publish': True})
            assert status == 201, (name, body)
        cls.secrets = {}
        for name, grants in KEYS.items():
            status, _h, body = cls.api('POST', '/api/v1/api-keys', {'name': name, **grants})
            assert status == 201, (name, body)
            cls.secrets[name] = json.loads(body)['secret']
        # A rotated key's old secret, still in its grace period
        status, _h, body = cls.api('POST', '/api/v1/api-keys/rotated/rotate', {'grace_minutes': 60})
        assert status == 200, body

    @classmethod
    def tearDownClass(cls):
        cls.pair.stop()
        cls.tmp.cleanup()

    @classmethod
    def api(cls, method, path, body=None):
        return request(*cls.python, method, path, cls.admin, body)

    def call(self, rng):
        """One generated call: (method, path, headers, body)."""
        name = rng.choice(list(self.queries))
        _sql, parameters = self.queries[name]
        args, body_params = [], {}
        for param, spec in parameters.items():
            if rng.random() < 0.06:
                continue  # missing
            value = values(spec, rng)
            if rng.random() < 0.3:
                body_params[param] = value
            else:
                args.append((param, value))
        if rng.random() < 0.05:
            args.append(('unknown', 'x'))
        if rng.random() < 0.3:
            args.append(('page', rng.choice(['1', '2', '3', '0', 'x', '7'])))
        if rng.random() < 0.3:
            args.append(('page_size', rng.choice(['1', '5', '10', '50', '51', '-1'])))
        if rng.random() < 0.05:
            args.append(('format', rng.choice(['csv', 'JSON', 'json', 'xml'])))
        if rng.random() < 0.05:
            args.append(('timeout', rng.choice(['5', '0.5', 'x', '-1'])))
        key = rng.choices(list(KEYS) + ['wrong'], weights=[30, 20, 15, 10, 8, 4, 2, 2, 5, 3, 8, 6, 2])[0]
        secret = self.secrets.get(key, 'not-a-key')
        headers = {'X-API-Key': secret, **(self.forwarding(rng) if self.proxied else {})}
        if rng.random() < 0.04:
            headers['Host'] = rng.choice(HOSTS)
        path = f'/q/{name}' + (('?' + urllib.parse.urlencode(args)) if args else '')
        if body_params and rng.random() < 0.7:
            # JSON numbers for numeric strings, half the time: the body keeps its types
            typed = {k: (json.loads(v) if rng.random() < 0.5 and v.lstrip('-').replace('.', '', 1).isdigit() else v)
                     for k, v in body_params.items()}
            return 'POST', path, headers, {'params': typed}
        for param, value in body_params.items():  # otherwise in the URL after all
            path += ('&' if '?' in path else '?') + urllib.parse.urlencode({param: value})
        return 'GET', path, headers, None

    @staticmethod
    def forwarding(rng):
        """The headers two proxies in front would send, or a client's own."""
        headers = {}
        roll = rng.random()
        if roll < 0.7:
            headers['X-Forwarded-For'] = rng.choice(FORWARDED_FOR)
        elif roll < 0.85:
            headers['X-Forwarded-For'] = rng.choice(ODD_FORWARDED_FOR)
        roll = rng.random()
        if roll < 0.7:
            headers['X-Forwarded-Proto'] = rng.choice(FORWARDED_PROTO)
        elif roll < 0.8:
            headers['X-Forwarded-Proto'] = rng.choice(ODD_FORWARDED_PROTO)
        if rng.random() < 0.05:  # gunicorn's own scheme headers: not trusted from any peer behind the data plane
            headers['X-Forwarded-Ssl'] = rng.choice(['on', 'off'])
        if rng.random() < 0.2:
            headers['X-Forwarded-Host'] = rng.choice(FORWARDED_HOST)
        return headers

    def mutate(self, step):
        """Partway through: change what calls see - each seen by both at once, or, with the metadata cache on, once
        its TTL has passed: the data plane is another instance, which metacache.py lets see a change that much later."""
        if step in (CALLS // 4, CALLS // 3, CALLS // 2, 2 * CALLS // 3, 3 * CALLS // 4):
            self.change(step)
            time.sleep(float(os.environ.get('QUERYAPIGATE_METADATA_CACHE_TTL') or 0))

    def change(self, step):
        if step == CALLS // 4:
            self.api('PATCH', '/api/v1/api-keys/masked', {'mask': [{'column': 'customer.email', 'action': 'null'},
                                                                   {'column': 'customer.last', 'action': 'mask'}]})
        elif step == CALLS // 3:
            self.api('PATCH', '/api/v1/api-keys/capped', {'max_rows': 7})
        elif step == CALLS // 2:
            self.api('PATCH', '/api/v1/api-keys/restricted', {'allowed_tables': ['customer']})
        elif step == 2 * CALLS // 3:
            status, _h, body = self.api('POST', '/api/v1/queries/by_last/versions', {
                'sql': 'SELECT id, last, city FROM customer WHERE last = :last ORDER BY id DESC',
                'parameters': {'last': 'str'}, 'description': 'v2', 'publish': True})
            assert status == 201, body
        elif step == 3 * CALLS // 4:
            self.api('PATCH', '/api/v1/api-keys/both', {'active': False})

    def served(self):
        return (self.pair.home / 'dataplane.log').read_text().count('Executing on ')

    def test_the_data_plane_answers_every_call_as_python_does(self):
        rng = random.Random(SEED)
        mismatches, outcomes = [], Counter()
        self.forwarded_ok = Counter()  # calls Python answered 200 that the data plane left to it: why so?
        calls = [self.call(rng) for _ in range(CALLS)]
        for method, path, headers, body in calls:  # warm: plans, and each key's recorded use
            request('127.0.0.1', self.pair.port, method, path, {**headers, 'X-Request-Id': 'warm'}, body)
        for step, (method, path, headers, body) in enumerate(calls):
            self.mutate(step)
            rid = f'diff-{step}'
            headers = {**headers, 'X-Request-Id': rid}
            before = self.served()
            through = request('127.0.0.1', self.pair.port, method, path, headers, body)
            served = self.served() > before
            direct = request(*self.python, method, path, headers, body)
            strip = lambda h: {k: v for k, v in h.items() if k not in COMPARED_HEADERS}  # noqa: E731
            same = through[0] == direct[0] and through[2] == direct[2] and strip(through[1]) == strip(direct[1])
            outcomes['served' if served else 'forwarded'] += 1
            outcomes[f'status {direct[0]}'] += 1
            if direct[0] == 200 and not served:
                key = next(k for k, s in self.secrets.items() if s == headers['X-API-Key'])
                self.forwarded_ok[(path.split('?')[0], key)] += 1
            if not same:
                sent = {k: v for k, v in headers.items() if k.startswith('X-Forwarded')}
                mismatches.append({'step': step, 'served': served, 'call': (method, path, headers.get('X-API-Key'),
                                   body, sent), 'python': (direct[0], direct[2][:300]), 'data plane': (through[0],
                                   through[2][:300]), 'headers': (strip(direct[1]), strip(through[1]))})
        history = self.history_mismatches()
        where = ' behind proxies' if self.proxied else ''
        print(f'\n{self.dialect}{where}: {CALLS} calls, seed {SEED}: {dict(outcomes)}; '
              f'{len(mismatches)} response and {len(history)} history differences', file=sys.stderr)
        if os.environ.get('DIFFERENTIAL_SHOW_FORWARDED'):
            print('forwarded, though Python answered 200:', self.forwarded_ok.most_common(25), file=sys.stderr)
        self.assertEqual(mismatches[:3], [])
        self.assertEqual(history[:3], [])
        ok = outcomes['status 200']
        self.assertGreater(outcomes['served'], ok * 0.5, 'the data plane served too few of the calls Python answered')

    def history_mismatches(self):
        """Each call's runs: none, or one from each side that differ only in when and how long."""
        entries, cursor = [], None
        while True:
            path = '/api/v1/history?limit=1000' + (f'&cursor={cursor}' if cursor else '')
            status, _h, body = self.api('GET', path)
            assert status == 200, body
            page = json.loads(body)
            entries += page['items']
            cursor = page['next_cursor']
            if not cursor:
                break
        by_call = defaultdict(list)
        for entry in entries:
            if str(entry.get('request_id', '')).startswith('diff-'):
                by_call[entry['request_id']].append({k: v for k, v in entry.items() if k not in TIMING})
        different = []
        for rid, pair in by_call.items():
            if len(pair) != 2 or pair[0] != pair[1]:
                different.append({rid: pair})
        return different


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipIf(details('QUERYAPIGATE_DATAPLANE_TEST_POSTGRES') is None, 'set QUERYAPIGATE_DATAPLANE_TEST_POSTGRES')
class PostgresDifferentialTests(DifferentialTests):
    dialect = 'postgres'
    env = 'QUERYAPIGATE_DATAPLANE_TEST_POSTGRES'


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipIf(details('QUERYAPIGATE_DATAPLANE_TEST_MYSQL') is None, 'set QUERYAPIGATE_DATAPLANE_TEST_MYSQL')
class MysqlDifferentialTests(DifferentialTests):
    dialect = 'mysql'
    env = 'QUERYAPIGATE_DATAPLANE_TEST_MYSQL'


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipIf(details('QUERYAPIGATE_DATAPLANE_TEST_POSTGRES') is None, 'set QUERYAPIGATE_DATAPLANE_TEST_POSTGRES')
class ProxiedPostgresDifferentialTests(DifferentialTests):
    dialect = 'postgres'
    env = 'QUERYAPIGATE_DATAPLANE_TEST_POSTGRES'
    proxied = True


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipIf(details('QUERYAPIGATE_DATAPLANE_TEST_MYSQL') is None, 'set QUERYAPIGATE_DATAPLANE_TEST_MYSQL')
class ProxiedMysqlDifferentialTests(DifferentialTests):
    dialect = 'mysql'
    env = 'QUERYAPIGATE_DATAPLANE_TEST_MYSQL'
    proxied = True


del DifferentialTests  # the base runs only through its subclasses

if __name__ == '__main__':
    unittest.main()
