"""The lowest dependency versions pyproject.toml allows are the ones CI's oldest-dependencies job tests: a minimum
raised in one place and not the other would claim a version nothing checks (0.19.1: DuckDB said 1.0, and every
DuckDB connection needed 1.2)."""
import os
import re
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def minimums():
    with open(os.path.join(ROOT, 'pyproject.toml'), 'rb') as f:
        extras = tomllib.load(f)['project']['optional-dependencies']
    found = {}
    for package, extra in (('duckdb', 'duckdb'), ('sqlglot', 'flow')):
        spec = next(r for r in extras[extra] if r.startswith(package))
        found[package] = re.search(r'>=([\d.]+)', spec).group(1)
    return found


class FloorTests(unittest.TestCase):
    def test_ci_tests_the_lowest_versions_pyproject_allows(self):
        with open(os.path.join(ROOT, '.github', 'workflows', 'ci.yml')) as f:
            workflow = f.read()
        job = workflow.split('  oldest-dependencies:', 1)[1].split('\n  postgres-metadata:', 1)[0]
        for package, minimum in minimums().items():
            pinned = re.search(rf'"{package}==([\d.]+)"', job).group(1)
            with self.subTest(package=package):
                self.assertEqual(tuple(int(p) for p in pinned.split('.'))[:2],
                                 tuple(int(p) for p in minimum.split('.'))[:2])

    def test_the_runtime_checks_agree(self):
        from queryapigate import duckfiles, masking
        found = minimums()
        self.assertEqual('.'.join(map(str, duckfiles.MIN_DUCKDB)), found['duckdb'])
        self.assertEqual('.'.join(map(str, masking.MIN_SQLGLOT)), found['sqlglot'])


if __name__ == '__main__':
    unittest.main()
