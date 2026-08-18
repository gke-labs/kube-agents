# AGENTS.md

## Project Overview

This repository contains the Kubernetes Agentic Harness (`kube-agents`). It is a collection of agent configurations, personas, and skills designed to manage Kubernetes/GKE operations. It utilizes a Platform Agent to transition from reactive manual management to proactive, intent-driven operations.

## Repository Layout

- `agents/`: Source of truth for agent blueprints (personas and skills).
  - `chat/`: The Planning Agent front door — the `default` Hermes profile that receives chat ingress, plans the work, and delegates each piece to a specialist.
  - `platform/`: Configuration for the Platform Agent, scaffolded at pod startup into the `platform` profile.
  - `cluster/`: The Cluster Agent profile _template_ (persona, scoped config, and runtime-debugging skills). The Platform Agent scaffolds this into per-cluster Hermes profiles at runtime; it is not deployed directly.
- `.agents/skills/`: Repository-level skills, not shipped in the agent images — review skills (adversarial change review, security audits, docs-drift, IaC parity, skill quality) run against pull requests and clusters, plus the `install-kube-agents`/`uninstall-kube-agents`/`upgrade-kube-agents` lifecycle skills that drive the repository's installer scripts.
- `charts/`: Canonical Helm charts (`kube-agents`) for deploying the Kube-Agents operator and profiles.
- `terraform/`: Companion reusable Terraform modules (`gke-cluster`, `kube-agents-iam`, `chat-pubsub`, `github-minter`, `gke-backup-plan`) for infrastructure provisioning, plus `examples/full-install/`, the single-apply composition that installs the Helm chart on top.
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
- `images.json`: Inventory of every container image an install pulls, with its upstream reference
  and pin. Read by `make mirror-images`, the provisioning scripts, and the docs generator.
- `INSTALL.md`: Installation guide.
- `README.md`: Project overview.

## Agent Setup & Integration

This repository is primarily a configuration and documentation repository for AI agents. The main exception is the Go-based Kubernetes operator in `k8s-operator/`, which requires compilation (see Local Validation Checks below).

To use these agents:

1. Follow the instructions in [INSTALL.md](INSTALL.md) to set up and register the Platform Agent in your agent harness.
2. Refer to the documentation site content in [docs/site/src/content/docs/](docs/site/src/content/docs/) for architecture, concepts, and operational guides.

## Before Starting a Task

Many people and agents work in this repository at once, so the first step of a non-trivial task
is finding out whether someone is already doing it. Scan the open work and report what you find
to the user **before** you write code. Skip the scan only when the user has already named the
issue or pull request you are working on, or when the change is a one-liner they asked for
directly.

Branches live on forks, so name the upstream repository on every call:

```bash
# Open PRs, with the files each one touches. File overlap is the strongest duplicate
# signal and one call gets it for every open PR.
gh pr list --repo gke-labs/kube-agents --state open --limit 100 \
  --json number,title,author,headRefName,isDraft,updatedAt,files

# Open issues, and who has already claimed them.
gh issue list --repo gke-labs/kube-agents --state open --limit 100 \
  --json number,title,assignees,labels

# Already tried? A closed pull request is a decision, not an absence.
gh search prs --repo gke-labs/kube-agents --state closed --limit 20 '<keywords>'
```

Then report before you start:

- **An open pull request touches your files or solves your problem.** Give the number, author,
  and URL, and say how your task differs. Do not push to someone else's branch and do not open
  a competing pull request without the user's go-ahead. Overlap alone is a merge-conflict
  warning, not a stop sign — say which it is.
- **An open issue describes the task and is unassigned.** Give the number and title, offer to
  claim it, and say what you would comment. Assign or comment only after the user agrees:
  `gh issue edit <number> --repo gke-labs/kube-agents --add-assignee @me`. `@me` is the account
  whose token you hold — a person — so you are volunteering them, not yourself. Contributors
  working from a fork without write access cannot self-assign; offer a comment instead.
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
| What each provisioning script does                       | `k8s-operator/scripts/README.md`             |
| Which container images an install pulls, and their pins  | `images.json`                                |
| The install procedure (self-contained, agent-executable) | `INSTALL.md`                                 |
| What the agent is and is not permitted to do             | the site's `reference/security-and-iam.md`   |
| How to develop a specific directory                      | that directory's `README.md` (keep it short) |

Rules:

