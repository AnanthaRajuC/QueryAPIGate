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


class SignedTests(unittest.TestCase):
    """Everything docker.yml publishes is signed keyless by digest, attested, and verified as a deployer would before
    the job ends (ADR 0018) - a release whose signature doesn't verify fails."""

    def setUp(self):
        with open(os.path.join(ROOT, '.github', 'workflows', 'docker.yml')) as f:
            self.workflow = yaml.safe_load(f)

    def test_each_publishing_job_signs_attests_and_verifies(self):
        for name in ('image', 'chart'):
            with self.subTest(job=name):
                job = self.workflow['jobs'][name]
                self.assertEqual(job['permissions'].get('id-token'), 'write')
                self.assertEqual(job['permissions'].get('attestations'), 'write')
                runs = '\n'.join(step.get('run', '') for step in job['steps'])
                uses = [step.get('uses', '') for step in job['steps']]
                self.assertIn('cosign sign --yes "$REF"', runs)
                # and the classic .sig too: policy-controller 0.13 finds no signature in a bundle alone (ADR 0018)
                self.assertIn('cosign sign --yes --new-bundle-format=false --use-signing-config=false "$REF"', runs)
                self.assertIn('--certificate-identity "$IDENTITY" --certificate-oidc-issuer "$OIDC_ISSUER"', runs)
                self.assertIn('for bundle in true false', runs)
                self.assertIn('gh attestation verify', runs)
                self.assertTrue(any(u.startswith('actions/attest-build-provenance@') for u in uses))
                signing = next(s for s in job['steps'] if 'cosign sign' in s.get('run', ''))
                # by digest, never a tag that can move: the image's from the build, the chart's from what helm pushed
                self.assertTrue('@${{ steps.build.outputs.digest }}' in signing['env']['REF']
                                or ('ref=$registry/queryapigate@$digest' in runs
                                    and signing['env']['REF'] == '${{ steps.push.outputs.ref }}'), signing['env'])

    def test_the_identity_is_this_workflow_at_its_ref_and_cosign_one_exact_version(self):
        self.assertEqual(self.workflow['env']['OIDC_ISSUER'], 'https://token.actions.githubusercontent.com')
        self.assertRegex(self.workflow['env']['COSIGN_VERSION'], r'^v\d+\.\d+\.\d+$')
        for name in ('image', 'chart'):
            verify = next(s for s in self.workflow['jobs'][name]['steps'] if 'cosign verify' in s.get('run', ''))
            self.assertEqual(verify['env']['IDENTITY'], 'https://github.com/${{ github.workflow_ref }}')

    def test_images_carry_provenance_and_an_sbom(self):
        build = next(s for s in self.workflow['jobs']['image']['steps'] if s.get('id') == 'build')
        self.assertIn('mode=max', build['with']['provenance'])
        self.assertIn('push', build['with']['sbom'])

    def test_a_rehearsal_never_uses_the_real_names(self):
        steps = self.workflow['jobs']['image']['steps'] + self.workflow['jobs']['chart']['steps']
        runs = '\n'.join(step.get('run', '') for step in steps)
        self.assertIn('image="$image-rehearsal"', runs)
        self.assertIn('registry="$registry-rehearsal"', runs)


if __name__ == '__main__':
    unittest.main()
