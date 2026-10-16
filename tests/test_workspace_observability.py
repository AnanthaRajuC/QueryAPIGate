"""What happened, per workspace (ADR 0023, delivery step 4): the audit log's entries say where they happened - inside
the hash chain - and a member reads its workspace's; alerts are a workspace's own, with the server's for server-wide
roles only; events reach their own workspace's subscribers; metrics tell two workspaces' `partner` keys apart."""
import asyncio
import queue
from datetime import date, timedelta
from unittest import mock

from queryapigate import admins, alerts, broadcast, events, history, metrics, scope
from tests.test_workspace_isolation import B, IsolationTestCase


class ObservabilityTestCase(IsolationTestCase):
    def member_of_b(self, role):
        admins.create_admin(f'b-{role}', None)
        _, secret = admins.issue_token(f'b-{role}')
        self.ok('put', f'/api/v1/workspaces/team-b/members/b-{role}', json={'role': role})
        return {'X-API-Key': secret}


class AuditTests(ObservabilityTestCase):
    def test_entries_say_where_they_happened_and_the_chain_holds_them(self):
        entries = self.ok('get', '/api/v1/audit').get_json()['items']
        where = {(e['action'], e['target']): e.get('workspace') for e in entries}
        self.assertEqual(where[('create_connection', 'lite')], 'team-b')  # team-b's own; default's came from setup
        self.assertEqual(where[('create_workspace', 'team-b')], 'team-b')  # about the workspace itself
        admins.create_admin('carol', 'developer')
        from queryapigate.services import administrators
        administrators.delete('carol', 'admin')
        server = [e for e in self.ok('get', '/api/v1/audit').get_json()['items'] if e['action'] == 'delete_admin']
        self.assertIsNone(server[0]['workspace'])  # the server's: its people
        self.assertTrue(self.ok('get', '/api/v1/audit/verify').get_json()['intact'])

    def test_a_member_reads_their_workspaces_entries_only(self):
        auditor = self.member_of_b('auditor')
        items = self.ok('get', f'{B}/api/v1/audit', headers=auditor).get_json()['items']
        self.assertTrue(items)
        self.assertEqual({e.get('workspace') for e in items}, {'team-b'})
        self.assertIn(('set_workspace_member', 'team-b'), {(e['action'], e['target']) for e in items})
        # the whole log, its chain and where it is delivered are the server's
        self.assertEqual(self.code('get', f'{B}/api/v1/audit/verify', 403, headers=auditor), 'role_forbidden')
        self.assertEqual(self.code('get', f'{B}/api/v1/audit/delivery', 403, headers=auditor), 'role_forbidden')
        self.assertEqual(self.code('get', f'{B}/api/v1/instances', 403, headers=auditor), 'role_forbidden')

    def test_a_server_wide_role_reads_all_of_it_or_one_workspaces(self):
        every = self.ok('get', '/api/v1/audit').get_json()['items']
        self.assertEqual({e.get('workspace') for e in every} >= {'default', 'team-b'}, True)
        only_b = self.ok('get', '/api/v1/audit?workspace=team-b').get_json()['items']
        self.assertEqual({e.get('workspace') for e in only_b}, {'team-b'})


