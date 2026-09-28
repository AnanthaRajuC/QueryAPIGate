"""Tests for the bundled example APIs (BACKLOG #27): four scenarios that load into a home, run, and unload again -
touching only what is marked as an example, and never overwriting anything of the user's."""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from unittest import mock

from queryapigate import apikeys, cli, config, create_app, definitions, examples, postman, store
from queryapigate.errors import ApiError

FIXED_NOW = datetime(2026, 9, 25, 12, 0, 0)


class ExamplesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('QUERYAPIGATE_API_KEY', 'QUERYAPIGATE_LOAD_EXAMPLES', 'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE'):
            os.environ.pop(name, None)
        apikeys._last_recorded_use.clear()
        # A smaller dataset keeps the suite quick; test_the_shipped_size_... checks the real one.
        size = mock.patch.object(examples, 'RENTAL_COUNT', 3000)
        size.start()
        self.addCleanup(size.stop)

    def saved_files(self):
        folder = os.path.join(self.home, 'saved_sql')
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def save_user_query(self, name, collection=None):
        store.save_version(name, {'sql_query': 'SELECT 1', 'author': 'me', 'description': 'mine', 'tags': [],
                                  'query_parameters': {}}, collection)

    def make_home_unwritable(self):
        """A real read-only data folder - what a Docker bind mount created by root looks like to the container's
        non-root user. (Mocking an OSError would not do: SQLite raises its own error type here.)"""
        if os.geteuid() == 0:
            self.skipTest('root ignores directory permissions')
        os.chmod(self.home, 0o555)
        self.addCleanup(os.chmod, self.home, 0o755)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class DatabaseTests(ExamplesTestCase):
    def test_an_unwritable_folder_is_an_apierror_with_a_hint(self):
        self.make_home_unwritable()
        with self.assertRaises(ApiError) as caught:
            examples.load()
        self.assertEqual(caught.exception.status, 500)
        self.assertIn('writable', caught.exception.message)

    def build(self, **kwargs):
        path = os.path.join(self.home, 'x.db')
        examples.build_database(path, FIXED_NOW, **kwargs)
        return sqlite3.connect(path)

    def test_shape_and_size(self):
        conn = self.build()
        tables = ('film', 'customer', 'rental', 'category', 'store', 'staff', 'address', 'payment')
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}
        self.assertEqual(counts, {'film': examples.FILM_COUNT, 'customer': examples.CUSTOMER_COUNT,
                                  'rental': examples.RENTAL_COUNT, 'category': len(examples.CATEGORIES),
                                  'store': 3, 'staff': 6, 'address': examples.CUSTOMER_COUNT,
                                  'payment': examples.RENTAL_COUNT})

    def test_the_new_tables_have_real_foreign_keys_with_no_orphans_or_duplicates(self):
        conn = self.build()
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM payment WHERE rental_id NOT IN (SELECT rental_id FROM rental)').fetchone()[0], 0)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM payment WHERE staff_id NOT IN (SELECT staff_id FROM staff)').fetchone()[0], 0)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM (SELECT rental_id, COUNT(*) c FROM payment GROUP BY rental_id HAVING c > 1)'
        ).fetchone()[0], 0, 'payment must be exactly 1:1 with rental, or example_all_rentals\' row count would drift')
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM staff WHERE store_id NOT IN (SELECT store_id FROM store)').fetchone()[0], 0)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM address WHERE customer_id NOT IN (SELECT customer_id FROM customer)'
        ).fetchone()[0], 0)

    def test_the_shipped_size_is_about_twenty_thousand_rentals(self):
        with mock.patch.object(examples, 'RENTAL_COUNT', 20000):
            path = os.path.join(self.home, 'full.db')
            examples.build_database(path, FIXED_NOW)
        self.assertEqual(sqlite3.connect(path).execute('SELECT COUNT(*) FROM rental').fetchone()[0], 20000)
        self.assertLess(os.path.getsize(path), 5_000_000)  # a small file, generated locally - nothing shipped

    def test_deterministic_for_a_given_day_and_seed(self):
        first = self.build().execute('SELECT * FROM rental ORDER BY rental_id').fetchall()
        os.remove(os.path.join(self.home, 'x.db'))
        second = self.build().execute('SELECT * FROM rental ORDER BY rental_id').fetchall()
        self.assertEqual(first, second)

    def test_the_time_based_queries_have_something_to_show(self):
        conn = self.build()
        today, active, overdue = (conn.execute(q).fetchone()[0] for q in (
            "SELECT COUNT(*) FROM rental WHERE date(rental_date) = '2026-09-25'",
            'SELECT COUNT(*) FROM rental WHERE return_date IS NULL',
            "SELECT COUNT(*) FROM rental WHERE return_date IS NULL AND rental_date < '2026-09-18 12:00:00'"))
        self.assertGreater(today, 0)
        self.assertGreater(active, 0)
        self.assertGreater(overdue, 0)
        self.assertLess(overdue, active)

    def test_nothing_is_in_the_future_and_returns_follow_rentals(self):
        conn = self.build()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rental WHERE rental_date > '2026-09-25 13:00:00'")
                         .fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rental WHERE return_date IS NOT NULL "
                                      "AND return_date < rental_date").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rental WHERE return_date > '2026-09-25 12:00:00'")
                         .fetchone()[0], 0)

    def test_no_half_written_file_is_left_behind(self):
        self.build()
        self.assertEqual(sorted(os.listdir(self.home)), ['x.db'])


