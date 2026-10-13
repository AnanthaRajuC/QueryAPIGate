"""The Helm chart (ADR 0013, BACKLOG #100): rendered with each shape and option, and the output checked - the
refusals, the security context on every pod, the routes - and the plain manifests kept in step with it.

Needs the `helm` binary (Helm 3 or 4): QUERYAPIGATE_TEST_HELM names one, else `helm` on PATH; skipped without."""
import os
import shutil
import subprocess
import unittest

import yaml

from queryapigate import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(ROOT, 'deploy', 'helm', 'queryapigate')
MANIFESTS = os.path.join(ROOT, 'deploy', 'kubernetes', 'queryapigate.yaml')
HELM = os.environ.get('QUERYAPIGATE_TEST_HELM') or shutil.which('helm')
SECRET = ['--set', 'secret.create=true', '--set', 'secret.values.QUERYAPIGATE_API_KEY=k']
POSTGRES = ['--set', 'store.type=postgres', '--set', 'existingSecret=qag', '--set', 'redis.inExistingSecret=true',
            '--set', 'replicas=3']


def render(*args):
    result = subprocess.run([HELM, 'template', 'qag', CHART, *args], capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr.strip())
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def refused(*args):
    result = subprocess.run([HELM, 'template', 'qag', CHART, *args], capture_output=True, text=True)
    return result.returncode != 0, result.stderr


def by_kind(docs, kind, name=None):
    found = [d for d in docs if d['kind'] == kind and (name is None or d['metadata']['name'] == name)]
    return found


def container(deployment):
    return deployment['spec']['template']['spec']['containers'][0]


