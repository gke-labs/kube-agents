# AGENTS.md

## Project Overview

This repository contains the Kubernetes Agentic Harness (`kube-agents`): agent configurations, personas, and skills for managing Kubernetes/GKE operations.

## Repository Layout

- `agents/`: Source of truth for agent blueprints (personas and skills).
  - `chat/`: The Planning Agent front door: the `default` Hermes profile that receives chat ingress, plans the work, and delegates each piece to a specialist.
  - `platform/`: Configuration for the Platform Agent, scaffolded at pod startup into the `platform` profile.
  - `cluster/`: The Cluster Agent profile _template_ (persona, scoped config, runtime-debugging skills), which the Platform Agent scaffolds into per-cluster Hermes profiles at runtime; not deployed directly.
  - `contributor/`: The contributor-agent protocol: the claim/PR/review/escalation loop for external bots (e.g. Kyber, Codebot Robot) coordinating over GitHub alone. Not a runtime blueprint; not shipped.
- `.agents/skills/`: Repository-level skills, not shipped in the agent images: review skills (adversarial, security, docs-drift, skill quality) run against pull requests and clusters, `review-preflight` runs the pre-PR set in a context that did not write the change, the `install-`/`uninstall-`/`upgrade-kube-agents` lifecycle skills drive the installer scripts, and `edit-mirrored-skill` edits mirrored `gke-*` skills.
- `.agents/rules/`: Repository-level rules, one file per family: code (`core_engineering.md`), workflows (`github_actions.md`), pre-PR passes (`pre_pr_review.md`), eval-driven development (`eval_driven_development.md`), docs (`documentation.md`).
- `a2a/`: Go module for the agent-to-agent bus — wire-protocol library and `a2a` topics CLI per `docs/designs/spec-a2a-payloads.md`, plus persona, gateway and auth-callout.
- `charts/`: Canonical Helm chart (`kube-agents`) deploying the operator and profiles.
- `terraform/`: Reusable Terraform modules (`gke-cluster`, `kube-agents-iam`, `kube-agents-scope-resolver`, `chat-pubsub`, `github-minter`, `gke-backup-plan`, `drift-pubsub`), plus `examples/full-install/`, the single-apply composition that installs the Helm chart on top.
- `deploy/`: Dockerfile, Kustomize bases, shared runtime assets.
- `docs/`: Documentation.
  - `site/`: The published site (Astro + Starlight), the canonical home for user-facing docs.
  - `architecture/`: The end-state specification (`01`–`09`): the target, not what ships today.
  - `designs/`: Per-feature design documents.
- `k8s-operator/`: Go/Kubebuilder operator reconciling `PlatformAgent` resources.
- `scripts/`: Repository tooling: `installer/` (shared by the front doors), `dev/`, `release/`.
- `examples/`: LiteLLM provider configs, vLLM serving, inference replay.
- `bench/`: Evaluation harness running [kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench) against the Platform Agent.
- `images.json`: Inventory of every container image an install pulls, with its upstream reference
  and pin; read by `make mirror-images`, the kustomize deploy targets, and the docs generator.
- `INSTALL.md`: Installation guide.
- `README.md`: Project overview.

## Where Tests Go

Tests live in many places. **Decide by whether a model call is in the loop:**

- **No** — it is a test and runs on every pull request. Put it beside the module it covers; in
  `tests/` when nothing is there to sit beside (shell scripts, rendered manifests); in
  `tests/integration/` when it spans two components ([`tests/integration/README.md`](tests/integration/README.md));
  in `bench/tests/` when one component is the bench harness. Carve-out: **security and permissions
  invariants** go in `tests/conformance/`.
- **Yes, and you plant the defect it has to find** — it is an eval in
  `bench/tasks/<name>/task.yaml` and runs in CI. [`docs/designs/bench-case-format.md`](docs/designs/bench-case-format.md)
  is the contract (`make bench-case-check`, `scripts/test_task_registration.py`). **A change to
  agent behaviour starts from one:** red locally, implement, green three times, registered:
  [`.agents/rules/eval_driven_development.md`](.agents/rules/eval_driven_development.md).
- **Yes, and it checks an install you already have** — it is a critical user journey in
  `bench/cuj/` ([`bench/cuj/README.md`](bench/cuj/README.md)). Manual by design: it grades a live
  deployment, plants nothing, and CI does not run it.
