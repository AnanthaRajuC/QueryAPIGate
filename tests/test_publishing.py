"""Publishing saved-query versions (schema 4, ADR 0001's Management API): a version is a draft until published,
and everything that serves a query - /q/<name>, MCP, the catalog, OpenAPI, Postman, bundle export, the CLI -
serves only the published one."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import apikeys, bundle, create_app, db, history, mcp_server, store
from tests.helpers import save_query, write_connections

ADMIN = {'X-API-Key': 'admin-key'}


class PublishingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(history.flush)  # runs first: nothing queued is written after the home is gone
        self.db_path = os.path.join(self.tmp.name, 'data.db')
        sqlite3.connect(self.db_path).close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True}})
        self.client = create_app().test_client()
        res = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': ['lite']}, headers=ADMIN)
        self.scoped = {'X-API-Key': res.get_json()['secret']}

    def save(self, value, publish=True, name='q'):
        """A version whose single result row is `value`, so a response shows which version ran."""
        fields = {'author': 'a', 'description': 'd', 'sql_query': f'SELECT {value} AS v', 'connection_name': 'lite'}
        return store.save_version(name, fields, publish=publish)[1]

    def served(self, headers, query=''):
        res = self.client.get(f'/q/q{query}', headers=headers)
        if res.status_code != 200:
            return res.status_code
        return res.get_json()[0]['v']


class DraftTests(PublishingTestCase):
    def test_the_legacy_save_route_still_publishes_what_it_saves(self):
        res = save_query(self.client, headers=ADMIN, body={
            'author': 'a', 'description': 'd', 'sql_query': 'SELECT 7 AS v', 'filename': 'q',
            'connection_name': 'lite'})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.served(self.scoped), 7)
        self.assertEqual(store.read_published(store.load_versions('q')), 1)

    def test_a_draft_is_not_served_until_published(self):
        self.save(1)
        self.save(2, publish=False)
        self.assertEqual(self.served(self.scoped), 1)
        self.assertEqual(self.served(ADMIN), 1)  # the admin key too, unless it asks for the draft by number

        store.publish_version('q', 2)
        self.assertEqual(self.served(self.scoped), 2)

    def test_only_the_admin_key_can_run_a_draft_and_only_by_number(self):
        self.save(1)
        self.save(2, publish=False)
        self.assertEqual(self.served(ADMIN, '?version=2'), 2)
        res = self.client.get('/q/q?version=2', headers=self.scoped)
        self.assertEqual(res.status_code, 404)
        # Indistinguishable from a version that was never created, so drafts can't be discovered
        self.assertEqual(res.get_json(), self.client.get('/q/q?version=99', headers=self.scoped).get_json()
                         | {'error': 'Version 2 not found'})

    def test_older_versions_stay_runnable_by_number_as_before(self):
        self.save(1)
        self.save(2)
        self.assertEqual(self.served(self.scoped, '?version=1'), 1)

    def test_rolling_back_makes_newer_versions_drafts_again(self):
        self.save(1)
        self.save(2)
        self.assertEqual(store.publish_version('q', 1), 2)
        self.assertEqual(self.served(self.scoped), 1)
        self.assertEqual(self.client.get('/q/q?version=2', headers=self.scoped).status_code, 404)

    def test_an_unpublished_query_is_not_served_at_all(self):
        self.save(1)
        self.assertEqual(store.unpublish('q'), 1)
        self.assertEqual(self.served(self.scoped), 404)
        self.assertEqual(self.served(ADMIN), 404)
        self.assertEqual(self.served(ADMIN, '?version=1'), 1)  # the admin key can still test it

    def test_publishing_a_version_that_does_not_exist_is_a_404(self):
        self.save(1)
        with self.assertRaises(Exception) as caught:
            store.publish_version('q', 5)
        self.assertEqual(getattr(caught.exception, 'status', None), 404)
        self.assertEqual(store.read_published(store.load_versions('q')), 1)


class DiscoveryTests(PublishingTestCase):
    def test_catalog_openapi_and_mcp_describe_only_published_queries(self):
        self.save(1, name='live')
        self.save(1, name='draft_only', publish=False)
        catalog = [q['name'] for q in self.client.get('/catalog', headers=self.scoped).get_json()['queries']]
        self.assertEqual(catalog, ['live'])
        paths = self.client.get('/openapi.json', headers=self.scoped).get_json()['paths']
        self.assertIn('/q/live', paths)
        self.assertNotIn('/q/draft_only', paths)
        tools = [t['name'] for t in mcp_server.list_tools_for(apikeys.OPEN)]
        self.assertIn('live', tools)
        self.assertNotIn('draft_only', tools)

    def test_descriptions_follow_the_published_version_not_the_newest(self):
        store.save_version('q', {'author': 'a', 'description': 'published one', 'sql_query': 'SELECT 1 AS v',
                                 'connection_name': 'lite', 'collection': None}, collection='c1')
        store.save_version('q', {'author': 'a', 'description': 'draft one', 'sql_query': 'SELECT 2 AS v',
                                 'connection_name': 'lite'}, publish=False)
        catalog = self.client.get('/catalog', headers=self.scoped).get_json()['queries']
        self.assertEqual([q['description'] for q in catalog], ['published one'])
        exported = bundle.export_bundle('c1')['queries']
        self.assertEqual([q['description'] for q in exported], ['published one'])

    def test_admin_listing_still_shows_every_query_and_which_version_is_published(self):
        self.save(1)
        self.save(2, publish=False)
        self.save(1, name='never', publish=False)
        listed = {q['name']: q for q in self.client.get('/api/v1/queries', headers=ADMIN).get_json()['items']}
        self.assertEqual((listed['q']['published_version'], listed['q']['latest_version']), (1, 2))
        self.assertIsNone(listed['never']['published_version'])
        self.assertEqual(sorted(name for name, *_ in store.latest_versions()), ['never', 'q'])
        self.assertEqual([name for name, *_ in store.live_versions()], ['q'])


class DeletionTests(PublishingTestCase):
    def test_deleting_the_published_version_falls_back_to_an_older_one_never_a_draft(self):
        self.save(1)
        self.save(2)
        self.save(3, publish=False)
        store.delete_saved('q', 2)
        self.assertEqual(store.read_published(store.load_versions('q')), 1)
        self.assertEqual(self.served(self.scoped), 1)

    def test_deleting_the_only_published_version_leaves_the_query_unpublished(self):
        self.save(1)
        self.save(2, publish=False)
        store.delete_saved('q', 1)
        self.assertIsNone(store.read_published(store.load_versions('q')))
        self.assertEqual(self.served(self.scoped), 404)

    def test_deleting_a_draft_leaves_the_published_version_alone(self):
        self.save(1)
        self.save(2, publish=False)
        store.delete_saved('q', 2)
        self.assertEqual(store.read_published(store.load_versions('q')), 1)


class UpgradeTests(unittest.TestCase):
    """A schema-3 store (before publishing existed) upgrades with every query published at its newest version:
    callers see no difference."""

    def test_existing_queries_are_published_at_their_newest_version(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            create_app()  # creates a current store
            conn = db.connection()
            store.save_version('a', {'sql_query': 'SELECT 1'})
            store.save_version('a', {'sql_query': 'SELECT 2'})
            store.save_version('b', {'sql_query': 'SELECT 3'})
            # Take the store back to schema 3: no published_version column
            conn.execute('ALTER TABLE saved_queries DROP COLUMN published_version')
            conn.execute('UPDATE schema_version SET version = 3')  # both backends run in autocommit mode
            for _ in range(2):  # and the upgrade is safe to run again
                db.init_schema()
                self.assertEqual(store.read_published(store.load_versions('a')), 2)
                self.assertEqual(store.read_published(store.load_versions('b')), 1)
            self.assertEqual(db.connection().execute('SELECT version FROM schema_version').fetchone()[0], 4)


if __name__ == '__main__':
    unittest.main()