@unittest.skipUnless(HELM, 'set QUERYAPIGATE_TEST_HELM, or put helm on PATH')
class ShapeTests(unittest.TestCase):
    def test_the_default_is_one_pod_on_sqlite_with_its_store_on_a_volume(self):
        docs = render(*SECRET)
        self.assertEqual(sorted(d['kind'] for d in docs),
                         ['Deployment', 'PersistentVolumeClaim', 'Secret', 'Secret', 'Service', 'ServiceAccount'])
        deployment = by_kind(docs, 'Deployment')[0]
        self.assertEqual((deployment['spec']['replicas'], deployment['spec']['strategy']['type']), (1, 'Recreate'))
        volumes = {v['name']: v for v in deployment['spec']['template']['spec']['volumes']}
        self.assertEqual(volumes['data']['persistentVolumeClaim']['claimName'], 'qag-queryapigate-data')
        claim = by_kind(docs, 'PersistentVolumeClaim')[0]
        self.assertEqual(claim['metadata']['annotations']['helm.sh/resource-policy'], 'keep')
        self.assertEqual(container(deployment)['image'], f'ghcr.io/anantharajuc/queryapigate:{__version__}')
        self.assertEqual(by_kind(docs, 'Secret', 'qag-queryapigate')[0]['stringData'], {'QUERYAPIGATE_API_KEY': 'k'})

    def test_postgres_runs_several_rolled_one_at_a_time(self):
        docs = render(*POSTGRES)
        deployment = by_kind(docs, 'Deployment')[0]
        self.assertEqual(deployment['spec']['replicas'], 3)
        self.assertEqual(deployment['spec']['strategy'],
                         {'type': 'RollingUpdate', 'rollingUpdate': {'maxUnavailable': 0, 'maxSurge': 1}})
        self.assertEqual(by_kind(docs, 'PodDisruptionBudget')[0]['spec']['minAvailable'], 2)
        self.assertFalse(by_kind(docs, 'Secret', 'qag-queryapigate') or by_kind(docs, 'PersistentVolumeClaim'))
        self.assertEqual(container(deployment)['envFrom'], [{'secretRef': {'name': 'qag'}}])

    def test_events_mcp_and_the_ingress(self):
        docs = render(*POSTGRES, '--set', 'ingress.enabled=true', '--set', 'events.enabled=true',
                      '--set', 'mcp.enabled=true', '--set', 'mcp.ingress.host=mcp.example.com')
        ingress = by_kind(docs, 'Ingress')[0]
        rules = {r['host']: [(p['path'], p['backend']['service']['name']) for p in r['http']['paths']]
                 for r in ingress['spec']['rules']}
        self.assertEqual(rules, {'api.example.com': [('/events', 'qag-queryapigate-events'), ('/', 'qag-queryapigate')],
                                 'mcp.example.com': [('/', 'qag-queryapigate-mcp')]})
        self.assertEqual(ingress['spec']['tls'][0]['hosts'], ['api.example.com', 'mcp.example.com'])
        self.assertEqual(ingress['metadata']['annotations']['nginx.ingress.kubernetes.io/proxy-buffering'], 'off')
        for name, command in (('qag-queryapigate-events', ['queryapigate', 'events', '--host', '0.0.0.0', '--port',
                                                           '5002']),
                              ('qag-queryapigate-mcp', ['queryapigate', 'mcp', '--host', '0.0.0.0', '--port', '5001'])):
            self.assertEqual(container(by_kind(docs, 'Deployment', name)[0])['command'], command)
            self.assertTrue(by_kind(docs, 'Service', name))
        env = {e['name']: e.get('value') for e in container(by_kind(docs, 'Deployment', 'qag-queryapigate')[0])['env']}
        self.assertEqual(env['QUERYAPIGATE_TRUST_PROXY'], '1')  # behind the ingress controller

    def test_config_becomes_the_environment(self):
        docs = render(*SECRET, '--set', 'config.QUERYAPIGATE_ALLOW_WRITES=1',
                      '--set-string', 'config.OTEL_SERVICE_NAME=gateway')
        env = {e['name']: e.get('value') for e in container(by_kind(docs, 'Deployment')[0])['env']}
        self.assertEqual((env['QUERYAPIGATE_ALLOW_WRITES'], env['OTEL_SERVICE_NAME'], env['QUERYAPIGATE_HOME']),
                         ('1', 'gateway', '/data'))

    def test_extra_volumes_reach_every_pod(self):
        docs = render(*POSTGRES, '--set', 'events.enabled=true', '--set', 'mcp.enabled=true',
                      '--set', 'extraVolumes[0].name=siem-tls', '--set', 'extraVolumes[0].secret.secretName=siem',
                      '--set', 'extraVolumeMounts[0].name=siem-tls',
                      '--set', 'extraVolumeMounts[0].mountPath=/etc/queryapigate/siem',
                      '--set', 'extraVolumeMounts[0].readOnly=true')
        for deployment in by_kind(docs, 'Deployment'):
            pod = deployment['spec']['template']['spec']
            self.assertIn({'name': 'siem-tls', 'secret': {'secretName': 'siem'}}, pod['volumes'])
            self.assertIn({'name': 'siem-tls', 'mountPath': '/etc/queryapigate/siem', 'readOnly': True},
                          pod['containers'][0]['volumeMounts'])

    def test_a_configuration_file_for_the_api_server_only(self):
        """configAsCode (ADR 0014): the file in a ConfigMap, mounted on the API server pods - which apply it as they
        start - and its checksum on them, so a change rolls them."""
        text = 'format: queryapigate-config\nformat_version: 1\nroles: []\n'
        docs = render(*POSTGRES, '--set', 'events.enabled=true', '--set', 'configAsCode.enabled=true',
                      '--set-string', f'configAsCode.file={text}', '--set', 'configAsCode.locked=true')
        self.assertEqual(by_kind(docs, 'ConfigMap', 'qag-queryapigate-config')[0]['data'], {'queryapigate.yaml': text})
        server = by_kind(docs, 'Deployment', 'qag-queryapigate')[0]
        env = {e['name']: e.get('value') for e in container(server)['env']}
        self.assertEqual((env['QUERYAPIGATE_CONFIG_FILE'], env['QUERYAPIGATE_CONFIG_LOCKED']),
                         ('/etc/queryapigate/config/queryapigate.yaml', '1'))
        self.assertNotIn('QUERYAPIGATE_CONFIG_PRUNE', env)
        self.assertIn({'name': 'config-file', 'mountPath': '/etc/queryapigate/config', 'readOnly': True},
                      container(server)['volumeMounts'])
        self.assertIn('checksum/config-file', server['spec']['template']['metadata']['annotations'])
        events = by_kind(docs, 'Deployment', 'qag-queryapigate-events')[0]
        self.assertFalse(any(e['name'].startswith('QUERYAPIGATE_CONFIG') for e in container(events)['env']))
        self.assertNotIn('checksum/config-file', events['spec']['template']['metadata']['annotations'])

    def test_a_configuration_file_from_your_own_config_map(self):
        docs = render(*SECRET, '--set', 'configAsCode.enabled=true', '--set', 'configAsCode.existingConfigMap=mine',
                      '--set', 'configAsCode.key=prod.yaml')
        self.assertFalse(by_kind(docs, 'ConfigMap'))
        pod = by_kind(docs, 'Deployment')[0]['spec']['template']['spec']
        self.assertIn({'name': 'config-file', 'configMap': {'name': 'mine'}}, pod['volumes'])
        env = {e['name']: e.get('value') for e in pod['containers'][0]['env']}
        self.assertEqual(env['QUERYAPIGATE_CONFIG_FILE'], '/etc/queryapigate/config/prod.yaml')

    def test_a_token_for_vault_alone(self):
        """vaultToken (ADR 0015): a projected token with its own audience, for Vault's Kubernetes auth - not the service
        account token, which stays unmounted."""
        docs = render(*POSTGRES, '--set', 'mcp.enabled=true', '--set', 'vaultToken.enabled=true',
                      '--set', 'vaultToken.audience=vault')
        for deployment in by_kind(docs, 'Deployment'):
            pod = deployment['spec']['template']['spec']
            self.assertFalse(pod['automountServiceAccountToken'])
            volume = next(v for v in pod['volumes'] if v['name'] == 'vault-token')
            self.assertEqual(volume['projected']['sources'][0]['serviceAccountToken'],
                             {'path': 'token', 'expirationSeconds': 3600, 'audience': 'vault'})
            env = {e['name']: e.get('value') for e in pod['containers'][0]['env']}
            self.assertEqual(env['QUERYAPIGATE_VAULT_K8S_TOKEN_FILE'], '/var/run/secrets/vault/token')
        self.assertFalse(any(v['name'] == 'vault-token' for d in by_kind(render(*SECRET), 'Deployment')
                             for v in d['spec']['template']['spec']['volumes']))

    def test_metrics_need_a_token_the_chart_makes_and_prometheus_sends(self):
        """metricsAuth (ADR 0017's review): the ingress routes every path, /metrics included, and its labels name keys,
        queries and connections - so every pod gets QUERYAPIGATE_METRICS_TOKEN from a Secret of the chart's own."""
        docs = render(*POSTGRES, '--set', 'mcp.enabled=true', '--set', 'events.enabled=true',
                      '--set', 'serviceMonitor.enabled=true')
        [secret] = by_kind(docs, 'Secret', 'qag-queryapigate-metrics')
        self.assertGreaterEqual(len(secret['data']['token']), 48)
        for deployment in by_kind(docs, 'Deployment'):
            env = {e['name']: e for e in container(deployment)['env']}
            self.assertEqual(env['QUERYAPIGATE_METRICS_TOKEN']['valueFrom'],
                             {'secretKeyRef': {'name': 'qag-queryapigate-metrics', 'key': 'token'}})
        endpoint = by_kind(docs, 'ServiceMonitor')[0]['spec']['endpoints'][0]
        self.assertEqual(endpoint['authorization'],
                         {'type': 'Bearer', 'credentials': {'name': 'qag-queryapigate-metrics', 'key': 'token'}})
        mine = render(*POSTGRES, '--set', 'serviceMonitor.enabled=true', '--set', 'metricsAuth.existingSecret=mine')
        self.assertEqual(by_kind(mine, 'Secret', 'qag-queryapigate-metrics'), [])
        self.assertEqual(by_kind(mine, 'ServiceMonitor')[0]['spec']['endpoints'][0]['authorization']['credentials'],
                         {'name': 'mine', 'key': 'token'})
        off = render(*POSTGRES, '--set', 'serviceMonitor.enabled=true', '--set', 'metricsAuth.enabled=false')
        self.assertNotIn('authorization', by_kind(off, 'ServiceMonitor')[0]['spec']['endpoints'][0])
        self.assertFalse(any(e['name'] == 'QUERYAPIGATE_METRICS_TOKEN' for d in by_kind(off, 'Deployment')
                             for e in container(d)['env']))

    def test_the_optional_parts(self):
        docs = render(*POSTGRES, '--set', 'serviceMonitor.enabled=true', '--set', 'networkPolicy.enabled=true',
                      '--set', 'autoscaling.enabled=true')
        self.assertEqual(by_kind(docs, 'ServiceMonitor')[0]['spec']['endpoints'][0]['path'], '/metrics')
        policy = by_kind(docs, 'NetworkPolicy')[0]['spec']
        self.assertEqual(policy['policyTypes'], ['Ingress', 'Egress'])
        hpa = by_kind(docs, 'HorizontalPodAutoscaler')[0]['spec']
        self.assertEqual((hpa['minReplicas'], hpa['maxReplicas']), (2, 6))
        self.assertNotIn('replicas', by_kind(docs, 'Deployment')[0]['spec'])  # the autoscaler sets it


