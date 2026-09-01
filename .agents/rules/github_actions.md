# GitHub Actions rules

[`AGENTS.md`](../../AGENTS.md) states both rules below under Pull Request Hygiene. This file holds
the format each one takes and the cases exempt from it.

## Pin GitHub Actions to a full commit SHA

Every third-party `uses:` in `.github/workflows/` must reference a 40-character commit SHA with the
human-readable version in a trailing comment (`uses: actions/checkout@3d3c42e… # v7.0.1`). Mutable
tags (`@v4`, `@main`) are not permitted — a retagged release would silently change what CI runs.
Local reusable workflows (`uses: ./.github/workflows/…`) are exempt. Dependabot updates the SHA and
the comment together.

## Guard automatically-triggered credentialed workflows against forks

A workflow that needs this repository's secrets and starts on its own — `push`, a tag, `schedule`,
or `workflow_run` — carries `if: github.repository == 'gke-labs/kube-agents'` on every job. A fork
inherits those triggers but none of the secrets, so an unguarded job fails there on every sync and
mails the fork owner. Put the guard on each job rather than trusting the skip to cascade through
`needs`; an `always()` added later removes the implicit `success()` and the job runs anyway.

Two classes need no guard: a workflow reachable only through `workflow_call` is gated by its caller
(`reusable-deploy-*.yml`), and a `workflow_dispatch`-only one runs only when someone deliberately
starts it (`rc-create-tag.yml`, `deploy-environment.yml`, `rc-tag-validated.yml`,
`e2e-gchat-test.yml`). `docs-deploy.yml` is push-triggered and deliberately unguarded, so a fork
can publish its own Pages site.
