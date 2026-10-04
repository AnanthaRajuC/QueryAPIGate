"""The how-to guides (how-to/): every guide is linked from the index and listed in the Console's Help screen, and every
relative link in them - to another guide, a document, or a heading in one - leads somewhere that exists."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOWTO = os.path.join(ROOT, 'how-to')
GUIDES = sorted(f for f in os.listdir(HOWTO) if re.match(r'\d\d-.+\.md$', f))


def read(path):
    with open(path) as f:
        return f.read()


def anchors(path):
    """GitHub's heading anchors: lower case, punctuation dropped, spaces to hyphens."""
    found, fenced = set(), False
    for line in read(path).splitlines():
        if line.startswith(('```', '~~~')):
            fenced = not fenced
        elif not fenced and line.startswith('#'):
            text = re.sub(r'[`*]', '', line.lstrip('#').strip().lower())
            found.add(re.sub(r'[^\w\- ]', '', text).replace(' ', '-'))
    return found


class HowToTests(unittest.TestCase):
    def test_the_guides_are_numbered_without_gaps(self):
        self.assertEqual([g[:2] for g in GUIDES], [f'{n:02d}' for n in range(1, len(GUIDES) + 1)])

    def test_the_index_links_every_guide(self):
        index = read(os.path.join(HOWTO, 'how-to.md'))
        for guide in GUIDES:
            self.assertIn(f']({guide})', index)

    def test_the_console_help_lists_every_guide(self):
        help_page = read(os.path.join(ROOT, 'frontend', 'src', 'features', 'help', 'HelpPage.tsx'))
        for guide in GUIDES:
            self.assertIn(f"path: 'how-to/{guide}'", help_page)

    def test_every_relative_link_resolves(self):
        for name in [*GUIDES, 'how-to.md']:
            path = os.path.join(HOWTO, name)
            for url in re.findall(r'\]\(([^)\s]+)\)', read(path)):
                if url.startswith(('http://', 'https://', 'mailto:')):
                    continue
                with self.subTest(guide=name, link=url):
                    target_path, _, fragment = url.partition('#')
                    target = os.path.normpath(os.path.join(HOWTO, target_path)) if target_path else path
                    self.assertTrue(os.path.exists(target), f'{url}: no such file')
                    if fragment and target.endswith('.md'):
                        self.assertIn(fragment, anchors(target), f'{url}: no such heading')


if __name__ == '__main__':
    unittest.main()
