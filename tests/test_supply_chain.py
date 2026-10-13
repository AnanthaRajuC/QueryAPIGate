"""What a release is built from is pinned (ADR 0018): every GitHub Action by commit SHA - a tag or a branch can be
moved to other code, a SHA can't - and the image's base images by digest. Dependabot proposes each update as a commit,
so pinning doesn't mean falling behind."""
import glob
import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = sorted(glob.glob(os.path.join(ROOT, '.github', 'workflows', '*.yml')))
PINNED = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40} # v\d+(\.\d+)*')


def _uses(path):
    """(line number, what follows `uses:`) for every step in a workflow."""
    with open(path) as f:
        for number, line in enumerate(f, 1):
            match = re.match(r'\s*(?:-\s*)?uses:\s*(.+?)\s*$', line)
            if match:
                yield number, match.group(1)


class PinnedTests(unittest.TestCase):
    def test_every_action_is_pinned_by_commit_with_its_version_beside_it(self):
        self.assertTrue(WORKFLOWS)
        bad = [f'{os.path.basename(path)}:{number} {used}' for path in WORKFLOWS for number, used in _uses(path)
               if not used.startswith(('./', 'docker://')) and not PINNED.fullmatch(used)]
        self.assertEqual(bad, [], 'pin each as owner/repo@<40-hex SHA> # vX.Y.Z')

    def test_every_base_image_is_pinned_by_digest(self):
        with open(os.path.join(ROOT, 'Dockerfile')) as f:
            froms = [line.split()[1] for line in f if line.startswith('FROM ')]
        self.assertGreaterEqual(len(froms), 2)
        for image in froms:
            with self.subTest(image=image):
                self.assertRegex(image, r'@sha256:[0-9a-f]{64}$')

    def test_dependabot_proposes_the_updates(self):
        with open(os.path.join(ROOT, '.github', 'dependabot.yml')) as f:
            ecosystems = {u['package-ecosystem'] for u in yaml.safe_load(f)['updates']}
        self.assertLessEqual({'github-actions', 'docker'}, ecosystems)


if __name__ == '__main__':
    unittest.main()
