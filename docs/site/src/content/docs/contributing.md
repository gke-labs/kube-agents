---
title: Contributing
description: How to submit changes to kube-agents.
---

## Before you begin

### Sign the Contributor License Agreement

Contributions must be accompanied by a [Contributor License Agreement](https://cla.developers.google.com/about) (CLA). You (or your employer) retain copyright to your contribution; the CLA gives us permission to use and redistribute it as part of the project.

If you or your current employer have already signed the Google CLA (even for a different project), you probably don't need to do it again. Check at <https://cla.developers.google.com/>.

### Community guidelines

This project follows [Google's Open Source Community Guidelines](https://opensource.google/conduct/).

## PR hygiene (from `AGENTS.md`)

- **Scope.** Keep changes scoped to the request. Don't bundle unrelated formatting changes.
- **Structure.** Maintain the shape and intent of agent configuration files. Don't restructure `agents/platform/` for cosmetic reasons in an unrelated PR.
- **Commit style.** [Conventional Commits](https://www.conventionalcommits.org/).
- **Branch location.** Push PR branches to your fork, not to the upstream repository.
- **PR template.** Use [`.github/PULL_REQUEST_TEMPLATE.md`](https://github.com/gke-labs/kube-agents/blob/main/.github/PULL_REQUEST_TEMPLATE.md). Don't use `--fill` with `gh pr create` — it bypasses the template.

## Local validation

Before pushing, run the checks CI enforces:

- **Prettier** on changed Markdown and YAML (what the `Prettier Check` CI job enforces — it checks changed `.md`/`.yaml`/`.yml` files):

  ```bash
  # format all Markdown/YAML in the repo (root Makefile target)
  make prettier-write
  # or target specific files
  npx prettier --write <files>
  ```

  Check without modifying:

  ```bash
  make prettier-check
  ```

- **Repo structure validation** (the `Validate Repo Structure` CI job runs this on every PR):

  ```bash
  make validate   # fails if skills live under agents/*/defaults/skills/ instead of agents/*/skills/
  ```

- **Docker build** (if you touched the platform-agent image):

  ```bash
  # from the repo root; supplies the required HERMES_AGENT_TAG (from tags.env) and builds --target platform, matching the Docker Build CI job
  make docker-build-platform
  ```

- **Operator compile + test** (if you touched `k8s-operator/`):

  ```bash
  make -C k8s-operator test   # runs manifests, generate, fmt, vet, then go test — this is what the Operator Tests CI job runs
  ```

- **Docs build** (if you touched `docs/site/`):

  ```bash
  cd docs/site
  npm ci
  npm run build
  ```

## Release Program & Versioning Strategy

`kube-agents` follows Semantic Versioning (`vX.Y.Z`) and an automated release train pipeline modeled after Kubernetes ecosystem projects (KCC, Knative, Cert-Manager). For full details, see the dedicated [Release Engineering Strategy](/kube-agents/overview/release-engineering/) guide.

### Release Train Cadence

1. **Weekly Releases (`v0.X.0`)**: Every Tuesday at 14:00 UTC during active pre-1.0 feature development.
2. **Fortnightly Releases (Bi-Weekly)**: Every second Tuesday as core API surfaces reach 1.0 stability.
3. **Patch Releases (`v0.X.Y`)**: Published on-demand for critical bug or security fixes.

### GitHub Milestones & Attribution

- Every issue, PR, and bug fix belongs to a **GitHub Milestone** (e.g. `v0.22.0`).
- Use GitHub Milestones to communicate feature availability to customers (_"Upgrade to `v0.22.0` for feature X"_).

### 3-Gate Release Verification Pipeline

Pushing a release tag (`v0.1.0`) triggers `.github/workflows/release-build-publish.yml` alongside the container image publishing workflows (`.github/workflows/docker-publish-ghcr.yml` and `.github/workflows/docker-publish-k8s-operator.yml`). For complete tag-to-artifact mapping rules, see [Release Versioning](/kube-agents/deploy/release-versioning/).

1. **Gate 1 (Static & Security Verification)**: Runs `make validate`, `make docs-check`, `shellcheck`, Google OSV Scanner, and Go unit tests (`k8s-operator`).
2. **Gate 2 (Packaging & SBOM Verification)**: Lints Helm charts (`charts/kube-agents`), generates SPDX SBOMs (`*.spdx.json`), and packages web download bundles (`.tar.gz`, `.zip`).
3. **Gate 3 (Ephemeral E2E Smoke Tests)**: Provisions an ephemeral `Kind` Kubernetes cluster inside CI to validate installer, upgrade, and teardown scripts.
4. **Publish GA Release**: Automatically generates release notes categorized by Conventional Commits (`feat`, `fix`, `sec`) and attaches Helm chart bundles, SBOMs, and web download archives once all gates pass.

## Code review

All submissions, including from project members, require review through GitHub pull requests. See [GitHub Help — About pull requests](https://help.github.com/articles/about-pull-requests/).

## Where to file issues

Bug reports, feature requests, and questions: [github.com/gke-labs/kube-agents/issues](https://github.com/gke-labs/kube-agents/issues).

The [`github-issue-resolver` watchdog](/kube-agents/concepts/autonomous-watchdogs/) polls open issues every 30 minutes and (within tight guardrails) may triage or respond automatically. Human review still gates any resolution.
