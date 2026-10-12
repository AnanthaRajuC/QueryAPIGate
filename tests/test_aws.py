"""AWS (ADR 0015, BACKLOG #98), slice 3: `${aws-sm:...}` from Secrets Manager, `aws-rds-iam` tokens for RDS and
Aurora, the TLS settings a driver is now given, and the alert when a provider keeps failing.

Against a fake AWS client always; Secrets Manager against a stand-in (moto) when QUERYAPIGATE_TEST_AWS_ENDPOINT is
set (CI's integration job). An RDS IAM token is signed here, with no call to AWS, so it's tested by its signature;
whether RDS accepts it can only be tested against RDS."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from queryapigate import alerts, aws, db, runners, secretrefs, store
from queryapigate.errors import ApiError
from queryapigate.services import connections

try:
    import boto3
except ImportError:
    boto3 = None


class ClientError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.response = {'Error': {'Code': code, 'Message': message}}


class FakeAws:
    def __init__(self):
        self.secrets = {'prod/reporting/warehouse': json.dumps({'password': 'from-secrets-manager', 'port': 5432}),
                        'prod/reporting/plain': 'just-a-string'}
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        name = SecretId.split(':secret:', 1)[1].rsplit('-', 1)[0] if SecretId.startswith('arn:') else SecretId
        if name not in self.secrets:
            raise ClientError('ResourceNotFoundException', "Secrets Manager can't find the specified secret.")
        return {'SecretString': self.secrets[name]}

    def generate_db_auth_token(self, DBHostname, Port, DBUsername, **region):
        self.calls += 1
        return f'{DBHostname}:{Port}/?Action=connect&DBUser={DBUsername}&X-Amz-Signature=sig{self.calls}'


class AwsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_AWS_SECRET_PREFIXES': 'prod/reporting/',
            'QUERYAPIGATE_AWS_RDS_HOSTS': '.abc123.eu-west-1.rds.amazonaws.com,reporting.internal'})
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_schema()
        self.fake = FakeAws()
        patcher = mock.patch.object(aws, '_client', lambda service: self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        for reset in (secretrefs.cache.clear, secretrefs._expiries.clear, aws.reset):
            reset()
            self.addCleanup(reset)

    def refused(self, value):
        with self.assertRaises(ApiError) as caught:
            secretrefs.check_value(value)
        return caught.exception.message


class SecretsManagerTests(AwsTestCase):
    def test_a_field_of_its_json_or_the_whole_string(self):
        self.assertEqual(secretrefs.expand('${aws-sm:prod/reporting/warehouse#password}'), 'from-secrets-manager')
        self.assertEqual(secretrefs.expand('${aws-sm:prod/reporting/plain}'), 'just-a-string')
        arn = 'arn:aws:secretsmanager:eu-west-1:123456789012:secret:prod/reporting/warehouse-AbCdEf'
        self.assertEqual(secretrefs.expand('${aws-sm:' + arn + '#password}'), 'from-secrets-manager')

    def test_only_names_the_deployer_allows_by_name_or_arn(self):
        self.assertIn("isn't a secret QUERYAPIGATE_AWS_SECRET_PREFIXES allows",
                      self.refused('${aws-sm:prod/payroll#x}'))
        arn = 'arn:aws:secretsmanager:eu-west-1:123456789012:secret:prod/payroll-AbCdEf'
        self.assertIn("isn't a secret", self.refused('${aws-sm:' + arn + '}'))
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AWS_SECRET_PREFIXES': ''}):
            self.assertIn('QUERYAPIGATE_AWS_SECRET_PREFIXES is not set',
                          self.refused('${aws-sm:prod/reporting/warehouse#password}'))

    def test_what_aws_says_no_to_names_why(self):
        for ref, words in (('${aws-sm:prod/reporting/missing#password}', "ResourceNotFoundException: Secrets Manager "
                                                                         "can't find the specified secret."),
                           ('${aws-sm:prod/reporting/warehouse#username}', "has no field 'username'"),
                           ('${aws-sm:prod/reporting/warehouse#port}', "isn't text"),
                           ('${aws-sm:prod/reporting/plain#password}', "isn't JSON")):
            with self.subTest(ref=ref), self.assertRaises(ApiError) as caught:
                secretrefs.expand(ref)
            self.assertEqual(caught.exception.code, 'secret_unavailable')
            self.assertIn(words, caught.exception.message)

    def test_without_boto3_it_says_which_extra(self):
        aws.reset()
        with mock.patch.object(aws, '_client', _real_client), mock.patch.dict('sys.modules', {'boto3': None}):
            with self.assertRaises(ApiError) as caught:
                secretrefs.expand('${aws-sm:prod/reporting/plain}')
        self.assertIn('pip install "queryapigate[aws]"', caught.exception.message)


_real_client = aws._client


class RdsIamTests(AwsTestCase):
    HOST = 'reporting.abc123.eu-west-1.rds.amazonaws.com'

    def connection(self, **extra):
        return {'name': 'rds', 'db': 'postgres', 'host': self.HOST, 'port': 5432, 'user': 'reporting',
                'database': 'analytics', 'sslmode': 'verify-full', 'credentials': {'source': 'aws-rds-iam'}, **extra}

    def test_the_token_is_the_password_for_the_connections_own_user(self):
        connections.create(self.connection(), 't')
        resolved = store.get_connection('rds')
        self.assertEqual(resolved['user'], 'reporting')
        self.assertEqual(resolved['password'],
                         f'{self.HOST}:5432/?Action=connect&DBUser=reporting&X-Amz-Signature=sig1')
        self.assertNotIn('credentials', resolved)
        self.assertEqual(store.get_connection('rds')['password'], resolved['password'])  # one token, for its time
        until = secretrefs.credential_expiry(resolved)
        self.assertAlmostEqual(until - time.monotonic(), 900 - 90, delta=2)  # 15 minutes, less a tenth

    def test_only_over_tls_without_a_password_on_an_allowed_host(self):
        def refused(**fields):
            with self.assertRaises(ApiError) as caught:
                connections.create(self.connection(**fields), 't')
            return caught.exception.message

        self.assertIn('set sslmode to verify-full', refused(sslmode='prefer'))
        self.assertIn('give the password - leave it out', refused(password='p'))
        self.assertIn("isn't a host QUERYAPIGATE_AWS_RDS_HOSTS allows", refused(host='db.example.com'))
        self.assertIn("isn't a host", refused(host='evil-abc123.eu-west-1.rds.amazonaws.com.example.com'))
        self.assertIn('set ssl_ca to the RDS CA bundle', refused(db='mysql', sslmode=None))
        self.assertIn('for postgres and mysql', refused(db='clickhouse'))
        self.assertIn('need the connection\'s user', refused(user=None))

    def test_mysql_gets_the_cleartext_plugin_tls_carries_it(self):
        connections.create(self.connection(db='mysql', port=3306, sslmode=None, ssl_ca='/etc/rds-ca.pem'), 't')
        resolved = store.get_connection('rds')
        self.assertEqual(resolved['auth_plugin'], 'mysql_clear_password')
        args = runners._connect_args(resolved, tls=runners.MYSQL_TLS)
        self.assertEqual((args['ssl_ca'], args['auth_plugin']), ('/etc/rds-ca.pem', 'mysql_clear_password'))

    @unittest.skipUnless(boto3, 'boto3 is not installed')
    def test_signed_here_by_botocore(self):
        aws.reset()
        env = {'AWS_ACCESS_KEY_ID': 'AKIAIOSFODNN7EXAMPLE', 'AWS_SECRET_ACCESS_KEY': 'wJalrXUtnFEMI/K7MDENG/bPxRfi',
               'QUERYAPIGATE_AWS_REGION': 'eu-west-1'}
        with mock.patch.object(aws, '_client', _real_client), mock.patch.dict(os.environ, env):
            (user, token), lasts = aws.rds_token(self.HOST, 5432, 'reporting')
        self.assertEqual((user, lasts), ('reporting', 900))
        self.assertTrue(token.startswith(f'{self.HOST}:5432/?Action=connect&DBUser=reporting&'), token)
        for part in ('X-Amz-Algorithm=AWS4-HMAC-SHA256', 'X-Amz-Expires=900', 'X-Amz-Signature=',
                     'eu-west-1%2Frds-db%2Faws4_request'):
            self.assertIn(part, token)


class TlsTests(unittest.TestCase):
    def test_a_driver_gets_the_tls_settings_and_nothing_else(self):
        details = {'host': 'h', 'user': 'u', 'sslmode': 'verify-full', 'sslrootcert': '/ca.pem', 'passfile': '/etc/x',
                   'service': 'other'}
        self.assertEqual(runners._connect_args(details, tls=runners.POSTGRES_TLS, database='dbname'),
                         {'host': 'h', 'user': 'u', 'sslmode': 'verify-full', 'sslrootcert': '/ca.pem'})


class AlertTests(AwsTestCase):
    def test_a_provider_failing_for_five_minutes(self):
        self.fake.secrets.clear()
        with self.assertRaises(ApiError):
            secretrefs.expand('${aws-sm:prod/reporting/warehouse#password}')
        self.assertEqual([a for a in alerts.collect() if a['kind'] == 'secret_provider_failing'], [])
        secretrefs.cache.status['aws-sm']['failing_since'] -= 301
        found = [a for a in alerts.collect() if a['kind'] == 'secret_provider_failing']
        self.assertEqual([a['title'] for a in found], ['The secret provider aws-sm is failing'])
        self.assertIn('ResourceNotFoundException', found[0]['detail'])


ENDPOINT = os.environ.get('QUERYAPIGATE_TEST_AWS_ENDPOINT')


@unittest.skipUnless(ENDPOINT and boto3,
                     'set QUERYAPIGATE_TEST_AWS_ENDPOINT to test against a Secrets Manager stand-in')
class StandInTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_AWS_SECRET_PREFIXES': 'qag-test/',
               'QUERYAPIGATE_AWS_REGION': 'eu-west-1', 'AWS_ACCESS_KEY_ID': 'testing',
               'AWS_SECRET_ACCESS_KEY': 'testing', 'AWS_ENDPOINT_URL_SECRETS_MANAGER': ENDPOINT}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for reset in (secretrefs.cache.clear, aws.reset):
            reset()
            self.addCleanup(reset)
        client = boto3.client('secretsmanager', region_name='eu-west-1', endpoint_url=ENDPOINT)
        name = f'qag-test/warehouse-{time.time_ns()}'
        self.arn = client.create_secret(Name=name, SecretString=json.dumps({'password': 'from-the-stand-in'}))['ARN']
        self.name = name

    def test_by_name_and_by_arn(self):
        self.assertEqual(secretrefs.expand('${aws-sm:' + self.name + '#password}'), 'from-the-stand-in')
        secretrefs.cache.clear()
        self.assertEqual(secretrefs.expand('${aws-sm:' + self.arn + '#password}'), 'from-the-stand-in')


if __name__ == '__main__':
    unittest.main()
