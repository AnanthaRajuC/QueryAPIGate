# How to verify a release before you run it

**Time:** 20 minutes. **You'll end up with:** proof that the image and chart you run were built by this repository's
release workflow, from the commit it names - and a cluster that refuses anything else.

From 0.31, every image and the Helm chart is signed keyless with cosign, by digest, by `.github/workflows/docker.yml`
on the release's tag, with SLSA provenance, an SBOM and a GitHub attestation
([ADR 0018](../documentation/adr/0018-signed-releases.md)). Keyless means there's no key to trust: the signing
certificate names the workflow and the tag it ran on, and the signature is recorded in Sigstore's public log.

Every command and output below was run for real, against the release workflow's **rehearsal** - the same workflow,
run by hand on `main`, publishing under `-rehearsal` names (`ghcr.io/anantharajuc/queryapigate-rehearsal`). So its
identity ends `@refs/heads/main`. For a release, it ends `@refs/tags/v<X.Y.Z>`, and the names have no `-rehearsal`:

| | Rehearsal (below) | A release |
|---|---|---|
| Image | `ghcr.io/anantharajuc/queryapigate-rehearsal` | `ghcr.io/anantharajuc/queryapigate` |
| Chart | `ghcr.io/anantharajuc/charts-rehearsal/queryapigate` | `ghcr.io/anantharajuc/charts/queryapigate` |
| Identity | `https://github.com/AnanthaRajuC/QueryAPIGate/.github/workflows/docker.yml@refs/heads/main` | `...docker.yml@refs/tags/v0.31.0` |

The tools: [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) 3.1.3 and, for GitHub's
attestation, the `gh` CLI (or cosign, as shown).

## Check a signature

```bash
cosign verify ghcr.io/anantharajuc/queryapigate-rehearsal:rehearsal-d4b295f19b4f \
  --certificate-identity https://github.com/AnanthaRajuC/QueryAPIGate/.github/workflows/docker.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```
```
The following checks were performed on each of these signatures:
  - The cosign claims were validated
  - Existence of the claims in the transparency log was verified offline
  - The code-signing certificate was verified using trusted certificate authority certificates
```

The same for the `-h2` image and the chart. What doesn't verify:

```
# the same image, as the v0.31.0 release - it was signed on main, not that tag
Error: no matching attestations: failed to verify certificate identity: no matching CertificateIdentity found ...

# as another repository's workflow
Error: no matching attestations: failed to verify certificate identity: no matching CertificateIdentity found ...

# 0.30.0, released before signing
Error: no signatures found
```

Each release is signed in two forms: the Sigstore bundle (`cosign verify`, the default) and the classic `.sig` tag
(`cosign verify --new-bundle-format=false`). Both verify; admission controllers below need the second.

## See how it was built, and from what

GitHub's attestation names the repository, the workflow, the commit and the run:

```bash
cosign verify-attestation ghcr.io/anantharajuc/queryapigate-rehearsal@sha256:726f77fe01212b457e69ba49d745f9ba9bfc0d33815dbc78b8045fea7abe09ce \
  --new-bundle-format --type https://slsa.dev/provenance/v1 \
  --certificate-identity https://github.com/AnanthaRajuC/QueryAPIGate/.github/workflows/docker.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```
```
predicate https://slsa.dev/provenance/v1
  workflow https://github.com/AnanthaRajuC/QueryAPIGate .github/workflows/docker.yml refs/heads/main
  commit git+https://github.com/AnanthaRajuC/QueryAPIGate@refs/heads/main {'gitCommit': 'd4b295f19b4fcc45bff2e786bfa382cc2568054e'}
  run https://github.com/AnanthaRajuC/QueryAPIGate/actions/runs/38352041218/attempts/1
```