@unittest.skipUnless(HELM, 'set QUERYAPIGATE_TEST_HELM, or put helm on PATH')
class HardeningTests(unittest.TestCase):
    def test_every_pod_runs_locked_down_with_somewhere_to_write(self):
        docs = render(*POSTGRES, '--set', 'events.enabled=true', '--set', 'mcp.enabled=true')
        deployments = by_kind(docs, 'Deployment')
        self.assertEqual(len(deployments), 3)
        for deployment in deployments:
            pod = deployment['spec']['template']['spec']
            self.assertTrue(pod['securityContext']['runAsNonRoot'])
            self.assertEqual(pod['securityContext']['seccompProfile'], {'type': 'RuntimeDefault'})
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertFalse(pod['enableServiceLinks'])
            ctr = pod['containers'][0]
            self.assertEqual(ctr['securityContext'], {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                                      'capabilities': {'drop': ['ALL']}})
            self.assertEqual(sorted(m['mountPath'] for m in ctr['volumeMounts']), ['/data', '/tmp'])
            self.assertIn('limits', ctr['resources'])
            self.assertEqual(ctr['readinessProbe']['httpGet']['path'], '/health')


@unittest.skipUnless(HELM, 'set QUERYAPIGATE_TEST_HELM, or put helm on PATH')
class RefusalTests(unittest.TestCase):
    def test_setups_that_cannot_work_fail_at_install(self):
        for args, words in (
                ((*SECRET, '--set', 'replicas=2'), 'runs one pod'),
                ((*SECRET, '--set', 'store.type=postgres', '--set', 'secret.values.QUERYAPIGATE_DATABASE_URL=x',
                  '--set', 'replicas=2'), 'needs Redis'),
                ((), 'Set existingSecret'),
                ((*SECRET, '--set', 'existingSecret=qag'), 'choose one'),
                ((*SECRET, '--set', 'store.type=postgres'), 'needs QUERYAPIGATE_DATABASE_URL'),
                ((*POSTGRES, '--set', 'config.QUERYAPIGATE_DATABASE_URL=postgresql://u:p@db/x'), 'not config'),
                # config is plain text in the Deployment: every secret setting is refused there, whatever the store
                ((*SECRET, '--set', 'config.QUERYAPIGATE_SECRET_KEY=x'), 'QUERYAPIGATE_SECRET_KEY is a secret'),
                ((*SECRET, '--set', 'config.QUERYAPIGATE_AUDIT_KEY=x'), 'QUERYAPIGATE_AUDIT_KEY is a secret'),
                ((*POSTGRES, '--set', 'config.QUERYAPIGATE_REDIS_URL=redis://:pw@redis:6379/0'),
                 'QUERYAPIGATE_REDIS_URL is a secret'),
                ((*SECRET, '--set-string', 'config.OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer x'),
                 'OTEL_EXPORTER_OTLP_HEADERS is a secret'),
                ((*SECRET, '--set', 'extraVolumeMounts[0].name=x'), 'mountPath'),
                ((*SECRET, '--set', 'autoscaling.enabled=true'), 'runs one pod'),
                ((*SECRET, '--set', 'replica=3'), 'replica'),  # a typo: the values schema
                ((*SECRET, '--set', 'config.allow_writes=1'), 'allow_writes'),
                ((*SECRET, '--set', 'store.type=mysql'), 'store/type'),
                ((*SECRET, '--set', 'configAsCode.enabled=true'), 'configAsCode.enabled needs the file'),
                ((*SECRET, '--set', 'configAsCode.enabled=true', '--set', 'configAsCode.file=x',
                  '--set', 'configAsCode.existingConfigMap=y'), 'choose one'),
                ((*SECRET, '--set', 'config.QUERYAPIGATE_CONFIG_FILE=/x'), 'through configAsCode'),
                ((*SECRET, '--set', 'config.QUERYAPIGATE_METRICS_TOKEN=x'), 'comes from metricsAuth'),
                ((*SECRET, '--set', 'secret.values.QUERYAPIGATE_METRICS_TOKEN=x'), 'comes from metricsAuth')):
            failed, stderr = refused(*args)
            self.assertTrue(failed, args)
            self.assertIn(words, stderr, args)


