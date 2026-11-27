---
title: Team runbooks
description: How to lay out, version, validate, and review team runbooks in Git using Open Knowledge Format (OKF) v0.2, and what kube-agents reads from them today.
sidebar:
  order: 10
---

Store operational runbooks in Git as Markdown files with YAML frontmatter using the [Open Knowledge Format (OKF) v0.2 specification](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md). Keeping runbooks in Git gives every procedure version history, pull-request review, and a stable path that `kube-agents` can inspect and index without anyone pasting stale steps into chat.

## Where runbooks live

Put runbooks under `knowledge/runbook/` in your GitOps repository (the repository with `role: gitops` in [`spec.integration.repositories`](/kube-agents/operator/platformagent-crd/#specintegration)). `knowledge/` sits outside the deployment paths (`clusters/<cluster>/` and `fleet/`), so Argo CD, Flux, Config Sync, and apply pipelines never apply runbooks to a cluster.

```text
<gitops-repo>/
├── clusters/<cluster>/
├── fleet/
├── knowledge/
│   ├── index.md
│   ├── log.md
│   └── runbook/
│       └── drain-stuck-node.md
├── .kube-agents/
│   └── intent.yaml
└── CODEOWNERS
```

The [reference GitOps repository layout](https://github.com/gke-labs/kube-agents/tree/main/examples/gitops-repo/knowledge) ships a starter `knowledge/` tree you can fork.

### Configuring knowledge paths with `.kube-agents/intent.yaml`

`.kube-agents/intent.yaml` at the repository root holds a single `paths:` list that bounds where fleet audits scan for `declares:` frontmatter and sets the root directories Curator loads `decision` and `lesson` records from:

```yaml
paths:
  - knowledge/
```

Include `knowledge/` in `paths:` whenever you also keep `decision` or `lesson` records there, because a non-empty `paths:` list replaces Curator's default `knowledge` root rather than appending to it. Runbook indexing ignores `intent.yaml` and always scans the fixed runbook directories listed below. Without `.kube-agents/intent.yaml`, or when a listed path does not exist at the audited commit, declaring audits fall back to scanning the whole repository under their content-mode file and byte caps.

### Using a separate read-only runbook repository

If your runbooks already live in a dedicated repository in the same GitHub organization (or on another configured forge such as GitLab, Bitbucket, or Gitea), attach it with `role: context` in [`spec.integration.repositories`](/kube-agents/operator/platformagent-crd/#adding-repositories-to-a-githubcom-install). The credential broker serves read-only snapshots of `context` repositories and refuses every write verb against them.

Both the `decision-memory` skill and the Curator indexer scan these runbook directories across `gitops`, `managed`, and `context` repositories:

- `knowledge/runbook/` (canonical singular) and `knowledge/runbooks/`
- `docs/runbooks/` and `docs/playbooks/`
- `runbooks/` and `playbooks/`

Keep runbook Markdown files directly inside these directories rather than in nested subdirectories. The Curator indexer reads top-level `.md` files in each directory and skips subdirectories during the scheduled `repo_memory` sweep.

### Reserved filenames

`index.md` (navigation) and `log.md` (history) are reserved at every level of `knowledge/`. Neither file declares a `type` key in frontmatter, and `index` and `log` must never be used as `type` values or entry filenames. At the `knowledge/` root, `README.md` and `STATE_OF_BEING.md` are also permitted without `type` frontmatter and are skipped by record ingestion.

## Minimum OKF v0.2 frontmatter

Every entry file under `knowledge/` requires YAML frontmatter delimited by `---` with a non-empty string `type`. For a runbook, set `type: runbook`.

```yaml
---
type: runbook
title: Drain a node blocked by a PodDisruptionBudget
tags: [gke, upgrades, pdb]
status: stable
sources:
  - https://wiki.example.com/sre/node-drain-pdb
generated:
  by: platform-sre
  at: 2026-09-15
verified:
  by: alice
  at: 2026-09-18
stale_after: 2027-03-15
---

# Drain a node blocked by a PodDisruptionBudget

## Preconditions

Confirm the node is cordoned and identify which pods hold the eviction back.

## Steps

1. Run `kubectl get pdb --all-namespaces` and find budgets with `ALLOWED DISRUPTIONS = 0`.
2. Check whether the owning workload is degraded before changing any budget.
```

The recommended OKF v0.2 provenance and lifecycle fields are optional, and the OKF validator checks their shape whenever they appear:

| Field         | Expected shape                                                                                           | Purpose                                                   |
| ------------- | -------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `type`        | Required non-empty string (never `index` or `log`)                                                       | Entry category (`runbook` for operational procedures).    |
| `sources`     | Non-empty list of non-empty strings or mappings                                                          | Upstream docs, tickets, or wikis the procedure came from. |
| `generated`   | Mapping with non-empty `by` and ISO-8601 date (`YYYY-MM-DD`) or timezone-qualified timestamp `at`        | Who authored or exported the file and when.               |
| `verified`    | Mapping (or non-empty list of mappings) with `by` and ISO-8601 date or timezone-qualified timestamp `at` | Who reviewed or tested the runbook and when.              |
| `status`      | `draft`, `stable`, or `deprecated` for `runbook` and other non-decision/lesson OKF docs                  | Lifecycle state used by reviewers and semantic indexing.  |
| `stale_after` | ISO-8601 date (`YYYY-MM-DD`) or timezone-qualified timestamp                                             | Scheduled review-by date for freshness checks.            |

`decision` and `lesson` notes under `knowledge/decision/` and `knowledge/lesson/` use a separate lifecycle vocabulary (`proposed`, `active`, `rejected`, `superseded`, `retired`) for Decision Memory. All other OKF types, including `runbook`, use `draft`, `stable`, and `deprecated`.

## `declares:` frontmatter vs. the runbook body

Three distinct mechanisms share `knowledge/`, and keeping them separate prevents unexpected audit suppressions or blocked pushes.

The Markdown body of a runbook holds human-readable procedures, diagnostic commands, and escalation steps for engineers and semantic recall. Fleet audits never parse prose to suppress findings, and narrative instructions never grant permissions.

An entry under `.kube-agents/intent.yaml` paths may also carry a `declares:` list in frontmatter for intentional cluster postures. Each item names `check`, `namespace` (`""` for cluster-scoped objects), and `object` (`Kind/name`), plus optional `cluster` (`<project>/<location>/<name>` or `project/<id>`) and optional `expires` (`YYYY-MM-DD` UTC). The declaring fleet audits (`obtainability-audit`, `compliance-audit`, `security-patch-orchestrator`, `fleet-wide-cost-analysis`, `stockout-prevention`, `gce-compute-fleet-audit`, and `gcp-networking-fabric-audit`) read `declares:` and move matching postures under _Declared intent_ with the file path. After `expires` passes, the posture returns as a finding with a _Declared intent expired_ line naming the note and date.

Pre-push manifest assertions (`scope` and `guard.expect`) belong in `knowledge/decision/` (`DEC-YYYY-NNN`) and `knowledge/lesson/` (`LES-YYYY-NNN`), where the credential broker evaluates them deterministically on `/v1/vcs/publish`. Keep machine-checked manifest guards in decision and lesson records rather than inside narrative runbooks.

## Versioning, review, and lifecycle

Treat `knowledge/` with the same change control as cluster configuration. Every addition, edit, or retirement lands through a pull request with commit history and line-by-line review.

Add `/knowledge/` and `/.kube-agents/` to `CODEOWNERS` (matching [`examples/gitops-repo/CODEOWNERS.example`](https://github.com/gke-labs/kube-agents/blob/main/examples/gitops-repo/CODEOWNERS.example)) and require code-owner review in branch protection. Both paths need review because a `declares:` item under `knowledge/` moves an audit posture off the findings ledger, and removing `.kube-agents/intent.yaml` widens declared-intent scanning to the whole repository.

Move runbooks through `draft`, `stable`, and `deprecated` as their review state changes. New procedures start at `status: draft`, which external-doc indexing maps to `proposed` so unreviewed drafts stay out of active-only recall. Reviewed procedures move to `status: stable` (mapped to `active` in the `kube-agents-decisions` bank; omitting `status` on an external runbook also indexes as `active`). When a procedure is replaced, set `status: deprecated` in a pull request and link to its replacement. Indexing maps `deprecated` to `superseded`, which drops the runbook from active-only semantic recall (`status:active`) while keeping Git history and links intact.

Set `stale_after` to the next scheduled review date so your team can audit aging procedures on a regular cadence. Today the OKF validator checks `stale_after` syntax, while no runtime watchdog enforces `stale_after` automatically. By contrast, `declares[].expires` is enforced at runtime by the declaring fleet audits.

## Validating `knowledge/` in your GitOps CI

`kube-agents` does not install a CI workflow into your GitOps repository automatically. To catch malformed frontmatter and broken links before merge, run [`scripts/check_okf_knowledge.py`](https://github.com/gke-labs/kube-agents/blob/main/scripts/check_okf_knowledge.py) against your `knowledge/` directory in pull-request CI, keeping its sibling helper [`scripts/check_docs_links.py`](https://github.com/gke-labs/kube-agents/blob/main/scripts/check_docs_links.py) in the same directory (standard library plus `PyYAML`): `python3 scripts/check_okf_knowledge.py knowledge`.

The validator enforces these checks across every file under `knowledge/`:

- no symbolic links inside `knowledge/`;
- no `type` key on reserved files (`index.md`, `log.md`, and root `README.md` or `STATE_OF_BEING.md`);
- valid YAML mapping frontmatter with a non-empty, non-reserved `type` on every entry;
- valid shapes for `sources`, `generated`, `verified`, `stale_after`, and per-type `status`;
- valid relative Markdown links and `#fragment` anchors inside the body.

## What kube-agents does with a runbook today

### Shipped behaviour

The declaring fleet-audit streams read `declares:` frontmatter from `.md` files under `.kube-agents/intent.yaml` paths in the GitOps repository (or `--repo` on a one-off run) and any GitHub `context` repositories, citing the matching file path under _Declared intent_.

On installs running with `--memory=hindsight`, the 10-minute `github-repo-watcher` runs the `repo_memory` sweep (`DECISION_MEMORY_REPO_SCAN` defaults to `true`; see [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/)). When the Curator binary is available alongside `NATS_URL` or `HINDSIGHT_API_URL`, it snapshots `gitops`, `managed`, and `context` repositories and indexes top-level runbooks verbatim into the `kube-agents-decisions` bank. Active (`stable`) runbooks then surface in `decision-memory` semantic checks (`check --semantic`) and Curator recall.

When asked about a documented procedure or repository path in chat, the Platform Agent reads files from registered `gitops`, `managed`, or `context` repositories through the `inspect-repository` skill and cites the repository, branch or ref, and commit SHA.

### Not shipped yet

Automatic pre-action runbook lookup does not ship yet: the Platform Agent and Cluster Agents do not automatically search `knowledge/runbook/` and cite a matching runbook before every troubleshooting or remediation turn. Direct connectors that read Google Docs, Confluence, OneDrive, or Google Drive — or convert external docs and PDFs into OKF pull requests — do not ship yet either. Runtime alerts or filtering on `stale_after` are also not built yet.

### What a runbook will never do

A runbook never widens the agent's permissions. Kubernetes RBAC (read-only on workloads and cluster state), GCP IAM (`read-only` by default), the credential broker's command policy, and the GitOps pull-request boundary (no merge or approve) are enforced outside the model. Instructions inside a runbook cannot authorize `kubectl delete`, direct cluster writes, or self-merging pull requests; see [Security & IAM](/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do).

Records indexed from `role: context` repositories stay read-only and untrusted. Curator tags them `role:context` and `trust:untrusted`, strips `guard.expect`, caps `severity: block` at `warn`, and wraps recalled chunks in `<untrusted_memory_data>` blocks so the agent reads them as reference data rather than executable instructions. Because `knowledge/` sits outside reconciler roots (`clusters/` and `fleet/`), runbooks are never applied to clusters.

## Bringing in runbooks from Docs, Confluence, or Drive

Because direct connectors do not ship today, bring existing runbooks from Google Docs, Confluence, or Drive into Git through a manual export and pull-request review:

1. Export each procedure to plain Markdown (in Google Docs, use **File → Download → Markdown**; in Confluence or Drive, export or copy as Markdown).
2. Put one operational procedure per file directly under `knowledge/runbook/<slug>.md` (or under `docs/runbooks/` in a `role: context` repository), replacing terminal screenshots with plain text commands and expected output.
3. Strip pasted tokens, credentials, or one-off cluster secrets, then add OKF frontmatter with `type: runbook`, `title`, `status: draft`, `sources` pointing back to the original document URL, `generated`, and an initial `stale_after` date.
4. Open a pull request so the owning team reviews the converted procedure under `CODEOWNERS`, adds `verified`, and switches `status` to `stable` before merging.

Avoid pasting long runbooks into chat threads as a substitute for Git. Chat pastes go stale, bypass review, and cannot be cited as a stable file path in pull requests or audit ledgers.

## Where to go next

- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how `submit-suggestion`, `fleet-audit`, and repository roles work together.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the built-in fleet audit procedures shipped in the agent image.
- [Security & IAM](/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do) — the enforced RBAC, IAM, and broker boundaries.
- [PlatformAgent CRD → Adding repositories](/kube-agents/operator/platformagent-crd/#adding-repositories-to-a-githubcom-install) — how to register `gitops`, `managed`, and `context` repositories.
