# AGENTS.md

## Project Overview

This repository contains the Kubernetes Agentic Harness (`kube-agents`). It is a collection of agent configurations, personas, and skills designed to manage Kubernetes/GKE operations. It utilizes a Platform Agent to transition from reactive manual management to proactive, intent-driven operations.

## Repository Layout

- `agents/`: Source of truth for agent blueprints (personas and skills).
  - `chat/`: The Chat Agent front door — the `default` Hermes profile that receives chat ingress and delegates to specialists.
  - `platform/`: Configuration for the Platform Agent, scaffolded at pod startup into the `platform` profile.
  - `cluster/`: The Cluster Agent profile _template_ (persona, scoped config, and runtime-debugging skills). The Platform Agent scaffolds this into per-cluster Hermes profiles at runtime; it is not deployed directly.
- `.agents/skills/`: Repository-level skills, not shipped in the agent images — review skills (security audits, docs-drift, skill quality) run against pull requests and clusters, plus the `install-kube-agents`/`uninstall-kube-agents`/`upgrade-kube-agents` lifecycle skills that drive the repository's installer scripts.
- `charts/`: Canonical Helm charts (`kube-agents`) for deploying the Kube-Agents operator and profiles.
- `terraform/`: Companion reusable Terraform modules (`gke-cluster`, `kube-agents-iam`, `chat-pubsub`, `github-minter`) for infrastructure provisioning, plus `examples/full-install/`, the single-apply composition that installs the Helm chart on top.
- `deploy/`: Deployment infrastructure code (Dockerfile, Kustomize bases, shared runtime assets).
- `docs/`: Documentation.
  - `site/`: The published documentation site (Astro + Starlight) — the canonical home for
    user-facing docs.
  - `architecture/`: The end-state architecture specification (`01`–`08`). Describes the target, not
    what ships today.
  - `designs/`: Per-feature design documents.
- `k8s-operator/`: Go/Kubebuilder operator reconciling `PlatformAgent` Custom Resources, plus provisioning scripts.
- `examples/`: Example integrations (LiteLLM provider configs, vLLM serving, inference replay).
- `bench/`: Evaluation harness that runs [kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench) against the Platform Agent as a pip-installed library.
- `INSTALL.md`: Installation guide.
- `README.md`: Project overview.

## Agent Setup & Integration

This repository is primarily a configuration and documentation repository for AI agents. The main exception is the Go-based Kubernetes operator in `k8s-operator/`, which requires compilation (see Local Validation Checks below).

To use these agents:

1. Follow the instructions in [INSTALL.md](INSTALL.md) to set up and register the Platform Agent in your agent harness.
2. Refer to the documentation site content in [docs/site/src/content/docs/](docs/site/src/content/docs/) for architecture, concepts, and operational guides.

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
| What each provisioning script does                       | `k8s-operator/scripts/README.md`             |
| The install procedure (self-contained, agent-executable) | `INSTALL.md`                                 |
| What the agent is and is not permitted to do             | the site's `reference/security-and-iam.md`   |
| How to develop a specific directory                      | that directory's `README.md` (keep it short) |

Rules:

- **Do not hand-write a table that mirrors a machine-readable file.** The cron schedule, the skill
  catalogue, and the provisioning steps are generated into `<!-- BEGIN GENERATED -->` regions by
  `scripts/generate_docs.py`, which also writes `docs/family-roster.txt` whole. Edit the source,
  then run `make docs-generate`.
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

Run `make docs-check` before pushing. It verifies generated regions are current, relative links
resolve, identifiers match their source, and every Markdown document has an entry in the
documentation map (`docs/README.md`) — the same four checks CI runs.

## Pull Request Hygiene

- Keep changes scoped to the request.
- Do not commit unrelated formatting changes.
- Maintain the structure and intent of the agent configuration files.
- Use Conventional Commits for commit messages.
- Push PR branches to a fork, not to the upstream repository.
- **Pin GitHub Actions to a full commit SHA.** Every third-party `uses:` in
  `.github/workflows/` must reference a 40-character commit SHA with the human-readable
  version in a trailing comment (`uses: actions/checkout@3d3c42e… # v7.0.1`). Mutable tags
  (`@v4`, `@main`) are not permitted — a retagged release would silently change what CI runs.
  Local reusable workflows (`uses: ./.github/workflows/…`) are exempt. Dependabot updates the
  SHA and the comment together.
- Use `.github/PULL_REQUEST_TEMPLATE.md` for PR body structure and level of
  detail. Do not use `--fill` with `gh pr create` as it bypasses the template.
