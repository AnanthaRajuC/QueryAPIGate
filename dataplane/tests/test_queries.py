"""The data plane runs a plan's statement as Python's runner does (ADR 0028, step 3c), on real servers: the session it
runs in, every value it writes into the SQL - hostile strings and session tricks included - the page it returns,
read-only, and its time limit.

Needs a PostgreSQL and a MySQL to use as scratch (each test makes and drops its own objects), named by
QUERYAPIGATE_DATAPLANE_TEST_POSTGRES / QUERYAPIGATE_DATAPLANE_TEST_MYSQL as host:port:user:password:database:

    docker run -d --name qag-dp-pg -e POSTGRES_PASSWORD=pw -p 127.0.0.1:55435:5432 postgres:16
    docker run -d --name qag-dp-my -e MYSQL_ROOT_PASSWORD=pw -e MYSQL_DATABASE=shop -p 127.0.0.1:55436:3306 mysql:8.4
    QUERYAPIGATE_DATAPLANE_TEST_POSTGRES=127.0.0.1:55435:postgres:pw:postgres \\
    QUERYAPIGATE_DATAPLANE_TEST_MYSQL=127.0.0.1:55436:root:pw:shop .venv/bin/python dataplane/tests/test_queries.py
"""
import json
import os
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from test_forwarding import binary  # noqa: E402

from queryapigate import dataplane, runners  # noqa: E402

HOSTILE = ["O'Brien", 'a\\b', "\\'; SELECT 1; --", "' OR '1'='1", "''", '\\', '\\\\\'', '$$', "E'", '\\u0041',
           '%s', '%(x)s', ':p', '"q"', '`b`', 'x\ny', 'tab\there', 'cr\rlf', '\x1a', 'é', 'Ünïcödé', '汉字',
           '😀 emoji', 'é combining', '‮ right-to-left', '﻿bom', '', ' ', 'NULL', '--', '/*', '*/',
           chr(0xbf) + "' gbk", chr(0x815c) + ' sjis', chr(0xe4) * 300]


def details(env):
    value = os.environ.get(env)
    if not value:
        return None
    host, port, user, password, database = value.split(':')
    return {'host': host, 'port': int(port), 'user': user, 'password': password, 'database': database}


def tag(value):
    if value is None:
        return ['null']
    if isinstance(value, bool):
        return ['bool', value]
    if isinstance(value, int):
        return ['int', str(value)]
    if isinstance(value, float):
        return ['float', repr(value)]
    return ['str', value]


