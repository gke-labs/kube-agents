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
- **Live validation.** Every PR describes how the change was exercised against a real, running installation. See [Live validation](#live-validation) below.

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

## Live validation

The checks above tell you the code compiles, the docs resolve, and the unit tests agree with themselves. None of them tell you whether the operator reconciled your change or the agent pod picked it up — this project's failure mode is a green build that configures nothing. So every pull request fills in the template's **Testing → Live validation** section with how the change was exercised against a real, running kube-agents installation. If you don't have one, [INSTALL.md](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md) stands one up.

[`AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/AGENTS.md) states this requirement in full and is canonical; what follows summarises it, so trust it over this page if the two ever differ.

What that section should say:

- **Which install, and what you did.** Cluster, image tag, operator version, and the steps you ran.
- **What you observed at each layer the change touches** — the CR `.status`, the Deployment env, the file or process inside the pod. A change that claims to reach the pod is verified by reading it in the pod.
- **Evidence the mechanism worked, not a coincidence.** If your new value happens to equal the previous default, observing it proves nothing. Set something distinctly different, confirm it lands, then revert and confirm it goes back.
- **What you could not cover, and why.** An honest gap is more useful than an implied one.
- **Cleanup.** Remove test artifacts, restore prior state, and note anything left behind.

Some changes can't reach a running installation — docs-only edits, CI workflow changes, code paths that need infrastructure you don't have. Write "Not live-tested" and say why. An empty section is not an answer.

## Code review

All submissions, including from project members, require review through GitHub pull requests. See [GitHub Help — About pull requests](https://help.github.com/articles/about-pull-requests/).

### Automated review

Every pull request is also reviewed by `kube-agents-bot`, a GitHub App that runs a coding agent over the branch diff. It only comments — it never pushes commits and never merges, and it does not replace the human review above. It introduces itself in a comment on every pull request it picks up; that comment states its current contract, so trust it over this page if the two ever differ.

- **It starts on its own** when a pull request is `opened`, `reopened`, or marked ready for review. It typically posts a couple of minutes later.
- **Pushing more commits does not re-trigger it.** To ask for a fresh review of the current commit, comment `/review` on a line of its own. Owners, members, and collaborators can trigger it.
- **Reading the result.** 👀 means the review started, a posted review means it finished. Findings are inline comments badged 🔴 High, 🟠 Medium, or 🟡 Low; findings about code outside the diff are listed in the summary body. "No findings" is the common outcome and is a real result — a 👀 with nothing following it is a bug in the bot.
- **Opting out.** The `agent:ignore` label excludes a pull request from review and outranks `/review`.

AI agents working in this repository have a further obligation: after opening a pull request they should offer to wait for this review and then walk its findings with you before changing any code. See [`AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/AGENTS.md).

## Where to file issues

Bug reports, feature requests, and questions: [github.com/gke-labs/kube-agents/issues](https://github.com/gke-labs/kube-agents/issues).

The [`github-issue-resolver` watchdog](/kube-agents/concepts/autonomous-watchdogs/) polls open issues every 30 minutes and (within tight guardrails) may triage or respond automatically. Human review still gates any resolution.