- **Docs-drift review before opening a PR:** run the `review-docs-drift` skill
  (`.agents/skills/review-docs-drift/SKILL.md`) against your branch diff and address its
  Blocking findings. This is a required pre-PR step for AI agents working in this repository;
  `make docs-check` enforces only the mechanical subset (generated regions, links, terminology,
  map coverage), while the skill also verifies that doc prose still matches the source.
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
  - **If the change cannot reach a running installation** — docs-only, a CI workflow, a code path
    that needs infrastructure you do not have — write "Not live-tested" and say why. An empty
    section is not an answer.
- **Expect an automated review after opening a PR.** Opening the pull request starts
  `kube-agents-bot`; see
  [Automated Review After Opening a Pull Request](#automated-review-after-opening-a-pull-request)
  for what it does and what you are expected to do with its findings.
- **Local Validation Checks:** Before committing, try to run checks locally to avoid CI failures:
  - **Formatting:** Run `prettier --write <files>` on changed Markdown, JSON, or YAML files. You can check all files using `make prettier-check` (note: this checks files outside your PR scope; CI only checks the ones your branch changed). Install the version CI pins (see the Install Prettier step in `.github/workflows/prettier.yml`), e.g. `npm install -g prettier@<that version>` — the manifests gate in `k8s-operator-test.yml` asserts byte-equality against that version's output, so a skew fails CI on files you did not touch. Prefer the installed binary over `npx prettier`, which re-resolves the package against the npm registry on every run and fails outright behind an authenticated mirror — that failure is why this step has previously been skipped rather than run.
  - **Docker Build:** Validate the agent runner Dockerfile by building it locally (e.g., `docker build --platform linux/amd64 -f deploy/docker/Dockerfile --target platform .`). Keep `--platform linux/amd64`: the base images are multi-arch and deployment targets are amd64 GKE nodes, so a bare build on an arm64 machine produces an image that cannot run on the cluster (#560).
  - **Operator Code:** If you modify `k8s-operator/`, run `make` or `go build` inside that directory to ensure compilation succeeds.

## Automated Review After Opening a Pull Request

Every pull request here is reviewed automatically by `kube-agents-bot`, a GitHub App that runs a
coding agent over the branch diff. It only comments — it never pushes commits and never merges.
Opening a pull request is therefore not the end of the task. The bot introduces itself in a comment
on every pull request it picks up, and that comment states its current contract; if it disagrees
with what follows, believe the comment and fix this section.

**When it runs.** On `opened`, `reopened`, and draft-marked-ready. **Pushing more commits does not
start another review** — an active branch would otherwise pay for a re-read on every push. To get a
fresh review of the current commit, comment `/review` on a line of its own (repository owners,
members, and collaborators only). The `agent:ignore` label opts a pull request out entirely and
outranks `/review`.

**How to read it.** A 👀 reaction means the review started; a posted review means it finished — the
review usually lands a couple of minutes after the pull request opens. A review that runs always
reports back, so a one-line "no findings" is a result, not silence. Findings arrive as inline
comments badged 🔴 High, 🟠 Medium, or 🟡 Low; findings the bot could not anchor to a changed line
appear in the summary body under **Findings outside this diff**. A 👀 with nothing following it is a
bug in the bot, not a verdict.

**What agents must do.** After creating a pull request, tell the user the bot review is on its way
and **offer to wait for it** instead of reporting the work as finished. If the user accepts, poll
until the review appears:

```bash
# Both commands name gke-labs/kube-agents explicitly: PR branches live on forks,
# but the review lives on the upstream pull request.

# Has the bot reviewed yet? Takes the LAST bot review and prints its timestamp
# first: after a /review the earlier review is still there, and reading it back
# looks exactly like the new one having landed. No output = no review yet.
# (gh reports the login without the [bot] suffix; the REST API below adds it.)
gh pr view <number> --repo gke-labs/kube-agents --json reviews \
  --jq '[.reviews[] | select(.author.login == "kube-agents-bot")] | last | select(.)
        | "\(.submittedAt)\n\(.body)"'

# The inline findings, with the comment ids needed to reply. --paginate matters:
# the default page holds 30 comments and a truncated list still looks complete.
# .line is null once a finding's line falls out of the diff, hence the fallback.
gh api repos/gke-labs/kube-agents/pulls/<number>/comments --paginate \
  --jq '.[] | select(.user.login == "kube-agents-bot[bot]")
        | "\(.path):\(.line // .original_line) [id \(.id)]\n\(.body)\n"'
```

Then work the findings **with** the user rather than acting on them unilaterally: summarise each
one, say whether you think it should be fixed, pushed back on, or deferred, and let the user decide
before you change code. The bot is a reviewer, not an authority — but a finding you disagree with
gets answered in its thread, not silently dropped:

```bash
gh api repos/gke-labs/kube-agents/pulls/<number>/comments/<comment-id>/replies \
  -f body='<the reasoning>'
```

After pushing fixes, remember that the push alone does not re-trigger anything: ask the user whether
to comment `/review` for another pass.
