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

- **Start from a freshly fetched `main`.** `main` moves fast enough that a week-old checkout is a different repository, so branch from `upstream/main` after fetching it rather than from whatever your working tree is on — a plan built by reading a stale checkout is wrong before you write a line. [`AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/AGENTS.md) states this in full and is canonical; [`docs/pull-request-workflow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/pull-request-workflow.md) has the commands, including how to tell whether `main` has moved underneath the files you are changing.
- **Check for existing work.** Before you start, scan open pull requests and issues for someone already on it — a PR touching the same files, or an issue you should be assigned to. [`AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/AGENTS.md) states this in full; the queries are in [`docs/pull-request-workflow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/pull-request-workflow.md).
- **Scope.** Keep changes scoped to the request. Don't bundle unrelated formatting changes.
- **Structure.** Maintain the shape and intent of agent configuration files. Don't restructure `agents/platform/` for cosmetic reasons in an unrelated PR.
- **Commit style.** [Conventional Commits](https://www.conventionalcommits.org/).
- **Branch location.** Push PR branches to your fork, not to the upstream repository.
- **PR template.** Use [`.github/PULL_REQUEST_TEMPLATE.md`](https://github.com/gke-labs/kube-agents/blob/main/.github/PULL_REQUEST_TEMPLATE.md). Don't use `--fill` with `gh pr create` — it bypasses the template.
- **Live validation.** Every PR describes how the change was exercised against a real, running installation. See [Live validation](#live-validation) below.
- **Self-review.** Every PR arrives already reviewed by its author, and says what that review found. See [Self-review](#self-review) below.

## Local validation

Before pushing, run the checks CI enforces. This section is about running the tests; for deciding
where a new one belongs, `docs/testing-map.md` maps the nine test homes to their runners.

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

- **Integration seams** (if you touched a component that another one talks to across a process, language, or protocol boundary):

  ```bash
  make test-integration   # just this tier, for working on a seam; CI reaches it through `make test-python`
  ```

  Real components wired together with the agent replaced by a fake — no cluster, no model. The tier is in `PYTHON_TEST_DIRS`, so `make test-python` runs it and the Run Python Unit Tests job gates on it; the target above is the fast loop for one tier while you work on a seam. Install a Go toolchain first if you want an honest answer — the injector seam compiles the real Go event-watcher client, and without `go` on `PATH` its four tests skip and the run still prints `OK`. `tests/integration/README.md` states the tier's contract.

- **Docs build** (if you touched `docs/site/`):

  ```bash
  cd docs/site
  npm ci
  npm run build
  ```

## Live validation

The checks above tell you the code compiles, the docs resolve, and the unit tests agree with themselves. None of them tell you whether the operator reconciled your change or the agent pod picked it up — this project's failure mode is a green build that configures nothing. So every pull request fills in the template's **Testing → Live validation** section with how the change was exercised against a real, running kube-agents installation. If you don't have one, [INSTALL.md](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md) stands one up.

[`AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/AGENTS.md) states this requirement and [`.agents/rules/pre_pr_review.md`](https://github.com/gke-labs/kube-agents/blob/main/.agents/rules/pre_pr_review.md) holds its mechanics; together they are canonical. What follows summarises the pair, so trust them over this page if they ever differ.

What that section should say:

- **Which install, and what you did.** Cluster, image tag, operator version, and the steps you ran.
- **What you observed at each layer the change touches** — the CR `.status`, the Deployment env, the file or process inside the pod. A change that claims to reach the pod is verified by reading it in the pod.
- **Evidence the mechanism worked, not a coincidence.** If your new value happens to equal the previous default, observing it proves nothing. Set something distinctly different, confirm it lands, then revert and confirm it goes back.
- **What you could not cover, and why.** An honest gap is more useful than an implied one.
- **Screenshots for graphical surfaces** (admin console, docs site, chat) come from `scripts/pr_evidence_screenshot.sh`, which publishes the image and prints Markdown stamped with the commit and capture time. Command output stays as fenced text transcripts, not screenshots.
- **Cleanup.** Remove test artifacts, restore prior state, and note anything left behind.

Some changes can't reach a running installation — docs-only edits, CI workflow changes, code paths that need infrastructure you don't have. Write "Not live-tested" and say why. An empty section is not an answer.

If your team shares one installation, take the lease before you mutate it: `scripts/live_test_lease.py` holds it as a ConfigMap in the install's own namespace. Copy `.claude/settings.json.example` to `.claude/settings.json` once per checkout, and its `PreToolUse` hook claims the lease on your first mutating command and blocks the command while another agent holds it. Read-only commands are never blocked, and nothing is protected until a checkout has a `vars.sh` or you configure an install. The hook is not committed — it would be branch content Claude Code runs unprompted — and it is Claude Code-specific, so from any other harness, or a plain shell, run `acquire` and `release` yourself:

```bash
cp .claude/settings.json.example .claude/settings.json   # opt into the hook
python3 scripts/live_test_lease.py status
python3 scripts/live_test_lease.py acquire --env my-cluster --pr 123 --note "why"
python3 scripts/live_test_lease.py release --env my-cluster
```

`--env` picks the install when more than one resolves, and the commands that act on one require it
there; `status` reports all of them either way. [`docs/designs/live-test-lease.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/live-test-lease.md) has the details.

## Self-review

Nobody reads a change as cheaply as the person who wrote it, and right now the first hostile reader of most pull requests here is a reviewer who has never seen the code. So every pull request is reviewed by its author first, and the template's **Self-Review** section carries what those passes found — merged into one list, since more than one pass is required.

[`AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/AGENTS.md) states this requirement and [`.agents/rules/pre_pr_review.md`](https://github.com/gke-labs/kube-agents/blob/main/.agents/rules/pre_pr_review.md) holds its mechanics; together they are canonical. What follows summarises the pair, so trust them over this page if they ever differ.

The method is the repository's own review skill, [`.agents/skills/review-adversarial/SKILL.md`](https://github.com/gke-labs/kube-agents/blob/main/.agents/skills/review-adversarial/SKILL.md) — run it against your branch diff with whatever agent you use. It works ten angles over the change, then re-derives each candidate from the source as a hostile second reader and throws out what it cannot defend.

Give the pass a context that did not write the change — a subagent, or a fresh session, handed the diff range and nothing else. An agent asked to review a diff in the same conversation that produced it mostly restates why the code is right, because the reasoning that produced the code is still in front of it. If your harness will not start a subagent without a human's approval, ask for the approval — that setting blocks this step rather than waiving it.

This pass is not the only one [`AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/AGENTS.md) requires before a pull request opens: the docs-drift pass runs on every change as well. In Claude Code, `/pr-preflight` covers both — a subagent per pass, one merged list back — and its plumbing lives in [`.agents/skills/review-preflight/SKILL.md`](https://github.com/gke-labs/kube-agents/blob/main/.agents/skills/review-preflight/SKILL.md) for any harness without slash commands. Reach for it rather than the session you are already in: coding agents are typically told not to spawn subagents unless asked, and invoking the command is that ask.

What the section should say:

- **What you looked for**, in the skill's terms — which angles you ran, and which you could not.
- **What kind of context each pass ran in** — a subagent, a fresh session, or the one that wrote the change. A reviewer weighs the rest of the section against that answer.
- **What it found, and where each finding ended up.** Fixed, naming the commit or hunk; or deliberately not fixed, with a reason. A reason is an argument about this change — the path is unreachable for a stated invariant, the fix belongs to the issue you just filed. "Out of scope" or "will fix later" alone is not.
- **Nothing you cannot back.** A self-review the diff contradicts is worse than no self-review: it spends the reviewer's trust before they reach the code. Fix what the pass confirms and report what it only suspects; a finding it could not pin down is an open question for the section, not a licence to rewrite working code.

Re-running the pass folds into the section rather than stacking a round beneath it. Keep what still holds, re-state what the new commits changed, and drop the superseded round — not a finding's disposition. The same goes for **Live validation**: a reviewer should be able to see at a glance what has been reviewed and exercised against the branch as it stands.

"No findings" is an ordinary outcome on a good change and costs you nothing — provided you also say what you looked for. A pass that names none of its angles is indistinguishable from no pass at all.

## Code review

All submissions, including from project members, require review through GitHub pull requests. See [GitHub Help — About pull requests](https://help.github.com/articles/about-pull-requests/).

Nobody merges by hand. This is a Prow repository: `google-oss-prow` squash-merges a pull request once it carries both the `lgtm` label — which a reviewer's "Approve" sets, and which Prow will not take from the author — and the `approved` label, which comes from an approver in the `OWNERS` file covering the changed paths. GitHub's own branch protection requires zero approving reviews, so the settings page is not where the review requirement lives. [`docs/pull-request-workflow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/pull-request-workflow.md) is canonical for this and has the rest: the labels that block a merge, `/hold`, which checks are required, and how to read what Tide is waiting for — including why `mergeStateStatus` cannot tell you.

### Automated review

Every pull request is also reviewed by `kube-agents-bot`, a GitHub App that runs a coding agent over the branch diff. It only comments — it never pushes commits and never merges, and it does not replace the human review above. It introduces itself in a comment on every pull request it picks up; that comment states its current contract, so trust it over this page if the two ever differ. The timings below are rounded; [`docs/pull-request-workflow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/pull-request-workflow.md) is where they are measured, alongside the commands to poll for a review and answer it.

- **It starts on its own** when a pull request is `opened`, `reopened`, or marked ready for review. The 👀 appears within seconds; the review itself lands about 9 minutes later on average, and up to 45 on a very large diff. A draft is not in the queue at all until you mark it ready.
- **Pushing more commits does not re-trigger it.** To ask for a fresh review of the current commit, comment `/review` on a line of its own — a strict read of only what the bot is certain of, or `/review all` for one as wide as its first review. Owners, members, and collaborators can trigger it. A re-read takes about as long as the first.
- **Reading the result.** 👀 means the review started, a posted review means it finished. Findings are inline comments badged 🔴 High, 🟠 Medium, or 🟡 Low; findings about code outside the diff are listed in the summary body. "No findings" is a real result, not silence — about two in five reviews come back clean. A 👀 with nothing following it 30 minutes later is a bug in the bot; `/review all` is the retry that matches the width of the review you lost.
- **It decides when a human is asked.** The bot posts an `AI Review` check run next to its review — `success` for "No findings", `neutral` when it found something — and a reviewer is auto-assigned only once that check is green. Opening a pull request no longer assigns anyone, so addressing the findings and running `/review` for a clean pass is what puts your change in front of a person. Pull requests opened by a bot are assigned as soon as the check completes either way, and an owner, member, or collaborator can comment `/request-review` to assign one immediately.
- **Opting out.** The `agent:ignore` label excludes a pull request from review and outranks both commands.
- **Resolving the threads is part of the work.** `main` will not merge while any conversation is open, whether the bot or a human started it, and an open thread keeps the pull request counted as its author's outstanding work. Whoever is confident a thread is addressed — author or reviewer — replies saying what changed, then resolves it. Threads that are still a judgment call stay open for the person who raised them.

AI agents working in this repository have a further obligation: after opening a pull request they should offer to wait for this review and then walk its findings with you before changing any code, and they resolve the threads they have addressed. See [`AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/AGENTS.md).

## Where to file issues

Bug reports, feature requests, and questions: [github.com/gke-labs/kube-agents/issues](https://github.com/gke-labs/kube-agents/issues).

The [`github-repo-watcher` poller](/kube-agents/concepts/autonomous-watchdogs/#pollers-file-cards-watchdogs-deliver-reports) checks open issues every 10 minutes, and the agent may (within tight guardrails) triage or respond to one automatically. Human review still gates any resolution.
