"""The audit log as a hash chain (ADR 0012, BACKLOG #96): each kind of tampering found and named, the chain kept through
trims and upgrades, keyed with QUERYAPIGATE_AUDIT_KEY and rotated, many writers making one chain."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from queryapigate import alerts, auditchain, cli, config, create_app, db, store
from tests import TEST_DATABASE_URL

KEY, OTHER = 'k' * 40, 'o' * 40
ADMIN = {'X-API-Key': 'admin-key'}


class ChainTestCase(unittest.TestCase):
    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               'QUERYAPIGATE_AUDIT_KEY': '', 'QUERYAPIGATE_AUDIT_LOG_LIMIT': '500',
                                               'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE': '', **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_schema()
        with db.transaction() as conn:
            for table in ('audit_log', 'audit_chain'):
                conn.execute(f'DELETE FROM {table}')
        auditchain._checked['report'] = None

    def write(self, count, start=0):
        for i in range(start, start + count):
            store.record_audit('alice', 'update_key', f'key-{i}', {'n': i})

    def verify(self):
        return auditchain.verify(db.connection())

    def rows(self):
        return db.connection().execute('SELECT seq, prev_hash, hash, entry_json FROM audit_log ORDER BY seq').fetchall()

    def problem(self):
        report = self.verify()
        self.assertFalse(report['intact'], report)
        return report['break']['problem'], report['break']['seq']


class ChainTests(ChainTestCase):
    def test_entries_are_linked_one_to_the_next(self):
        self.write(4)
        rows = self.rows()
        self.assertEqual([r['seq'] for r in rows], [1, 2, 3, 4])
        self.assertEqual(rows[0]['prev_hash'], auditchain.GENESIS)
        self.assertEqual([r['prev_hash'] for r in rows[1:]], [r['hash'] for r in rows[:-1]])
        self.assertTrue(all(r['hash'].startswith('sha256:') for r in rows))
        self.assertEqual(self.verify(), {'intact': True, 'entries': 4, 'first_seq': 1, 'last_seq': 4, 'keyed': False,
                                         'unchecked_without_key': 0, 'entries_stored': 4})

    def test_a_changed_entry(self):
        self.write(5)
        with db.transaction() as conn:
            entry = json.loads(conn.execute('SELECT entry_json FROM audit_log WHERE seq = 3').fetchone()[0])
            entry['actor'] = 'mallory'
            conn.execute('UPDATE audit_log SET entry_json = ? WHERE seq = 3', (json.dumps(entry),))
        self.assertEqual(self.problem(), ('changed', 3))

    def test_a_removed_entry(self):
        self.write(5)
        with db.transaction() as conn:
            conn.execute('DELETE FROM audit_log WHERE seq = 3')
        self.assertEqual(self.problem(), ('missing', 4))

    def test_the_oldest_entries_removed_is_not_a_trim(self):
        self.write(5)
        with db.transaction() as conn:
            conn.execute('DELETE FROM audit_log WHERE seq <= 2')
        self.assertEqual(self.problem(), ('missing', 3))

    def test_the_newest_entries_removed_needs_a_copy_outside_to_show(self):
        self.write(5)
        with db.transaction() as conn:
            conn.execute('DELETE FROM audit_log WHERE seq = 5')
        self.assertTrue(self.verify()['intact'])  # the chain's end can't know what came after it: the SIEM copy does
        self.write(1, start=9)  # ... and the next entry continues from what's left, so seq 5 is used again
        self.assertEqual(self.rows()[-1]['seq'], 5)

    def test_an_entry_inserted_or_swapped(self):
        self.write(4)
        with db.transaction() as conn:
            two, three = (conn.execute('SELECT entry_json FROM audit_log WHERE seq = ?', (n,)).fetchone()[0]
                          for n in (2, 3))
            conn.execute('UPDATE audit_log SET entry_json = ? WHERE seq = 2', (three,))
            conn.execute('UPDATE audit_log SET entry_json = ? WHERE seq = 3', (two,))
        self.assertEqual(self.problem(), ('changed', 2))

    def test_a_rewrite_recomputing_every_hash_after_an_edit_passes_unkeyed(self):
        self.write(3)
        self._rewrite_seq_2_and_rehash(key=None)
        self.assertTrue(self.verify()['intact'])  # what a plain hash can't catch - and why the key exists

    def _rewrite_seq_2_and_rehash(self, key):
        with db.transaction() as conn:
            rows = conn.execute('SELECT seq, entry_json FROM audit_log ORDER BY seq').fetchall()
            prev = auditchain.GENESIS
            for row in rows:
                entry = json.loads(row['entry_json'])
                if row['seq'] == 2:
                    entry['actor'] = 'mallory'
                digest = auditchain.compute(prev, row['seq'], entry, key)
                conn.execute('UPDATE audit_log SET entry_json = ?, prev_hash = ?, hash = ? WHERE seq = ?',
                             (json.dumps(entry), prev, digest, row['seq']))
                prev = digest


class KeyedTests(ChainTestCase):
    env = {'QUERYAPIGATE_AUDIT_KEY': KEY}

    def test_keyed_entries_and_a_rewrite_without_the_key(self):
        self.write(3)
        self.assertTrue(all(r['hash'].startswith('hmac-sha256:') for r in self.rows()))
        self.assertTrue(self.verify()['keyed'])
        ChainTests._rewrite_seq_2_and_rehash(self, key=None)  # all of it re-hashed, unkeyed
        self.assertEqual(self.problem(), ('unkeyed_after_keyed', 3))
        ChainTests._rewrite_seq_2_and_rehash(self, key=OTHER)  # ... or keyed with a key that isn't ours
        self.assertEqual(self.problem()[0], 'changed')

    def test_without_the_key_keyed_entries_are_not_checked(self):
        self.write(2)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_KEY': ''}):
            report = self.verify()
            out = io.StringIO()
            with contextlib.redirect_stderr(out), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(['audit', 'verify'])
        self.assertEqual((report['intact'], report['unchecked_without_key']), (True, 2))
        self.assertEqual(code, 2)  # nothing was checked: not "intact"
        self.assertIn('Not checked: all 2 entries are keyed', out.getvalue())

    def test_setting_a_key_is_recorded_and_rotating_it_too(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_KEY': ''}):
            self.write(2)  # before any key
        create_app()
        self.assertEqual(store.read_audit_log()[-1]['action'], 'audit_key_set')
        self.assertTrue(self.verify()['intact'])
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_KEY': f'{OTHER},{KEY}'}):
            create_app()
            self.assertEqual(store.read_audit_log()[-1]['action'], 'audit_key_rotated')
            self.write(1)
            self.assertTrue(self.verify()['intact'])  # old entries under KEY, new under OTHER
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_KEY': OTHER}):
            self.assertEqual(self.problem()[0], 'changed')  # KEY no longer listed: its entries can't be checked as ok

    def test_a_short_or_repeated_key_is_refused(self):
        for value in ('short', f'{KEY},{KEY}'):
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_KEY': value}), \
                    self.assertRaises(ValueError):
                config.check_settings()


class TrimAndUpgradeTests(ChainTestCase):
    env = {'QUERYAPIGATE_AUDIT_LOG_LIMIT': '3'}

    def test_the_rolling_cap_moves_the_anchor(self):
        self.write(7)
        self.assertEqual([r['seq'] for r in self.rows()], [5, 6, 7])
        anchor = db.connection().execute('SELECT seq, hash FROM audit_chain').fetchone()
        self.assertEqual(anchor[0], 4)
        self.assertTrue(self.verify()['intact'])
        with db.transaction() as conn:
            conn.execute('DELETE FROM audit_log WHERE seq = 5')  # not a trim: the anchor says 4 came last
        self.assertEqual(self.problem(), ('missing', 6))

    def test_entries_an_older_instance_wrote_unlinked_are_linked_in(self):
        self.write(1)
        with db.transaction() as conn:  # a 0.24 process, during a rolling upgrade
            conn.execute('INSERT INTO audit_log (timestamp, entry_json) VALUES (?, ?)',
                         ('2026-10-12 09:00:00', json.dumps({'timestamp': '2026-10-12 09:00:00', 'action': 'old'})))
        self.write(1, start=1)
        self.assertEqual([json.loads(r['entry_json'])['action'] for r in self.rows()],
                         ['update_key', 'old', 'update_key'])
        self.assertTrue(self.verify()['intact'])


@unittest.skipIf(TEST_DATABASE_URL, 'builds a SQLite store as 0.24 left it')
class UpgradeFromSchema13Tests(unittest.TestCase):
    def test_existing_entries_are_chained_and_the_start_recorded(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home,
                                                                                 'QUERYAPIGATE_AUDIT_KEY': ''}):
            conn = sqlite3.connect(os.path.join(home, 'queryapigate.db'))
            conn.executescript("""
                CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES (13);
                CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                                        entry_json TEXT NOT NULL);""")
            for i in range(3):
                conn.execute('INSERT INTO audit_log (timestamp, entry_json) VALUES (?, ?)',
                             (f'2026-10-0{i + 1} 10:00:00', json.dumps({'action': f'old-{i}'})))
            conn.commit()
            conn.close()
            db.init_schema()
            entries = store.read_audit_log()
            self.assertEqual([e['action'] for e in entries], ['old-0', 'old-1', 'old-2', 'audit_chain_started'])
            self.assertEqual(entries[-1]['changes'], {'entries_chained': 3})
            report = auditchain.verify(db.connection())
            self.assertEqual((report['intact'], report['entries']), (True, 4))


class ManyWritersTests(ChainTestCase):
    def test_writers_at_once_make_one_chain(self):
        threads = [threading.Thread(target=self.write, args=(10, i * 10)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual([r['seq'] for r in self.rows()], list(range(1, 51)))
        self.assertTrue(self.verify()['intact'])


class ExportFileTests(ChainTestCase):
    def test_the_export_file_carries_the_chain_and_verifies_on_its_own(self):
        path = os.path.join(self.tmp.name, 'audit.jsonl')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE': path}):
            self.write(4)
        lines = [json.loads(line) for line in open(path)]
        self.assertEqual([line['seq'] for line in lines], [1, 2, 3, 4])
        self.assertTrue(auditchain.verify_file(path)['intact'])
        lines[1]['target'] = 'forged'
        with open(path, 'w') as f:
            f.writelines(json.dumps(line) + '\n' for line in lines)
        self.assertEqual(auditchain.verify_file(path)['break']['problem'], 'changed')


class SiemCopyTests(ChainTestCase):
    """What a SIEM received (ADR 0019): delivery is at least once, so its copy may hold an entry twice, and after a
    retry not in order - an untouched copy still verifies; a copy someone edited still doesn't."""

    def copy(self, lines, name='siem.log'):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'w') as f:
            f.writelines(line + '\n' for line in lines)
        return path

    def entries(self, n=5):
        path = os.path.join(self.tmp.name, 'audit.jsonl')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE': path}):
            self.write(n)
        return [line.rstrip('\n') for line in open(path)]

    @staticmethod
    def as_syslog(line):
        entry = json.loads(line)
        return (f'2026-10-13T10:00:00+00:00 api-1 queryapigate[42]: <110>1 2026-10-13T10:00:00Z api-1 queryapigate 42 '
                f'{entry["action"]} [meta sequenceId="{entry["seq"]}"] {line}')

    def test_exact_repeats_are_dropped_and_counted(self):
        lines = self.entries()
        report = auditchain.verify_file(self.copy(lines[:3] + lines[1:3] + lines[3:]))  # 2 and 3 sent again
        self.assertTrue(report['intact'], report)
        self.assertEqual((report['first_seq'], report['last_seq'], report['repeats']), (1, 5, 2))

    def test_entries_are_walked_in_seq_order_whatever_order_they_arrived_in(self):
        lines = self.entries()
        self.assertTrue(auditchain.verify_file(self.copy([lines[0], lines[3], lines[1], lines[2], lines[4]]))['intact'])

    def test_a_repeat_that_differs_is_never_dropped(self):
        lines = self.entries()
        forged = json.loads(lines[2])
        forged['target'] = 'forged'
        report = auditchain.verify_file(self.copy([*lines, json.dumps(forged)]))
        self.assertFalse(report['intact'])
        self.assertEqual(report['break']['problem'], 'conflicting_copies')
        self.assertEqual((report['break']['seq'], report['break']['lines']), (3, [3, 6]))

    def test_syslog_lines_as_a_receiver_writes_them(self):
        lines = [self.as_syslog(line) for line in self.entries()]
        self.assertTrue(auditchain.verify_file(self.copy(lines + lines[:1]))['intact'])
        edited = lines[1].replace('"seq": 2', '"seq": 2, "note": "added"')
        self.assertNotEqual(edited, lines[1])
        report = auditchain.verify_file(self.copy([lines[0], edited, *lines[2:]]))
        self.assertEqual(report['break']['problem'], 'changed')

    def test_a_missing_entry_is_still_missing(self):
        lines = self.entries()
        report = auditchain.verify_file(self.copy(lines[:2] + lines[3:]))
        self.assertEqual((report['break']['problem'], report['break']['seq']), ('missing', 4))

    def test_the_cli_says_how_many_repeats_it_dropped(self):
        lines = self.entries(3)
        path = self.copy(lines + lines[1:2])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['audit', 'verify', '--file', path]), 0)
        self.assertIn('Intact: entries 1 to 3', out.getvalue())
        self.assertIn('1 repeated entries dropped', out.getvalue())


