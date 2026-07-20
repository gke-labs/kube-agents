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

- **Prettier** on changed Markdown, JSON, YAML:

  ```bash
  npx prettier --write <files>
  # or, from k8s-operator/:
  make prettier-write
  ```

  Check without modifying:

  ```bash
  npx prettier --check .
  ```

- **Docker build** (if you touched the platform-agent image):

  ```bash
  docker build -f deploy/docker/Dockerfile --target platform .
  ```

- **Operator compile + test** (if you touched `k8s-operator/`):

  ```bash
  cd k8s-operator
  make       # runs generate, manifests, fmt, vet, test, build
  ```

- **Docs build** (if you touched `docs/site/`):

  ```bash
  cd docs/site
  npm install
  npm run build
  ```

## Code review

All submissions, including from project members, require review through GitHub pull requests. See [GitHub Help — About pull requests](https://help.github.com/articles/about-pull-requests/).

## Where to file issues

Bug reports, feature requests, and questions: [github.com/gke-labs/kube-agents/issues](https://github.com/gke-labs/kube-agents/issues).

The [`github-issue-resolver` watchdog](/kube-agents/concepts/autonomous-watchdogs/) polls open issues every 30 minutes and (within tight guardrails) may triage or respond automatically. Human review still gates any resolution.