- **Do not hand-write a table that mirrors a machine-readable file.** The cron schedule, the skill
  catalogue, the provisioning steps, and the container-image inventory are generated into
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
resolve, identifiers match their source, and every Markdown document has an entry in the
documentation map (`docs/README.md`) — the same four checks CI runs.

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
- Use `.github/PULL_REQUEST_TEMPLATE.md` for PR body structure and level of
  detail. Do not use `--fill` with `gh pr create` as it bypasses the template.
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
  This bullet is the canonical statement of the requirement; the site's
  [contributing guide](docs/site/src/content/docs/contributing.md) and the comment in
  [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) summarise it — change
  this list first, then reconcile them to it.
  - **Run the pass in a context that did not write the change** — a subagent, or a new session,
    handed the diff range and nothing else. Not your plan, not your reasoning, not the summary you
    were about to write. Reviewing a diff in the conversation that produced it is the one
    configuration that reliably does not work: the same context that talked you into the code
    talks you into approving it, and the blind spot sits exactly where you were already wrong. It
    is why `.claude/commands/pr-review-batch.md` gives every pull request its own subagent, and a
    self-review earns it for the same reason.
  - **A finding you decide not to fix is an answer**, provided the reason is an argument about
    this change rather than a shrug. "Out of scope", "pre-existing", and "will fix later" are not
    reasons on their own; the separate issue you filed is.
  - **Fix what the pass confirms; report what it only suspects.** A finding it could not pin down
    is an open question for the section, not a licence to rewrite working code — chasing an
    uncertain finding on your own change is how a self-review makes it worse than it started.
  - **"No findings" is an answer only alongside what you looked for.** The skill's angles are the
    vocabulary for that, and a pass that names none of them is indistinguishable from no pass.
  - **Do not claim more than you did.** A self-review the diff contradicts is worse than none: it
    spends the reviewer's trust before they reach the code.
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
- **IaC parity review when a PR touches more than one install surface's territory:**
  the provisioning scripts (`k8s-operator/scripts/`, `k8s-operator/config/`), the
  Terraform modules, and the Helm chart each express the same install, and nothing in
  the languages keeps them together. `make iac-parity-check`
  (`scripts/check_iac_parity.py`) enforces the scalar subset — image tags, IAM role
  bundles, identifiers, KMS and backup defaults — and CI runs it. Run the
  `review-iac-parity` skill (`.agents/skills/review-iac-parity/SKILL.md`) for the
  structural drift no scalar comparison catches: a resource only one surface creates,
  a knob only one surface can express. The scripts and `k8s-operator/config/` are the
  source of truth; deliberate divergences are listed in both the skill and the
  script's docstring, and adding one means editing both.