class ManifestTests(ExamplesTestCase):
    def test_every_query_passes_the_same_validation_as_saving(self):
        for scenario in examples.SCENARIOS.values():
            for q in scenario['queries']:
                fields, _ = definitions.validate_definition({**{k: v for k, v in q.items() if k != 'name'},
                                                             'filename': q['name']})
                self.assertEqual(fields['sql_query'], q['sql_query'])

    def test_four_scenarios_each_with_queries_and_a_role(self):
        self.assertEqual(sorted(examples.SCENARIOS), ['examples-dashboard', 'examples-export', 'examples-partner',
                                                      'examples-reporting'])
        for name, scenario in examples.SCENARIOS.items():
            self.assertTrue(scenario['queries'], name)
            self.assertTrue(scenario['role']['name'].startswith('example-'))
        self.assertEqual(len(set(examples.QUERY_NAMES)), len(examples.QUERY_NAMES))

    def test_names_are_prefixed_so_they_rarely_collide_with_a_users_own(self):
        for name in examples.QUERY_NAMES + examples.ROLE_NAMES:
            self.assertTrue(name.startswith('example'), name)

    def test_the_dashboard_queries_are_cached_and_the_others_are_not(self):
        for collection, scenario in examples.SCENARIOS.items():
            for q in scenario['queries']:
                self.assertEqual(bool(q.get('cache_ttl')), collection == 'examples-dashboard', q['name'])

    def test_extra_roles_each_span_more_than_one_collection(self):
        self.assertTrue(examples.EXTRA_ROLES)
        for role in examples.EXTRA_ROLES:
            self.assertTrue(role['name'].startswith('example-'), role['name'])
            self.assertGreater(len(role['collections']), 1, role['name'])
            for collection in role['collections']:
                self.assertIn(collection, examples.SCENARIOS, role['name'])
        self.assertEqual(examples.ROLE_NAMES,
                         [s['role']['name'] for s in examples.SCENARIOS.values()] +
                         [r['name'] for r in examples.EXTRA_ROLES])


