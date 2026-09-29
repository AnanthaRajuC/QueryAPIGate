"""Tests for `queryapigate collection export|import` (BACKLOG #35): a collection as one portable bundle,
validated exactly like saving a query and all-or-nothing before anything is written."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from queryapigate import bundle, cli, db, store
from queryapigate.errors import ApiError
from tests.helpers import write_connections


class BundleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE', 'QUERYAPIGATE_AUDIT_LOG_LIMIT'):
            os.environ.pop(name, None)
        write_connections({'a': {'db': 'sqlite', 'database': 'x.db', 'active': True}})

    def save(self, name, collection=store._UNSET, **extra):
        fields = {'sql_query': 'SELECT 1', 'author': 'me', 'description': f'{name} d', 'tags': ['t'],
                  'query_parameters': {}, 'connection_name': 'a', **extra}
        store.save_version(name, fields, collection)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def doc(self, queries, **top):
        return {'format': bundle.FORMAT, 'format_version': 1, 'collection': 'reporting', 'queries': queries, **top}

    def entry(self, name, **extra):
        return {'name': name, 'sql_query': 'SELECT 1', 'author': 'me', 'description': 'd', **extra}

    def files(self):
        names = sorted(row[0] for row in db.connection().execute('SELECT name FROM saved_queries').fetchall())
        return [f'{n}.json' for n in names]


class ExportTests(BundleTestCase):
    def test_exports_only_the_collections_queries_definitions_only(self):
        self.save('q1', 'reporting', cache_ttl=30, query_parameters={'id': {'type': 'int'}},
                  sql_query='SELECT :id')
        self.save('q2', 'reporting')
        self.save('other', 'ops')
        self.save('loose')
        document = bundle.export_bundle('reporting')
        self.assertEqual((document['format'], document['format_version'], document['collection']),
                         (bundle.FORMAT, 1, 'reporting'))
        self.assertEqual([q['name'] for q in document['queries']], ['q1', 'q2'])
        q1 = document['queries'][0]
        self.assertEqual(q1['cache_ttl'], 30)
        self.assertEqual(q1['query_parameters'], {'id': {'type': 'int'}})
        for forbidden in ('execution_history', 'uuid', 'created_at', 'status', 'version', 'collection'):
            self.assertNotIn(forbidden, q1)
        self.assertNotIn('cache_ttl', document['queries'][1])  # unset fields are omitted, not null

    def test_exports_the_latest_version(self):
        self.save('q1', 'reporting', sql_query='SELECT 1')
        self.save('q1', sql_query='SELECT 2')
        self.assertEqual(bundle.export_bundle('reporting')['queries'][0]['sql_query'], 'SELECT 2')

    def test_an_unknown_or_empty_collection_is_an_error(self):
        with self.assertRaises(ApiError) as caught:
            bundle.export_bundle('nope')
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(ApiError):
            bundle.export_bundle('Bad Name')

    def test_cli_writes_stdout_or_a_file(self):
        self.save('q1', 'reporting')
        code, out, _ = self.cli('collection', 'export', 'reporting')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['queries'][0]['name'], 'q1')
        target = os.path.join(self.home, 'out', 'r.json')
        code, _, err = self.cli('collection', 'export', 'reporting', '--out', target)
        self.assertEqual(code, 0, err)
        with open(target) as f:
            self.assertEqual(json.load(f)['collection'], 'reporting')
        self.assertFalse(any(n.endswith('.part') for n in os.listdir(os.path.dirname(target))))

    def test_cli_failure_is_a_clean_message_and_writes_nothing(self):
        target = os.path.join(self.home, 'r.json')
        code, _, err = self.cli('collection', 'export', 'nope', '--out', target)
        self.assertEqual(code, 1)
        self.assertIn('queryapigate collection export:', err)
        self.assertFalse(os.path.exists(target))


class RoundTripTests(BundleTestCase):
    def test_an_export_imports_into_another_home_identically(self):
        self.save('q1', 'reporting', cache_ttl=30, query_parameters={'id': {'type': 'int'}}, sql_query='SELECT :id')
        self.save('q2', 'reporting', tags=['x', 'y'])
        document = bundle.export_bundle('reporting')
        with tempfile.TemporaryDirectory() as other:
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': other}):
                write_connections({'a': {'db': 'sqlite', 'database': 'y.db', 'active': True}})
                result = bundle.import_bundle(document)
                self.assertEqual(result['created'], ['q1', 'q2'])
                self.assertEqual(result['missing_connections'], [])
                self.assertEqual(store.collection_members(), {'reporting': ['q1', 'q2']})
                again = bundle.export_bundle('reporting')
        again.pop('exported_at')
        document.pop('exported_at')
        self.assertEqual(again, document)  # what came out is what went in


class ImportTests(BundleTestCase):
    def test_creates_queries_in_the_bundles_collection_and_audits(self):
        result = bundle.import_bundle(self.doc([self.entry('q1'), self.entry('q2')]))
        self.assertEqual((result['created'], result['skipped']), (['q1', 'q2'], []))
        self.assertEqual(store.collection_members(), {'reporting': ['q1', 'q2']})
        audit = store.read_audit_log()
        self.assertEqual([e['target'] for e in audit], ['q1', 'q2'])
        self.assertEqual((audit[0]['actor'], audit[0]['changes']['source'], audit[0]['changes']['collection']),
                         ('cli', 'import', 'reporting'))

    def test_collection_override(self):
        bundle.import_bundle(self.doc([self.entry('q1')]), collection_override='staging')
        self.assertEqual(store.collection_members(), {'staging': ['q1']})

    def test_dry_run_writes_nothing(self):
        result = bundle.import_bundle(self.doc([self.entry('q1')]), dry_run=True)
        self.assertEqual(result['created'], ['q1'])
        self.assertEqual(self.files(), [])
        self.assertEqual(store.read_audit_log(), [])

    def test_any_invalid_query_stops_everything_before_the_first_write(self):
        good, bad = self.entry('good'), self.entry('bad', query_parameters={'ghost': {'type': 'int'}})
        with self.assertRaises(ApiError) as caught:
            bundle.import_bundle(self.doc([good, bad]))
        self.assertIn("Query 'bad'", caught.exception.message)
        self.assertIn('ghost', caught.exception.message)
        self.assertEqual(self.files(), [])

    def test_validation_is_the_same_as_saving(self):
        cases = [self.entry('q', author=''), self.entry('q', cache_ttl=-1), self.entry('q', connection_name=5),
                 self.entry('q', tags=5), self.entry('q', sql_query=''), self.entry('bad name/x'),
                 {'name': 'q'}]
        for entry in cases:
            with self.assertRaises(ApiError, msg=str(entry)):
                bundle.import_bundle(self.doc([entry]))
        self.assertEqual(self.files(), [])

    def test_unknown_entry_fields_are_rejected_not_dropped(self):
        for field in ('collection', 'uuid', 'execution_history', 'nonsense'):
            with self.assertRaises(ApiError, msg=field):
                bundle.import_bundle(self.doc([self.entry('q1', **{field: 'x'})]))

    def test_malformed_bundles(self):
        one = [self.entry('q')]
        for document in ('x', [], {}, {'format': 'other'}, self.doc([]), self.doc(one, format_version=2),
                         self.doc(one, collection='Bad'), self.doc(one * 2), self.doc('nope')):
            with self.assertRaises(ApiError, msg=str(document)):
                bundle.import_bundle(document)
        self.assertEqual(self.files(), [])

    def test_conflict_default_imports_nothing(self):
        self.save('q1')
        with self.assertRaises(ApiError) as caught:
            bundle.import_bundle(self.doc([self.entry('fresh'), self.entry('q1')]))
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('q1', caught.exception.message)
        self.assertEqual(self.files(), ['q1.json'])  # 'fresh' was not written either

    def test_skip_leaves_existing_queries_alone_and_is_how_an_interrupted_import_finishes(self):
        self.save('q1', sql_query='SELECT 99')
        result = bundle.import_bundle(self.doc([self.entry('q1'), self.entry('q2')]), on_conflict='skip')
        self.assertEqual((result['created'], result['skipped']), (['q2'], ['q1']))
        _, data = store.select_version(store.load_versions(store.resolve_saved_file('q1')))
        self.assertEqual(data['sql_query'], 'SELECT 99')
        self.assertEqual(store.collection_members(), {'reporting': ['q2']})  # q1's collection untouched (none)

    def test_new_version_adds_a_version_for_an_uncollected_or_same_collection_query(self):
        self.save('q1')
        self.save('q2', 'reporting')
        result = bundle.import_bundle(self.doc([self.entry('q1', sql_query='SELECT 2'),
                                                self.entry('q2', sql_query='SELECT 3')]), on_conflict='new-version')
        self.assertEqual(result['new_versions'], ['q1', 'q2'])
        content = store.load_versions(store.resolve_saved_file('q1'))
        self.assertEqual(store.select_version(content)[0], 2)
        self.assertEqual(store.collection_members(), {'reporting': ['q1', 'q2']})

    def test_new_version_never_moves_a_query_out_of_a_different_collection(self):
        self.save('q1', 'ops')
        with self.assertRaises(ApiError) as caught:
            bundle.import_bundle(self.doc([self.entry('fresh'), self.entry('q1')]), on_conflict='new-version')
        self.assertIn("q1 (in collection 'ops')", caught.exception.message)
        self.assertEqual(self.files(), ['q1.json'])
        self.assertEqual(store.collection_members(), {'ops': ['q1']})

    def test_missing_connections_are_a_warning_not_an_error(self):
        result = bundle.import_bundle(self.doc([self.entry('q1', connection_name='nowhere'),
                                                self.entry('q2', connection_name='a')]))
        self.assertEqual(result['missing_connections'], ['nowhere'])
        self.assertEqual(result['created'], ['q1', 'q2'])

    def test_an_invalid_policy_is_rejected(self):
        with self.assertRaises(ApiError):
            bundle.import_bundle(self.doc([self.entry('q1')]), on_conflict='overwrite')


class ImportCliTests(BundleTestCase):
    def write_bundle(self, document):
        path = os.path.join(self.home, 'bundle.json')
        with open(path, 'w') as f:
            json.dump(document, f)
        return path

    def test_import_prints_a_summary_and_warns_about_connections(self):
        path = self.write_bundle(self.doc([self.entry('q1', connection_name='nowhere')]))
        code, out, err = self.cli('collection', 'import', path)
        self.assertEqual(code, 0, err)
        self.assertIn("Imported into collection 'reporting': 1 new", out)
        self.assertIn('nowhere', out)

    def test_dry_run_flag(self):
        path = self.write_bundle(self.doc([self.entry('q1')]))
        code, out, _ = self.cli('collection', 'import', path, '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('Would import', out)
        self.assertEqual(self.files(), [])

    def test_conflict_exits_1_with_the_reason(self):
        self.save('q1')
        path = self.write_bundle(self.doc([self.entry('q1')]))
        code, _, err = self.cli('collection', 'import', path)
        self.assertEqual(code, 1)
        self.assertIn('Nothing imported', err)
        code, _, _ = self.cli('collection', 'import', path, '--on-conflict', 'skip')
        self.assertEqual(code, 0)

    def test_bad_files_are_clean_errors(self):
        code, _, err = self.cli('collection', 'import', os.path.join(self.home, 'missing.json'))
        self.assertEqual(code, 1)
        broken = os.path.join(self.home, 'broken.json')
        with open(broken, 'w') as f:
            f.write('{not json')
        code, _, err = self.cli('collection', 'import', broken)
        self.assertEqual(code, 1)
        self.assertIn('not valid JSON', err)

    def test_a_leftover_old_prefix_setting_still_stops_these_commands(self):
        path = self.write_bundle(self.doc([self.entry('q1')]))
        with mock.patch.dict(os.environ, {'SQL2API_API_KEY': 'secret'}):
            code, _, err = self.cli('collection', 'import', path)
        self.assertEqual(code, 2)
        self.assertEqual(self.files(), [])
        self.assertNotIn('secret', err)

    def test_bare_collection_command_shows_help(self):
        code, out, _ = self.cli('collection')
        self.assertEqual(code, 2)
        self.assertIn('export', out)


if __name__ == '__main__':
    unittest.main()
