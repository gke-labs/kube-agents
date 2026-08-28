# AGENTS.md

## Project Overview

This repository contains the Kubernetes Agentic Harness (`kube-agents`). It is a collection of agent configurations, personas, and skills designed to manage Kubernetes/GKE operations. It utilizes a Platform Agent to transition from reactive manual management to proactive, intent-driven operations.

## Repository Layout

- `agents/`: Source of truth for agent blueprints (personas and skills).
  - `chat/`: The Planning Agent front door — the `default` Hermes profile that receives chat ingress, plans the work, and delegates each piece to a specialist.
  - `platform/`: Configuration for the Platform Agent, scaffolded at pod startup into the `platform` profile.
  - `cluster/`: The Cluster Agent profile _template_ (persona, scoped config, and runtime-debugging skills). The Platform Agent scaffolds this into per-cluster Hermes profiles at runtime; it is not deployed directly.
- `.agents/skills/`: Repository-level skills, not shipped in the agent images — review skills (adversarial change review, security audits, docs-drift, skill quality) run against pull requests and clusters, with `review-preflight` running the pre-PR set of them in a context that did not write the change, plus the `install-kube-agents`/`uninstall-kube-agents`/`upgrade-kube-agents` lifecycle skills that drive the repository's installer scripts.
- `charts/`: Canonical Helm charts (`kube-agents`) for deploying the Kube-Agents operator and profiles.
- `terraform/`: Companion reusable Terraform modules (`gke-cluster`, `kube-agents-iam`, `chat-pubsub`, `github-minter`, `gke-backup-plan`, `drift-pubsub`) for infrastructure provisioning, plus `examples/full-install/`, the single-apply composition that installs the Helm chart on top. `drift-pubsub` is not yet part of that composition.
- `deploy/`: Deployment infrastructure code (Dockerfile, Kustomize bases, shared runtime assets).
- `docs/`: Documentation.
  - `site/`: The published documentation site (Astro + Starlight) — the canonical home for
    user-facing docs.
  - `architecture/`: The end-state architecture specification (`01`–`09`). Describes the target, not
    what ships today.
  - `designs/`: Per-feature design documents.
- `k8s-operator/`: Go/Kubebuilder operator reconciling `PlatformAgent` Custom Resources, plus the shared installer helpers under `scripts/`.
- `examples/`: Example integrations (LiteLLM provider configs, vLLM serving, inference replay).
- `bench/`: Evaluation harness that runs [kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench) against the Platform Agent as a pip-installed library.
- `images.json`: Inventory of every container image an install pulls, with its upstream reference
  and pin. Read by `make mirror-images`, the kustomize deploy targets, and the docs generator.
- `INSTALL.md`: Installation guide.
- `README.md`: Project overview.

## Where Tests Go

Tests live in nine places here, with different runners and different answers to "does this catch a
regression before merge". Choosing the wrong one rarely fails loudly — the test runs somewhere you
did not expect, or nowhere at all, and the suite reports green around it.

**Decide by asking whether a model call is in the loop.**

- **No** — it is a test, and it runs on every pull request. Put it beside the module it covers; in
  `tests/` when there is nothing to sit beside, as for a shell script or a rendered manifest; or in
  `tests/integration/` when it spans two components.
  See [`tests/integration/README.md`](tests/integration/README.md).
- **Yes, and you plant the defect it has to find** — it is an eval, it belongs in
  `bench/tasks/<name>/task.yaml`, and it runs in the Prow presubmit, so adding one changes what
  every pull request reports. [`docs/designs/bench-case-format.md`](docs/designs/bench-case-format.md)
  is the contract for what that file must carry; `make bench-case-check` checks it locally
  and `scripts/test_task_registration.py` gates it.
- **Yes, and it checks an install you already have** — it is a critical user journey, and it goes in
  `bench/cuj/`. **This tier is manual by design**, not pending automation: it needs a real
  deployment to point at and CI has none, so no job runs it and adding one changes nothing about
  what CI reports. It plants nothing, so it grades the deployment rather than the agent.
  See [`bench/cuj/README.md`](bench/cuj/README.md).