class LoadTests(ExamplesTestCase):
    def test_load_installs_everything_marked(self):
        added = examples.load(FIXED_NOW)
        self.assertEqual(sorted(added['queries']), sorted(examples.QUERY_NAMES))
        self.assertEqual(sorted(added['roles']), sorted(examples.ROLE_NAMES))
        self.assertTrue(added['connection'])
        state = examples.status()
        self.assertTrue(state['loaded'])
        self.assertFalse(state['partial'])
        self.assertEqual(state['collections'], sorted(examples.SCENARIOS))
        self.assertTrue(os.path.isfile(os.path.join(self.home, examples.DB_FILE)))
        self.assertTrue(store.read_connections()[examples.CONNECTION]['example'])
        self.assertTrue(all(r['example'] for r in apikeys.list_roles().values()))
        for file in store.list_saved():
            self.assertTrue(file['example'], file['filename'])

    def test_it_is_idempotent_and_never_adds_versions(self):
        examples.load(FIXED_NOW)
        again = examples.load(FIXED_NOW)
        self.assertEqual(again, {'connection': False, 'queries': [], 'roles': []})
        for file in store.list_saved():
            self.assertEqual([v['version'] for v in file['versions']], [1], file['filename'])

    def test_nothing_of_the_users_is_touched_by_loading(self):
        self.save_user_query('mine', 'my-group')
        store.update_connections({'prod': {'db': 'sqlite', 'database': 'prod.db', 'active': True}})
        apikeys.create_role('their-role', connections=['prod'])
        examples.load(FIXED_NOW)
        self.assertIn('mine', self.saved_files()[0] + ' '.join(self.saved_files()))
        self.assertNotIn('example', store.read_connections()['prod'])
        self.assertNotIn('example', apikeys.list_roles()['their-role'])
        self.assertFalse(store.load_versions(os.path.join(self.home, 'saved_sql', 'mine.json')).get('example'))

    def test_a_users_query_with_an_examples_name_stops_the_load_and_changes_nothing(self):
        self.save_user_query('example_top_films')
        with self.assertRaises(ApiError) as caught:
            examples.load(FIXED_NOW)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("saved query 'example_top_films'", caught.exception.message)
        self.assertEqual(self.saved_files(), ['example_top_films.json'])
        self.assertFalse(os.path.exists(os.path.join(self.home, examples.DB_FILE)))
        self.assertEqual(apikeys.list_roles(), {})
        self.assertEqual(store.read_connections(), {})

    def test_a_users_connection_role_or_file_with_an_examples_name_also_stops_it(self):
        store.update_connections({'examples': {'db': 'sqlite', 'database': 'mine.db', 'active': True}})
        with self.assertRaises(ApiError) as caught:
            examples.load(FIXED_NOW)
        self.assertIn("connection 'examples'", caught.exception.message)
        store.delete_connection('examples')
        apikeys.create_role('example-partner')
        with self.assertRaises(ApiError) as caught:
            examples.load(FIXED_NOW)
        self.assertIn("role 'example-partner'", caught.exception.message)
        apikeys.delete_role('example-partner')
        with open(os.path.join(self.home, examples.DB_FILE), 'w') as f:
            f.write('not ours')
        with self.assertRaises(ApiError) as caught:
            examples.load(FIXED_NOW)
        self.assertIn(examples.DB_FILE, caught.exception.message)
        with open(os.path.join(self.home, examples.DB_FILE)) as f:
            self.assertEqual(f.read(), 'not ours')  # never overwritten

    def test_an_interrupted_load_is_reported_as_partial_and_finished_by_running_it_again(self):
        with mock.patch.object(apikeys, 'create_role', side_effect=OSError('disk gone')):
            with self.assertRaises(OSError):
                examples.load(FIXED_NOW)
        state = examples.status()
        self.assertFalse(state['loaded'])
        self.assertTrue(state['partial'])
        self.assertTrue(examples.load(FIXED_NOW)['roles'])
        self.assertTrue(examples.status()['loaded'])
        for file in store.list_saved():
            self.assertEqual(len(file['versions']), 1)  # finishing did not re-add what was there

    def test_a_user_edited_connection_loses_its_mark_and_blocks_a_reload(self):
        examples.load(FIXED_NOW)
        store.update_connections({'examples': {'db': 'sqlite', 'database': 'examples.db', 'active': True}})  # UI edit
        self.assertFalse(examples.status()['loaded'])
        with self.assertRaises(ApiError):
            examples.load(FIXED_NOW)


