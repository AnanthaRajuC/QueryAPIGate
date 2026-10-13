"""The documentation site (mkdocs.yml, docs/) shows every page in documentation/ and every ADR: each needs a stub in
docs/ that includes it and an entry in the site's navigation, and `mkdocs build --strict` fails on a link to a page
that has neither - found only by the docs workflow, after the push (ADR 0017's first commit)."""
import glob
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pages():
    found = []
    for pattern in ('*.md', '*.MD', os.path.join('adr', '*.md')):
        found += glob.glob(os.path.join(ROOT, 'documentation', pattern))
    return sorted(os.path.relpath(p, ROOT) for p in found)


class DocsSiteTests(unittest.TestCase):
    def test_every_page_has_a_stub_that_includes_it(self):
        for page in _pages():
            with self.subTest(page=page):
                stub = os.path.join(ROOT, 'docs', page)
                self.assertTrue(os.path.exists(stub), f'docs/{page} is missing')
                with open(stub) as f:
                    self.assertEqual(f.read().strip(), f'--8<-- "{page}"')

    def test_every_page_is_in_the_navigation(self):
        with open(os.path.join(ROOT, 'mkdocs.yml')) as f:
            nav = f.read()
        for page in _pages():
            with self.subTest(page=page):
                self.assertIn(f': {page}\n', nav)


if __name__ == '__main__':
    unittest.main()
