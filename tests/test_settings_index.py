"""The settings are one list (rc1 review, D1): every QUERYAPIGATE_* name the code reads - the server and its other
processes (queryapigate/), the data plane (dataplane/src) - is in settingnames.KNOWN, each name there is read somewhere,
and documentation/CONFIGURATION.md lists exactly these. A setting added without its documentation fails here."""
import glob
import logging
import os
import re
import tempfile
import unittest
from unittest import mock

from queryapigate import config, create_app, settingnames

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = re.compile(r'QUERYAPIGATE_[A-Z0-9_]*[A-Z0-9]')
# Not settings: a Flask config key, and the prefix the JWT settings are found by
NOT_SETTINGS = {'QUERYAPIGATE_VERSION', 'QUERYAPIGATE_JWT'}


def _read(pattern):
    names = set()
    for path in glob.glob(os.path.join(ROOT, pattern), recursive=True):
        if path.endswith(os.path.join('queryapigate', 'settingnames.py')) or 'console_dist' in path:
            continue
        with open(path, encoding='utf-8') as f:
            names |= set(NAME.findall(f.read()))
    return names


def _without_files(names):
    files = {f'{name}_FILE' for name in config.SECRET_SETTINGS}
    return {n for n in names if n not in files and n not in NOT_SETTINGS}


class SettingsIndexTests(unittest.TestCase):
    def test_the_code_reads_exactly_the_known_settings(self):
        read = _without_files(_read('queryapigate/**/*.py') | _read('dataplane/src/*.rs'))
        self.assertEqual(sorted(read - settingnames.KNOWN), [], 'read but not in settingnames.KNOWN')
        self.assertEqual(sorted(settingnames.KNOWN - read), [], 'in settingnames.KNOWN but read nowhere')

    def test_configuration_md_lists_exactly_the_known_settings(self):
        with open(os.path.join(ROOT, 'documentation', 'CONFIGURATION.md'), encoding='utf-8') as f:
            listed = _without_files(set(re.findall(r'`(QUERYAPIGATE_[A-Z0-9_]+)`', f.read())))
        self.assertEqual(sorted(settingnames.KNOWN - listed), [], 'not in documentation/CONFIGURATION.md')
        self.assertEqual(sorted(listed - settingnames.KNOWN), [], 'documented but no setting')

    def test_an_unknown_name_is_a_warning_at_startup(self):
        env = {'QUERYAPIGATE_QUERY_TIMEOUTS': '5', 'QUERYAPIGATE_EVENTS_SERVICE_HOST': '10.0.0.7',
               'QUERYAPIGATE_PORT_5000_TCP_ADDR': '10.0.0.7', 'QUERYAPIGATE_ENCRYPTION_KEY_FILE': ''}
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home, **env}), \
                self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
        found = [line for line in logs.output if 'no setting of this server' in line]
        self.assertEqual(len(found), 1, logs.output)
        self.assertIn('QUERYAPIGATE_QUERY_TIMEOUTS is no setting', found[0])  # the service links aren't typos