class UnloadTests(ExamplesTestCase):
    def test_unload_removes_exactly_the_examples(self):
        self.save_user_query('mine', 'my-group')
        store.update_connections({'prod': {'db': 'sqlite', 'database': 'prod.db', 'active': True}})
        apikeys.create_role('their-role', connections=['prod'])
        examples.load(FIXED_NOW)
        removed = examples.unload()
        self.assertEqual(sorted(removed['queries']), sorted(examples.QUERY_NAMES))
        self.assertEqual(self.saved_files(), ['mine.json'])
        self.assertEqual(sorted(store.read_connections()), ['prod'])
        self.assertEqual(sorted(apikeys.list_roles()), ['their-role'])
        self.assertFalse(os.path.exists(os.path.join(self.home, examples.DB_FILE)))
        self.assertEqual(store.collection_members(), {'my-group': ['mine']})
        self.assertEqual(examples.status(), {'loaded': False, 'partial': False, 'connection': None, 'queries': [],
                                             'roles': [], 'collections': []})

    def test_unload_never_removes_a_user_query_that_shares_a_name(self):
        examples.load(FIXED_NOW)
        # The user replaces an example with their own file of the same name (no example mark).
        path = os.path.join(self.home, 'saved_sql', 'example_top_films.json')
        os.remove(path)
        self.save_user_query('example_top_films')
        examples.unload()
        self.assertEqual(self.saved_files(), ['example_top_films.json'])

    def test_unload_when_nothing_is_loaded_is_a_no_op(self):
        self.assertEqual(examples.unload(), {'connection': False, 'queries': [], 'roles': [],
                                             'keys_still_granted': []})

    def test_unload_reports_keys_whose_grant_is_now_inert(self):
        examples.load(FIXED_NOW)
        apikeys.create_key('partner-key', connections=[], collections=['examples-partner'])
        removed = examples.unload()
        self.assertEqual(removed['keys_still_granted'], ['partner-key'])
        self.assertEqual(apikeys.list_keys()['partner-key']['collections'], ['examples-partner'])  # left as it was

    def test_load_after_unload_works_again(self):
        examples.load(FIXED_NOW)
        examples.unload()
        self.assertTrue(examples.load(FIXED_NOW)['queries'])
        self.assertTrue(examples.status()['loaded'])


class RunTests(ExamplesTestCase):
    """The examples are only worth shipping if they work: every query, with the example values its own rules
    produce, must answer 200 with rows."""

    def setUp(self):
        super().setUp()
        examples.load()  # today, for real, as a user would
        self.client = create_app().test_client()

    def test_every_example_query_runs_and_returns_rows(self):
        for scenario in examples.SCENARIOS.values():
            for q in scenario['queries']:
                specs = definitions.effective_parameters({'sql_query': q['sql_query'],
                                                          'query_parameters': q.get('query_parameters')})
                params = {k: postman.example_value(s) for k, s in specs.items()}
                if 'text' in params:
                    params['text'] = 'Harbor'  # a pattern has no generated example
                res = self.client.get(f"/q/{q['name']}", query_string=params)
                self.assertEqual(res.status_code, 200, f"{q['name']}: {res.get_data(as_text=True)[:200]}")
                self.assertTrue(res.get_json(), q['name'])

    def test_the_kpis_are_sensible(self):
        today = self.client.get('/q/example_kpi_rentals_today').get_json()[0]
        self.assertGreater(today['rentals_today'], 0)
        active = self.client.get('/q/example_kpi_active_rentals').get_json()[0]['active_rentals']
        overdue = self.client.get('/q/example_kpi_overdue').get_json()[0]['overdue']
        self.assertGreater(active, overdue)
        self.assertGreater(overdue, 0)

    def test_the_optional_category_filter_works(self):
        everything = self.client.get('/q/example_top_films?top_n=50').get_json()
        comedy = self.client.get('/q/example_top_films?top_n=50&category=Comedy').get_json()
        self.assertTrue(comedy)
        self.assertTrue(all(r['category'] == 'Comedy' for r in comedy))
        self.assertGreater(len({r['category'] for r in everything}), 1)  # unfiltered really is unfiltered
        self.assertEqual([r['rank'] for r in comedy], sorted(r['rank'] for r in comedy))  # ranked within Comedy
        self.assertEqual(comedy[0]['rank'], 1)

    def test_the_export_streams_every_row(self):
        res = self.client.get('/q/example_all_rentals?stream=true&format=csv')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.get_data(as_text=True).splitlines()), examples.RENTAL_COUNT + 1)

    def test_parameter_rules_are_enforced(self):
        self.assertEqual(self.client.get('/q/example_top_films?category=Nope').status_code, 400)
        self.assertEqual(self.client.get('/q/example_top_films?top_n=51').status_code, 400)
        self.assertEqual(self.client.get('/q/example_film_search?text=a1').status_code, 400)
        self.assertEqual(self.client.get('/q/example_film_lookup').status_code, 400)  # film_id is required

    def test_the_dashboard_queries_are_served_from_cache_on_the_second_call(self):
        first = self.client.get('/q/example_kpi_active_rentals')
        second = self.client.get('/q/example_kpi_active_rentals')
        self.assertEqual(first.headers['X-Cache'], 'MISS')
        self.assertEqual(second.headers['X-Cache'], 'HIT')


