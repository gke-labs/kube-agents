---
# Claude Code loads this rule only beside files matching `paths`; Antigravity uses `trigger` and `description`.
paths:
  - ".github/workflows/**"
  - ".github/buildkitd.toml"
trigger: model_decision
description: "GitHub Actions workflow rules: pinning third-party actions to full commit SHAs, guarding credentialed workflows against forks, collapsing superseded pull-request runs with a concurrency group, detecting changed files with git rather than the API, and pulling Docker Hub images through mirror.gcr.io."
---

# GitHub Actions rules

[`AGENTS.md`](../../AGENTS.md) states the first two rules below under Pull Request Hygiene. This
file holds the format each one takes and the cases exempt from it, and the rules after them,
which `AGENTS.md` does not state.

## Pin GitHub Actions to a full commit SHA

Every third-party `uses:` in `.github/workflows/` must reference a 40-character commit SHA with the
human-readable version in a trailing comment (`uses: actions/checkout@3d3c42e… # v7.0.1`). Mutable
tags (`@v4`, `@main`) are not permitted — a retagged release would silently change what CI runs.
Local reusable workflows (`uses: ./.github/workflows/…`) are exempt. Dependabot updates the SHA and
the comment together.

## Guard automatically-triggered credentialed workflows against forks

A workflow that needs this repository's secrets and starts on its own — `push`, a tag, `schedule`,
`status`, or `workflow_run` — carries `if: github.repository == 'gke-labs/kube-agents'` on every job. A fork
inherits those triggers but none of the secrets, so an unguarded job fails there on every sync and
mails the fork owner. Put the guard on each job rather than trusting the skip to cascade through
`needs`; an `always()` added later removes the implicit `success()` and the job runs anyway.

Two classes need no guard: a workflow reachable only through `workflow_call` is gated by its caller
(`reconcile-environment.yml`, `teardown-environment.yml`), and a `workflow_dispatch`-only one runs only when someone deliberately
starts it (`rc-create-tag.yml`, `deploy-environment.yml`, `rc-tag-validated.yml`,
`e2e-gchat-test.yml`). `docs-deploy.yml` is push-triggered and deliberately unguarded, so a fork
can publish its own Pages site.

## Collapse superseded pull-request runs with a concurrency group

Every workflow that starts on `pull_request` carries this block at the top level:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

A second push to a pull request then cancels the first push's queued and running runs instead of
queueing behind them. The group falls back to the run id, so `push`, `merge_group` and
`workflow_dispatch` runs each sit in a group of their own and are never cancelled or displaced:
GitHub holds one pending run per group and cancels the older one, which on a burst of merges to
`main` would lose the runs in the middle before they started. The cancel flag is an expression
rather than `true` so that the intent reads from the block; the fallback is what makes it safe.
`tests/test_pull_request_concurrency.py` holds the group key, the fallback, the head key below
and the pull-request cancel; it accepts a bare `true` for the cancel flag, since the fallback keeps
that safe too.

A workflow whose `pull_request` trigger also lists `edited` adds the head SHA to the group,
`-${{ github.event.pull_request.head.sha }}`, as `validate-pr-title.yml` and the
`pull_request_target` workflow `risk_classify.yml` do. `edited` does not advance the head and
nothing orders run creation by event, so an `edited` run still carrying the old head can arrive
after the `synchronize` run for the new one; in a group keyed on the number alone it cancels or
displaces that run, and the new head carries no check run under the workflow's name until the
next push or edit. Keyed by head, a run can only supersede runs for its own head.

`smoke-test-sticky.yml` has no group on purpose and says why in its header: it starts on `status`
and `push`, never on `pull_request`, and a dropped pending run there would drop a re-pin.

## Detect changed files with git, not the API

A `dorny/paths-filter` step on a workflow that starts on `pull_request` sets `token: ""`. With a
token the action lists a pull request's files through the GitHub API, a page at a time, with no
retry around any page, and one failed page reds the check: on 2026-10-14 between 01:33 and 01:42
UTC that call failed in four runs across `a2a-test.yml` and `k8s-operator-test.yml`, two of them
re-run by hand, and in the five thousand pull-request runs of those workflows around them it failed
in none; the flaky-check tracker opened #2984 and #2983 for the two checks. With no token the action
diffs the checked-out merge commit against the base SHA with git, the path it already takes on push
and merge-queue runs, after one depth-one fetch of the base SHA from the server the checkout just
used. `tests/test_paths_filter_uses_git_diff.py` holds the input on every such step.

The checkout before the step stays at its default depth of one. A depth of two would make the base
tip local and spare that fetch, but it changes what a `workflow_dispatch` run on `main` tests: at
depth one the shallow root makes the action see every file as changed and the whole suite runs, at
depth two it sees the tip commit's files alone and a dispatch after a commit outside the filter's
paths tests nothing.

A `pull_request_target` workflow is the exemption: there the action runs in the base branch's
context and refuses an empty token, so it keeps the API path. None uses the action today.

## Pull Docker Hub images through mirror.gcr.io

Docker Hub limits anonymous pulls per source IP, and GitHub-hosted runners share egress IPs, so a
burst of pull requests fails every job that pulls from Docker Hub at once: on 2026-10-09 every
failed image build and every failed GHCR publish that day died on it, most within seconds of
starting. A workflow on a GitHub-hosted runner therefore pulls Docker Hub images through
`mirror.gcr.io`, which serves public Docker Hub images without that limit and takes no credential,
so a fork keeps the same path.

- Every `docker/setup-buildx-action` step takes `buildkitd-config: .github/buildkitd.toml`, which
  mirrors `docker.io` for the builder (a reference the mirror does not hold falls through to
  Docker Hub), and `driver-opts: image=mirror.gcr.io/moby/buildkit:buildx-stable-1`, because the
  action pulls the builder image on the runner's own daemon before the builder exists, so the
  config cannot cover it. Both on every step, whether or not the job builds today: a job's build
  can be a `run:` line, which no check can tell from one that does not.
- A job `container:`, a `services:` image, a `docker://` action, or a `docker run` or
  `docker pull` of a Docker Hub image, names it as `mirror.gcr.io/<repository>` with the same tag
  and digest.

`tests/test_workflow_pins_and_fork_guards.py` checks the buildx steps, job containers, service
images and `docker://` actions of every workflow against this rule, and that the config file holds
the mirror.

A floating tag resolves to the manifest the mirror last refreshed, which can trail Docker Hub by
days; pin a digest where that matters. The kind node image the installer matrix starts is the one
exception: kind pairs that image with its own release, so naming one on the mirror would also pin
the Kubernetes version the matrix tests, and that is a separate decision. Cloud Build pulls from
Google's egress and is out of scope.