- **Yes, and it is the release gate** — `tests/e2e/`, run on a schedule by the release pipeline.

A new Python test directory only runs if a `PYTHON_TEST_DIRS` glob in the `Makefile` reaches it
(`tests/conformance/` excepted by design); add the glob in the same change. Full map:
[`docs/testing-map.md`](docs/testing-map.md).

## Agent Setup & Integration

This repository is mostly configuration and documentation for AI agents, plus the Go modules in
`k8s-operator/` and `a2a/`. Follow [INSTALL.md](INSTALL.md) to set up and register the Platform
Agent; [docs/site/src/content/docs/](docs/site/src/content/docs/) has the architecture, concepts,
and operational guides.

## Before Starting a Task

### Branch from a `main` you have just fetched

`main` takes roughly ten commits a day. Always fetch first and branch from the fetched ref:

```bash
# `upstream` is the remote for gke-labs/kube-agents (`origin` on a direct clone).
git fetch upstream main
# --no-track keeps a bare `git push` off upstream/main; push to your fork.
git switch -c <branch> --no-track upstream/main
```

Already on a branch? Measure whether `main` moved underneath the files you are changing with the
drift check in
[`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#measure-how-far-a-branch-has-drifted-from-main).
If it lists a file, rebase onto `upstream/main` and re-read those files before writing more; if it
lists nothing, being behind is a merge-conflict risk to settle later, not a reason to stop.
[`CONTRIBUTING.md`](CONTRIBUTING.md) points here rather than restating this.

### Check whether someone is already doing it

Before writing code on a non-trivial task, scan open PRs and issues with the queries in
[`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#check-whether-someone-is-already-doing-it)
and report to the user (skip only when the user named the issue/PR or asked for a one-liner):

- **An open pull request touches your files or solves your problem.** Give the number, author, and
  URL, and say how your task differs. Do not push to someone else's branch or open a competing PR
  without the user's go-ahead. File overlap alone is a merge-conflict warning, not a stop sign.
- **An open issue describes the task and is unassigned.** Give the number and title, offer to claim
  it, and say what you would comment. Assign or comment only after the user agrees (the token is a
  person's account).
- **The issue is assigned to someone else.** Report it and ask before starting.
- **Nothing matches.** Say so in one line and carry on.

Record the result in the PR's **Context** section (`Closes #<number>`, or the related open PR and
how yours differs) per [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md). Do
not apply `status:` labels here; those belong to the runtime claim loop in
[`agents/platform/skills/github-issue-resolver/SKILL.md`](agents/platform/skills/github-issue-resolver/SKILL.md).

## Skills Guidelines

- Skills live under `agents/platform/skills/` (Platform Agent) and `agents/cluster/skills/` (Cluster Agent); each holds a `SKILL.md`.
- Place a skill by persona: fleet, provisioning and GitOps-write skills go to the Platform Agent; read-only, single-cluster runtime debugging to the Cluster Agent.
- `agents/platform/skills/gke-*` (a reserved prefix) are copies of `google/skills`. One with an `upstream.lock` in `agents/platform/skill-overlays/<skill>/` is edited in place and recorded with `make skills-refresh` (see `edit-mirrored-skill`); the rest are overwritten by `scripts/sync-upstream-skills.py`, so also register their changes in one of its `SKILL_*` registries (any file of the skill).

## Engineering Rules

Read the matching file in [`.agents/rules/`](.agents/rules/) before writing code:

- **No magic constants.** Declare every hardcoded value (number, string, duration, path, limit) as a
  named constant at the top of the file, after imports and before the first function, on lines you
  write or touch in Go, Python, and Bash (exempt: `0`, `1`, `-1`, `""`, a literal that is the
  subject of its line, and test files): [`.agents/rules/core_engineering.md`](.agents/rules/core_engineering.md).
- **Name it for what it holds.** CodeQL treats `secret` or `trusted` in an identifier as a
  credential; word lists in [`.agents/rules/core_engineering.md`](.agents/rules/core_engineering.md).

## Documentation Guidelines

Every fact has one home; check whether the topic has an owner before adding prose:

| Content                                        | Canonical home                       |
| ---------------------------------------------- | ------------------------------------ |
| Installing and running kube-agents yourself    | `docs/site/src/content/docs/`        |
| End-state architecture                         | `docs/architecture/`                 |
| Per-feature design rationale                   | `docs/designs/`                      |
| Installer defaults and the `install.env` model | `scripts/installer/README.md`        |
| Container images an install pulls, and pins    | `images.json`                        |
| The install procedure (agent-executable)       | `INSTALL.md`                         |
| Commands behind this file's PR rules           | `docs/pull-request-workflow.md`      |
| What the agent is and is not permitted to do   | site `reference/security-and-iam.md` |
| How to develop a specific directory            | its `README.md` (keep it short)      |
| Maintainer environments, workflow secret maps  | `docs/environment-reconcile.md`      |
| Release runbooks                               | `scripts/release/README.md`          |
| Evaluation project pool and Prow configuration | `docs/ci-pool-projects.md`           |
| Agent rules, by family                         | `.agents/rules/`                     |
| Who to ask about an area or a running service  | `docs/ownership.md`                  |

Rules (see also [`.agents/rules/documentation.md`](.agents/rules/documentation.md)):

- **Do not hand-write a table that mirrors a machine-readable file.** Edit the source and run
  `make docs-generate` to refresh `<!-- BEGIN GENERATED -->` regions.
- **Do not restate `make` targets.** `make help` prints them; new targets get a `## description`.
- **Link rather than summarise** when another page owns the topic.
- **The site carries no maintainer identifier** (App/installation ID, workflow secret/variable,
  internal project, service account, Workload Identity pool, repo path, maintainer environment).
- **Do not document pull-request status** or cite a PR/issue number as the reason a behaviour exists
  in user/maintainer docs.
- **Verify identifiers against source, not against other docs** (`install.defaults.env`,
  `k8s-operator/go.mod`, the identifier table in `docs/README.md`).
- **Link a new document from the page that owns its topic; add no map entry** to `docs/README.md`.
- **Write it straight and match length to the task.** Lead with the fact; cut hype (`comprehensive`,
  `robust`, `seamless`), "not X, but Y", and filler sections; prefer prose to `**Bold term:**`
  lists (`SKILL.md` excepted per `.agents/skills/skill-review/SKILL.md`).

Run `make docs-check` before pushing: generated regions, links/reachability, terminology, site
audience, and this file plus `CLAUDE.md` against the context budget.

## Contributing as an agent

Unattended agents (no human in the loop) must read
[`agents/contributor/AGENTS.md`](agents/contributor/AGENTS.md): it defines the agent-to-agent loop
(claims, escalations, review tiers) and governs where unattended execution conflicts with "ask the
user" clauses here. Agents with a user in the loop follow this file.

## Pull Request Hygiene

- Keep changes scoped to the request; do not commit unrelated formatting changes.
- Maintain the structure and intent of the agent configuration files.
- **Conventional Commits & PR Title Enforcement:** PR titles and commit messages use
  `type(optional-scope): description` (`feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`,
  `build`, `ci`, `chore`, `revert`; breaking changes marked with `!` before `:` or a
  `BREAKING CHANGE:` footer). Confirm the title prefix with the author before opening a PR.
- Push PR branches to a fork, not to the upstream repository.
- **Pin every third-party GitHub Action to a full commit SHA with a version comment**
  (`uses: actions/checkout@3d3c42e… # v7.0.1`), and **guard automatically-triggered credentialed
  workflows against forks** with `if: github.repository == 'gke-labs/kube-agents'` on every job.
  Exemptions: [`.agents/rules/github_actions.md`](.agents/rules/github_actions.md).
- Use [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) (never `gh pr create --fill`).
  A bug fix fills in **Bug Fix: Preventing Recurrence**: why it shipped, what catches it now, where
  else it lives ([`.agents/rules/pre_pr_review.md`](.agents/rules/pre_pr_review.md)).
- **AI Agent Attribution:** No AI co-author trailers (`Co-Authored-By:`) on commits; note AI
  assistance in the PR description instead.
- **Write PR titles, bodies, commits, and review replies straight:** plain declaratives leading with
  the outcome, without self-grading (`comprehensive`, `production-ready`).
- **Adversarial and docs-drift self-review before opening a PR:** run `review-adversarial`
  (`.agents/skills/review-adversarial/SKILL.md`) and `review-docs-drift`
  (`.agents/skills/review-docs-drift/SKILL.md`) against your branch diff **in a clean context that
  did not write the change** (`/pr-preflight` spawns one). Fix confirmed findings and record one
  merged disposition list under **Self-Review**: what you looked for, what was found, the context
  used. Mechanics: [`.agents/rules/pre_pr_review.md`](.agents/rules/pre_pr_review.md).
- **Live-test the change before opening a PR:** fill in **Testing → Live validation** with how the
  change was exercised against a running installation ([INSTALL.md](INSTALL.md)), the red-to-green
  eval loop for agent behaviour changes, or "Not live-tested" with the reason when no install can
  reach it. Mechanics and shared-install lease rules:
  [`.agents/rules/pre_pr_review.md`](.agents/rules/pre_pr_review.md).
- **Keep `Self-Review` and `Live validation` current, not chronological:** fold later passes and
  fixes into the sections rather than appending rounds; round-by-round history belongs in the
  review threads ([`.agents/rules/pre_pr_review.md`](.agents/rules/pre_pr_review.md)).
- **The install has one engine: Terraform + Helm.** `terraform/examples/full-install` (via
  `lifecycle.sh`) owns every GCP resource and the chart every Kubernetes resource;
  `install.sh` / `uninstall.sh` / `upgrade.sh` only generate `terraform.tfvars` and drive it. Do not
  add a second expression of an install step. `make chart-check` holds operator-owned YAML mirrored
  into the chart in step.
- **Expect an automated review after opening a PR** from `kube-agents-bot`; see
  [Automated Review After Opening a Pull Request](#automated-review-after-opening-a-pull-request).
- **Leave no conversation unanswered.** Open threads block merge and keep the PR counted as
  [its author's outstanding work](docs/pull-request-workflow.md#who-owns-an-open-pull-request), a
  declined bot thread excepted (below). Reply first, then resolve every addressed thread per
  [`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#resolving-conversations).
- **You do not merge it; Tide does** once a reviewer's `lgtm` and an `OWNERS` approver's `approved`
  are present and required checks pass. Never post `/lgtm` or `/approve` on someone's behalf unless
  asked. Mechanics: [`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#how-a-change-merges).
- **Local Validation Checks:** Before committing, run the checks for what you touched:
  `prettier --write` on changed Markdown/YAML, `make shellcheck` on shell scripts, a
  `--platform linux/amd64` Docker build (plus `scripts/check_image_layers.py` for a new `RUN`/`COPY`
  in `deploy/docker/Dockerfile`), `go build` in `k8s-operator/` or `a2a/`, `make terraform-test` on
  Terraform modules. Commands:
  [`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#local-validation-before-committing).

### The behavioural presubmit gate

`pull-kube-agents-smoke-test` runs the eval matrix in `hack/ci-eval-pr.sh` (3 repetitions per active
case, 1.5–3.5 hours against a 360-minute ceiling). Non-inert pushes restart it, so open the PR early
and batch changes; a Tide retest after `main` moves reuses the head's green in minutes. It goes red
when a case on `BOOTSTRAP_ADMITTED` in `hack/ci-eval-pr.sh` fails all repetitions, or any case trips
an absolute rung (forbidden cluster mutation, verifier error, inconsistent liveness signals; a record
with no run at all is excluded as infrastructure unless every case hits one). Demotion:
`docs/eval-gate-roster.md`; verdict ladder:
[`docs/designs/testing-strategy.md`](docs/designs/testing-strategy.md) §4.2. On a red, read the
health bot comment and <https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html>:
fix a regression your PR caused, or file a `presubmit-gate` issue for a gate flake. One `/retest` is
reasonable for a suspected transient; never merge around a red gate, and never instruct anyone to.
`/override` (admin-only) is only for a red the eval crew classified as not the PR's
([how a change merges](docs/pull-request-workflow.md#how-a-change-merges)).

## Automated Review After Opening a Pull Request

`kube-agents-bot` reviews every pull request; it comments only and never pushes or merges. Its intro
comment states its live contract: if it disagrees with what follows, believe the comment and fix
this section. Polling and reply commands:
[`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#the-automated-review) and
[resolving the threads](docs/pull-request-workflow.md#resolving-conversations).

**What any reviewer reads first — human or agent, this bot included.** Read **Self-Review** before
the diff:

- **Absent, empty, or a bare "reviewed it"** → report that first; the section is required.
- **A claim the diff does not support** → report that as a serious finding in its own right.
- **A finding the author rejected with a reason** → engage with the reason; do not restate the finding.

**When it runs.** On `opened`, `reopened`, and draft-marked-ready. A push re-triggers nothing unless
the bot's last review said the branch does not merge. Comment `/review` (owners, members,
collaborators) for a strict pass over the current commit, or `/review all` for a first-review-width
pass. The `agent:ignore` label opts out.

**A human reviewer is requested once its check passes, or at the bot's third round.**
`.github/workflows/auto_request_review.yml` assigns from `.github/auto_request_review.yml` when the
`AI Review` check run goes green (zero findings on the first review, no 🔴 High on later ones;
🟠 Medium is posted, not held: [the cases](docs/pull-request-workflow.md#what-the-check-means)), or,
once, when the bot has reviewed three commits and the check is still grey. The first request posts
a hand-off comment: from there the reviewer decides, and you reply in the threads. Bot-opened PRs
assign on check completion; `/request-review` (at the start of the comment, by owners, members, or
collaborators) overrides the gate for a disputed finding or a missing review.

**What agents must do.** After opening a ready PR (a draft sits outside the queue until marked
ready), tell the user the bot review is on its way and **offer to wait for it**; a one-line "no
findings" is a result, a review that never arrives is a bug in the bot (the workflow doc says how
long to wait). Work the findings **with** the user: summarise each, say whether to fix, push back, or
defer, and let them decide before changing code; answer a disagreed finding in its thread.
[The stop rule](docs/pull-request-workflow.md#green-is-settled) has the full text: before green, at
most two `/review`s of your own, and at the third reviewed commit the PR goes to a human whatever
the colour (`/request-review` if none is on it yet), then stop. **Green is settled**: a `success` check ends the bot's part, and no `/review` in any form
follows it; answer each open 🟠 Medium by reply, push a fix only for one you would have fixed
unasked, and the human rules on the rest. Only a human requesting changes, a non-author's `/review`,
or a diff grown past twice the green head's size owes one more; a merge of `main` that changes
nothing of yours earns none. On a later round, decline by default: a 🟠 Medium is fixed only as a
`behaviour` finding on code this PR added, by one mechanical edit per site; a correct 🔴 High is a
fix on any round; a fix that would add a mechanism is a design question for the human, deferred to
an issue. **Testing**, **Live validation** and **Self-Review** go current in the same push as the
fix, before any `/review`; a skipped live run is written `Not live-tested: <what a run would show,
why no install reaches it>`; a description ask repeated after your reply is a dispute for the human
reviewer, not another edit.

**Resolve a thread** only when **fully confident the issue is addressed**: the fix is on the PR head
with its commit named, or the finding is factually wrong against `main` (not a stale checkout) and
you have said why. A judgment call, a declined ask, or an unanswered rebuttal stays open with a
reply; resolving does not end a disagreement. A declined `kube-agents-bot` finding stays open until
a human rules on it (an approval, or a reply accepting the decline; one asking for the fix owes it),
then you resolve it citing the ruling; with a user in the loop you may resolve it once your reply
and **Self-Review** give the reason, bar the description thread, but prefer not to. Reply first: a
resolved thread collapses, and the reply is the only record a reviewer may see.

## Before Reviewing Someone Else's Pull Request

Before running a review you were asked for, check whether `kube-agents-bot` and the author's
**Self-Review** + **Live validation** already cover the current head. If both hold and neither is
stale, **ask rather than decide**: show the evidence and let the requester choose whether to spend
another round. Two traps:

- **Currency:** a review at an older commit is stale unless the only commits since are merges from
  the base branch, and any unresolved review thread means work is still outstanding.
- **Unanswered Self-Review:** "no findings" counts only alongside what was looked for.

Mechanics, queries, and verdicts live in
[`.claude/commands/pr-review-batch.md`](.claude/commands/pr-review-batch.md).