- **Yes, and it is the release gate** — `tests/e2e/`, which the release-candidate pipeline runs on a
  schedule. Adding to it holds up releases rather than pull requests.

One rule holds wherever it lands: a new test directory only runs if a `PYTHON_TEST_DIRS` glob in the
`Makefile` reaches it, and a directory the globs miss fails nothing — it sits unexecuted while the
suite reports green around it. Add the glob in the same change.

The nine homes, what runs each, and how far "runs on a pull request" is from "gates a merge" are in
[`docs/testing-map.md`](docs/testing-map.md).

## Agent Setup & Integration

This repository is primarily a configuration and documentation repository for AI agents. The main exception is the Go-based Kubernetes operator in `k8s-operator/`, which requires compilation (see Local Validation Checks below).

To use these agents:

1. Follow the instructions in [INSTALL.md](INSTALL.md) to set up and register the Platform Agent in your agent harness.
2. Refer to the documentation site content in [docs/site/src/content/docs/](docs/site/src/content/docs/) for architecture, concepts, and operational guides.

## Before Starting a Task

### Branch from a `main` you have just fetched

`main` takes on the order of ten commits a day, so a checkout that has sat for a week is a
different repository from the one you are about to describe to the user. Reading a stale working
tree does not fail loudly — it answers your questions, just about code that no longer exists — and
the plan you build on those answers can be wrong in a way no amount of care during the work will
catch. A session planned an addition to `.github/workflows/auto_request_review.yml` from a
checkout 42 commits behind, describing the third-party action that workflow used to run; #736 had
since rewritten it to drive `scripts/request_reviewers.py`, whose `skip_reason` already did the
thing the session was proposing to add. Nothing about the plan looked wrong until it came time to
edit the file.

So fetch first, and branch from the fetched ref rather than from whatever the working tree happens
to be sitting on:

```bash
# `upstream` here is whichever remote points at gke-labs/kube-agents; on a clone of
# the upstream repository rather than a fork, that is `origin`. Every command in this
# section names it, so substitute throughout rather than in one line.
git fetch upstream main

# --no-track matters. Branching from a remote-tracking ref otherwise sets the new
# branch to track upstream/main, and a bare `git push` later then proposes
# `git push upstream HEAD:main` -- a push to the upstream repository, which Pull
# Request Hygiene below forbids. Publish to your fork: `git push -u <fork> <branch>`.
git switch -c <branch> --no-track upstream/main
```

