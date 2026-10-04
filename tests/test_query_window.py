"""A statement's own LIMIT/OFFSET is respected by paging (BACKLOG #74): the page window is applied *within* it.

Before, sqltools.paginate() replaced a trailing LIMIT with the page window, so "... LIMIT 3" returned a whole page
(10 rows by default, 50 with page_size=50) and the author's limit was silently discarded."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import create_app, history, sqltools
from queryapigate.errors import ApiError
from tests.helpers import save_query, write_connections


class PaginateTests(unittest.TestCase):
    def window(self, sql, limit=11, offset=0, params=None, dialect=None):
        return sqltools.paginate(sql, limit, offset, params, dialect).rsplit('\n', 1)[-1]

    def test_a_statement_without_its_own_window_gets_the_page(self):
        self.assertEqual(self.window('SELECT * FROM t'), 'LIMIT 11 OFFSET 0')
        self.assertEqual(self.window('SELECT * FROM t', 11, 30), 'LIMIT 11 OFFSET 30')

    def test_its_own_limit_caps_every_page(self):
        self.assertEqual(self.window('SELECT * FROM t LIMIT 3'), 'LIMIT 3 OFFSET 0')
        self.assertEqual(self.window('SELECT * FROM t LIMIT 25', 11, 10), 'LIMIT 11 OFFSET 10')
        self.assertEqual(self.window('SELECT * FROM t LIMIT 25', 11, 20), 'LIMIT 5 OFFSET 20')
        self.assertEqual(self.window('SELECT * FROM t LIMIT 25', 11, 30), 'LIMIT 0 OFFSET 30')

    def test_its_own_offset_is_where_paging_starts(self):
        self.assertEqual(self.window('SELECT * FROM t LIMIT 10 OFFSET 5'), 'LIMIT 10 OFFSET 5')
        self.assertEqual(self.window('SELECT * FROM t LIMIT 10 OFFSET 5', 4, 3), 'LIMIT 4 OFFSET 8')
        self.assertEqual(self.window('SELECT * FROM t OFFSET 2'), 'LIMIT 11 OFFSET 2')

    def test_every_form_of_window(self):
        self.assertEqual(self.window('SELECT * FROM t LIMIT 5, 3'), 'LIMIT 3 OFFSET 5')  # MySQL/SQLite: offset, count
        self.assertEqual(self.window('SELECT * FROM t limit 7'), 'LIMIT 7 OFFSET 0')
        self.assertEqual(self.window('SELECT * FROM t LIMIT ALL'), 'LIMIT 11 OFFSET 0')
        self.assertEqual(self.window('SELECT * FROM t FETCH FIRST 2 ROWS ONLY'), 'LIMIT 2 OFFSET 0')
        self.assertEqual(self.window('SELECT * FROM t FETCH FIRST ROW ONLY'), 'LIMIT 1 OFFSET 0')
        self.assertEqual(self.window('SELECT * FROM t OFFSET 1 ROWS FETCH NEXT 4 ROWS ONLY'), 'LIMIT 4 OFFSET 1')

    def test_bound_parameters_in_the_window(self):
        self.assertEqual(self.window('SELECT * FROM t LIMIT :n', params={'n': 4}), 'LIMIT 4 OFFSET 0')
        self.assertEqual(self.window('SELECT * FROM t LIMIT :n OFFSET :o', params={'n': '4', 'o': 2}),
                         'LIMIT 4 OFFSET 2')
        for bad in (None, 'four', -1, True, 1.5):
            with self.assertRaises(ApiError, msg=repr(bad)):
                self.window('SELECT * FROM t LIMIT :n', params={'n': bad})

    def test_comments_and_literals_neither_hide_nor_fake_a_window(self):
        self.assertEqual(self.window('SELECT * FROM t LIMIT 3 -- the top three'), 'LIMIT 3 OFFSET 0')
        self.assertEqual(self.window('SELECT * FROM t LIMIT 3 /* top */'), 'LIMIT 3 OFFSET 0')
        result = sqltools.paginate("SELECT 'x LIMIT 3' AS s", 11, 0)
        self.assertEqual(result, "SELECT 'x LIMIT 3' AS s\nLIMIT 11 OFFSET 0")
        # A LIMIT inside a subquery is the subquery's own business
        result = sqltools.paginate('SELECT * FROM (SELECT * FROM t LIMIT 2) s', 11, 0)
        self.assertEqual(result, 'SELECT * FROM (SELECT * FROM t LIMIT 2) s\nLIMIT 11 OFFSET 0')

    def test_a_backslash_escaped_quote_is_read_per_dialect(self):
        # MySQL reads 'a\' LIMIT 3' as one string, so there is no trailing window to take over
        sql = "SELECT 'a\\' LIMIT 3'"
        self.assertEqual(sqltools.paginate(sql, 11, 0, dialect='mysql'), sql + '\nLIMIT 11 OFFSET 0')


class EndToEndTests(unittest.TestCase):
    """Through the real HTTP API against SQLite: rows returned, X-Has-More and paging."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(history.flush)
        db = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE n (i INTEGER)')
        conn.executemany('INSERT INTO n VALUES (?)', [(i,) for i in range(1, 101)])
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        write_connections({'lite': {'db': 'sqlite', 'database': db, 'active': True}})
        self.client = create_app().test_client()

    def run_sql(self, sql, query='', params=None):
        res = self.client.post(f'/execute_sql{query}', json={'sql': sql, 'connection_name': 'lite',
                                                              **({'params': params} if params else {})})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = res.get_json()
        rows = body if isinstance(body, list) else []  # an empty page is {"message": "No results returned"}
        return [row['i'] for row in rows], res.headers['X-Has-More']

    def test_limit_3_returns_3_rows_whatever_the_page_size(self):
        for query in ('', '?page_size=5', '?page_size=50'):
            self.assertEqual(self.run_sql('SELECT i FROM n ORDER BY i LIMIT 3', query), ([1, 2, 3], 'false'), query)

    def test_paging_moves_through_the_statements_own_rows_and_stops_at_its_end(self):
        sql = 'SELECT i FROM n ORDER BY i LIMIT 25'
        self.assertEqual(self.run_sql(sql, '?page_size=10'), (list(range(1, 11)), 'true'))
        self.assertEqual(self.run_sql(sql, '?page_size=10&page=2'), (list(range(11, 21)), 'true'))
        self.assertEqual(self.run_sql(sql, '?page_size=10&page=3'), (list(range(21, 26)), 'false'))
        self.assertEqual(self.run_sql(sql, '?page_size=10&page=4'), ([], 'false'))

    def test_a_limit_that_ends_exactly_on_a_page_boundary_has_no_more(self):
        self.assertEqual(self.run_sql('SELECT i FROM n ORDER BY i LIMIT 10', '?page_size=10'),
                         (list(range(1, 11)), 'false'))

    def test_offset_count_form_and_bound_limit(self):
        self.assertEqual(self.run_sql('SELECT i FROM n ORDER BY i LIMIT 5, 3'), ([6, 7, 8], 'false'))
        self.assertEqual(self.run_sql('SELECT i FROM n ORDER BY i LIMIT :k', params={'k': 2}), ([1, 2], 'false'))

    def test_a_trailing_comment_after_limit_no_longer_breaks_the_query(self):
        self.assertEqual(self.run_sql('SELECT i FROM n ORDER BY i LIMIT 2 -- first two'), ([1, 2], 'false'))

    def test_a_saved_query_keeps_its_limit_on_q(self):
        save_query(self.client, {'author': 'a', 'description': 'top', 'filename': 'top3',
                                                     'sql_query': 'SELECT i FROM n ORDER BY i DESC LIMIT 3',
                                                     'connection_name': 'lite'})
        res = self.client.get('/q/top3?page_size=50')
        self.assertEqual([r['i'] for r in res.get_json()], [100, 99, 98])
        self.assertEqual(res.headers['X-Has-More'], 'false')


if __name__ == '__main__':
    unittest.main()
