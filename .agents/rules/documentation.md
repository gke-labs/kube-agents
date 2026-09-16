---
# Claude Code loads this rule only beside files matching `paths`; other tools ignore this block.
paths:
  - "**/*.md"
  - "**/*.mdx"
---

# Documentation rules

[`AGENTS.md`](../../AGENTS.md) states each documentation rule in a line and owns the canonical-home
table. This file holds the form the audience rule takes — who the site is for, the identifiers it
never carries, and what enforces both — and the long form of the two prose rules.

## Who the site is for

`docs/site/src/content/docs/` is for people installing and operating kube-agents on their own
clusters. Every page there answers one question about a hunk: could this reader act on it against
their own install? A page that tells a maintainer which workflow runs when, which repository
environment carries which variable, or how a Prow project is onboarded fails that test however
well it is written, and it goes to the home the `AGENTS.md` table names for it — maintainer
environments and their secrets and variables, release runbooks, or the evaluation pool.

`contributing.md` is the one exception. The CLA and community guidelines have to be reachable from
the public site, so the page stays, points at the repository's `CONTRIBUTING.md` for everything
else, and is the one site row `docs-check-map` lets name contributors.

Three shapes give a maintainer page away when the subject does not:

- It describes which workflow runs when, or which repository secret or variable feeds it.
- It addresses the reader as someone changing the source — "when changing this code", "before you
  edit", a test name, a Go symbol.
- Its map row in `docs/README.md` wants to say the audience is maintainers, CI engineers, or
  contributors. The site table's audience cell may not name those readers; a page for them goes in
  the section of the map that matches its new home.

## Identifiers the site never carries

The site is public and is mirrored, quoted, and indexed. None of the following appears on it:

- GitHub App IDs or installation IDs.
- Workflow secret or variable names (`secrets.X`, `vars.X`).
- The maintainers' GCP project IDs, the service-account emails inside them, and their Workload
  Identity pool or provider names.
- Internal repository paths.
- The maintainers' own projects, clusters, and deployment environments as things the reader would
  configure or reach — the Prow project and build cluster, the evaluation pool, the `autopush`
  and `staging` installs. The `staging` release stage a consumer pins, and either word in its
  ordinary sense, are not environment names.

Placeholders and the defaults an install creates in the user's own project are fine:
`<PROJECT_ID>`, `${PROJECT_ID}`, `your-project`, `kubeagents-platform-gsa@<project>.iam.gserviceaccount.com`,
`kubeagents-system`, the cluster name the quickstart passes to `install.sh`. The line is whether the
value is the reader's or ours.

## PR and issue numbers are history, not reasons

`AGENTS.md`'s "Do not document pull-request status" covers the sentence that says a PR adds or
proposes something. It also covers a PR or issue number offered as the reason a behaviour exists:
"retries stop at three (#NNN)" tells the reader nothing they can act on and rots when the thread
is closed or the behaviour changes again. State the reason in prose. The number belongs in
a design doc or a maintainer runbook when the history itself is the subject. This binds
documentation — the site, `docs/`, the READMEs. `AGENTS.md`, the skills, the rules files,
`.claude/commands/`, and `docs/pull-request-workflow.md` (the commands behind `AGENTS.md`'s
pull-request rules, an instruction document by content) are instructions; a number there points
at the thread where a rule was argued, and the reason still has to stand in the prose beside it.
The rule binds the line you write: a citation already in a document stands until the line it is
on is touched, and a review pass raises it on that line, not on the backlog around it.

## Enforcement

- `make docs-check-audience` (`scripts/check_docs_audience.py`) fails when a file under
  `docs/site/src/content/docs/` matches a shape in `scripts/docs_audience_denylist.txt` — one
  regular expression per line, so the next identifier is a one-line addition — or names a
  service-account email whose project is not a placeholder, or names a project ID that
  `hack/ci-env.sh` exports. The project IDs are read from that script at run time rather than
  copied, so the denylist never repeats them. The check also fails when it finds no site page or
  derives no project ID, so a moved site root or a reworded export cannot turn it green. Neither
  the denylist nor this file spells out the values: refer to them by shape or by the file that
  holds them.
- `make docs-check-map` (`scripts/check_docs_map.py`) rejects a site row whose audience cell names
  maintainers, CI engineers, or contributors, and fails when it finds no published-site table at
  all, so a reworded heading cannot turn the check green with nothing read.
- The `review-docs-drift` skill asks the reader question of every hunk under `docs/site/`, and the
  `review-adversarial` skill treats a site hunk that arrives with a CI change as a prompt to ask
  whether the page is a runbook. Neither check reads prose for you: a page can describe the
  maintainers' Prow setup without naming one identifier, and only the reader question catches it.

## Write it straight

Lead with the fact — no preamble, no restating the question, no "it's worth noting". Cut hype and
self-assessment (`comprehensive`, `robust`, `seamless`, `simply`, `powerful`). Skip the "not X,
but Y" antithesis and rule-of-three padding: one precise example beats three synonyms. Prefer
prose to a `**Bold term:** explanation` list. Claim first, caveat after; a hedge in front of a
fact hides it. `SKILL.md` files are the exception to the prose preference —
`.agents/skills/skill-review/SKILL.md` asks for terse imperative bullets there.

Match a document's length to what the task needs. Agent-written documents run long by default, so
cover the substance and stop: no filler sections, no summary that repeats the section above it, no
boilerplate scaffolding a reader will skip. Anthropic's
[Opus 5 prompting guide](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5)
is the upstream source for both rules.