class QueryTests:
    """Mixed into a TestCase per database."""

    dialect = None
    details = None

    def python(self, sql, params=None, limit=100, offset=0, timeout=30.0, **options):
        """Python's runner, read-only, on a fresh session: (columns, rows)."""
        return runners.RUNNERS[self.dialect](self.details, sql, params or {}, limit, offset, True, timeout, **options)

    def rust(self, requests):
        """The data plane's runner, every request on one connection, in order."""
        body = {'dialect': self.dialect, 'details': json.dumps(self.details), 'requests': [
            {'statement': dataplane.statement(sql, self.dialect),
             'values': {k: tag(v) for k, v in (params or {}).items()}, 'limit': limit, 'offset': offset,
             'timeout': timeout, 'search_path': search_path}
            for sql, params, limit, offset, timeout, search_path in requests]}
        probe = subprocess.run([str(binary()), '--probe-query'], input=json.dumps(body), capture_output=True,
                               text=True, timeout=60)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        return json.loads(probe.stdout)

    def one(self, sql, params=None, limit=100, offset=0, timeout=30.0, search_path=None):
        result = self.rust([(sql, params, limit, offset, timeout, search_path)])[0]
        self.assertNotIn('error', result, result)
        return result

    def test_the_same_session_as_python_s(self):
        sql = self.SETTINGS_SQL
        _columns, rows = self.python(sql, limit=10000)
        python = {name: str(value) for name, value in rows}
        rust = {name: value for name, value in self.one(sql, limit=10000)['rows']}
        differ = {name: (python.get(name), rust.get(name)) for name in set(python) | set(rust)
                  if python.get(name) != rust.get(name) and name not in self.PER_SESSION}
        self.assertEqual(differ, {})

    def test_every_hostile_string_arrives_as_written(self):
        results = self.rust([(self.ECHO, {'p': text}, 10, 0, 30.0, None) for text in HOSTILE])
        for text, result in zip(HOSTILE, results, strict=True):
            with self.subTest(text=text):
                self.assertEqual(result.get('rows'), [[text]], result)
                self.assertEqual(self.python(self.ECHO, {'p': text})[1], [(text,)])

    def test_numbers_and_booleans_as_python_binds_them(self):
        for value in (5, -5, 0, 2 ** 63 - 1, -2 ** 63, 5.0, -0.0, 1e16, 1e-7, 0.1, 123.456, True, False, None):
            with self.subTest(value=value):
                _columns, rows = self.python(self.ECHO, {'p': value})
                rust = self.one(self.ECHO, {'p': value})['rows'][0][0]
                python = rows[0][0]
                if isinstance(python, float):  # a double, which MySQL may write without an exponent
                    self.assertEqual(float(rust), python)
                else:
                    self.assertEqual(self.as_python(rust), python)

    def test_the_page_is_the_runner_s(self):
        sql = self.NUMBERS
        for limit, offset in ((5, 0), (5, 5), (3, 98), (1, 0), (100, 0)):
            with self.subTest(limit=limit, offset=offset):
                rows = [[str(v) for v in row] for row in self.python(sql, limit=limit, offset=offset)[1]]
                self.assertEqual(self.one(sql, limit=limit, offset=offset)['rows'], rows)


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipIf(details('QUERYAPIGATE_DATAPLANE_TEST_POSTGRES') is None, 'set QUERYAPIGATE_DATAPLANE_TEST_POSTGRES')
class PostgresQueryTests(QueryTests, unittest.TestCase):
    dialect = 'postgres'
    details = details('QUERYAPIGATE_DATAPLANE_TEST_POSTGRES')
    SETTINGS_SQL = 'SELECT name, setting FROM pg_settings ORDER BY name'
    PER_SESSION = {'application_name'}
    ECHO = 'SELECT :p AS v'
    NUMBERS = 'SELECT g AS n FROM generate_series(1, 100) AS g ORDER BY g'

    @staticmethod
    def as_python(text):
        return None if text is None else {'t': True, 'f': False}.get(text) if text in ('t', 'f') else (
            int(text) if text.lstrip('-').isdigit() else Decimal(text))

    def test_a_query_s_session_changes_never_reach_the_next_message(self):
        # Each turns off something a string literal's reading depends on, for the session, committed with the query
        for trick in ("SELECT set_config('standard_conforming_strings', 'off', false)",
                      "SELECT set_config('backslash_quote', 'on', false)",
                      "SELECT set_config('client_encoding', 'SJIS', false)",
                      "SELECT set_config('search_path', 'pg_catalog', false)"):
            with self.subTest(trick=trick):
                results = self.rust([(trick, None, 10, 0, 30.0, None)] +
                                    [(self.ECHO, {'p': text}, 10, 0, 30.0, None) for text in HOSTILE])
                for text, result in zip(HOSTILE, results[1:], strict=True):
                    self.assertEqual(result.get('rows'), [[text]], (text, result))

    def test_read_only(self):
        result = self.rust([("SELECT nextval('qag_dp_seq')", None, 10, 0, 30.0, None)])[0]
        self.assertIn('error', result)  # read-only transaction, or no such sequence: never a write

    def test_the_time_limit(self):
        result = self.rust([('SELECT pg_sleep(3)', None, 10, 0, 0.5, None)])[0]
        self.assertEqual(result, {'error': 'Timeout'})

    def test_a_pinned_search_path(self):
        self.assertEqual(self.one('SELECT current_schemas(false)::text AS s', search_path='pg_catalog')['rows'],
                         [['{pg_catalog}']])


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipIf(details('QUERYAPIGATE_DATAPLANE_TEST_MYSQL') is None, 'set QUERYAPIGATE_DATAPLANE_TEST_MYSQL')
class MysqlQueryTests(QueryTests, unittest.TestCase):
    dialect = 'mysql'
    details = details('QUERYAPIGATE_DATAPLANE_TEST_MYSQL')
    SETTINGS_SQL = ('SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.session_variables '
                    'ORDER BY VARIABLE_NAME')
    # Per connection, or what the hint itself sets for the statement
    PER_SESSION = {'pseudo_thread_id', 'timestamp', 'rand_seed1', 'rand_seed2', 'max_execution_time',
                   'gtid_next', 'last_insert_id', 'warning_count', 'error_count', 'identity', 'insert_id',
                   'statement_id'}
    ECHO = 'SELECT :p AS v'
    NUMBERS = ('WITH RECURSIVE g(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < 100) '
               'SELECT n FROM g ORDER BY n')

    @staticmethod
    def as_python(text):
        if text is None:
            return None
        return int(text) if text.lstrip('-').isdigit() else (float(text) if 'e' in text.lower() else Decimal(text))

    def test_the_page_is_the_runner_s(self):
        # WITH ... the hint can't go on: the prototype forwards it; the page logic is checked on a SELECT
        self.NUMBERS = 'SELECT n FROM (SELECT 1 AS n UNION SELECT 2 UNION SELECT 3 UNION SELECT 4) t ORDER BY n'
        for limit, offset in ((2, 0), (2, 2), (1, 3), (10, 0)):
            with self.subTest(limit=limit, offset=offset):
                rows = [[str(v) for v in row] for row in self.python(self.NUMBERS, limit=limit, offset=offset)[1]]
                self.assertEqual(self.one(self.NUMBERS, limit=limit, offset=offset)['rows'], rows)

    def test_a_stored_function_s_session_changes_never_reach_the_next_statement(self):
        import mysql.connector
        admin = mysql.connector.connect(**self.details)
        cursor = admin.cursor()
        cursor.execute('DROP FUNCTION IF EXISTS qag_dp_trick')
        cursor.execute("CREATE FUNCTION qag_dp_trick() RETURNS INT DETERMINISTIC BEGIN "
                       "SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'; SET character_set_client = 'gbk'; "
                       "RETURN 1; END")
        self.addCleanup(lambda: (cursor.execute('DROP FUNCTION IF EXISTS qag_dp_trick'), admin.close()))
        results = self.rust([('SELECT qag_dp_trick() AS t', None, 10, 0, 30.0, None)] +
                            [(self.ECHO, {'p': text}, 10, 0, 30.0, None) for text in HOSTILE])
        for text, result in zip(HOSTILE, results[1:], strict=True):
            self.assertEqual(result.get('rows'), [[text]], (text, result))

    def test_read_only(self):
        import mysql.connector
        admin = mysql.connector.connect(**self.details)
        cursor = admin.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS qag_dp_t (id INT)')
        cursor.execute('DROP FUNCTION IF EXISTS qag_dp_write')
        cursor.execute('CREATE FUNCTION qag_dp_write() RETURNS INT DETERMINISTIC MODIFIES SQL DATA BEGIN '
                       'INSERT INTO qag_dp_t VALUES (1); RETURN 1; END')
        self.addCleanup(lambda: (cursor.execute('DROP FUNCTION IF EXISTS qag_dp_write'),
                                 cursor.execute('DROP TABLE IF EXISTS qag_dp_t'), admin.close()))
        result = self.rust([('SELECT qag_dp_write() AS w', None, 10, 0, 30.0, None)])[0]
        self.assertIn('error', result)
        self.assertIn('READ ONLY', result['error'].upper())


if __name__ == '__main__':
    unittest.main()
