"""The lowest dependency versions pyproject.toml allows are the ones CI's oldest-dependencies job tests: a minimum
raised in one place and not the other would claim a version nothing checks (0.19.1: DuckDB said 1.0, and every
DuckDB connection needed 1.2; 1.0.0-rc1: Flask said 2.2, which can't be imported beside the Werkzeug pip installs
with it, and PyYAML said 6.0, which can't be installed on Python 3.12 or later)."""
import os
import re
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Each package whose floor CI tests, and the extra that declares it - None for a core dependency. Every core
# dependency is here, and every extra a stable feature needs (ADR 0017); the experimental features' extras (otel,
# aws, mongo, h2) join when they graduate.
FLOORS = {'Flask': None, 'Werkzeug': None, 'PyYAML': None, 'openpyxl': None,
          'duckdb': 'duckdb', 'sqlglot': 'flow', 'mcp': 'mcp', 'PyJWT': 'jwt', 'redis': 'redis'}


def _project():
    with open(os.path.join(ROOT, 'pyproject.toml'), 'rb') as f:
        return tomllib.load(f)['project']


def _name(requirement):
    return re.match(r'[A-Za-z0-9_.-]+', requirement).group(0)


def minimums():
    """{package: its minimum, as pyproject.toml writes it}."""
    project = _project()
    found = {}
    for package, extra in FLOORS.items():
        requirements = project['dependencies'] if extra is None else project['optional-dependencies'][extra]
        spec = next(r for r in requirements if _name(r) == package)
        found[package] = re.search(r'>=([\d.]+)', spec).group(1)
    return found


def _version(text):
    parts = tuple(int(p) for p in text.split('.'))
    return parts + (0,) * (3 - len(parts))


class FloorTests(unittest.TestCase):
    def test_ci_tests_the_lowest_versions_pyproject_allows(self):
        with open(os.path.join(ROOT, '.github', 'workflows', 'ci.yml')) as f:
            workflow = f.read()
        job = workflow.split('  oldest-dependencies:', 1)[1].split('\n  postgres-metadata:', 1)[0]
        for package, minimum in minimums().items():
            with self.subTest(package=package):
                pinned = re.search(rf'"{re.escape(package)}(?:\[\w+\])?==([\d.]+)"', job)
                self.assertIsNotNone(pinned, f'the oldest-dependencies job pins no {package}')
                self.assertEqual(_version(pinned.group(1)), _version(minimum))

    def test_every_core_dependency_has_its_floor_tested(self):
        self.assertEqual({_name(r) for r in _project()['dependencies']},
                         {p for p, extra in FLOORS.items() if extra is None})

    def test_the_runtime_checks_agree(self):
        from queryapigate import duckfiles, masking
        found = minimums()
        self.assertEqual('.'.join(map(str, duckfiles.MIN_DUCKDB)), found['duckdb'])
        self.assertEqual('.'.join(map(str, masking.MIN_SQLGLOT)), found['sqlglot'])


if __name__ == '__main__':
    unittest.main()
