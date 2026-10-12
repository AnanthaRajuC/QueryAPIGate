"""FIPS 140 (ADR 0016, BACKLOG #101), slice 2: QueryAPIGate uses only FIPS-approved algorithms - checked here over its
code - and runs on a validated module: CI's FIPS job runs the whole suite in Red Hat UBI 9 with OpenSSL's FIPS
provider forced (QUERYAPIGATE_TEST_FIPS=1), and the first test below proves it was really in force."""
import ast
import glob
import hashlib
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIPS = os.environ.get('QUERYAPIGATE_TEST_FIPS') == '1'

APPROVED_HASHES = {'sha224', 'sha256', 'sha384', 'sha512', 'sha3_224', 'sha3_256', 'sha3_384', 'sha3_512'}
# `random` only where nothing depends on it being unpredictable - never for a secret (`secrets` is for those).
RANDOM_ALLOWED = {'queryapigate/history.py': 'sampling which runs are kept',
                  'queryapigate/examples.py': 'the example database, from a fixed seed'}


def _calls():
    """(path, line, dotted name, call node) for every call in queryapigate/."""
    for path in sorted(glob.glob(os.path.join(ROOT, 'queryapigate', '**', '*.py'), recursive=True)):
        rel = os.path.relpath(path, ROOT)
        with open(path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value,
                                                                                                     ast.Name):
                yield rel, node.lineno, f'{node.func.value.id}.{node.func.attr}', node


class ApprovedAlgorithmTests(unittest.TestCase):
    def test_every_hash_is_an_approved_one(self):
        bad = [f'{path}:{line} {name}' for path, line, name, _ in _calls()
               if name.startswith('hashlib.') and name.split('.')[1] not in APPROVED_HASHES | {'file_digest'}]
        self.assertEqual(bad, [], 'MD5, SHA-1, BLAKE2 and hashlib.new() are not FIPS-approved (or not checkable) - use '
                                  'SHA-256')

    def test_every_hmac_is_keyed_with_an_approved_hash(self):
        bad = []
        for path, line, name, node in _calls():
            if name not in ('hmac.new', 'hmac.digest'):
                continue
            digest = node.args[2] if len(node.args) > 2 else next(
                (k.value for k in node.keywords if k.arg == 'digestmod'), None)
            ok = (isinstance(digest, ast.Attribute) and isinstance(digest.value, ast.Name)
                  and digest.value.id == 'hashlib' and digest.attr in APPROVED_HASHES)
            if not ok:
                bad.append(f'{path}:{line}')
        self.assertEqual(bad, [])

    def test_no_secret_comes_from_random(self):
        bad = [f'{path}:{line} {name}' for path, line, name, _ in _calls()
               if name.startswith('random.') and path not in RANDOM_ALLOWED]
        self.assertEqual(bad, [], 'random is predictable: use secrets (or say here why random is enough)')

    def test_no_uuid_from_md5_or_sha1(self):
        bad = [f'{path}:{line} {name}' for path, line, name, _ in _calls() if name in ('uuid.uuid3', 'uuid.uuid5')]
        self.assertEqual(bad, [])

    def test_the_page_lists_each_use(self):
        with open(os.path.join(ROOT, 'documentation', 'DATA_PROTECTION.md')) as f:
            page = f.read().split('## Cryptography', 1)[1]
        for use in ('Fernet', 'HMAC-SHA256', 'SHA-256', 'secrets', 'TLS', 'RS256'):
            self.assertIn(use, page)


@unittest.skipUnless(FIPS, 'CI\'s FIPS job sets QUERYAPIGATE_TEST_FIPS=1')
class FipsInForceTests(unittest.TestCase):
    """That the rest of the suite really ran on a validated module, in FIPS mode - not that it merely could have."""

    def test_md5_is_refused(self):
        with self.assertRaises(ValueError):
            hashlib.md5(b'x')

    def test_cryptography_is_the_distributions_in_fips_mode(self):
        import cryptography
        from cryptography.hazmat.backends.openssl import backend
        self.assertTrue(backend._fips_enabled)
        self.assertTrue(cryptography.__file__.startswith('/usr/lib64/'), cryptography.__file__)

    def test_what_queryapigate_encrypts_and_signs_works_there(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        self.assertEqual(Fernet(key).decrypt(Fernet(key).encrypt(b'pw')), b'pw')
        import hmac
        self.assertEqual(len(hmac.new(b'k', b'm', hashlib.sha256).hexdigest()), 64)


if __name__ == '__main__':
    unittest.main()
