# Design: SemVer Deployment, Infrastructure & Operational Playbook Versioning

**Status:** Implemented (with deliberate exceptions listed in §5)
**Date:** 2026-07-31

---

## 1. Purpose

`kube-agents` development flows deploy from commit SHAs and the `:latest` tag across
container images, Kubernetes manifests, operator defaults, and scripts. That is right for
fast iteration and wrong for production GitOps, which needs immutable, comparable versions.
This design adopts **Semantic Versioning (SemVer 2.0.0)** for every production deployment
artifact: container images, the Helm chart, Terraform modules, and the release
documentation and governance playbooks around them.

## 2. Design decisions

1. **OCI registry for Helm charts, not a traditional chart repository.** The chart is
   published as an OCI artifact to GHCR (`oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents`)
   by `chart-release.yml` on SemVer tag pushes (`*.*.*`), reusing existing GHCR auth and storage.
   The chart `version` tracks `appVersion`: the workflow packages with both set from the
   git tag, so there is no independent chart-only release train.
2. **Terraform modules sourced by Git release tag.** Reusable modules live under
   `terraform/modules/<module-name>/` and consumers pin them with
   `git::https://github.com/gke-labs/kube-agents.git//terraform/modules/<module-name>?ref=1.2.0`,
   avoiding a separate module-registry backend.
3. **RC pipeline feeds SemVer promotion.** Pre-release validation keeps using RC tags
   (`rc_YYMMDDHHMM_<short_sha>`, `*_validated` on success — see `scripts/release/README.md`).
   A human then tags the validated commit `MAJOR.MINOR.PATCH`, which triggers the
   image and chart publication workflows.
4. **The operator defaults to its own release version.** Release builds inject the version
   via `-ldflags "-X ...DefaultPlatformAgentVersion=X.Y.Z"`; when a `PlatformAgent` CR
   omits `spec.deployment.image`, the generated Deployment uses the matching versioned
   agent image instead of `:latest`. Precedence: CR spec > `PLATFORM_AGENT_IMAGE` env >
   build-injected version > `latest`.

## 3. What ships

| Artifact             | Mechanism                                                                                                                                                                                   |
| :------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Container images     | `docker-publish-ghcr.yml` / `docker-publish-k8s-operator.yml` add an `X.Y.Z` tag on tag pushes (`:latest` only from branch pushes; every push also gets the SHA)                            |
| Operator default tag | Build-time version injection into `DefaultPlatformAgentVersion` (Makefile and operator Dockerfile ldflags)                                                                                  |
| Helm chart           | `charts/kube-agents/` (CRDs, operator, PlatformAgent CR), published and cosign-signed by digest via `chart-release.yml`                                                                     |
| Terraform modules    | `terraform/modules/{gke-cluster,kube-agents-iam,chat-pubsub,github-minter}/`, consumed via `?ref=1.2.0`; `terraform/examples/full-install/` composes all four plus the chart into one apply |
| Release guide        | [Release versioning & promotion](../site/src/content/docs/deploy/release-versioning.md)                                                                                                     |
| Governance           | `standardization_validator_sop.md` Rule 3 (immutable-tag compliance); pre-release artifact checks live in CI (`validate.yml` and the RC pipeline), not in an agent SOP                      |

## 4. Version flow

```mermaid
graph TD
    A["RC pipeline: rc_YYMMDDHHMM_sha → *_validated"] --> B["Human tags commit X.Y.Z"]
    B --> C["CI publishes GHCR images :X.Y.Z"]
    B --> D["CI publishes + signs OCI chart (version = appVersion = tag)"]
    B --> E["Git tag becomes ?ref=X.Y.Z for TF modules"]
    C --> F["PlatformAgent CR: spec.deployment.tag: X.Y.Z (or omit image for the operator default)"]
    D --> G["helm install oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents --version X.Y.Z"]
    E --> H["module source = git::...?ref=1.2.0"]
```

## 5. Deliberate exceptions and known gaps

- **Development flows keep `latest`.** `k8s-operator/config/manager/kustomization.yaml`
  still sets `newTag: latest` and `k8s-operator/scripts/common.sh` still offers `latest`
  as the default `IMAGE_TAG` — both serve the interactive/dev install path, not GitOps
  production deploys. Rule 3 of the standardization validator is what guards production
  namespaces.
- **User-supplied untagged images fall back to `latest`, not the injected version.** The
  operator deliberately does not stamp its own release version onto third-party image
  repositories (`resolveAgentImage`).
- **`imagePullPolicy` is static.** Deciding pull policy dynamically from tag shape
  (SemVer → `IfNotPresent`, mutable → `Always`) was considered and not implemented; the
  chart and templates set explicit values instead.
- **No chart lint rule forbids `:latest`.** Chart defaults simply never produce it;
  enforcement in rendered workloads comes from the governance SOP, not `helm lint`.