@unittest.skipUnless(HELM, 'set QUERYAPIGATE_TEST_HELM, or put helm on PATH')
class LockstepTests(unittest.TestCase):
    def test_the_chart_is_this_release(self):
        with open(os.path.join(CHART, 'Chart.yaml')) as f:
            chart = yaml.safe_load(f)
        self.assertEqual((chart['version'], chart['appVersion']), (__version__, __version__))

    def test_the_plain_manifests_match_what_the_chart_renders(self):
        with open(MANIFESTS) as f:
            plain = [d for d in yaml.safe_load_all(f) if d]
        docs = render(*POSTGRES, '--set', 'events.enabled=true', '--set', 'ingress.enabled=true')
        for plain_name, chart_name in (('queryapigate', 'qag-queryapigate'),
                                       ('queryapigate-events', 'qag-queryapigate-events')):
            mine, theirs = by_kind(plain, 'Deployment', plain_name)[0], by_kind(docs, 'Deployment', chart_name)[0]
            ours_pod, chart_pod = mine['spec']['template']['spec'], theirs['spec']['template']['spec']
            for key in ('securityContext', 'enableServiceLinks', 'automountServiceAccountToken',
                        'terminationGracePeriodSeconds'):
                self.assertEqual(ours_pod.get(key), chart_pod.get(key), (plain_name, key))
            a, b = ours_pod['containers'][0], chart_pod['containers'][0]
            for key in ('image', 'command', 'ports', 'readinessProbe', 'livenessProbe', 'lifecycle', 'securityContext',
                        'resources', 'volumeMounts'):
                self.assertEqual(a.get(key), b.get(key), (plain_name, key))
            if plain_name == 'queryapigate':
                self.assertEqual(mine['spec']['strategy'], theirs['spec']['strategy'])


if __name__ == '__main__':
    unittest.main()
