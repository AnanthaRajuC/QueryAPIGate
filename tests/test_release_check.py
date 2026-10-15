"""Tests for .github/scripts/check_release.py, the guard that runs before a release is published."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / '.github' / 'scripts' / 'check_release.py'
spec = importlib.util.spec_from_file_location('check_release', SCRIPT)
check_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_release)


def git(root, *args):
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=root, check=True,
                   capture_output=True)


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'queryapigate').mkdir()
        self.write_version('0.2.0')
        (self.root / 'CHANGELOG.md').write_text('## [Unreleased]\n\n## [0.2.0] - 2026-09-21\n')
        git(self.root, 'init', '-q', '-b', 'main')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'release')
        git(self.root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')  # what CI sees as origin/main

    def write_version(self, version):
        (self.root / 'queryapigate' / '__init__.py').write_text(f"__version__ = '{version}'\n")

    def test_consistent_release_passes(self):
        self.assertEqual(check_release.problems('v0.2.0', self.root), [])

    def test_tag_that_does_not_match_the_package_version_is_refused(self):
        # the mistake this guard exists for: tagging a commit that still has the old version (and, as in the real
        # history, whose changelog already documents that old version)
        self.write_version('0.1.0')
        (self.root / 'CHANGELOG.md').write_text('## [Unreleased]\n\n## [0.1.0] - 2026-09-20\n')
        reasons = check_release.problems('v0.2.0', self.root)
        self.assertEqual(len(reasons), 1)
        self.assertIn('does not match the package version 0.1.0', reasons[0])
        self.assertIn('expected v0.1.0', reasons[0])

    def test_tag_on_a_commit_that_is_not_on_main_is_refused(self):
        (self.root / 'extra.txt').write_text('unmerged work')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'not merged into main yet')
        reasons = check_release.problems('v0.2.0', self.root)
        self.assertEqual(len(reasons), 1)
        self.assertIn('not on origin/main', reasons[0])

    def test_missing_or_undated_changelog_section_is_refused(self):
        (self.root / 'CHANGELOG.md').write_text('## [Unreleased]\n\n## [0.2.0] - Unreleased\n')
        self.assertIn('CHANGELOG.md has no dated', check_release.problems('v0.2.0', self.root)[0])
        (self.root / 'CHANGELOG.md').unlink()
        self.assertIn('CHANGELOG.md has no dated', check_release.problems('v0.2.0', self.root)[0])

    def test_every_problem_is_reported_at_once(self):
        self.write_version('0.1.0')
        (self.root / 'CHANGELOG.md').unlink()
        self.assertEqual(len(check_release.problems('v0.2.0', self.root)), 2)

    def test_the_release_notes_are_the_versions_changelog_section(self):
        (self.root / 'CHANGELOG.md').write_text(
            '## [Unreleased]\n\n## [0.3.0] - 2026-10-14\n\nNewer.\n\n## [0.2.0] - 2026-09-21\n\nThe summary.\n\n'
            '### Fixed\n- a thing\n\n[Unreleased]: https://example.com/compare\n')
        self.assertEqual(check_release.notes('v0.2.0', self.root), 'The summary.\n\n### Fixed\n- a thing\n')
        self.assertEqual(check_release.notes('v0.3.0', self.root), 'Newer.\n')
        with self.assertRaises(SystemExit):
            check_release.notes('v9.9.9', self.root)
        out = subprocess.run(['python3', str(SCRIPT), '--notes', 'v0.3.0'], cwd=self.root, capture_output=True,
                             text=True)
        self.assertEqual((out.returncode, out.stdout), (0, 'Newer.\n'))

    def test_command_line_exit_codes(self):
        run = lambda tag: subprocess.run(['python3', str(SCRIPT), tag], cwd=self.root,  # noqa: E731
                                         capture_output=True, text=True)
        good, bad = run('v0.2.0'), run('v9.9.9')
        self.assertEqual(good.returncode, 0)
        self.assertIn('Release check passed', good.stdout)
        self.assertEqual(bad.returncode, 1)
        self.assertIn('::error::', bad.stdout)  # shown as an annotation in the GitHub UI


if __name__ == '__main__':
    unittest.main()