- **Expect an automated review after opening a PR.** Opening the pull request starts
  `kube-agents-bot`; see
  [Automated Review After Opening a Pull Request](#automated-review-after-opening-a-pull-request)
  for what it does and what you are expected to do with its findings.
- **Leave no conversation unresolved.** `main` will not merge while a review thread is open, and
  the open thread also keeps the pull request counted as its author's outstanding work.
  Reply, then resolve every thread you are confident is addressed — commands and the bar for
  "confident" are in
  [Automated Review After Opening a Pull Request](#automated-review-after-opening-a-pull-request).
- **Local Validation Checks:** Before committing, try to run checks locally to avoid CI failures:
  - **Formatting:** Run `prettier --write <files>` on changed Markdown, JSON, or YAML files. You can check all files using `make prettier-check` (note: this checks files outside your PR scope; CI only checks the ones your branch changed). Install the version CI pins (see the Install Prettier step in `.github/workflows/prettier.yml`), e.g. `npm install -g prettier@<that version>` — the manifests gate in `k8s-operator-test.yml` asserts byte-equality against that version's output, so a skew fails CI on files you did not touch. Prefer the installed binary over `npx prettier`, which re-resolves the package against the npm registry on every run and fails outright behind an authenticated mirror — that failure is why this step has previously been skipped rather than run.
  - **Docker Build:** Validate the agent runner Dockerfile by building it locally (e.g., `docker build --platform linux/amd64 -f deploy/docker/Dockerfile --target platform .`). Keep `--platform linux/amd64`: the base images are multi-arch and deployment targets are amd64 GKE nodes, so a bare build on an arm64 machine produces an image that cannot run on the cluster (#560).
  - **Image Layer Budget:** If you add a `RUN` or `COPY` to `deploy/docker/Dockerfile`, build the `credential-proxy` target with `-t credential-proxy:latest` and run `python3 scripts/check_image_layers.py`. Docker's overlay2 driver stops mounting at 128 layers and that chain is the longest in the file; because buildx has no such limit, an over-budget image passes every PR build and fails only in Cloud Build, on main, after merge (#658). CI runs the same check in `docker-build.yml`.
  - **Operator Code:** If you modify `k8s-operator/`, run `make` or `go build` inside that directory to ensure compilation succeeds.

## Automated Review After Opening a Pull Request

Every pull request here is reviewed automatically by `kube-agents-bot`, a GitHub App that runs a
coding agent over the branch diff. It only comments — it never pushes commits and never merges.
Opening a pull request is therefore not the end of the task. The bot introduces itself in a comment
on every pull request it picks up, and that comment states its current contract; if it disagrees
with what follows, believe the comment and fix this section.

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

**How to read it.** A 👀 reaction means the review started; a posted review means it finished.
Across #630–#699 the 👀 landed within seconds of the trigger, and the review a median of **9
minutes** after that — 15 minutes at the 90th percentile, 45 in the slowest of the 54 reviews in
that range (#634, an XXL diff). A `/review` re-read is no quicker: median 11 minutes, and none of
the 42 measured took longer than 22. A review that runs always reports back, so a one-line "no
findings" is a result, not silence. Findings arrive as inline comments badged 🔴 High, 🟠 Medium, or
🟡 Low; findings the bot could not anchor to a changed line appear in the summary body under
**Findings outside this diff**. A 👀 with nothing following it is a bug in the bot, not a verdict —
it happened to 3 of the 57 pull requests picked up in that range (#647, #649, #679), which is rare
enough to be worth waiting through and common enough that you must not wait forever.

**What agents must do.** After creating a pull request, tell the user the bot review is on its way
and **offer to wait for it** instead of reporting the work as finished. If the user accepts, poll on
a schedule rather than continuously — nothing is worth checking in the first 5 minutes, then once a
minute. Expect the review by 15 minutes; at 30 with nothing posted, stop waiting and tell the user
the bot dropped this one. Nothing retries on its own, so ask whether to spend a trigger — and say
which: the review that went missing was a first-review-width one, so `/review all` is what replaces
it, and `/review` narrows the retry to what the bot is certain of. Two things make a wait read
wrong:

- **The 👀 does not come back.** A reaction is one per user, so the eyes from the first review are
  still sitting there when you comment `/review` or mark a draft ready. Only the review list moves:
  note how many bot reviews exist _before_ you re-trigger, and wait for that count to change rather
  than for a reaction that already fired.
- **A draft is not waiting on anything.** The trigger is ready-for-review, not opened. Every
  multi-hour gap in the range above was a draft sitting unreviewed by design — #652 for 12 hours,
  #659 for 18 — and measured from the ready event each was picked up in seconds. Do not start the
  clock, or report the bot broken, while the pull request is still a draft.

Poll with:

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
to comment `/review` for another pass — `/review` to confirm the fixes against a strict read,
`/review all` when the branch changed enough that it deserves a first-review-width look again. Then
wait for it the same way, counting reviews rather than watching for a second 👀.

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
often a stale checkout rather than a wrong finding. Anything short of that stays open. A judgment call, a reviewer asking for
something you chose not to do, a rebuttal nobody has answered yet — reply and leave it to them.
Resolving says the conversation is finished; it is not a way to end a disagreement.

Reply first, always. A resolved thread collapses, so the reviewer who opened it may never expand it
again, and the reply is the only record of what happened. Name what changed and the commit that
changed it. Then resolve:

```bash
# Every unresolved thread, with both ids you need: resolveReviewThread takes the
# thread's node id, while the reply endpoint above takes the first comment's
# databaseId. REST returns only the latter, which is why this one is GraphQL.
gh api graphql -f query='
query($pr: Int!) {
  repository(owner: "gke-labs", name: "kube-agents") {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id isResolved isOutdated viewerCanResolve path line
          comments(first: 20) { nodes { databaseId author { login } body } }
        }
      }
    }
  }
}' -F pr=<number> --jq '.data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isResolved | not)
  | "\(.path):\(.line // "outdated") thread \(.id) canResolve=\(.viewerCanResolve)
  reply to \(.comments.nodes[0].databaseId) — \(.comments.nodes[0].author.login): \(.comments.nodes[0].body | split("\n")[0])
  replies so far: \(.comments.nodes | length - 1)"'

# Per thread, once the reply naming the fix is posted:
gh api graphql -f query='
mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) { thread { isResolved } }
}' -f thread='<PRRT_...>'
```

Four ways that goes wrong quietly:

- `first: 100` is a cap, not a promise. A long-lived pull request can carry more threads than that;
  page for the rest, or say you only looked at the first hundred rather than reporting the branch
  clear.
- `line` is `null` once a thread's line falls out of the diff. Outdated is not addressed — the code
  moved, which says nothing about whether the finding still holds. Read the thread.
- `viewerCanResolve` is the authoritative answer to whether your token can resolve at all; it
  differs between a maintainer and a contributor pushing from a fork. Check it before you reply,
  because a mutation that fails after the reply is posted leaves a half-answered thread that looks
  handled.
- `unresolveReviewThread`, same `threadId`, is the undo. Use it the moment the user disagrees with
  something you resolved.

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