class SurfaceTests(ChainTestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['audit', 'verify', *argv])
        return code, out.getvalue() + err.getvalue()

    def test_the_api_the_alert_and_the_cli(self):
        self.write(3)
        client = create_app().test_client()
        items = client.get('/api/v1/audit', headers=ADMIN).get_json()['items']
        self.assertEqual([(e['seq'], e['hash'][:7]) for e in items][-1], (1, 'sha256:'))
        self.assertTrue(client.get('/api/v1/audit/verify', headers=ADMIN).get_json()['intact'])
        self.assertEqual(self.run_cli()[0], 0)
        self.assertIn('unkeyed', self.run_cli()[1])
        with db.transaction() as conn:
            conn.execute("UPDATE audit_log SET entry_json = '{\"action\": \"nothing to see\"}' WHERE seq = 2")
        report = client.get('/api/v1/audit/verify', headers=ADMIN).get_json()
        self.assertEqual((report['intact'], report['break']['seq']), (False, 2))
        auditchain._checked['report'] = None
        found = [a for a in alerts.collect() if a['kind'] == 'audit_chain_broken']
        self.assertEqual((found[0]['severity'], found[0]['title']),
                         ('critical', 'The audit log has been tampered with'))
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn('BROKEN at entry 2', out)


if __name__ == '__main__':
    unittest.main()