Already partway into a branch when you read this, or picking one back up after a few days? Being
behind is not itself the problem — forty commits touching nothing you care about cost you nothing.
What wastes work is `main` moving _underneath the files you are changing_. Measure that with the
drift check in
[`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#measure-how-far-a-branch-has-drifted-from-main),
which lists the files you are changing that `main` has also changed since you diverged. Anything it
lists, rebase onto `upstream/main` and re-read those files before you write more, because what you
have already read about them may no longer be true. Nothing listed, and being behind is a
merge-conflict risk to settle later, not a reason to stop.

This subsection is the canonical statement of the requirement; the site's
[contributing guide](docs/site/src/content/docs/contributing.md) summarises it — change this
first, then reconcile that to it.

### Check whether someone is already doing it

Many people and agents work in this repository at once, so the next step of a non-trivial task
is finding out whether someone is already doing it. Scan the open work and report what you find
to the user **before** you write code. Skip the scan only when the user has already named the
issue or pull request you are working on, or when the change is a one-liner they asked for
directly.

The queries are in
[`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#check-whether-someone-is-already-doing-it).
Then report before you start:

- **An open pull request touches your files or solves your problem.** Give the number, author,
  and URL, and say how your task differs. Do not push to someone else's branch and do not open
  a competing pull request without the user's go-ahead. Overlap alone is a merge-conflict
  warning, not a stop sign — say which it is.
- **An open issue describes the task and is unassigned.** Give the number and title, offer to
  claim it, and say what you would comment. Assign or comment only after the user agrees — and
  the account whose token you hold is a person, so you are volunteering them, not yourself.
- **The issue is assigned to someone else.** Report it and ask before starting anything.
- **Nothing matches.** Say so in one line and carry on.

Carry the result into the pull request's **Context** section — `Closes #<number>`, or the
related open pull request and how yours differs.
[`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) already reserves that
section for it.

This is not the `status:in-progress` claim in
[`agents/platform/skills/github-issue-resolver/SKILL.md`](agents/platform/skills/github-issue-resolver/SKILL.md).
That is the deployed Platform Agent claiming an issue on a user's repository at runtime. Here
the assignee is the claim; do not apply `status:` labels to issues in this repository.

## Skills Guidelines

- Skills are located under `agents/platform/skills/` (Platform Agent: provisioning, governance, cost, manifest generation, GitOps) and `agents/cluster/skills/` (Cluster Agent: single-cluster runtime debugging and operations).
- Each skill directory must contain a `SKILL.md` file providing instructions for that specific skill.
- Place a skill according to its persona: fleet/provisioning/GitOps-write skills belong to the Platform Agent; read-only, single-cluster runtime-debugging skills belong to the Cluster Agent.
- When adding new skills, ensure they follow the existing structure and are clearly documented to be understood by AI agents.

## Documentation Guidelines

Every fact has one home. Duplicating documentation across files is how it goes stale, so before
adding a paragraph, check whether the topic already has an owner:

| Content                                                  | Canonical home                               |
| -------------------------------------------------------- | -------------------------------------------- |
| User-facing narrative, how-to, and reference             | `docs/site/src/content/docs/`                |
| End-state architecture                                   | `docs/architecture/`                         |
| Per-feature design rationale                             | `docs/designs/`                              |
| Shared installer defaults and the `vars.sh` state model  | `k8s-operator/scripts/README.md`             |
| Which container images an install pulls, and their pins  | `images.json`                                |
| The install procedure (self-contained, agent-executable) | `INSTALL.md`                                 |
| The commands behind this file's pull-request rules       | `docs/pull-request-workflow.md`              |
| What the agent is and is not permitted to do             | the site's `reference/security-and-iam.md`   |
| How to develop a specific directory                      | that directory's `README.md` (keep it short) |

Rules:

- **Do not hand-write a table that mirrors a machine-readable file.** The cron schedule, the skill
  catalogue, and the container-image inventory are generated into
  `<!-- BEGIN GENERATED -->` regions by `scripts/generate_docs.py`, which also writes
  `docs/family-roster.txt` whole. Edit the source, then run `make docs-generate`.
- **Do not restate the `make` targets.** `make help` prints them from the Makefile. New targets get
  a `## description` comment.
- **Link rather than summarise** when another page already owns the topic. If you must summarise,
  say which page is canonical, the way the site's credential-isolation page defers to
  `docs/credential-isolation-design.md`.
- **Do not document pull-request status.** Docs describe the current state of `main`; a merged PR
  leaves that prose silently stale.
- **Verify identifiers against source, not against other docs.** Service account names live in
  `k8s-operator/scripts/common.sh`, the Go version in `k8s-operator/go.mod`.
- **Add a document to the map (`docs/README.md`) with one line, and change nothing else there.**
  Write the row in the compact `| cell | cell |` form and never re-align a table: the map is edited
  from several branches every week, and a re-aligned table rewrites rows your PR did not author.
  `docs/README.md` §5 owns the rest of that contract — including why a file inside an existing
  family needs no map edit at all.
- **Write it straight.** Lead with the fact — no preamble, no restating the question, no "it's
  worth noting". Cut hype and self-assessment (`comprehensive`, `robust`, `seamless`, `simply`,
  `powerful`). Skip the "not X, but Y" antithesis and rule-of-three padding: one precise example
  beats three synonyms. Prefer prose to a `**Bold term:** explanation` list. Claim first, caveat
  after; a hedge in front of a fact hides it. `SKILL.md` files are the exception to the prose
  preference — `.agents/skills/skill-review/SKILL.md` asks for terse imperative bullets there.
- **Match a document's length to what the task needs.** Agent-written documents run long by
  default, so cover the substance and stop: no filler sections, no summary that repeats the
  section above it, no boilerplate scaffolding a reader will skip. Anthropic's
  [Opus 5 prompting guide](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5)
  is the upstream source for this and for the conciseness rule above.

Run `make docs-check` before pushing. It verifies generated regions are current, relative links
resolve, identifiers match their source, every Markdown document has an entry in the documentation
map (`docs/README.md`), and this file plus `CLAUDE.md` stay inside the context budget
(`scripts/check_context_budget.py`) — the same five checks CI runs.

## Pull Request Hygiene

- Keep changes scoped to the request.
- Do not commit unrelated formatting changes.
- Maintain the structure and intent of the agent configuration files.
- **Conventional Commits & PR Title Enforcement:** All PR titles and commit messages must strictly adhere to the Conventional Commits specification (`type(optional-scope): description`):
  - **Permitted Types:** `feat` (new user-facing capability), `fix` (bug fix), `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
  - **Breaking Changes:** Mark with `!` before the colon (e.g. `feat!:`, `fix(operator)!:`) or a `BREAKING CHANGE:` footer.
  - **Release Preparation:** Standardized PR titles ensure consistent commit history and establish the Conventional Commit metadata required for the automated SemVer release pipeline. AI agents must ensure the proposed PR title prefix accurately reflects the changes in the branch diff and confirm classification with the author before opening a PR.
- Push PR branches to a fork, not to the upstream repository.
- **Pin GitHub Actions to a full commit SHA.** Every third-party `uses:` in
  `.github/workflows/` must reference a 40-character commit SHA with the human-readable
  version in a trailing comment (`uses: actions/checkout@3d3c42e… # v7.0.1`). Mutable tags
  (`@v4`, `@main`) are not permitted — a retagged release would silently change what CI runs.
  Local reusable workflows (`uses: ./.github/workflows/…`) are exempt. Dependabot updates the
  SHA and the comment together.
- **Guard automatically-triggered credentialed workflows against forks.** A workflow that needs
  this repository's secrets and starts on its own — `push`, a tag, `schedule`, or `workflow_run`
  — carries `if: github.repository == 'gke-labs/kube-agents'` on every job. A fork inherits those
  triggers but none of the secrets, so an unguarded job fails there on every sync and mails the
  fork owner. Put the guard on each job rather than trusting the skip to cascade through `needs`;
  an `always()` added later removes the implicit `success()` and the job runs anyway. Two classes
  need no guard: a workflow reachable only through `workflow_call` is gated by its caller
  (`reusable-deploy-*.yml`), and a `workflow_dispatch`-only one runs only when someone deliberately
  starts it (`rc-create-tag.yml`, `rc-deploy-environment.yml`, `rc-tag-validated.yml`,
  `e2e-gchat-test.yml`). `docs-deploy.yml` is push-triggered and deliberately unguarded, so a fork
  can publish its own Pages site.
- Use `.github/PULL_REQUEST_TEMPLATE.md` for PR body structure and level of
  detail. Do not use `--fill` with `gh pr create` as it bypasses the template.
- **AI Agent Attribution & Commit Authorship:**
  - Do not add AI agents as git commit co-authors or include `Co-Authored-By:` trailers in commit messages.
  - Note AI assistance in the PR description (e.g. `Generated with the help of <Agent/Model>.`).
- **Write PR titles, bodies, commit messages, and review replies the same way** the Documentation
  Guidelines' "Write it straight" rule requires: what changed and why, in plain declaratives. Do
  not grade your own work — "comprehensive", "significantly improves", and "production-ready" are
  claims the diff either supports or does not, and the reviewer is the one who decides. Lead with
  the outcome: the first sentence of a PR body, a review reply, or a report back to the user
  answers "what happened", and the supporting detail follows it.
- **Adversarial self-review before opening a PR, and record it in the PR body.** Run the
  `review-adversarial` skill (`.agents/skills/review-adversarial/SKILL.md`) against your branch
  diff, fix what it confirms, and fill in the template's **Self-Review** section with what you
  looked for, what it found, and the disposition of each finding. This is a required pre-PR step
  for AI agents working in this repository: you are the change's first hostile reader, and a
  reviewer who has to find what you could have found spends their attention on the wrong things.
  The section carries every pre-PR pass, not this one alone — the docs-drift pass below runs on
  every change too — merged into one list, so a reviewer reads what was looked for in one place
  rather than inferring which passes ran from which findings appeared.
  This bullet is the canonical statement of the requirement; the site's
  [contributing guide](docs/site/src/content/docs/contributing.md) and the comment in
  [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) summarise it — change
  this list first, then reconcile them to it.
  - **Run the pass in a context that did not write the change** — a subagent, or a new session,
    handed the diff range and nothing else. Not your plan, not your reasoning, not the summary you
    were about to write. Reviewing a diff in the conversation that produced it is the one
    configuration that reliably does not work: the same context that talked you into the code
    talks you into approving it, and the blind spot sits exactly where you were already wrong.
  - **`/pr-preflight` is how you get one**, and it covers the docs-drift pass below at the same
    time. It wraps
    [`.agents/skills/review-preflight/SKILL.md`](.agents/skills/review-preflight/SKILL.md), which
    holds the plumbing and the rules for what to do with what comes back. Read the skill directly if
    your harness has no slash commands. Invoking the command is also the request to delegate that an
    agent is otherwise told to wait for — coding agents are instructed not to spawn subagents on
    their own initiative, so an agent that reads only the rule above finds its one route closed and
    takes the silent fallback.
  - **If your harness will not spawn one without a human's approval, go and get the approval.** A
    setting that requires sign-off before starting a subagent blocks this step; it does not waive
    it. Ask when you hit it, not after the review, and say what you are blocked on. Quietly running
    the pass in the session that wrote the code instead buys a review from the context that already
    believes the change is correct, and reporting that as a self-review without the caveat tells
    the reviewer something untrue about how the change was checked.
  - **Every finding gets a disposition: fixed, or deliberately not with a reason that argues about
    this change.** "Out of scope", "pre-existing", and "will fix later" are not reasons on their
    own; the separate issue you filed is. Fix what a pass confirms and report what it only
    suspects — a finding it could not pin down is an open question for the section, not a licence
    to rewrite working code. And "no findings" is an answer only alongside what you looked for: a
    pass that names none of its angles is indistinguishable from no pass.
    [`.agents/skills/review-preflight/SKILL.md`](.agents/skills/review-preflight/SKILL.md) §6
    elaborates, including how to merge two passes that grade differently.
  - **Do not claim more than you did.** A self-review the diff contradicts is worse than none: it
    spends the reviewer's trust before they reach the code. Name the kind of context each pass ran
    in — subagent, fresh session, or the one that wrote the change — so the claim above it is
    something a reviewer can weigh rather than take on trust.
- **Docs-drift review before opening a PR:** run the `review-docs-drift` skill
  (`.agents/skills/review-docs-drift/SKILL.md`) against your branch diff and address its
  Blocking findings. This is a required pre-PR step for AI agents working in this repository;
  `make docs-check` enforces only the mechanical subset (generated regions, links, terminology,
  map coverage, context budget), while the skill also verifies that doc prose still matches the
  source. Its dispositions go in **Self-Review** with the adversarial pass's, not in a section of
  their own. `/pr-preflight` runs this pass alongside the adversarial one, each in its own context.
- **Live-test the change before opening a PR, and describe it in the PR body.** Every pull
  request fills in the template's **Testing → Live validation** section with how the change was
  exercised against a real, running kube-agents installation — see [INSTALL.md](INSTALL.md) if
  you do not have one. Green unit tests and a clean `make docs-check` are necessary, not
  sufficient: they cannot tell you whether the operator reconciled the change or the agent pod
  picked it up. This bullet is the canonical statement of the requirement; the site's
  [contributing guide](docs/site/src/content/docs/contributing.md) and the comment in
  [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) summarise it — change
  this list first, then reconcile them to it.
  - **Name the install and what you observed.** Cluster, image tag, operator version; what you
    did; and the result at each layer the change claims to touch — the CR `.status`, the
    Deployment env, the file or process inside the pod.
  - **Prove the mechanism, not a coincidence.** If the new value happens to equal the old
    default, the observation proves nothing. Set something distinctly different, then revert and
    confirm it goes back.
  - **Say what you could not cover, and why**, rather than implying full coverage. Clean up test
    artifacts, restore prior state, and note anything left behind.
  - **Screenshots of graphical surfaces go through `scripts/pr_evidence_screenshot.sh`**, which
    publishes the image where a PR body can render it and prints Markdown stamped with the
    commit and capture time. Command output stays as fenced text transcripts — a screenshot of a
    terminal is evidence degraded, not evidence.
  - **If the install is shared with other agents, take the lease.**
    `scripts/live_test_lease.py` holds it as a ConfigMap in the install's own namespace. Copy
    `.claude/settings.json.example` to `.claude/settings.json` once per checkout and its
    `PreToolUse` hook claims the lease for you on the first mutating command and denies the
    command while somebody else holds it — so two agents cannot overwrite each other's live
    validation. Without the copy nothing is enforced, and you run `acquire` and `release` by hand.
    [`docs/designs/live-test-lease.md`](docs/designs/live-test-lease.md) covers what counts as a
    mutation, how an install is discovered, and why the wiring is not committed.
  - **If the change cannot reach a running installation** — docs-only, a CI workflow, a code path
    that needs infrastructure you do not have — write "Not live-tested" and say why. An empty
    section is not an answer.
- **Keep these sections current, not chronological.** **Self-Review** and **Live validation** tell
  a reviewer at a glance what has been reviewed and exercised against the branch as it stands. A
  second pass — after review findings, after a rebase — folds into what is there rather than being
  appended beneath it: work that still holds stays and is not re-run just to have been run against
  the new head, a check the new commits invalidated is re-run or kept with a line saying it no
  longer reaches the head, and new findings join the rest. What a re-run drops is the superseded
  round, not the contents these sections owe a reviewer — the angles you ran, the layers you
  observed, what you could not cover. Round-by-round history of a _reviewer's_ findings is the
  exception: it belongs in the threads, where a reply naming the fix and its commit stays attached
  to the finding it answers.
- **The install has one engine: Terraform + Helm.** `terraform/examples/full-install`
  (through its `lifecycle.sh`) owns every GCP resource and the chart owns every
  Kubernetes resource; `install.sh` / `uninstall.sh` / `upgrade.sh` are front doors
  that generate `terraform.tfvars` and drive it. Do not add a second expression of an
  install step — a kubectl-applied manifest a chart template already renders, a gcloud
  call the composition already makes. The two places manifests still exist twice on
  purpose (`k8s-operator/config/crd` + `config/rbac` mirrored into the chart by
  `make chart-check`, and the kustomize integration manifests kept in step with the
  chart templates for the dev path) each have a check or a comment saying so.
- **Expect an automated review after opening a PR.** Opening the pull request starts
  `kube-agents-bot`; see
  [Automated Review After Opening a Pull Request](#automated-review-after-opening-a-pull-request)
  for what it does and what you are expected to do with its findings.
- **Leave no conversation unresolved.** `main` will not merge while a review thread is open, and
  the open thread also keeps the pull request counted as its author's outstanding work.
  Reply, then resolve every thread you are confident is addressed — the bar for "confident" is in
  [Automated Review After Opening a Pull Request](#automated-review-after-opening-a-pull-request),
  and the commands are in
  [`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#resolving-conversations). Two
  things that make a branch look clear when it is not: a thread whose line fell out of the diff
  reads as outdated, which says the code moved and nothing about whether the finding still holds,
  so read it; and the listing stops at the first hundred threads, so a long-lived pull request
  needs paging before you can call it clear rather than say you looked at a hundred.
- **Local Validation Checks:** Before committing, run what your change touches — `prettier --write`
  on changed Markdown and YAML, a local Docker build of the agent runner, the image-layer budget if
  you added a `RUN` or `COPY` to `deploy/docker/Dockerfile`, and `go build` inside `k8s-operator/`.
  Each has a constraint that costs a CI run to rediscover — the pinned prettier version, the
  mandatory `--platform linux/amd64`, the layer ceiling that only fails after merge. The
  commands and those reasons are in
  [`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#local-validation-before-committing).

## Automated Review After Opening a Pull Request

Every pull request here is reviewed automatically by `kube-agents-bot`, a GitHub App that runs a
coding agent over the branch diff. It only comments — it never pushes commits and never merges.
Opening a pull request is therefore not the end of the task. The bot introduces itself in a comment
on every pull request it picks up, and that comment states its current contract; if it disagrees
with what follows, believe the comment and fix this section.
[`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#the-automated-review) holds the
mechanics: how long a review takes, the commands to poll for one, and how to reply to a finding —
with [resolving the threads](docs/pull-request-workflow.md#resolving-conversations) alongside it.

**What any reviewer reads first — human or agent, this bot included.** Read the pull request's
**Self-Review** section before the diff. It tells you what the author already looked for, what they
found, and what they consciously chose not to fix, so the review can start where theirs stopped.
Three things to do with it:

- **Absent, empty, or a bare "reviewed it"** → say so as the first thing you report. The section is
  required (see Pull Request Hygiene) and an unanswered one is the finding.
- **A claim it makes that the diff does not support** → that is a finding in its own right, and a
  more serious one than most defects: it misdirects every reader after you.
- **A finding the author rejected with a reason** → engage with the reason. Restating the finding
  as though the reason were not there wastes both of you.

**When it runs.** On `opened`, `reopened`, and draft-marked-ready. **Pushing more commits does not
start another review** — an active branch would otherwise pay for a re-read on every push. To get a
fresh review of the current commit, comment `/review` on a line of its own (repository owners,
members, and collaborators only) — that pass is the strict one, only what the bot is certain of,
while `/review all` re-reads at the width of the automatic first review and includes findings it
believes are real without being sure. The `agent:ignore` label opts a pull request out entirely and
outranks both.

**A human reviewer is requested only once its check passes.** The bot posts an `AI Review` check
run alongside its review — `success` when it found nothing, `neutral` when it did — and
`.github/workflows/auto_request_review.yml` waits for that check to go green before assigning
anyone from `.github/auto_request_review.yml`. Opening a pull request no longer pings a human, so
clearing the findings and commenting `/review` for a clean pass is what puts the change in front of
a reviewer. Two exceptions: a pull request opened by a bot is assigned as soon as the check
completes, whatever the conclusion, because Dependabot cannot re-run `/review` on itself; and an
owner, member, or collaborator can comment `/request-review` (at the start of the comment) to
assign a reviewer immediately — the override for a finding you have answered but disagree with, or
for a review that never arrived. Nothing here changes who is picked; that is still the config file.

**What agents must do.** After creating a pull request, tell the user the bot review is on its way
and **offer to wait for it** instead of reporting the work as finished — unless you opened a draft,
which is not in the queue at all until it is marked ready, so a wait started there never ends and
the bot is not broken for failing to answer it. A review that runs always reports back, so a
one-line "no findings" is a result rather than silence; a review that never arrives is a bug in the
bot, not a verdict, and the workflow doc says how long to wait and which trigger replaces the pass
you lost.

Then work the findings **with** the user rather than acting on them unilaterally: summarise each
one, say whether you think it should be fixed, pushed back on, or deferred, and let the user decide
before you change code. The bot is a reviewer, not an authority — but a finding you disagree with
gets answered in its thread, not silently dropped. After pushing fixes, remember that the push alone
does not re-trigger anything: ask the user whether to comment `/review` for another pass — `/review`
to confirm the fixes against a strict read, `/review all` when the branch changed enough that it
deserves a first-review-width look again.

Pushing fixes is also what makes the pull request body stale. Fixes that answer a finding, and any
live test you re-ran to confirm them, belong in **Self-Review** and **Live validation** — folded
into what is already there, per "Keep these sections current, not chronological" above. Do it once
the last `/review` pass has settled, for the reason the next paragraph gives about threads: a fresh
review brings fresh findings, and folding them in twice is the same wasted round. Nothing else in
this workflow reopens the body, so a branch whose sections still describe the commit it was opened
at is the normal outcome of skipping it here.

**Then resolve the conversations.** `main` requires every conversation on a pull request to be
resolved before it can merge, and the triage sweep counts an open thread as work outstanding on the
author — so a branch whose fixes have all landed still sits blocked, and still shows up as the
author's problem rather than the reviewers'. Clearing the threads is part of finishing the change,
not a courtesy someone else will get to. Do it once the fixes are pushed and the last `/review` pass
has settled: a fresh review opens fresh threads, so resolving before it lands means doing it twice.

Resolve a thread — the bot's or a human's — when you are **fully confident the issue is addressed**:
the fix is on the pull request head and you can name the commit, or the finding is factually wrong
and you have said why. Check that second one against the merge target as it stands now, not against
your working copy — a finding that looks wrong because the file it cites does not say that is very
often a stale checkout rather than a wrong finding. Anything short of that stays open. A judgment
call, a reviewer asking for something you chose not to do, a rebuttal nobody has answered yet —
reply and leave it to them. Resolving says the conversation is finished; it is not a way to end a
disagreement. Reply first, always: a resolved thread collapses, so the reply naming what changed and
the commit that changed it is the only record the reviewer may ever see.

## Before Reviewing Someone Else's Pull Request

The section above is about your own pull request being reviewed, and it already says where a
reviewer starts: the **Self-Review** section, before the diff. This one is the question that comes
before even that — whether the review you have been asked for needs to happen at all.

By the time anyone asks, a pull request here has usually been read twice already: `kube-agents-bot`
reads every one, and "Pull Request Hygiene" separately required the author to run
`review-adversarial` over their own diff, record the result in **Self-Review**, and exercise the
change under **Live validation**. Where both of those hold and neither has gone stale, a third
hostile read is usually redundant spend.

So check for both first — and then **ask rather than decide**. Say what the evidence is and that
the extra round may be unnecessary; let the person who asked choose whether to spend it. Skipping a
review unilaterally is not yours to do, and neither is quietly running one you have reason to
believe nobody needs.

Two things make this go wrong quietly:

- **Currency.** A clean review sitting at an older commit proves nothing about the current head —
  unless the only commits since it are merges from the base branch, which are not new work to
  review. Treat a review whose commit has vanished from the branch as stale, not clean. An
  unresolved review thread says the same thing: work outstanding, however clean the latest review
  reads.
- **A Self-Review that is present but unanswered.** "No findings" counts only alongside what was
  looked for, so a bare "reviewed it" is an absent section with characters in it — and, per the
  section above, the first finding your review reports rather than a reason to skip it.

[`.claude/commands/pr-review-batch.md`](.claude/commands/pr-review-batch.md) is the canonical home
for the mechanics — the queries, what counts as a clean verdict, the verdicts they produce, and what
to put in front of the user. Follow it whether or not the review was started through the slash
command, and change it rather than this section when the mechanics move.
