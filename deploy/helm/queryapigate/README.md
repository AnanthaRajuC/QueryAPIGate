# QueryAPIGate Helm chart

> **Experimental** in 0.26: value names may still change, always noted in the
> [changelog](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/CHANGELOG.md).

Saved SQL as REST endpoints and MCP tools for AI agents, governed - on Kubernetes. Chart *X.Y.Z* runs QueryAPIGate
*X.Y.Z* ([ADR 0013](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/adr/0013-helm-chart.md)).

```bash
helm install queryapigate oci://ghcr.io/anantharajuc/charts/queryapigate --version <release> -f values.yaml
```

## Two shapes

**One pod on SQLite** - the default. The store on a PersistentVolumeClaim (kept on `helm uninstall`):

```yaml
existingSecret: queryapigate   # a Secret with QUERYAPIGATE_API_KEY, and QUERYAPIGATE_SECRET_KEY to encrypt passwords
```

**Several pods on PostgreSQL and Redis:**

```yaml
store: {type: postgres}
replicas: 3
existingSecret: queryapigate   # also holding QUERYAPIGATE_DATABASE_URL and QUERYAPIGATE_REDIS_URL
redis: {inExistingSecret: true}
ingress: {enabled: true, host: api.example.com}
events: {enabled: true}        # live events, routed at /events
mcp: {enabled: true, ingress: {host: mcp.example.com}}
```

`helm install` refuses what can't work: more than one pod on SQLite, more than one without Redis, PostgreSQL without a
database URL, a secret setting in `config` instead of the secret, a misspelt value.

## Values

| Value | Default | |
|---|---|---|
| `store.type` | `sqlite` | `sqlite` (one pod, a volume) or `postgres` |
| `store.persistence.*` | 1Gi, the default class | SQLite's volume; `existingClaim` to bring your own |
| `replicas` | `1` | API server pods |
| `existingSecret` | | A Secret you manage, as environment variables - recommended |
| `secret.create`, `secret.values` | `false`, `{}` | Or let the chart make one (the values then sit in your values file) |
| `redis.inExistingSecret` | `false` | `QUERYAPIGATE_REDIS_URL` is in `existingSecret` |
| `config` | `{}` | Any other `QUERYAPIGATE_*` or `OTEL_*` setting - not the secret ones, refused here |
| `configAsCode.*` | off | A configuration file (ADR 0014) the API server applies as it starts, once per file: `file` inline (or `--set-file`) or `existingConfigMap`; `prune`, `locked`. A change to `file` rolls the pods |
| `vaultToken.*` | off | A projected token for Vault's Kubernetes auth (ADR 0015), set as `QUERYAPIGATE_VAULT_K8S_TOKEN_FILE` - not the service account token, which stays unmounted |
| `serviceAccount.annotations` | `{}` | IRSA for `${aws-sm:...}` and `aws-rds-iam`: `eks.amazonaws.com/role-arn` |
| `extraVolumes`, `extraVolumeMounts` | `[]` | Files settings name by path (the SIEM's certificates, an export directory), in every pod |
| `ingress.*` | off | `className`, `host`, `tls`, `annotations` (ingress-nginx's no-buffering for `/events`) |
| `events.*`, `mcp.*` | off | The events server and the MCP server, each its own Deployment; `mcp.ingress.host` for its own host |
| `resources` | 250m / 256Mi, 1Gi limit | Per API server pod |
| `podSecurityContext`, `securityContext` | non-root, read-only root filesystem, no capabilities, RuntimeDefault seccomp | |
| `podDisruptionBudget.*` | on with more than one pod | All but one kept up |
| `serviceMonitor.*` | off | Prometheus Operator, `/metrics` |
| `networkPolicy.*` | off | In only from `ingressFrom`, out only to `egress` and DNS |
| `autoscaling.*` | off | PostgreSQL and Redis only |
| `nodeSelector`, `tolerations`, `affinity`, `topologySpreadConstraints`, `priorityClassName`, `serviceAccount.*`, `podAnnotations`, `podLabels`, `imagePullSecrets` | | As usual |

Every value is checked by `values.schema.json`. The full reference, with the rest of the deployment story:
[DEPLOYMENT.md](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/DEPLOYMENT.md#kubernetes-helm).
A walkthrough, run on kind:
[Install on Kubernetes with Helm](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/54-install-on-kubernetes-with-helm.md).