class AlertTests(ObservabilityTestCase):
    def test_a_workspaces_alerts_are_its_own(self):
        soon = (date.today() + timedelta(days=2)).isoformat()
        self.ok('patch', f'{B}/api/v1/api-keys/partner', json={'expires_at': soon})
        def found(url):
            return {(a['kind'], a['target'].get('name')) for a in self.ok('get', url).get_json()['items']}
        b_kinds, kinds = found(f'{B}/api/v1/alerts'), found('/api/v1/alerts')
        self.assertIn(('key_expiring', 'partner'), b_kinds)
        self.assertNotIn(('key_expiring', 'partner'), kinds)  # default's partner doesn't expire

    def test_the_servers_alerts_are_for_server_wide_roles(self):
        admin = self.member_of_b('admin')
        with mock.patch.object(alerts, '_history_health', return_value=[alerts._alert(
                'critical', 'history_failed', 'history', 'History writes failing', 'detail')]):
            server_wide = {a['kind'] for a in self.ok('get', f'{B}/api/v1/alerts').get_json()['items']}
            member = {a['kind'] for a in self.ok('get', f'{B}/api/v1/alerts', headers=admin).get_json()['items']}
        self.assertIn('history_failed', server_wide)
        self.assertNotIn('history_failed', member)

    def test_a_key_hitting_its_rate_limit_is_its_workspaces(self):
        for _ in range(alerts.RATE_LIMITED_PER_HOUR):
            alerts.note_rate_limited('key', ('team-b', 'partner'))
        self.addCleanup(alerts._refusals.clear)
        b = [a for a in self.ok('get', f'{B}/api/v1/alerts').get_json()['items'] if a['kind'] == 'key_rate_limited']
        default = [a for a in self.ok('get', '/api/v1/alerts').get_json()['items'] if a['kind'] == 'key_rate_limited']
        self.assertEqual(([a['target']['name'] for a in b], default), (['partner'], []))


class EventTests(ObservabilityTestCase):
    def test_the_console_feed_hears_its_own_workspace(self):
        hub = broadcast.Broadcaster()
        team_b, everyone_default = hub.subscribe(workspace='team-b'), hub.subscribe()
        hub.publish({'n': 1}, 'partner', 'team-b')
        self.assertEqual(team_b.get_nowait(), {'n': 1})
        with self.assertRaises(queue.Empty):
            everyone_default.get_nowait()

    def test_the_event_log_replays_and_delivers_by_workspace(self):
        self.titles('/q/films')
        self.titles(f'{B}/q/films')
        history.flush()
        replay = [events._replay_page(0, None, {}, workspace=w) for w in ('default', 'team-b')]
        self.assertEqual([len(page) for page in replay], [1, 1])
        self.assertNotEqual(replay[0][0][0], replay[1][0][0])

        async def deliver():
            server = events.EventServer('127.0.0.1', 0)
            b_client, default_client = events._Client(None, mock.Mock(), workspace='team-b'), \
                events._Client(None, mock.Mock())
            server.by_key[('team-b', None)] = {b_client}
            server.by_key[('default', None)] = {default_client}
            server._deliver(999, 'partner', (events.RUN, 'saved', 'films', 'lite', 'success'), b'x', None, 'team-b')
            return b_client.queue.qsize(), default_client.queue.qsize()
        self.assertEqual(asyncio.run(deliver()), (1, 0))

    def test_queryapigate_events_settles_the_workspace_as_rest_does(self):
        from queryapigate import apikeys
        b_key = apikeys.authenticate(self.b_key)
        self.assertEqual(events._settle_workspace(b_key, None).workspace, 'team-b')
        self.assertIsNone(events._settle_workspace(b_key, 'default'))  # another workspace: none
        owner = apikeys.authenticate('admin-key')
        self.assertEqual(events._settle_workspace(owner, 'team-b').workspace, 'team-b')
        self.assertIsNone(events._settle_workspace(owner, 'no-such'))


class MetricsTests(ObservabilityTestCase):
    def test_two_workspaces_keys_and_connections_of_one_name_are_counted_apart(self):
        def counted():  # the counters are the process's: what this test adds to them
            with scope.using('team-b'):
                b = metrics.summary_for_key('partner')['queries']
            return b, metrics.summary_for_key('partner')['queries']
        b_before, default_before = counted()
        self.titles('/q/films', headers={'X-API-Key': self.default_key})
        self.titles(f'{B}/q/films', headers={'X-API-Key': self.b_key})
        self.titles(f'{B}/q/films', headers={'X-API-Key': self.b_key})
        rendered = self.client.get('/metrics', headers={'X-API-Key': 'admin-key'}).get_data(as_text=True)
        self.assertIn('connection="team-b/lite"', rendered)
        self.assertIn('key="team-b/partner"', rendered)
        b_after, default_after = counted()
        self.assertEqual((b_after - b_before, default_after - default_before), (2, 1))
