# ADR 0018: Signed releases, and what they're built from

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended 2026-10-13, from the rehearsal (guide 59):** each release is signed in **two forms** - the Sigstore
  bundle cosign 3 writes by default, and the classic `.sig` tag (`--new-bundle-format=false`). Sigstore's
  policy-controller 0.13.1 finds only the second: given the bundle alone it refused the signed image as unsigned. The
  workflow verifies each form on its own. And Kyverno 1.19.1 needs Fulcio's current roots in the policy (`roots:`,
  from `https://fulcio.sigstore.dev/api/v2/trustBundle`): its built-in root predates Fulcio's intermediate.
- **Backlog:** the first item of "Before 1.0.0-rc1" in BACKLOG.md's roadmap.

## Context

A deployer runs what ghcr and PyPI hand them, and can't tell whether this repository's release workflow built it.
The threat model says so plainly: "the chart and image are as trusted as ghcr and this repository's release
workflow ... Neither is signed yet, so pin the version, and a digest if you mirror them." What exists today:

- **The image** (two variants, `amd64` and `arm64`) and **the chart** (an OCI artifact) are pushed to ghcr with
  `GITHUB_TOKEN` by `docker.yml`, on a tag. Nothing signs them. A tag can be moved: `:0.30.0` today needn't be the
  same bytes tomorrow, and nothing would show it.
- **PyPI** uses trusted publishing (OIDC, no stored token) - but through `pypa/gh-action-pypi-publish@release/v1`, a
  branch: whoever can push to that branch decides what runs with permission to publish QueryAPIGate.
- **What a release is built from** isn't pinned: `python:3.12-slim` and `node:22-slim` by tag, the GitHub Actions by
  tag (`docker/build-push-action@v6`). A build next week can pull different bytes under the same names.
- **No SBOM, no provenance.** A security team asking "what's inside, and which commit made it?" has the Dockerfile
  and the tag to go on.

Kubernetes admission controllers (Sigstore's policy-controller, Kyverno), `cosign verify` and `gh attestation
verify` can all check releases - if there's a signature to check.

## Decision

### 1. Every image and chart is signed, keyless

`docker.yml` signs what it pushed **by digest** - each image variant's multi-arch index, and the chart - with
`cosign sign`, keyless: the signing certificate comes from Sigstore's Fulcio for this workflow's GitHub OIDC
identity, and the signature is recorded in the public Rekor log. There's no key to keep, leak or rotate. What a
signature proves is the identity:

```
https://github.com/AnanthaRajuC/QueryAPIGate/.github/workflows/docker.yml@refs/tags/v<X.Y.Z>
```

issued by `https://token.actions.githubusercontent.com` - so it was this repository's workflow, run on that tag. The
job verifies its own signatures before it ends, with exactly the command a deployer runs; a release whose signature
doesn't verify fails.

### 2. Provenance and an SBOM with each

- **Images:** BuildKit's own attestations - SLSA provenance (`provenance: mode=max`) and an SPDX SBOM (`sbom: true`) -
  attached to each image index, and a GitHub artifact attestation (`actions/attest-build-provenance`), so `gh
  attestation verify oci://ghcr.io/anantharajuc/queryapigate:<X.Y.Z> --owner AnanthaRajuC` works too.
- **The chart:** a GitHub artifact attestation for its OCI digest.
- **PyPI:** PEP 740 attestations for the wheel and sdist, which the publish action makes under trusted publishing;
  `pypi.org` shows them, and `pypi-attestations verify` checks them.

### 3. What a release is built from is pinned

- **Base images by digest:** `FROM python:3.12-slim@sha256:...`, `FROM node:22-slim@sha256:...`. Dependabot proposes
  new digests (its `docker` ecosystem), so pinning doesn't mean falling behind on security fixes - it means each
  change is a reviewed commit.
- **Every action by commit SHA**, with its version in a comment (`uses: docker/build-push-action@<sha> # v6.x`), in
  every workflow - the publishing ones first. Dependabot's `github-actions` ecosystem keeps them current, the same
  way. A test fails on a `uses:` that isn't a full SHA.
- **Tools downloaded in workflows** (Helm, kind, kubectl) are already checked against their published checksums;
  `cosign` is installed by `sigstore/cosign-installer`, pinned like any action, at one exact cosign version.

### 4. Deployers can check it, and are told how

- A **"Verify a release"** section in SECURITY.md, and a how-to guide run for real: `cosign verify` for the image
  and the chart, `gh attestation verify`, reading the SBOM, and a Sigstore policy-controller `ClusterImagePolicy`
  (and the Kyverno equivalent) admitting only images this workflow signed.
- **The Helm chart's docs** show installing by digest, and each release's notes give the digests.
- The threat model's "Neither is signed yet" becomes what a signature does and doesn't promise.

### What it doesn't promise

- **That the code is right** - only that this repository's workflow, on that tag, built it. A compromised
  maintainer account can still tag a release; the signature then says exactly that, and Rekor keeps the record.
- **Reproducible builds.** Two builds of one commit aren't byte-identical (timestamps, package mirrors); provenance
  says how it was built, not that anyone can rebuild the same bytes. Out of scope here.
- **The base image's own supply chain** - Docker Hub's official images. Pinning by digest fixes what we build on; it
  doesn't vouch for it.

## Alternatives considered

- **A long-lived cosign key in a repository secret.** One more secret to keep, leak and rotate, and a signature
  that proves only "someone with the key". Keyless proves the workflow and the tag, and needs nothing stored.
- **Helm's own provenance files (`helm package --sign`, PGP).** Not stored beside OCI charts by ghcr, and PGP keys
  have the same problem as above. cosign signs an OCI chart exactly as it signs an image.
- **Notation (Notary v2).** Fine, but fewer clusters can check it today than cosign; and Sigstore's keyless flow
  needs no certificate authority of our own.

## Consequences

- A deployer can refuse anything not built by this repository's release workflow - by admission policy, not by
  trust.
- Releases depend on Sigstore's public services (Fulcio, Rekor) being up at tag time. If they're down, the release
  fails and is re-run; nothing is published unsigned.
- Every workflow change that adds an action needs its SHA - a little friction, on purpose.
- Rekor's log is public: it records that a release happened, which it does anyway.

## Delivery

1. **Pins:** base images by digest; every action by SHA; Dependabot for both; the test that refuses a `uses:`
   without a SHA.
2. **Signing:** cosign keyless for both image variants and the chart, BuildKit provenance and SBOM, GitHub artifact
   attestations, and the workflow verifying its own signatures. Tried end to end before a real tag: a `workflow_dispatch`
   run that signs into a throwaway repository path and verifies it.
3. **Verifying, documented and run for real:** SECURITY.md, the guide (`cosign verify`, `gh attestation verify`,
   the SBOM, a policy-controller cluster refusing an unsigned image), the threat model, the chart's docs - and release
   prep for 0.31.