(decoded from the attestation's payload). With the `gh` CLI signed in, `gh attestation verify
oci://<image> --owner AnanthaRajuC` checks the same. The image's own provenance, from the build, records what it was
built on - the base images by digest:

```bash
docker buildx imagetools inspect ghcr.io/anantharajuc/queryapigate-rehearsal:rehearsal-d4b295f19b4f --format '{{json .Provenance}}'
```
```
pkg:docker/node@22-slim?digest=sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
pkg:docker/python@3.12-slim?digest=sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
```

## Read the SBOM

```bash
docker buildx imagetools inspect ghcr.io/anantharajuc/queryapigate-rehearsal:rehearsal-d4b295f19b4f --format '{{json .SBOM}}'
```

An SPDX 2.3 document per platform - 179 packages for `linux/amd64`, among them:

```
queryapigate 0.30.0
flask 3.1.3
werkzeug 3.1.9
cryptography 50.0.2
openssl 3.5.7-1~deb13u3
```

Feed it to your scanner (Grype, Trivy) to see which advisories apply, without pulling the image.

## Have a cluster refuse anything else

### Sigstore policy-controller

On a kind cluster with [policy-controller](https://docs.sigstore.dev/policy-controller/overview/) 0.13.1 (Helm chart
`sigstore/policy-controller` 0.10.8), a policy admitting only images this workflow signed on a release tag:

```yaml
apiVersion: policy.sigstore.dev/v1beta1
kind: ClusterImagePolicy
metadata:
  name: queryapigate-signed
spec:
  images:
    - glob: "ghcr.io/anantharajuc/**"
  authorities:
    - keyless:
        url: https://fulcio.sigstore.dev
        identities:
          - issuer: https://token.actions.githubusercontent.com
            subjectRegExp: '^https://github\.com/AnanthaRajuC/QueryAPIGate/\.github/workflows/docker\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$'
      ctlog:
        url: https://rekor.sigstore.dev
```

and `kubectl label namespace qag policy.sigstore.dev/include=true`. With the identity widened to `refs/heads/main`
too, the rehearsal image ran (`pod/signed created`, then `/health` answered `0.30.0`); without, or for 0.30.0:

```
admission webhook "policy.sigstore.dev" denied the request: ... signature keyless validation failed for authority
authority-0 for ghcr.io/anantharajuc/queryapigate-rehearsal@sha256:41a3...: no matching signatures: none of the
expected identities matched ...

admission webhook "policy.sigstore.dev" denied the request: ... ghcr.io/anantharajuc/queryapigate:0.30.0@sha256:4fca...
signature keyless validation failed ...: no signatures found
```

policy-controller finds only the classic `.sig` form - an image signed with a Sigstore bundle alone is refused as
unsigned, which is why releases carry both.

### Kyverno

With [Kyverno](https://kyverno.io/) 1.19.1 (Helm chart `kyverno/kyverno` 3.9.1):

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: queryapigate-signed
spec:
  validationFailureAction: Enforce
  webhookTimeoutSeconds: 30
  rules:
    - name: signed-by-its-release-workflow
      match:
        any:
          - resources:
              kinds: [Pod]
      verifyImages:
        - imageReferences: ["ghcr.io/anantharajuc/*"]
          mutateDigest: true
          verifyDigest: true
          attestors:
            - entries:
                - keyless:
                    issuer: https://token.actions.githubusercontent.com
                    subjectRegExp: '^https://github\.com/AnanthaRajuC/QueryAPIGate/\.github/workflows/docker\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$'
                    roots: |
                      <the certificates from https://fulcio.sigstore.dev/api/v2/trustBundle>
                    rekor:
                      url: https://rekor.sigstore.dev
```

**Give it `roots`.** Without them Kyverno 1.19.1 refused the signed image - `cert verification failed: x509:
certificate signed by unknown authority` - because its built-in Sigstore root predates Fulcio's current
intermediate. With the two certificates Fulcio publishes at `/api/v2/trustBundle`, it admitted the signed image and
rewrote the pod to run it by digest (`mutateDigest`), and refused 0.30.0: `no signatures found`.

## On the Helm chart

Verify the chart before installing it, and install by digest:

```bash
cosign verify ghcr.io/anantharajuc/charts/queryapigate:0.31.0 \
  --certificate-identity https://github.com/AnanthaRajuC/QueryAPIGate/.github/workflows/docker.yml@refs/tags/v0.31.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
helm install qag oci://ghcr.io/anantharajuc/charts/queryapigate --version 0.31.0 \
  --set image.digest=sha256:<the image's digest, from cosign verify>
```

## Good to know

- **A signature proves who built it, not that it's right**: this repository's workflow, on that tag, from that
  commit. A maintainer account that's compromised can still tag a release - and Rekor's public log then records it.
- **Pin by digest, not tag.** A tag can be moved to other bytes; a digest can't, and it's what was signed.
- **Releases before 0.31 aren't signed.** A policy like the ones above refuses them.

## Next steps

- [SECURITY.md](../SECURITY.md#verify-a-release) - the two commands, for a release.
- [ADR 0018](../documentation/adr/0018-signed-releases.md) - what's signed, how, and what it doesn't promise.
- [Install on Kubernetes with Helm](54-install-on-kubernetes-with-helm.md).
