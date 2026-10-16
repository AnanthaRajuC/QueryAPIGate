"""Schema 17 (ADR 0023, delivery step 1): workspaces in the store. A store from the release before it comes up with
everything it had in `default`; each owned name is unique within its workspace only; and the foreign keys between
saved queries, their versions and their runs follow the new keys - so deleting one workspace's query cascades to its
own versions and runs, never another workspace's of the same name. On whichever store the suite runs on."""
import os

from queryapigate import create_app, db
from tests import TEST_DATABASE_URL
from tests.test_upgrades import FixtureTestCase, restore_postgres

OWNED = ('connections', 'saved_queries', 'saved_query_versions', 'execution_history', 'api_keys', 'roles',
         'destinations', 'exports', 'descriptions', 'usage_counters', 'config_managed', 'write_requests', 'events')
RELEASE = '0.37.0'  # the last release before schema 17


class WorkspaceStoreTests(FixtureTestCase):
    kind = '.postgres' if TEST_DATABASE_URL else ''

    def setUp(self):
        home, _ = self.open_home(RELEASE, self.kind)
        if self.kind:
            with open(os.path.join(home, 'store.sql')) as f:
                restore_postgres(f.read())
        create_app()  # upgrades the store
        self.conn = db.connection()

    def count(self, sql, *params):
        return self.conn.execute(sql, params).fetchone()[0]

    def test_everything_a_store_had_is_in_default(self):
        self.assertEqual(self.count('SELECT version FROM schema_version'), db.SCHEMA_VERSION)
        self.assertEqual([row[0] for row in self.conn.execute('SELECT name FROM workspaces').fetchall()], ['default'])
        for table in OWNED:
            with self.subTest(table=table):
                self.assertEqual(self.count(f"SELECT COUNT(*) FROM {table} WHERE workspace <> 'default'"), 0)
        # what the fixture made is all still there, in default
        self.assertEqual(self.count("SELECT COUNT(*) FROM saved_query_versions WHERE workspace = 'default' "
                                    "AND query_name = 'films_by_rating'"), 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM api_keys WHERE name = 'reporting'"), 1)

    def test_a_name_is_unique_within_its_workspace_only(self):
        with db.transaction() as tx:
            tx.execute("INSERT INTO workspaces (name, created_at, details_json) VALUES ('team-b', 'now', '{}')")
            tx.execute("INSERT INTO connections (workspace, name, db, active, created_at, updated_at, details_json) "
                       "VALUES ('team-b', 'films', 'sqlite', 1, 'now', 'now', '{}')")  # default has a 'films' too
        self.assertEqual(self.count("SELECT COUNT(*) FROM connections WHERE name = 'films'"), 2)
        with self.assertRaises(db.Error):  # but not twice in one workspace
            with db.transaction() as tx:
                tx.execute("INSERT INTO connections (workspace, name, db, active, created_at, updated_at, "
                           "details_json) VALUES ('team-b', 'films', 'sqlite', 1, 'now', 'now', '{}')")

    def test_deleting_a_query_cascades_within_its_own_workspace(self):
        with db.transaction() as tx:
            tx.execute("INSERT INTO workspaces (name, created_at, details_json) VALUES ('team-b', 'now', '{}')")
            tx.execute("INSERT INTO saved_queries (workspace, name, published_version) "
                       "VALUES ('team-b', 'films_by_rating', 1)")
            tx.execute("INSERT INTO saved_query_versions (workspace, query_name, version, uuid, created_at, "
                       "last_modified_at, fields_json) "
                       "VALUES ('team-b', 'films_by_rating', 1, 'u', 'now', 'now', '{}')")
            tx.execute("INSERT INTO execution_history (workspace, query_name, version, executed_at, entry_json) "
                       "VALUES ('team-b', 'films_by_rating', 1, 'now', '{}')")
        with db.transaction() as tx:
            tx.execute("DELETE FROM saved_queries WHERE workspace = 'default' AND name = 'films_by_rating'")
        for table, column in (('saved_query_versions', 'query_name'), ('execution_history', 'query_name')):
            with self.subTest(table=table):
                rows = self.conn.execute(f"SELECT workspace FROM {table} WHERE {column} = 'films_by_rating'").fetchall()
                self.assertEqual([row[0] for row in rows], ['team-b'])  # default's went with its query; team-b's stay

    def test_a_run_can_only_name_a_query_version_of_its_own_workspace(self):
        with self.assertRaises(db.Error):
            with db.transaction() as tx:  # films_by_rating v2 exists - in default, not team-b
                tx.execute("INSERT INTO workspaces (name, created_at, details_json) VALUES ('team-b', 'now', '{}')")
                tx.execute("INSERT INTO execution_history (workspace, query_name, version, executed_at, entry_json) "
                           "VALUES ('team-b', 'films_by_rating', 2, 'now', '{}')")