class RolesEndToEndTests(ExamplesTestCase):
    """The example roles are the keys the walkthrough tells people to create - they must behave as advertised."""

    def setUp(self):
        super().setUp()
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        examples.load()
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin-key'}

    def key_from(self, role):
        res = self.client.post('/api_keys', json={'name': f'k-{role}', 'role': role}, headers=self.admin)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return {'X-API-Key': res.get_json()['key']}

    def test_a_partner_key_reaches_only_the_partner_queries(self):
        key = self.key_from('example-partner')
        self.assertEqual(self.client.get('/q/example_film_lookup?film_id=1', headers=key).status_code, 200)
        self.assertEqual(self.client.get('/q/example_film_search?text=Harbor', headers=key).status_code, 200)
        self.assertEqual(self.client.get('/q/example_top_films', headers=key).status_code, 403)
        self.assertEqual(self.client.get('/q/example_all_rentals', headers=key).status_code, 403)

    def test_a_partner_key_cannot_be_redirected_or_run_ad_hoc_sql(self):
        key = self.key_from('example-partner')
        res = self.client.get('/q/example_film_lookup?film_id=1&connection_name=other', headers=key)
        self.assertEqual(res.status_code, 403)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM rental', 'connection_name': 'examples'},
                               headers=key)
        self.assertEqual(res.status_code, 403)

    def test_each_role_reaches_its_own_collection_and_no_other(self):
        reach = {}
        for role, scenario in (('example-reporting', 'examples-reporting'), ('example-dashboard', 'examples-dashboard'),
                               ('example-export', 'examples-export'), ('example-partner', 'examples-partner')):
            key = self.key_from(role)
            catalog = self.client.get('/catalog', headers=key).get_json()['queries']
            reach[role] = {q['name'] for q in catalog}
            expected = {q['name'] for q in examples.SCENARIOS[scenario]['queries']}
            self.assertEqual(reach[role], expected, role)

    def test_the_executive_role_reaches_both_collections_and_nothing_else(self):
        key = self.key_from('example-executive')
        catalog = self.client.get('/catalog', headers=key).get_json()['queries']
        reach = {q['name'] for q in catalog}
        expected = {q['name'] for q in examples.SCENARIOS['examples-reporting']['queries']} | \
            {q['name'] for q in examples.SCENARIOS['examples-dashboard']['queries']}
        self.assertEqual(reach, expected)
        excluded = examples.SCENARIOS['examples-export']['queries'] + examples.SCENARIOS['examples-partner']['queries']
        for q in excluded:
            self.assertNotIn(q['name'], reach)
        role = apikeys.list_roles()['example-executive']
        self.assertFalse(role['allow_writes'])
        self.assertEqual(role['rate_limit'], '300/hour')
        self.assertEqual(role['connections'], [])

    def test_the_roles_are_read_only_and_rate_limited(self):
        for scenario in examples.SCENARIOS.values():
            role = apikeys.list_roles()[scenario['role']['name']]
            self.assertFalse(role['allow_writes'])
            self.assertEqual(role['rate_limit'], scenario['role']['rate_limit'])
            self.assertEqual(role['connections'], [])


