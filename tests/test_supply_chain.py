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
            # the image after FROM's own flags (--platform=$BUILDPLATFORM, the data plane's cross-compiling stage)
            froms = [next(word for word in line.split()[1:] if not word.startswith('--'))
                     for line in f if line.startswith('FROM ')]
        self.assertGreaterEqual(len(froms), 3)
        for image in froms:
            with self.subTest(image=image):
                self.assertRegex(image, r'@sha256:[0-9a-f]{64}$')

    def test_every_copy_from_is_a_stage_so_dependabot_sees_its_image(self):
        # Dependabot updates FROM lines, not COPY --from=<image>: an image copied from directly would never move
        with open(os.path.join(ROOT, 'Dockerfile')) as f:
            dockerfile = f.read()
        stages = set(re.findall(r'^FROM .+ AS (\w+)$', dockerfile, re.M))
        for source in re.findall(r'^COPY --from=(\S+)', dockerfile, re.M):
            with self.subTest(source=source):
                self.assertIn(source, stages)

    def test_python_is_the_release_the_pinned_uv_resolves(self):
        # ADR 0031: uv installs python-build-standalone and checks its hash; asking for a minor version, the uv
        # release decides the patch, so the uv stage's digest pins Python and its bump brings the next release
        with open(os.path.join(ROOT, 'Dockerfile')) as f:
            dockerfile = f.read()
        self.assertRegex(dockerfile, re.compile(r'^FROM ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+@sha256:[0-9a-f]{64} AS uv$',
                                                re.M))
        self.assertRegex(dockerfile, r'uv python install 3\.\d+ ')
        self.assertIn('COPY --from=uv /uv /usr/local/bin/uv', dockerfile)
        self.assertIn('COPY --from=python /opt/python /opt/python', dockerfile)

    def test_the_siem_receivers_ci_tests_against_are_pinned_by_digest(self):
        """ADR 0019: rsyslog, syslog-ng and the OpenTelemetry Collector, as siem_receivers.sh runs them."""
        with open(os.path.join(ROOT, '.github', 'scripts', 'siem_receivers.sh')) as f:
            images = dict(re.findall(r'^(RSYSLOG|SYSLOG_NG|OTELCOL)=(\S+)$', f.read(), re.M))
        self.assertEqual(set(images), {'RSYSLOG', 'SYSLOG_NG', 'OTELCOL'})
        for name, image in images.items():
            with self.subTest(name=name):
                self.assertRegex(image, r':[\w.-]+@sha256:[0-9a-f]{64}$')

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
                # cosign by way of sign(), which tries three times: Rekor once answered cosign's own retried upload
                # with a 409 and failed a release (0.47.0)
                self.assertIn('for attempt in 1 2 3; do\n    cosign sign --yes "$@" "$REF" && return', runs)
                self.assertRegex(runs, re.compile(r'^sign(\s+#.*)?$', re.M))
                # and the classic .sig too: policy-controller 0.13 finds no signature in a bundle alone (ADR 0018)
                self.assertRegex(runs, re.compile(r'^sign --new-bundle-format=false --use-signing-config=false$', re.M))
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


class H2DriverTests(unittest.TestCase):
    """The H2 driver the package bundles isn't committed (OpenSSF Scorecard: Binary-Artifacts): one script fetches it,
    for builds and for the Dockerfile alike, from Maven Central or a mirror of it, checked against a pinned SHA-256."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'fetch_h2_jar', os.path.join(ROOT, '.github', 'scripts', 'fetch_h2_jar.py'))
        self.fetch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fetch)

    def test_the_dockerfile_fetches_it_with_the_script(self):
        with open(os.path.join(ROOT, 'Dockerfile')) as f:
            dockerfile = f.read()
        self.assertIn('COPY .github/scripts/fetch_h2_jar.py /tmp/fetch_h2_jar.py', dockerfile)
        self.assertIn('RUN python /tmp/fetch_h2_jar.py --dest ./queryapigate/lib', dockerfile)
        self.assertNotIn('repo1.maven.org', dockerfile)  # one place knows where it comes from, and its hash

    def test_the_server_looks_for_the_version_fetched(self):
        from queryapigate import config
        self.assertEqual(config.BUNDLED_H2_JAR.resolve(), self.fetch.TARGET)

    def _serve(self, *bodies):
        """urlopen answering each call with the next body (bytes), or raising it (an exception)."""
        from unittest import mock
        calls, answers = [], iter(bodies)

        def urlopen(request, timeout=None):
            calls.append(request.full_url)
            body = next(answers)
            if isinstance(body, Exception):
                raise body
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = body
            response.__enter__.return_value.status = 200
            return response
        return calls, urlopen

    def test_a_download_with_another_hash_is_never_written_from_any_mirror(self):
        # CI, 2026-10-15: repo1.maven.org twice answered GitHub's runners with something else, the same something both
        # times - the hash refused it, and now the other mirrors are asked before the build gives up
        import io
        import tempfile
        from contextlib import redirect_stderr
        from pathlib import Path
        from unittest import mock
        tries = len(self.fetch.MIRRORS) * self.fetch.ATTEMPTS
        calls, urlopen = self._serve(*[b'<html>not the driver</html>'] * tries)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'h2.jar'
            with mock.patch.object(self.fetch.urllib.request, 'urlopen', urlopen), \
                    mock.patch.object(self.fetch.time, 'sleep'), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(self.fetch.fetch(target), 1)
            self.assertFalse(target.exists())
        self.assertEqual(len(calls), tries)
        hosts = {url.split('/maven2/')[0] for url in calls}
        self.assertEqual(hosts, {m.split('/maven2/')[0] for m in self.fetch.MIRRORS})
        self.assertIn("starts b'<html>not the driver</html>'", err.getvalue())  # what came instead, for the log

    def test_a_mirror_that_answers_wrongly_or_not_at_all_is_passed_over(self):
        import hashlib
        import io
        import tempfile
        import urllib.error
        from contextlib import redirect_stderr, redirect_stdout
        from pathlib import Path
        from unittest import mock
        good = b'the driver'
        bad_then_down = [b'error page'] + [urllib.error.URLError('blocked')] * (self.fetch.ATTEMPTS - 1)
        calls, urlopen = self._serve(*bad_then_down, good)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'lib' / 'h2.jar'
            with mock.patch.object(self.fetch, 'SHA256', hashlib.sha256(good).hexdigest()), \
                    mock.patch.object(self.fetch.urllib.request, 'urlopen', urlopen), \
                    mock.patch.object(self.fetch.time, 'sleep'), redirect_stderr(io.StringIO()), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(self.fetch.fetch(target), 0)
            self.assertEqual(target.read_bytes(), good)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)  # readable by an image's own user
        self.assertTrue(calls[-1].startswith(self.fetch.MIRRORS[1]))