class EndpointTests(ExamplesTestCase):
    def setUp(self):
        super().setUp()
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin-key'}

    def test_status_load_and_unload_over_http(self):
        self.assertFalse(self.client.get('/examples', headers=self.admin).get_json()['loaded'])
        res = self.client.post('/examples', headers=self.admin)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertTrue(res.get_json()['loaded'])
        self.assertTrue(self.client.get('/examples', headers=self.admin).get_json()['loaded'])
        res = self.client.delete('/examples', headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(sorted(res.get_json()['queries']), sorted(examples.QUERY_NAMES))
        self.assertFalse(self.client.get('/examples', headers=self.admin).get_json()['loaded'])

    def test_a_conflict_is_a_409_and_changes_nothing(self):
        self.save_user_query('example_top_films')
        res = self.client.post('/examples', headers=self.admin)
        self.assertEqual(res.status_code, 409)
        self.assertIn('example_top_films', res.get_json()['error'])
        self.assertEqual(self.saved_files(), ['example_top_films.json'])

    def test_admin_only(self):
        self.client.post('/examples', headers=self.admin)
        key = self.client.post('/api_keys', json={'name': 'k', 'connections': ['examples']},
                               headers=self.admin).get_json()['key']
        for method in ('get', 'post', 'delete'):
            res = getattr(self.client, method)('/examples', headers={'X-API-Key': key})
            self.assertEqual(res.status_code, 403, method)

    def test_load_and_unload_are_audited_once_each(self):
        self.client.post('/examples', headers=self.admin)
        self.client.post('/examples', headers=self.admin)  # a no-op: not audited again
        self.client.delete('/examples', headers=self.admin)
        actions = [e['action'] for e in self.client.get('/audit_log', headers=self.admin).get_json()['entries']]
        self.assertEqual([a for a in actions if 'examples' in a], ['unload_examples', 'load_examples'])
        self.assertNotIn('save_query', actions)  # one summary entry, not one per query

    def test_list_files_flags_examples(self):
        self.save_user_query('mine')
        self.client.post('/examples', headers=self.admin)
        files = self.client.get('/list_files', headers=self.admin).get_json()['files']
        flags = {f['filename']: f['example'] for f in files}
        self.assertFalse(flags['mine'])
        self.assertTrue(flags['example_top_films'])


class StartupTests(ExamplesTestCase):
    def test_off_by_default(self):
        create_app()
        self.assertFalse(examples.status()['loaded'])
        self.assertEqual(self.saved_files(), [])

    def test_the_setting_loads_them_at_startup(self):
        os.environ['QUERYAPIGATE_LOAD_EXAMPLES'] = 'yes'
        create_app()
        self.assertTrue(examples.status()['loaded'])
        entries = store.read_audit_log()
        self.assertEqual([(e['actor'], e['action']) for e in entries], [('startup', 'load_examples')])

    def test_restarting_with_the_setting_on_adds_nothing(self):
        os.environ['QUERYAPIGATE_LOAD_EXAMPLES'] = '1'
        create_app()
        create_app()
        self.assertEqual(len(store.read_audit_log()), 1)
        for file in store.list_saved():
            self.assertEqual(len(file['versions']), 1)

    def test_a_no_word_or_unset_does_not_remove_anything(self):
        os.environ['QUERYAPIGATE_LOAD_EXAMPLES'] = 'yes'
        create_app()
        os.environ['QUERYAPIGATE_LOAD_EXAMPLES'] = 'no'
        create_app()
        self.assertTrue(examples.status()['loaded'])  # removal is explicit: `examples unload`

    def test_a_malformed_value_fails_startup_loudly(self):
        for value in ('maybe', 'ye', '2'):
            os.environ['QUERYAPIGATE_LOAD_EXAMPLES'] = value
            with self.assertRaises(ValueError, msg=value):
                config.check_settings()
            with self.assertRaises(ValueError, msg=value):
                create_app()
        for value in ('', 'yes', 'NO', 'On', '0', 'TRUE'):
            os.environ['QUERYAPIGATE_LOAD_EXAMPLES'] = value
            config.check_settings()  # must not raise

    def test_a_conflict_is_a_warning_not_a_startup_failure(self):
        self.save_user_query('example_top_films')
        os.environ['QUERYAPIGATE_LOAD_EXAMPLES'] = 'yes'
        with self.assertLogs('queryapigate', level='WARNING') as captured:
            app = create_app()
        self.assertTrue(any('could not be loaded' in line for line in captured.output))
        self.assertEqual(app.test_client().get('/health').status_code, 200)
        self.assertEqual(self.saved_files(), ['example_top_films.json'])

    def test_a_really_unwritable_home_is_a_warning_not_a_crash(self):
        # This crashed the container: examples.load() let SQLite's own OperationalError escape, which the startup hook
        # (catching only ApiError/OSError) did not handle, so the worker died and gunicorn kept restarting it.
        self.make_home_unwritable()
        os.environ['QUERYAPIGATE_LOAD_EXAMPLES'] = 'yes'
        with self.assertLogs('queryapigate', level='WARNING') as captured:
            app = create_app()
        self.assertTrue(any('could not be loaded' in line for line in captured.output), captured.output)
        self.assertEqual(app.test_client().get('/health').status_code, 200)

    def test_an_unexpected_failure_while_loading_is_still_only_a_warning(self):
        os.environ['QUERYAPIGATE_LOAD_EXAMPLES'] = 'yes'
        with mock.patch.object(examples, 'load', side_effect=RuntimeError('something new')):
            with self.assertLogs('queryapigate', level='WARNING') as captured:
                app = create_app()
        self.assertTrue(any('something new' in line for line in captured.output), captured.output)
        self.assertEqual(app.test_client().get('/health').status_code, 200)

    def test_a_read_only_home_is_a_warning_not_a_startup_failure(self):
        os.environ['QUERYAPIGATE_LOAD_EXAMPLES'] = 'yes'
        with mock.patch.object(examples, 'load', side_effect=OSError('read-only file system')):
            with self.assertLogs('queryapigate', level='WARNING') as captured:
                app = create_app()
        self.assertTrue(any('read-only file system' in line for line in captured.output))
        self.assertEqual(app.test_client().get('/health').status_code, 200)


class CliTests(ExamplesTestCase):
    def test_status_load_unload(self):
        code, out, _ = self.run_cli('examples', 'status')
        self.assertEqual((code, 'Not loaded' in out), (0, True))
        code, out, err = self.run_cli('examples', 'load')
        self.assertEqual(code, 0, err)
        self.assertIn('11 queries in 4 collections', out)
        code, out, _ = self.run_cli('examples', 'load')
        self.assertIn('already loaded', out)
        code, out, _ = self.run_cli('examples', 'status')
        self.assertIn('Loaded: 11 queries', out)
        code, out, _ = self.run_cli('examples', 'unload')
        self.assertEqual(code, 0)
        self.assertIn('Removed 11 example queries', out)
        code, out, _ = self.run_cli('examples', 'unload')
        self.assertIn('nothing changed', out)

    def test_a_conflict_exits_1_with_the_reason(self):
        self.save_user_query('example_top_films')
        code, _, err = self.run_cli('examples', 'load')
        self.assertEqual(code, 1)
        self.assertIn('Nothing loaded', err)
        self.assertEqual(self.saved_files(), ['example_top_films.json'])

    def test_unload_warns_about_inert_grants(self):
        self.run_cli('examples', 'load')
        apikeys.create_key('partner-key', connections=[], collections=['examples-partner'])
        _, out, _ = self.run_cli('examples', 'unload')
        self.assertIn('partner-key', out)

    def test_an_unwritable_home_is_a_clean_message_not_a_traceback(self):
        self.make_home_unwritable()
        code, _, err = self.run_cli('examples', 'load')
        self.assertEqual(code, 1)
        self.assertIn('queryapigate examples load:', err)
        self.assertNotIn('Traceback', err)

    def test_bare_command_shows_help_and_a_leftover_old_setting_still_stops_it(self):
        code, out, _ = self.run_cli('examples')
        self.assertEqual((code, 'load' in out), (2, True))
        with mock.patch.dict(os.environ, {'SQL2API_API_KEY': 'secret'}):
            code, _, err = self.run_cli('examples', 'load')
        self.assertEqual(code, 2)
        self.assertNotIn('secret', err)
        self.assertEqual(self.saved_files(), [])

    def test_the_home_flag(self):
        with tempfile.TemporaryDirectory() as other:
            code, _, err = self.run_cli('examples', 'load', '--home', other)
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.isfile(os.path.join(other, examples.DB_FILE)))


if __name__ == '__main__':
    unittest.main()
