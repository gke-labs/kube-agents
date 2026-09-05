# Documentation map

This file is the map of the Markdown documentation in the `kube-agents`
repository: what lives where, what each document does, and which files contain
machine-generated regions. It serves human contributors and AI agents alike —
in particular, the PR docs-drift review consults it to find which documents a
code change should have updated.

The documentation **rules** — the canonical-home table ("every fact has one
home"), the generated-regions rule, link-don't-summarise, verify identifiers
against source — are owned by [`AGENTS.md`](../AGENTS.md) at the repository
root. This file is the **map**, not the rulebook; read `AGENTS.md` before
editing any doc.

## 1. Directory overview

Dot-directories at the repository root (`.agents/`, `.github/`, `.claude/`)
hold tooling — review skills, agent rules, PR templates, agent config — not
documentation; they are out of the map's scope and `docs-check-map` exempts
them. `.agents/rules/` is the one the canonical-home table in `AGENTS.md`
points at, so a rule's home is found through that table rather than through
this map.

This file states **no document counts**, anywhere — not a repository total, not
a per-directory total, not a per-family total. A count is a number every
concurrent pull request has to edit on the same line, so a tree that grows by
two documents a week turns one shared line into a permanent merge conflict
between every branch that touches it. `docs-check-map` derives the totals it
needs from `git ls-files` at check time and prints them; nothing is restated
here for it to disagree with.

```text
kube-agents/
├── README.md, INSTALL.md, AGENTS.md, CLAUDE.md    project front door, install
│                                                  guide, contributor/agent rules
├── agents/                                        agent blueprints (runtime docs)
│   ├── chat/                                      Planning Agent front door: persona
│   │                                              docs, onboarding templates,
│   │                                              plugin design READMEs
│   ├── cluster/                                   Cluster Agent profile TEMPLATE:
│   │                                              persona docs + runtime-debugging
│   │                                              SKILL.md bundles
│   └── platform/                                  Platform Agent profile
│       ├── AGENTS.md, SOUL.md, CAPABILITIES.md    persona and workspace docs
│       ├── docs/                                  runtime references (glossary,
│       │                                          console links) + design docs
│       ├── governance/                            cron-run SOP playbooks + the
│       │                                          first-run inventory-scan and
│       │                                          report-prioritization SOPs
│       └── skills/                                SKILL.md bundles + the
│                                                  gke-compute-classes references
├── bench/                                         devops-bench evaluation harness README
│                                                  + the task/harness authoring how-to
├── charts/                                        canonical Helm charts (kube-agents)
├── docs/                                          human documentation
│   ├── README.md                                  this map
│   ├── family-roster.txt                          GENERATED snapshot of every
│   │                                              collapsed family's members
│   ├── architecture/                              END-STATE spec set 01–09 + README
│   ├── designs/                                   per-feature design documents
│   ├── contributing.md, security-requirements.md,
│   │   credential-isolation-design.md,
│   │   pull-request-workflow.md                   standalone docs
│   └── site/                                      Astro + Starlight site: README +
│                                                  the published pages
├── examples/                                      gitops-repo template + inference/
│                                                  integration READMEs
├── k8s-operator/                                  operator, event watcher, Minty READMEs
├── scripts/                                       installer/, dev/, release/,
│                                                  feedback_form/ and testdata/ READMEs
├── terraform/                                     companion Terraform modules +
│                                                  the full-install composition
└── tests/e2e/                                     Google Chat E2E suite README
```

The published documentation site is built from `docs/site/src/content/docs/`
and served from GitHub Pages at <https://gke-labs.github.io/kube-agents/>
(Astro `base: '/kube-agents'`).

## 2. Canonical homes, generated regions, and identifier sources

Which file owns which category of content is defined once, in the
canonical-home table in [`AGENTS.md`](../AGENTS.md) — do not duplicate a fact
outside its home; link to it.

Four artifacts are **generated, not hand-written** — three regions inside
hand-written documents, plus one whole file. `scripts/generate_docs.py` (run
via `make docs-generate`) rewrites everything between the markers; everything
outside them is hand-written. Never edit inside the markers — edit the source
and regenerate.

<!-- prettier-ignore -->
| Generated file or region | Block marker | Source of truth |
| --- | --- | --- |
| `docs/site/src/content/docs/reference/cron-jobs.md` | `<!-- BEGIN GENERATED: cron-jobs -->` | `agents/chat/defaults/cron/jobs.json` and `agents/platform/cron/jobs.json` |
| `docs/site/src/content/docs/skills/index.mdx` | `{/* BEGIN GENERATED: skill-catalog */}` (MDX comment syntax) | `name`/`description` frontmatter of every `agents/platform/skills/*/SKILL.md` and `agents/cluster/skills/*/SKILL.md` |
| `docs/site/src/content/docs/deploy/docker-images.md` | `<!-- BEGIN GENERATED: container-images -->` | `images.json` |
| `docs/family-roster.txt` | whole file (`family-roster`) | The collapsed-family globs in this map's section 4, resolved against `git ls-files` |

CI enforcement: `make docs-check` runs the same checks as
`.github/workflows/docs-check.yml` —

- `docs-check-generated` — `scripts/generate_docs.py --check`; fails if a
  generated region or file no longer matches its source. This is what catches a
  document deleted from inside a collapsed family row's glob (see section 5).
- `docs-check-links` — `scripts/check_docs_links.py`; relative links must
  resolve to **git-tracked** targets.
- `docs-check-terminology` — `hack/check-docs-terminology.sh`; identifiers in
  prose must match their source (service-account names, versions, the
  fleet-audit finding-id pattern and rendering caps, …), and a quoted cron
  prompt must be a verbatim substring of the `jobs.json` it quotes.
- `docs-check-map` — `scripts/check_docs_map.py`; every tracked `.md`/`.mdx`
  file must be matched by an inventory entry in this map (globs count), every
  path in the inventory's path column must exist, and every table row in this
  file must keep its single-space padding. Root-level dot-directories
  (`.agents/`, `.github/`, `.claude/`, …) are tooling, not docs: the map does
  not inventory them and the check does not require them — the map and the
  check share one scope. A dot-directory nested inside a documented area
  (`examples/gitops-repo/.github/`) is example content and stays in scope.
- `docs-check-context-budget` — `scripts/check_context_budget.py`; `AGENTS.md`
  plus `CLAUDE.md` are loaded into every agent session before the first prompt,
  and their combined size must stay inside the `BUDGET` that file sets.

### Identifier sources

Docs state identifiers — names, defaults, versions, paths — as fact, and each
identifier has exactly one source file. Verify a doc's claim against the
source, never against another doc. The `review-docs-drift` skill classifies a
PR that touches one of these files as a change to documented identifiers and
uses this table to find what to re-verify; when a new category of documented
identifier appears, add its source here.

<!-- prettier-ignore -->
| Identifier | Source of truth |
| --- | --- |
| Service-account names and namespace | `scripts/installer/common.sh` |
| Defaults an install gets for saying nothing (region, cluster, permission set, registry prefix) | `install.defaults.env` |
| Go toolchain version | `k8s-operator/go.mod` (and `a2a/go.mod`, kept in step) |
| A2A wire constants: protocol version, stream names, size thresholds, token grammar | `a2a/lib/envelope.go` and `a2a/lib/topics.go` |
| Minimum supported tool versions (`gcloud`) | `scripts/installer/min_versions.sh` |
| Toolsets, plugins, and MCP servers of an agent profile | that profile's `config.yaml` (`agents/platform/`, `agents/chat/`, `agents/cluster/`) |
| Cron job rosters and schedules | `agents/chat/defaults/cron/jobs.json` and `agents/platform/cron/jobs.json` |
| Persona rules and `§N` section numbering | the profile's `SOUL.md` |
| RBAC bindings and KSA defaults laid down per agent | `k8s-operator/internal/controller/platformagent_manifests.go` |
| `app.kubernetes.io/*` label values on installed objects | `k8s-operator/internal/controller/manifest_helpers.go`, each `kustomization.yaml`, and `a2a/gateway/spawn.go` (gateway-spawned session pods) |
| The mode switch's key, values, and skew reason (`KUBEAGENTS_MODE`, `today`/`next`, `ModeNotRecognized`) | `k8s-operator/internal/controller/mode.go` and `platformagent_manifests.go` (writer), `agents/platform/scripts/runtime_mode.py` (reader) |
| Controller permissions | `k8s-operator/config/rbac/` |
| The operator RBAC self-check: the `RBACIncomplete` reason, its re-check interval and condition message; the floating tags `make deploy` refuses and `ALLOW_MUTABLE_IMG` | `k8s-operator/internal/controller/rbac_selfcheck.go`; `k8s-operator/Makefile` |
| `make` targets | the root `Makefile` and `k8s-operator/Makefile` |
| The GitHub environment variables an install is configured from, which install.env key each becomes, and which are required to reconcile a long-lived environment | `MAPPING`, `REQUIRED_ALWAYS` and `REQUIRED_STRICT` in `scripts/release/render_install_env.sh` |
| Paths baked into the agent image (`/opt/defaults/...`) | `deploy/docker/Dockerfile` |
| Image-patch module names and the behaviour they add | the module's own docstring under `deploy/docker/patches/`, plus the `COPY`/`RUN` list in `deploy/docker/Dockerfile` |
| Bundled Hermes platform plugins the image installs (no patch) | the plugin's own `adapter.py` docstring under `deploy/docker/plugins/`, plus the `COPY`/`RUN` list in `deploy/docker/Dockerfile` |
| Slack bot token scopes an install must grant | upstream `_build_full_manifest` in `hermes_cli/slack_cli.py` as patched by `deploy/docker/patches/apply_slack_reactions_scope.py`; the prose copies in `INSTALL.md` and `scripts/installer/print_instructions_slack.sh` must match it and each other |
| What pod start-up force-syncs from the image vs. preserves on the PV | `deploy/shared/docker-entrypoint.sh` |
| Shared agent defaults (`approvals.*`, `security.*`) | `deploy/shared/defaults/config.yaml` and `renderConfigYAML()` in `k8s-operator/internal/controller/platformagent_manifests.go` |
| Image defaults and override env vars (`PLATFORM_AGENT_IMAGE` et al.) | `k8s-operator/internal/controller/manifest_helpers.go` |
| OTLP endpoint default, discovery candidates, and `otlpEndpointSource` values | `k8s-operator/internal/controller/telemetry.go` |
| DNS/metadata-daemon defaults, the `dnsClusterIPsSource` / `metadataDaemonIPSource` values, and the `additionalEgress` prefix floors (`/12`, `/48`) | `k8s-operator/internal/controller/netpolprofile.go` and `platformagent_controller.go` |
| Agent egress-allowlist policy: metadata addresses, the `-sandbox-metadata-deny` name, the `controlPlaneCIDRs` floors (`/16`, `/32`), and the `EgressAllowlistRefused` reason | `k8s-operator/internal/controller/platformagent_egress_policy.go` and `platformagent_controller.go` |
| Scoped service-account pool: `CREDENTIAL_PROXY_SCOPED_SA_POOL{,_FILE}`, the pool file path, the scope-key spelling, and the `ka-<name>-<hash8>` account ids | `agents/platform/scripts/scoped_sa_pool.py`, `k8s-operator/internal/controller/platformagent_manifests.go`, `terraform/modules/kube-agents-iam/scoped_pool.tf` |
| Image inventory: every image an install pulls, and its upstream pin | `images.json` |
| Registry prefix defaults (`REGISTRY_PREFIX`, `THIRD_PARTY_REGISTRY_PREFIX`) | `install.defaults.env` |
| Provisioning image-tag attachment (`qualify_image_ref`) | `scripts/installer/common.sh` |
| GKE host-discovery label | `scripts/installer/common.sh` |
| GitOps clone layout (`/opt/data/gitops/...`) and leases | `agents/platform/scripts/gitops_workspace.py` |
| Chat platforms an install posts to, the order, and the fallback | `agents/platform/scripts/chat_platforms.py` |
| fleet-audit finding-id pattern and rendering caps | `agents/platform/skills/fleet-audit/scripts/audit_report.py` |
| Helm chart value defaults (KSA/secret names, image repos, tag rules) | `charts/kube-agents/values.yaml` |
| Release tag families (`rc_*`, `rc_*_validated`, `staging_<ts>_<sha>`, GA `X.Y.Z`) and the shared lookups over them | `scripts/release/common.sh` |
| GA release gate: its conditions, exit codes, dispatch modes, and step outputs | `scripts/release/resolve_scheduled_release.sh`, `scripts/release/decide_release_gate.sh`, and `.github/workflows/release-publish.yml` |
| Stock `PlatformAgent.metadata.name` used as the admin-console installation ID | `charts/kube-agents/values.yaml` (`platformAgent.name`) |
| Terraform module defaults (GSA/KSA/namespace, role set, channel) | `terraform/modules/*/variables.tf` |
| Memory bank name, scope-tag spelling, and provider name | `agents/chat/plugins/memory/kube_agents_memory/config_schema.py` |
| Per-profile Hindsight recall settings the agent uses | `agents/chat/defaults/hindsight/config.json`, `agents/platform/hindsight/config.json` |
| Hindsight endpoint (`HINDSIGHT_API_URL`, derived from the namespace) | `k8s-operator/internal/controller/platformagent_manifests.go` |
| Admission webhook server port (`--webhook-port` default) | `DefaultPort` in `k8s-operator/internal/webhook/platformagent_webhook.go` |
| Live-test lease: ConfigMap name, TTL, install-configuration keys read, which commands count as mutations | `scripts/live_test_lease.py` |
| PR evidence screenshots: publish branch, file-name provenance, caption format | `scripts/pr_evidence_screenshot.sh` |
| Context budget for the always-loaded agent instruction files (`AGENTS.md`, `CLAUDE.md`) | `BUDGET` in `scripts/check_context_budget.py` |
| Who may set the `approved` label on a change | `OWNERS`, `k8s-operator/OWNERS`, and `OWNERS_ALIASES` |
| Which labels Tide merges on, and which Prow presubmits gate | `prow/oss/config.yaml` and `prow/prowjobs/gke-labs/kube-agents/` in `GoogleCloudPlatform/oss-test-infra` — not a file in this repository |
| Queue-wait thresholds that justify onboarding an eval project, and the window they run over | `scripts/pool_pressure.py` |
| Testing-domain slugs a bench case may claim | `docs/designs/domains.yaml` |
| Seeded-fleet fixture role names and the cluster slot each lives on | `bench/tf/fleet/fixtures.json` |
| Day-N availability gate per fixture, and the project-scoped fixtures that sit on no cluster | `docs/designs/fleet-fixtures.yaml`, which overlays `fixtures.json` and may not rename a role |
| Credential-proxy refusal rule ids, refused flags, forced git config | `agents/platform/scripts/credential_proxy.py` |
| Command-policy allowlisted verbs and denied `kubectl`/`gcloud` flags | `agents/platform/scripts/command_policy.py` |
| Which CI pool project maps to which GitOps repository | `gitops_repo_for_project()` in `hack/ci-deploy.sh` |
| Roles the pool verifier accepts as Artifact Registry upload rights, and the API set it requires | `scripts/verify_ci_pool_project.py`, whose `VALID_CMEK_STATES` mirrors `is_valid_cmek_encryption_state()` in `scripts/installer/installer_common.sh`, whose `PLATFORM_GSA_ROLES` mirrors `local.read_only_roles` in `terraform/examples/full-install/main.tf`, and whose cluster names mirror `bench/tf/fleet` and whose fixture check parses the summary line `hack/fleet-kubeconfigs.sh` prints, whose signing probe reads `githubMinter.kms.keyVersion` from `charts/kube-agents/values.yaml`, whose `LEDGER_APP_ID`/`LEDGER_INSTALLATION_ID` mirror the `EVAL_LEDGER_*` defaults in `hack/ci-eval-pr.sh`, and whose mapping check reads `hack/ci-deploy.sh` from `gke-labs/main` as well as the local tree |

## 3. Documentation eras and status

Not every document describes the same thing. When checking a doc against the
code, first check which era it belongs to:

- **`docs/architecture/` (01–09 + README) describes the END-STATE target, not
  what ships.** Each file carries the banner "Specifies the end state, not
  current behaviour." Do not treat mismatches between these specs and the code
  as doc bugs — the delta is the roadmap (`07-implementation-roadmap.md`).
- **The site (`docs/site/src/content/docs/`) and component READMEs describe
  what ships today** on `main`. These are the docs that must track code
  changes.
- **`docs/designs/` holds per-feature design rationale.** Status varies per
  document and is declared inline: `agent-communication.md` is a design of
  record, not yet implemented; `audit-logging-user-attribution.md` is a draft
  with an implemented/planned split per plane;
  `gchat-session-metadata-data-flow.md` documents implemented behavior.
- **Runtime assets that are NOT human docs:** `agents/platform/docs/glossary.md`
  and `agents/platform/docs/gcp-console-links.md` are baked into the agent
  image at `/opt/defaults/docs/` by `deploy/docker/Dockerfile` and are read by
  the agent at runtime. Similarly, every `SOUL.md`, `AGENTS.md`,
  `CAPABILITIES.md`, `SKILL.md`, and governance SOP under `agents/` is agent
  runtime material, copied into images or scaffolded into the pod — editing
  them changes agent behavior, not just prose. (The human-facing glossary is
  the separate site page `reference/glossary.md`.)

## 4. Inventory

One row per document; large uniform families are collapsed into a single row
whose path cell is a glob. Paths are repository-root-relative.

Two conventions keep this section mergeable, and both are load-bearing — a
document lands here from a different branch most weeks, and anything that makes
one branch rewrite another branch's bytes turns into a conflict on every open
pull request:

- **Every table is `<!-- prettier-ignore -->`d and padded with single spaces.**
  Prettier aligns Markdown table columns to the widest cell, so without the
  marker one new row re-pads every other row in the table and two PRs adding
  rows always collide. Add a row in the compact `| cell | cell |` form; never
  re-align. `docs-check-map` fails the build if a row grows a double space.
- **A family row characterises its family; it is not a roster.** Adding a skill,
  an SOP, or a reference under an existing glob needs no edit here at all — the
  glob already covers it. Edit the row only when the family's character
  changes. The per-file enumeration lives in the generated
  [`family-roster.txt`](family-roster.txt); run `make docs-generate` to refresh
  it.

### Repository root and repo meta

<!-- prettier-ignore -->
| Path | Category | Purpose and summary | Key topics | Audience / notes |
| --- | --- | --- | --- | --- |
| `README.md` | Project overview | Front door for "The Kubernetes Agentic Harness": Planning Agent + Platform Agent managing GKE via GitOps PRs and ChatOps, with quick-start pointers and an architecture diagram. | Value proposition, components, governance/isolation summary, links to the docs site | Evaluators and adopters; also usable by an agent to start setup |
| `INSTALL.md` | Install guide | Self-contained, executable installation guide: automated GCP/GKE provisioning, manual Kubernetes deployment, local dev, declarative Terraform+Helm install (pointer to its canonical guide), teardown, troubleshooting. Commands only; explanation lives on the site. | Prerequisites, provisioning stages, integrations, teardown | Written to be runnable end-to-end by a human or an AI agent |
| `AGENTS.md` | Contributor rules | Workspace instructions: repo layout, branching from a freshly fetched `main`, the pre-task scan of open pull requests and issues, skills guidelines, the engineering rules, the canonical-home documentation rules, generated-regions rule, PR hygiene, the live-validation requirement, and the automated pull-request review contract. States the rules; the commands that carry them out live in `docs/pull-request-workflow.md` and the mechanics that are prose in `.agents/rules/`. | Doc ownership table, engineering rules, `make docs-check`, fresh base, duplicate-work scan, Conventional Commits, fork PRs, bot review | AI coding agents and human contributors; owns the doc RULES; loaded into every session, so `make docs-check-context-budget` caps its size |
| `CLAUDE.md` | Contributor rules | Imports `AGENTS.md` and points to it for commit authorship and PR attribution guidance. | Points to `AGENTS.md` rules | Claude Code sessions |
| `admin_console/README.md` | Component README | Local setup and operating boundaries for the Kube Agents Console. | Connection, LLM gateway setup, chat, observability, integrations, validation | Console users and contributors |
| `admin_console/CONNECTION_SECURITY.md` | Security reference | Security contract for the local console's persisted connection lease. | Stored metadata, filesystem controls, identity binding, revalidation, trust boundary | Console users and security reviewers |

### `agents/` — agent blueprints (runtime documents)

<!-- prettier-ignore -->
| Path | Category | Purpose and summary | Key topics | Audience / notes |
| --- | --- | --- | --- | --- |
| `agents/chat/SOUL.md` | Runtime persona | Persona of the Planning Agent — front door, planner and delegator: work out what the request needs, file it via `kanban_create` with the right specialist, relay results with attribution. Contains the planning loop, routing table, board-management rules, and red lines. | Planning/delegation, kanban board reads, attribution, no synchronous agent calls | System persona for the `default` Hermes profile; baked into the image |
| `agents/chat/AGENTS.md` | Runtime workspace | Operating doc for the Planning Agent: asynchronous delegation via the kanban board, roster discovery, red lines (plan-and-delegate-don't-do, no infrastructure tools), stateless across sessions. | Kanban delegation, injected roster, red lines | Runtime doc; baked into the image |
| `agents/chat/defaults/onboarding/*.md` | Runtime template | First-turn greeting instructions injected by the `bootstrap_onboarding` plugin: `scan_in_progress.md` (discovery still running) and `scan_completed.md` (report ready to deliver). Neither contains inventory content — the report itself is delivered verbatim by the delivery cron job. | First-run greeting, delivery expectations | Injected into the LLM turn, not shown as-is |
| `agents/chat/defaults/plugins/agent_roster/README.md` | Design doc | Why the routable-specialist roster is injected into every Chat Agent turn by a `pre_llm_call` hook instead of fetched with a `list_agents` tool call (a roundtrip cost ~6s of the ~17s acknowledgement), why the block and the MCP tool must keep rendering from the same module, and why the roster must never be cached. | Roster injection, `pre_llm_call`, fail-soft, no caching | Human design doc for contributors changing the flow |
| `agents/chat/defaults/plugins/bootstrap_onboarding/README.md` | Design doc | Design and maintenance rules of the first-run onboarding flow: the two `no_agent` cron jobs (scan gate → kanban card to `platform`; verbatim delivery), the `pre_llm_call` hook, the `/opt/data` state markers, and the load-bearing reasons the jobs must stay split and on the `default` profile. | Bootstrap flow, state markers, scheduler snapshot semantics, guardrails | Human design doc for contributors changing the flow |
| `agents/chat/defaults/plugins/legacy_slash_commands/README.md` | Design doc | Why a typed `/hermes sethome` used to draw "Unknown command `/hermes`" (Slack only routes registered slashes to the slash handler), and how the `pre_gateway_dispatch` hook unwraps the legacy `/hermes <subcommand>` form the way the Slack adapter already does for registered slashes. | Legacy slash rewrite, `pre_gateway_dispatch`, Slack manifest registration | Human design doc for contributors changing the flow |
| `agents/chat/plugins/memory/kube_agents_memory/README.md` | Component README | The default memory provider: a thin wrapper around the Hindsight plugin Hermes ships, giving one bank for everyone with a `user:<id>` or `scope:shared` tag on every fact. Covers why the wrapper exists (Hindsight cannot learn the current user's id), that the stock plugin is loaded rather than forked, and how an install selects it (`--memory=hindsight`). | Single bank, scope tags, wrapper-not-fork, provider selection | Contributors changing the provider; the design itself is `docs/designs/memory.md` |
| `agents/chat/plugins/memory/multiuser_memory/README.md` | Component README | The file-based memory provider kept as the zero-infrastructure alternative to `kube_agents_memory`: what it stores (one Markdown file per user, one shared), what it gives up (no ranking, no search, the whole store in the window every turn), and how an install selects it (`--memory=file`). | File-per-user store, provider selection, tradeoff | Contributors changing the provider; the choice itself is `docs/designs/memory.md` |
| `agents/cluster/*.md` (SOUL, AGENTS, CAPABILITIES) | Runtime persona | The Cluster Agent profile TEMPLATE personas: a read-only SRE pinned to exactly one GKE cluster — worker protocol (`kanban_show` → diagnose → `kanban_complete` with structured RCA + proposed patch metadata), read-only red lines (never mutate, never open PRs), and the routing blurb `list_agents` shows for each `cluster-*` profile. | Single-cluster diagnostics, kanban worker handoff, read-only red lines | Scaffolded into per-cluster profiles by `cluster_agent_profile.py`; force-synced from the image template on pod start |
| `agents/cluster/skills/*/SKILL.md` | Skill bundle | The Cluster Agents' single-cluster runtime-debugging bundles: `gke-observability`, `gke-reliability`, `gke-storage`, `gke-workload-scaling` (+ HPA/VPA example assets), `gke-workload-security` (+ netpol/WI assets and an audit script), `gke-workload-troubleshooting`. | Per-skill diagnostics procedures | Listed under their own persona group in the generated skill catalog |
| `agents/platform/SOUL.md` | Runtime persona | Persona of the Platform Agent ("Harness Custodian & Architect", the `platform` profile): kanban worker protocol, GitOps-only declarative changes, recovery ladder, observability guidance, incident-communication policy, deployment architecture. | Worker protocol, declarative workflow playbook, autonomy, incident triage | System persona; several docs reference its section numbers ("SOUL.md §N") |
| `agents/platform/AGENTS.md` | Runtime workspace | Workspace doc: session startup (consult the glossary), memory conventions (daily notes, `MEMORY.md`), how kanban work arrives and must be closed, red lines. | Startup, memory, kanban worker protocol | Runtime doc |
| `agents/platform/CAPABILITIES.md` | Runtime routing | One-paragraph routing blurb advertising the Platform Agent as the fleet-wide GKE architect and default specialist. | What to route to it | Consumed by the Planning Agent's roster discovery |
| `agents/platform/cron/README.md` | Component README | Editing rules for the Platform Agent's own cron store, kept beside `jobs.json` rather than inside it because `cron/jobs.py::_save_jobs_unlocked` rewrites the file to exactly `jobs` and `updated_at` and destroys any top-level comment on the first tick. Covers what actually fires this roster (`profile-cron-tick`, not the gateway thread), why no id may appear on both rosters, why no job sets `deliver: "local"` and what `deliver: "chat"` does instead, why `schedule.display` must mirror `expr`, and the two-release sequence for retiring a watchdog given that `merge_cron_store` never prunes. | Roster ownership, duplicate-id hazard, delivery targets, `--cron-retire` | Contributors editing `agents/platform/cron/jobs.json`; the tests it relies on live in the `fleet-audit` skill |
| `agents/platform/docs/glossary.md` | Runtime reference | Glossary of agentic terms (agent platforms, runtimes, Chat vs Platform agents, Hermes profiles, kanban coordination) that the agents consult at session start. | Terminology | Baked to `/opt/defaults/docs/`; NOT the human glossary (see site `reference/glossary.md`) |
| `agents/platform/docs/gcp-console-links.md` | Runtime reference | GCP Console URL templates (Logs/Trace/Metrics Explorer, GKE Workloads) that agents fill with `{project_id}` to give users clickable links. | Console deep links | Baked to `/opt/defaults/docs/` |
| `agents/platform/docs/autoops-architecture.md` | Design doc | The AutoOps extension architecture: one fixed path from an operational signal to an approved GitOps pull request, plus the five contracts (ingestion, session and state, judgment, context reach, remediation) a new operational domain implements to ride that path. Records what ships today on the GKE-events path and what a second domain has to supply. | Inject envelope and `kind`, `_build_agent_query()`, cross-domain CUJs | Human design doc; not baked into the image despite its location |
| `agents/platform/docs/session_management.md` | Design doc | Architecture of alert-to-session routing: GKE warning events flow through a stateful REST bridge into persistent diagnostic agent sessions, with SQLite schemas and troubleshooting commands. | Event dedup, chat-thread resolution, `incident_context` plugin, verification | Human design/ops doc; not baked into the image despite its location |
| `agents/platform/governance/*.md` | SOP playbook | Uniform cron-run governance playbooks, each with a purpose line and an execution checklist. The live ones are fleet audits: each emits a validated findings file and routes it through the `fleet-audit` skill, which publishes it as the stream's ledger issue. The rest are retained on disk but unscheduled — their watchdogs shipped disabled and were then retired from the cron roster. Three are first-run onboarding SOPs run by bootstrap kanban cards rather than by cron: `inventory.md` (environment discovery, fans the workload audit out to the Cluster Agents and aggregates what they return into `INVENTORY.raw.md`), `cluster_inventory_audit_sop.md` (the per-cluster half, run by one Cluster Agent against the cluster it is pinned to) and `inventory_prioritize_sop.md` (ranks those findings into the short `INVENTORY.md` the user is sent). The k8s-event-watcher daily recap is the one entry that documents a `no_agent` script rather than instructing an agent. | Fleet audits, drift reconciliation, cost, capacity, upgrades, first-run inventory, event-watcher recap | Runtime playbooks fired by the cron watchdogs (inventory: by the bootstrap cards; event-watcher recap: by a `no_agent` script tick); the site's governance-sops page names them and says which are live |
| `agents/platform/skills/*/SKILL.md` | Skill bundle | The Platform Agent's capability bundles, each a YAML-frontmatter (`name`, `description`) plus procedural instructions: `gke-*` skills covering cluster lifecycle and day-2 operations, the Cluster-Agent orchestration skills, self-monitoring, and GitHub issue resolution. Two are named here because they are structurally special rather than to enumerate the family: `fleet-audit` is the harness the cron audits share (one ledger issue per stream plus narrow per-finding remediation PRs), and `submit-suggestion` is the GitOps write path. The install/uninstall/upgrade lifecycle skills live in `.agents/skills/` — those skills drive the repository's own installers and are not shipped in the agent images. | Per-skill procedures | Runtime docs; enumerated by the generated skill catalog `docs/site/src/content/docs/skills/index.mdx` — frontmatter is the source of that table |
| `agents/platform/scripts/providers/*/fixtures/README.md` | Reference | Provenance of the recorded forge API responses the contract harness replays, one directory per forge package: the `{payload, responses}` file shape, why an unfixtured call raises rather than reading `None`, and the three edits made to every recording (trimmed to the read fields plus neighbours, redacted of every login and identifier, composed so a list verb has something to filter). | Fixture provenance, contract harness, redaction | Developers adding a forge or changing a translation; the harness is `agents/platform/scripts/test_providers_contract.py` |
| `agents/platform/skills/*/references/*.md` | Skill reference | Cheat-sheet references across the Platform Agent skills (ComputeClass facets, autoscaler tuning, upgrade runbooks and troubleshooting, manifest recipes, AI/TPU failure signatures) loaded on demand by the skills. | Skill reference behavior | Runtime reference material |
| `agentplugins/README.md` | Component README | What an agent plugin is in this repository — a Helm chart that creates an `AgentPlugin` plus an OCI image the agent loads — the two that ship here, how they are installed and tested, and the two constraints (name pattern, target profile) that catch people adding a third. | Plugin directory overview | Anyone adding or installing a plugin |
| `agentplugins/gke-stockout-investigator/README.md` | Component README | What the stockout investigator does, how to install it, and the two behaviours that are not obvious from the chart: alerts become kanban tasks owned by the `platform` profile, and one stockout stays one investigation across autoscaler retries. | Stockout plugin install and behaviour | Operators installing or debugging the plugin |
| `agentplugins/pubsub-platform/README.md` | Component README | Installing the Pub/Sub ingress adapter, and the route keys that decide whether an alert becomes work at all: filter, threshold, dedup fields and dispatch mode. Defers to the shipped adapter reference for the message flow itself. | Alert ingress configuration | Operators configuring alert routes |
| `agentplugins/gke-stockout-investigator/files/skills/gke-stockout-investigator/SKILL.md` | Skill bundle | The GKE Stockout Investigator's procedure: judge whether a scale-up failure is real or a stale duplicate, diagnose the capacity shortfall, and propose a GitOps remediation PR. Shipped inside the plugin's OCI image and registered at load time as `gkestockoutinvestigator:gke-stockout-investigator`, so it does not appear in the generated skill catalog. | Stockout diagnosis and remediation | Runtime procedure; travels with the plugin image, not the agent image |
| `agentplugins/gke-stockout-investigator/scenarios/README.md` | Developer guide | How to trigger a stockout investigation by hand: one script per failure kind, each wedging a real workload and publishing the matching alert. Covers why a workload is needed at all, the dedup/namespace/admission constraints the harness works around, and how to add a scenario. | Manual scenario triggering | Demo and skill-exercise tooling, also driven by the E2E suite and the RC promotion gate |
| `agentplugins/pubsub-platform/files/platforms/pubsub/README.md` | Plugin reference | How the Pub/Sub platform adapter routes alert notifications to agent profiles: subscription config, filtering and deduplication, and the `agent_profile` key that sends the resulting work to a specialist. | Pub/Sub alert ingress | Runtime reference; the adapter is a gateway-level singleton on the default profile |

### `docs/` — architecture, designs, and standalone documents

<!-- prettier-ignore -->
| Path | Category | Purpose and summary | Key topics | Audience / notes |
| --- | --- | --- | --- | --- |
| `docs/architecture/README.md` | End-state spec | Index of the design set: authoritative build-ready spec of the end state where read-only agents replace the human interface to Kubernetes, proposing all changes through GitOps. States the core invariants. | Invariants, doc-set structure, build-from-docs instructions | "Design complete"; describes the end state, not current code |
| `docs/architecture/01-vision-scope.md` | End-state spec | North star: agents become the primary presentation layer for Kubernetes operations, serving platform / cluster-admin / developer-team audiences. | Vision, audiences, known deltas, success criteria | End-state; not current behaviour |
| `docs/architecture/02-agent-personas.md` | End-state spec | The three-persona roster (Platform, Cluster Admin, Developer Team) with shared anatomy, differing only in scope, authority, skills, and permissions. | Persona anatomy, skill allocation, ChatOps addressing, boundary matrix | End-state; only the Platform Agent exists today |
| `docs/architecture/03-security-model.md` | End-state spec | Security and trust model on five pillars: scoped identity, downward-only privilege attenuation, AI-specific defenses, declarative-only mutation, read-only human ceiling. | Threat classes, trust boundaries, per-tier RBAC, egress allowlist | End-state; not current behaviour |
| `docs/architecture/04-workflow-model.md` | End-state spec | The single change loop for all tiers: agent proposes declaratively, human approves via PR merge, customer CI/CD applies. Autonomy governs proposing, never approving. | Propose/review/reconcile, autonomy vs gates, proactivity, failure isolation | End-state; not current behaviour |
| `docs/architecture/05-system-architecture.md` | End-state spec | Whole-system assembly: hub-and-spoke topology, component inventory, data flows, shared services, NFR targets. | Components C1–C15, flows F1–F5, topology, NFRs | End-state; not current behaviour |
| `docs/architecture/06-api-and-data-contracts.md` | End-state spec | Exact interfaces to implement: tier-discriminated `Agent` CRD, pre-created read-only identity contract, GitOps repo layout, OKF schema, review-gate contract. | CR shape, cardinality, identity contract, naming conventions | End-state; the CR shape is labeled illustrative |
| `docs/architecture/07-implementation-roadmap.md` | End-state spec | Phased sequence from the current state (direct-mutation agents, `PlatformAgent` only) to the three read-only personas, with acceptance criteria per phase. | Delta table, phases, definition of done | End-state; sequencing only |
| `docs/architecture/08-agent-runtime-and-identity.md` | End-state spec | Simplest v1 runtime: a thin controller reconciles the `Agent` CRD into one isolated Hermes pod per agent bound to one pre-created read-only service account. | Runtime, identity referencing (never minting), deferred hardening | End-state; deliberately simplicity-over-defense-in-depth |
| `docs/architecture/09-capability-envelope.md` | End-state spec | How a request's authority travels between agents once they are separate workloads: the gateway mints a capability in NATS KV, hops attenuate by writing narrower children naming their one permitted successor, and a verification service resolves the chain. | Capability lifecycle, attenuation, delegate binding, per-hop enforcement, no cryptographic keys | Agreed design, north-star tier; presumes agents are separate workloads, so nothing in it is built yet |
| `docs/designs/agent-communication.md` | Feature design | How the Platform Agent and per-cluster subagents exchange information: a file-based typed handover channel plus optional kanban delegation. | Blackboard model, record envelope, `write_handover` tool | Design of record; NOT yet implemented (banner in file) |
| `docs/designs/agent-shell-sandboxing.md` | Feature design | The whole of #737: moves the agent's shell out of the agent pod into a per-agent sandbox StatefulSet reached over Hermes' `ssh` backend, and moves the credential proxy into a third pod of its own so no container the model can reach holds a credential or can mint the agent's GCP identity. | Terminal backend survey, Agent Sandbox vs Agent Substrate, pod-scoped Workload Identity, federation and the ID-token gap, `sync_back` write channel, gVisor/WAL blocker | Shell sandbox and the three-pod split implemented; #737 Part A (Session KV interface) not |
| `docs/designs/admin-console.md` | Feature design | Product and implementation design for the Kube Agents Console, including its shared FastAPI chat abstraction, Streamlit composition, authenticated interaction API, activity, connection, LLM gateway setup, integration status, and Kanban read models. | Admin UX, asynchronous interactions, API contract, correlation, security boundaries | Local implementation; shared proxy API and production hardening planned |
| `docs/designs/audit-logging-user-attribution.md` | Feature design | Closes the gap where audit logs identify the agent SA but not the requesting human, by carrying requester and trace/session IDs through existing telemetry. | Attribution contract per plane, correlation recipes, trust model | Draft, P0; per-plane implemented-vs-planned split declared inline |
| `docs/designs/bench-case-format.md` | Feature design | The contract for a `bench/tasks/*/task.yaml`: which fields the devops-bench loader honours and which this repository's lints do, how to choose between an exact check and a judged score, and which of the deterministic keys reds a presubmit. | `id` vs the `task_id` alias, exact-vs-judged test, the three gating keys, mandatory `domain` and `verification_spec` | Case authors; enforced by `scripts/validate_bench_cases.py` |
| `docs/designs/bench-fleet-catalog.md` | Feature design | The case author's half of the seeded fleet: the eight fixture role slugs, why a case addresses a fixture by role and never by cluster name or project id, and which fixtures are assertable at day 0, 1, 7 and 30. | Role vocabulary, slot addressing, SOP age gates, read-only rule | Case authors; `bench/tf/fleet/README.md` is the operator's half |
| `docs/designs/cron-report-relay.md` | Feature design | How a scheduled job's result reaches chat with the context a follow-up question needs: the specialist reasons, hands its finished report to the Chat Agent over the Session KV server, and the Chat Agent speaks. | `deliver: "chat"`, `/v1/cron-reports`, relay turn, per-job-per-day session, `incident_context`, `report_to_chat` | Implemented; the whole Platform Agent roster delivers this way |
| `docs/designs/design_537148738.md` | Feature design | Technical design proposal for CI/CD pipeline security scanning and dependency management (Buganizer Task 537148738). | Action SHA pinning, output encapsulation, security scanning, Dependabot | Implemented (design of record) |
| `docs/designs/drift-detection.md` | Feature design | Design of record, not yet implemented. Splits drift into computing the diff (commoditized) and judging it (the differentiator); enters the shipped pipeline as a `gitops-drift` inject. | `managedFields` + audit-log attribution, revert-or-codify, CUJ selection | Status banner declares it unimplemented; no GitOps tool required |
| `docs/designs/eval-scorer.md` | Feature design | How the eval scorer decides and how its baseline is established, compared against and reset: the six-rung verdict ladder as built, the append-only JSONL record, the five-component version key, computed admission, the local and GCS backends, rung 6's judged comparison, the nightly job that fills the store, and how a quality-over-time dashboard reads the same data. | Six-rung ladder, append-only JSONL, version key, computed admission, `objectCreator` immutability, rung 6 margin, BigQuery external table | Design of record; partially implemented — GCS backend defaults off, bucket and dashboard do not exist (banner in file) |
| `docs/designs/fleet-audit-issue-ledger.md` | Feature design | Replaces the audit's PR-as-report with one ledger issue per stream plus narrow per-finding remediation PRs; hybrid auto/pull-based gating and a first-class `recommendation` field. | Ledger issue, remediation PR lifecycle, promotion gating, migration | Design of record; implemented (banner in file) |
| `docs/designs/gchat-session-metadata-data-flow.md` | Feature design | The implemented attribution path from a Google Chat message to Hermes OTel spans via the `session_store` plugin and the `session_otel_bridge`. | Session metadata allowlist, span stamping, SQLite KV store | Documents implemented behavior; site `reference/attribution.md` summarizes it |
| `docs/designs/gitops-workspace-leases.md` | Feature design | Gives every concurrent agent a private GitOps clone keyed by a lease, replacing the one shared working tree that audits and suggestions corrupted for each other. | Lease layout, `.lease` marker, reaper, credential-proxy `git` gate | Design of record; implemented (banner in file) |
| `docs/designs/live-test-lease.md` | Feature design | Serializes mutating access to an installation several agents share, so two live validations cannot describe an install neither of them left behind. Covers the in-cluster ConfigMap lease, why enforcement is a hook rather than an instruction, and what counts as a mutation. | Compare-and-swap acquisition, command classification, install.env discovery, fail-to-ask | Design of record; implemented (banner in file); contributor tooling, not shipped to installs |
| `docs/designs/memory.md` | Feature design | The memory design and the A/B that decided it: what Hermes offers, why a document store and not a flat file, which provider an install gets and where that choice is carried, the two Hindsight pods, how scope tags keep every user apart in one bank, and what the experiment measured. | Background, scaling case, provider choice, two pods, scope tags, `any_strict`, injection path, tools, experiment | Implemented on the Chat Agent profile; TTL mechanism built but deferred, returning soon; #111-#116 open |
| `docs/designs/multi-forge-support.md` | Feature design | What it would take to drive a forge that is not GitHub: the layers that name GitHub today, which of them are worth unwinding regardless, and why an MCP server is an addition rather than the mechanism. | `RepoRef` identity, provider-selected token strategy, brokered CLI vs proxy providers, sequencing | Design of record; NOT yet implemented (banner in file) |
| `docs/designs/multi-project-scope.md` | Feature design | Why the Platform Agent manages one GCP project today (one `clusters list`, one project of IAM bindings, "1 per project" in the architecture) and the opt-in scope that replaces it: explicit projects, folders, and organisations on `spec.scope`, resolved through Cloud Asset Inventory, granted by container-level IAM so a new project under a declared folder onboards with no change, with a per-project outcome snapshot so a denied project reads as a finding rather than an empty fleet. | `spec.scope`, Asset Inventory resolution, folder-level IAM inheritance, `fleet_scope.json`, single-project assumptions inventory, sequencing | Design of record; NOT yet implemented (banner in file) |
| `docs/designs/pr-comment-conversation.md` | Feature design | How a reviewer commenting on an agent-authored pull request wakes the agent, and how the answer gets back into the thread without a state file. | Token-free cron gate, forge provider protocol, state-free idempotency markers | Design of record; §§2–6 implemented, staleness escalation designed only (banner in file) |
| `docs/designs/semver-deployment-versioning.md` | Feature design | Design rationale for adopting SemVer 2.0.0 across container images, the Helm chart, Terraform modules, release docs, and governance playbooks — decisions, shipped mechanisms, and deliberate exceptions. | OCI Helm charts, Git ref TF modules, version-injection defaults | Implemented; exceptions declared inline |
| `docs/designs/e2e-testing-harness.md` | Feature design | Architecture, execution model, and scenario matrix of the multi-stage E2E testing harness, RC promotion gates, and nightly matrix. | E2E stages, fleet audit probes, stockout scenarios, live chat | Design of record; implemented (banner in file) |
| `docs/designs/testing-strategy.md` | Feature design | What "tested" means for an autonomous actor with standing authority in a production fleet, and the four tiers that answer it: unit, integration (real seams, fake agent), presubmit evals, release gate, with nightly a merged but never-yet-run pipeline. Carries the domain-coverage rule, the six-rung eval gate ladder, and the statistical treatment of judged scores. | Tiers, domain as unit of coverage, 3 repetitions per case, non-inferiority against `main`'s baseline, pinned judge and pinned scorer, keep writing new cases | **Draft**, in review; §4.2's statistical gate is advisory until baselines exist. Unit tests are real and the integration tier gates; the seeded fleet is applied and standing; presubmit runs the full case matrix and has blocked merges since 2026-09-02; the release gate is one test; nightly is merged and its project and GitHub environment exist, but it has no cron and has never run (§4.4) |
| `docs/designs/spec-a2a-payloads.md` | Feature design | Wire protocol for agent-to-agent messaging over NATS JetStream (`a2a-jetstream/0.4`): the envelope, the A2A payload layering rule, addressee-scoped task subjects, the task lifecycle mapped onto streams, the topic namespace, and the conformance assertions the client library must pass. | Envelope, layering rule, addressee token, reserved `identity`/`authority`, steering, topics, conformance assertions | Stage 1 client library and conformance suite implemented in `a2a/` |
| `docs/designs/spec-chatops-gateway.md` | Feature design | The chat-to-bus gateway: deterministic code with no model of its own, owning sessions (one backend conversation, one `contextId`), requester identity onto the bus as an advisory `authority` block, the group-chat substrate, and Discord as the test backend. | Session model, `authority` block, roster snapshots, `openDirect`, session lifecycle | Merged design of record; the gateway program is implemented (`a2a/gateway`), session spawning dark behind `A2A_SPAWN_SESSIONS`; the operator render is not yet in-tree |
| `docs/designs/spec-mode-switch.md` | Feature design | The single dev-mode toggle (`spec.mode`, `today`/`next`) that keeps the A2A stack dark: one optional CRD enum read through one nil-safe fail-closed helper, Degraded with a named reason on version skew, not surfaced in the Helm chart until graduation. | `renderMode` helper, `KUBEAGENTS_MODE`, per-feature override sketch | Draft for review; the switch itself is implemented in the operator (mode enum, Degraded on skew, managed-.env pin); the components it gates are not |
| `docs/designs/spec-nats-deployment.md` | Feature design | The NATS deployment for the A2A fabric: stream and retention layout, connection-time authorization via auth callout, the audit model (the stream is the buffer and replay window, the log sink is the archive), and the client resilience contract as testable requirements. | Streams, accounts, deny-by-default users, audit exporter, restart conformance assertion | Draft for review; not yet implemented |
| `docs/designs/spec-subagent-profiles.md` | Feature design | Subagents as declarative profiles: the `AgentProfile` resource, submission as a bus message rather than an API call, dispatcher-rendered Jobs, four reserved artifact names, the janitor for orphaned tasks, and the kanban retirement inventory with its two named gaps. | `AgentProfile`, dispatcher, artifact names, lifecycle, kanban gap table | Profile schema and strict loader implemented in `a2a/profiles`; the CRD and dispatcher are not |
| `docs/contributing.md` | Contributor guide | Short entry point: Google CLA and community guidelines, deferring everything else to the site's contributing page and `AGENTS.md`. | CLA, pointers | Human contributors |
| `docs/credential-isolation-design.md` | Feature design | Design keeping API keys, tokens, and SA credentials out of the agent sandbox; credentialed operations proxied through an Envoy credential broker in a Pod of its own. | Pod anatomy, CLI forwarding, guarantee and stated limitation | Canonical design; site `reference/credential-isolation.md` defers here |
| `docs/pull-request-workflow.md` | Contributor guide | The commands behind `AGENTS.md`'s pull-request rules: the duplicate-work scan, the branch-drift check against `upstream/main`, the local validation checks and the constraint each one exists for, and the `kube-agents-bot` review — how long it takes, how to poll for it, how to reply and resolve. Ends at the merge: the two labels Tide requires, the `OWNERS` approvers behind them, `/hold`, why branch protection reads as though there is no review gate, and which party owes an open pull request its next move. | `gh` and GraphQL recipes, drift `comm` check, prettier/Docker/layer-budget detail, bot timing and failure modes, Tide `lgtm`+`approved`, `OWNERS`, PR ownership rule | AI coding agents and human contributors; mechanics only, `AGENTS.md` owns the rules |
| `docs/testing-map.md` | Contributor guide | The mechanics behind `AGENTS.md`'s rule for where a test goes: the ten test homes, what runs each, and how far "runs on a pull request" is from "gates a merge" — three paths-filtered workflows that report green having run nothing, a release gate on a three-hourly schedule rather than manual dispatch, and a directory no `PYTHON_TEST_DIRS` glob reaches. | Test-tier routing, `make test-python`/`test-bench`/`verify`, `dorny/paths-filter` green-on-nothing, RC pipeline schedule, test discovery globs | AI coding agents and human contributors; mechanics only, `AGENTS.md` owns the rule |
| `docs/security-requirements.md` | Requirements | Provider-neutral security configuration model across three dimensions (permission, interaction, authorization), explicitly distinguishing current behavior from planned capabilities. | Permission sets, credential-isolation requirements, attribution requirements | Referenced by the site's security pages; current-vs-planned marked inline |
| `docs/chatops/microsoft-teams.md` | Integration guide | Deployment, Azure Entra ID / Bot Framework app registration, single-tenant policy lockdown, and Microsoft Teams ChatOps configuration walkthrough. | Microsoft Teams, Bot Framework, Entra ID OAuth, App manifest, Adaptive Cards | Administrators enabling Microsoft Teams ChatOps |
| `docs/site/README.md` | Component README | How to develop the docs site: local preview/build, layout, adding a page, CI build/deploy workflows, publishing from a fork. | `npm run dev`, frontmatter, GitHub Pages base | Site contributors |

### `docs/site/src/content/docs/` — the published site

Frontmatter `title`/`description` is the page's own summary; rows below add
only what the title does not say.

<!-- prettier-ignore -->
| Path (under `docs/site/src/content/docs/`) | Category | Purpose and summary | Key topics | Audience / notes |
| --- | --- | --- | --- | --- |
| `index.mdx` | Site page | Landing page (hero + cards): the project pitch and entry points. | Chat + Platform agents, components, skills | Everyone |
| `404.md` | Site page | Custom not-found page linking to key entry points. | Navigation | Site infrastructure |
| `contributing.md` | Site page | Full contributing guide: CLA, PR hygiene, live validation, the local checks CI enforces, the automated `kube-agents-bot` review, and how Prow merges a change. | CLA, Conventional Commits, `make` checks, automated review, Prow merge | Contributors; `docs/contributing.md` points here |
| `overview/what-is-kube-agents.md` | Site page | Inventory of the first-party components: what installs where and what runs after the provisioner reconciles. | Operator, agent Deployment, gateway, Minty | New users |
| `overview/architecture.mdx` | Site page | Component map and the three request flows (chat, cron tick, remediation PR) through one Hermes gateway hosting the two profiles. | Flows, kanban coordination, topology, failure modes | The shipping-architecture page |
| `overview/proactive-autonomy.md` | Site page | The hands-free loop: cron jobs fire the Platform Agent at governance SOPs; audits, PRs, alerts. | Watchdog loop, safety rails | New users |
| `concepts/index.mdx` | Site page | Card-grid hub linking the nine concept pages. | Navigation | — |
| `concepts/platform-agent.md` | Site page | Persona, safety rails, and tool wiring of the Platform Agent. | SOUL.md, MCP servers, toolsets, plugins | — |
| `concepts/cluster-agents.md` | Site page | The per-cluster read-only specialists: scoping, the create/prune lifecycle and hourly reconcile, and the kanban delegation flow. | Cluster Agent lifecycle, read-only scoping, fan-out | — |
| `concepts/chatops.md` | Site page | Chat ingress: Google Chat and Slack terminate at the Planning Agent front door, which delegates to the Platform Agent. Both opt-in. | Enablement flags, allowed users, session metadata | — |
| `concepts/skills.md` | Site page | How the Platform Agent loads and invokes skill bundles; adding and importing skills. | SKILL.md format, frontmatter contract | — |
| `concepts/governance-sops.md` | Site page | What the governance SOPs are (strategy vs skills' tactics) and which ship. | SOP roster | Sources live in `agents/platform/governance/` |
| `concepts/autonomous-watchdogs.md` | Site page | Cron-scheduled jobs that make the agent proactive; job shape, disabling, adding. | `agents/chat/defaults/cron/jobs.json` | Schedule table lives on `reference/cron-jobs.md` (generated) |
| `concepts/declarative-workflow.md` | Site page | All infrastructure changes route through Git; how `submit-suggestion` and Minty enforce it. | No direct mutation, short-lived tokens, anti-patterns | — |
| `concepts/inference-gateway.md` | Site page | Model access as a config toggle: LiteLLM for hosted models, vLLM for local, optional replay caching. | Provider choice, replay modes | — |
| `concepts/observability.md` | Site page | OTel traces, Prometheus metrics, and Cloud Logging routing for agent and gateway. | Exports per component, console links, tool-call audit | — |
| `install/quickstart-gke.mdx` | Site page | One-command bootstrap of cluster, operator, and Platform Agent; what just happened; common flags. | `install.sh`, toggles, uninstall pointer | — |
| `install/prerequisites.md` | Site page | What must be in place before provisioning: tooling, GCP project, cert-manager, chat platform, LLM credentials. | Prerequisites | — |
| `install/manual.md` | Site page | Installing the Platform Agent workspace into an existing Hermes-compatible harness by hand. | Copy workspace, register, wire infra | — |
| `install/helm-and-kind.md` | Site page | Points to the canonical Helm chart and Terraform modules in `main` (published from the first `X.Y.Z` tag) and states Kind is unsupported. | Chart/module pointers, no Kind | — |
| `install/uninstall.md` | Site page | Removing the agent, operator, and provisioned GCP resources; agent-only vs full teardown. | Teardown | — |
| `deploy/index.md` | Site page | Hub for the deploy section: Docker, Kustomize, Minty, release versioning, telemetry, GitOps, CI pool projects, environment reconcile. | Navigation | — |
| `deploy/kustomize.md` | Site page | What ships in `deploy/kustomize/` and what the operator lays down on top of it. | Base vs operator-created objects | — |
| `deploy/release-versioning.md` | Site page | Release lifecycle, automated SemVer 2.0 governance, clean promotion, GA release execution, and emergency hotfix runbook. | SemVer calculation, staging promotion gate, clean promotion, CLI dispatch, emergency runbook | SREs and maintainers |
| `deploy/docker-images.md` | Site page | The images shipped from this repo and how tags are managed. | Image list, base pin, registry overrides, CI | — |
| `deploy/token-minter.md` | Site page | Minty: the in-cluster broker minting short-lived GitHub App installation tokens; no long-lived secret on disk. | Token flow, KMS-held key, setup | Operator-side README: `k8s-operator/config/integrations/github/README.md` |
| `deploy/telemetry.md` | Site page | Where OTel, Prometheus, and Cloud Logging fit in the shipping deploy, and how to point it at a collector other than the GKE-managed one. | What runs where, OTLP endpoint precedence and discovery, non-GKE clusters | — |
| `deploy/gitops-argocd.md` | Site page | Standing up ArgoCD and Config Connector as the pull-based reconciler that applies what the agent proposes. | Pull vs push, read-only repo App, fleet auth, prune gates | Reference repo layout: `examples/gitops-repo/README.md` |
| `deploy/ci-pool-projects.md` | Site page | Prerequisites and setup for GCP projects in the Boskos evaluation pool. | Enabled APIs, host cluster, IAM, AR cleanup policy, GitHub App token minter, ledger read credential, pre-flight verification, Boskos registration | CI engineers |
| `deploy/environment-reconcile.md` | Site page | Keeping the long-lived `autopush` and `staging` environments in step with the composition, rather than only re-tagging their images. | Drift report, nightly reconcile, rebuild button, the required per-environment variables, what a rebuild does not preserve | CI engineers and maintainers |
| `operator/index.md` | Site page | Overview of the Kubebuilder controller reconciling `PlatformAgent` CRs and the resources it manages. | Managed resources, webhooks, layout | — |
| `operator/platformagent-crd.md` | Site page | Reference for the `PlatformAgent` custom resource shape and reconcile behavior. | `spec.harness`/`deployment`/`security`/`integration`, `status` | — |
| `operator/agentplugin-crd.md` | Site page | Reference for the `AgentPlugin` custom resource shape, OCI image volume mounting, and security allowlisting. | `spec.agentRef`/`image`/`env`/`config`, `status` | — |
| `operator/development.md` | Site page | Building, testing, and iterating on the operator locally. | Kubebuilder workflow, fast iteration | — |
| `reference/index.mdx` | Site page | Card-grid hub for the reference section. | Navigation | — |
| `reference/config.md` | Site page | `agents/platform/config.yaml` annotated: MCP servers, toolsets, memory, plugins. | Config keys | — |
| `reference/cron-jobs.md` | Site page | Annotated cron reference; the jobs table is a **generated region** sourced from `agents/chat/defaults/cron/jobs.json`. | Job schema, editing | Do not hand-edit the table; `make docs-generate` |
| `reference/examples.md` | Site page | Tour of the inference example bundles shipped in `examples/`. | Replay, LiteLLM, vLLM bundles | — |
| `reference/glossary.md` | Site page | Human-facing glossary of kube-agents and ecosystem terminology. | Terminology | Distinct from the runtime `agents/platform/docs/glossary.md` |
| `reference/attribution.md` | Site page | Operator-facing runbook for connecting an agent action back to the requesting human; query recipes. | Attribution contract, trust boundary | Summarizes `docs/designs/audit-logging-user-attribution.md` |
| `reference/security-and-iam.md` | Site page | What the agent is and is not permitted to do: Workload Identity model, IAM permission sets, read-only Kubernetes RBAC, auditing posture. | Identity, permission sets, read-only mode | Canonical home for agent permissions per `AGENTS.md` |
| `reference/credential-isolation.md` | Site page | How the operator keeps credentials out of the agent sandbox via the Envoy sidecar. | Pod anatomy, request paths, limitation, troubleshooting, broker split, egress allowlist | Defers to `docs/credential-isolation-design.md`; owns troubleshooting and the `egressPolicy` topic |
| `reference/resource-labels.md` | Site page | The `app.kubernetes.io` labels stamped on every object the project installs, and how to select on them. | Label contract, selector immutability, query recipes | Canonical home for the label contract; complements `reference/attribution.md` |
| `skills/index.mdx` | Site page | The skill catalog; the grouped table is a **generated region** sourced from every skill's `SKILL.md` frontmatter. | Skill roster by area | Do not hand-edit the table; `make docs-generate` |

### `examples/`

<!-- prettier-ignore -->
| Path | Category | Purpose and summary | Key topics | Audience / notes |
| --- | --- | --- | --- | --- |
| `examples/gitops-repo/README.md` | Example | Top of the reference GitOps repository template customers fork as their source of truth: layout map and the propose/apply/review-gate/version-pin contracts (agents propose via PR; only customer CI/CD applies). | Repo layout, guarded paths, version pins | Implements the `docs/architecture/` contracts |
| `examples/gitops-repo/*/**` sub-docs | Example | Short uniform READMEs and knowledge entries for each template directory: branch-protection ruleset, workflows (actuation pipeline), per-cluster agents/namespaces/provisioning, fleet, OKF knowledge index + sample cluster blueprint, and admission policy (`kube-agents-agent-readonly`, sourced from `k8s-operator/config/admission/agent-rbac-policy.yaml`). Densely cross-reference the architecture specs by section. | Per-directory contracts, OKF taxonomy, admission policy | Template consumers; tied to the END-STATE spec set |
| `examples/inference-replay/README.md` | Example | Deploying the Inference Replay proxy that intercepts the LiteLLM service and serves cached LLM responses from a PVC-backed store. | Context-aware hashing, off/on modes | Developers wanting cheap deterministic iteration |
| `examples/litellm-chatgpt-subscription/README.md` | Example | LiteLLM proxy backed by a consumer ChatGPT subscription via the OAuth device-code flow. | Device flow, PVC token persistence | Users without API keys |
| `examples/litellm-gemini/README.md` | Example | LiteLLM proxy configured for Gemini models: secret, manifests, metric verification. | API-key secret, PodMonitoring | Cluster operators |
| `examples/vllm-gemma/README.md` | Example | Serving Gemma models with vLLM on GPU nodes, based on the official GKE tutorial. | GPU serving, vLLM metrics | Self-hosted inference |

### `charts/`, `terraform/`, `k8s-operator/`, and `tests/`

<!-- prettier-ignore -->
| Path | Category | Purpose and summary | Key topics | Audience / notes |
| --- | --- | --- | --- | --- |
| `bench/README.md` | Component README | The evaluation harness that runs `kubernetes-sigs/devops-bench` against the Platform Agent as a pip-installed library: layout, running evals, the rate-based presubmit gate (`bench-gate`), harness registration, offline tests. | Eval invocation, `BENCH_TF_ROOT` stacks, the gate and its thresholds | Developers writing or running evals |
| `bench/CUSTOM-TASKS.md` | How-to | Authoring new devops-bench tasks and agent harnesses, here or in a private repository: layout convention, the `devops-bench` SHA pin, OpenTofu stacks, `task.yaml` and `verification_spec`, custom `AgentHarness`. | Task authoring, verification spec, harnesses | Developers writing evals; the rules it applies are `docs/designs/bench-case-format.md` |
| `bench/baselines/README.md` | Reference | The checked-in baseline store: the append-only per-case JSONL format and why it keeps history, the five-component version key a record is filed under, how a nightly run on `main` fills it and how the reader pools the newest lines until it has enough runs to admit a case, how admission is computed from that evidence rather than declared, and what invalidates a record. | Version key, admission bar, staleness, `bench-gate record` | Developers changing the agent, the judge, the verifiers, or the fleet |
| `bench/cuj/README.md` | Test guide | Running and extending the pytest-discovered, black-box Critical User Journey suite. | Live portal tests, scenario contract, `/tmp` evidence | Developers writing or running CUJ tests |
| `bench/tasks/DRAFTS.md` | Reference | Status table for the Phase 2 domain scenarios: one per domain, the planted defect each needs, its isolation class, and the seeded-fleet shopping list. Also the extra cluster-debugging drafts beyond that domain's one row, the two cross-cutting failure cases, and the activation blockers. | Scenario corpus, seeded fleet defects, activation state | Status is per row, not per file: the seeded fleet landed, the probes and the audit canary are active in presubmit, the four full audits run in the nightly tier (`NIGHTLY_TASKS` in `hack/ci-eval-pr.sh`), and two rows are held on named blockers. Also carries the measured run durations the recast rests on |
| `bench/tests/fixtures/runs/README.md` | Reference | Provenance of the captured devops-bench run records the gate tests grade: which cluster and commit produced them, which are red and which green, the one task still uncaptured, and the judge spread across three identical runs that argues for the two-speed gate. | Fixture provenance, judge variance | Developers changing the scorer |
| `bench/tf/fleet/README.md` | Component README | The seeded dirty fleet: three standing clusters whose planted defects are the fixtures the Phase 2 presubmit scenarios assert on, the defect-to-scenario map, and the scheduled re-apply that keeps the defects planted. | Seeded fleet, planted defects, reconcile | Developers writing evals; the fleet owner |
| `charts/kube-agents/README.md` | Component README | Canonical GKE-oriented Helm chart (`kube-agents`) for deploying the Kube-Agents operator and PlatformAgent CR via GitOps. | Chart configuration, values, CRD installation | Deployment operators and GitOps pipelines |
| `k8s-operator/README.md` | Component README | The Go/Kubebuilder operator managing the `PlatformAgent` CRD: prerequisites, pointers to the installer/composition, dev iteration. | CRD lifecycle, dev workflow | Operator developers |
| `k8s-operator/cmd/k8s-event-watcher/README.md` | Component README | The Go daemon that streams, filters, and deduplicates GKE warning events and forwards unique incidents to trigger autonomous diagnostic sessions. | Event filtering, dedup windows, snapshots | Watcher developers/operators |
| `k8s-operator/config/integrations/github/README.md` | Component README | The GitHub Token Minter (Minty) integration: short-lived GitHub App tokens brokered against Workload Identity OIDC, App key held in Cloud KMS. | Token flow, App setup, KMS import | Operators wiring GitOps write access; site page `deploy/token-minter.md` is the narrative |
| `k8s-operator/config/integrations/hindsight/README.md` | Component README | The Hindsight API and its Postgres/pgvector database — the memory store behind the Chat Agent: the kustomize dev copy of what the chart renders, why it needs no credentials, and the in-cluster URL the agent image bakes in. | Passwordless Postgres, NetworkPolicy, digest pinning, no HF egress | Operators installing the memory store; the design rationale is `docs/designs/memory.md` |
| `scripts/installer/README.md` | Component README | **Canonical** home for the shared installer defaults (the table; the values live in `install.defaults.env`), the `install.env` configuration model, and the helper scripts the installer front doors still share. | Shared defaults, configuration model, helper inventory | Installer and dev-tooling developers |
| `scripts/dev/README.md` | Component README | The script automating GCP Workload Identity Federation so GitHub Actions can deploy keylessly. | WIF/OIDC CI auth | Repo maintainers |
| `scripts/feedback_form/README.md` | Component README | The public Google Form that files outside feedback as `external-feedback` issues, for reporters whose enterprise-managed GitHub accounts cannot open issues here: the Apps Script behind it, its setup, the token it needs, and what to do when filing fails. | External feedback intake, Apps Script, token setup | Repo maintainers |
| `k8s-operator/testing/staging_workloads/README.md` | Component README | Terraform PoC that stamps out multi-cluster GKE staging fleets with realistic workload bundles and traffic simulators. | Cluster maps, workload bundle, load shapes | Developers building staging fleets |
| `scripts/eval_dashboard/SCHEMA.md` | Reference | The `data.json` contract (schema version 1) between the eval-dashboard collector (`scripts/eval_dashboard/collect.py`, which writes it from Prow logs) and the dashboard renderer and publisher built against it in parallel: field names, types, derivation rules, and the additive-only change policy with `schema_version` bumps. | Run and case records, task results, pass rates, additive-only evolution | Dashboard collector/renderer/publisher developers |
| `scripts/release/README.md` | Component README | Overview of the release automation scripts: candidate tag creation, environment provisioning and teardown, GKE readiness & E2E test execution, validated tag promotion, the nightly staging promotion, the in-place reconcile and drift report for the long-lived environments, and the GA release. Carries what the nightly environment needs before it can run, and is canonical for the GA release gate and how its schedule is turned on. | Release Candidate scripts, RC automation, environment reconcile | CI maintainers and release operators |
| `scripts/testdata/pool_pressure/README.md` | Reference | Provenance of the fixtures behind `scripts/test_pool_pressure.py`: which artifacts are real captures and which are hand-built, why the build logs are truncated to the head the check actually reads, why `breach/boskos.json` holds an exhausted pool, and how to re-capture a build. | Fixture provenance, truncation, re-capture | Developers changing the pool-pressure check |
| `terraform/examples/ci-pool-minter/README.md` | Component README | Additive root composition provisioning the GitHub token minter's GCP half in one Prow evaluation-pool project, so its GitOps scenarios can mint a token scoped to that project's repository; documents the App-installation and KMS PEM-import steps Terraform cannot take. | Per-project minter, one repo per lease, manual steps | CI engineers |
| `terraform/examples/full-install/README.md` | Component README | Single-apply root composition of the modules plus a helm_release of the canonical chart — the install engine `./install.sh` drives via `lifecycle.sh`; covers image-tag overrides, Chat/GitHub integration wiring, teardown order. | Install engine, composed values | Infrastructure engineers |
| `terraform/modules/gke-cluster/README.md` | Component README | Reusable Terraform module for the GKE cluster hosting Kube-Agents: Autopilot or Standard (`cluster_mode`), existing-cluster mode, optional gVisor pool and CMEK. | Cluster shapes, Workload Identity, CMEK | Infrastructure engineers |
| `terraform/modules/kube-agents-iam/README.md` | Component README | Reusable Terraform module for provisioning the agent's GSA, Workload Identity binding, and read-only IAM role set. | GCP IAM, Workload Identity, role grants | Infrastructure engineers |
| `terraform/modules/chat-pubsub/README.md` | Component README | Reusable Terraform module for the Google Chat inbound backend: events topic/subscription, both service-identity registrations, publisher/subscriber IAM. | Chat Pub/Sub, service identities, IAM | Infrastructure engineers |
| `terraform/modules/github-minter/README.md` | Component README | Reusable Terraform module for the GitHub token-minter identity: minter GSA, Workload Identity binding, import-only KMS signing key (the one-shot PEM import via the Minty CLI is documented there and run by `install.sh`). | Minter GSA, KMS asymmetric key, WI | Infrastructure engineers |
| `terraform/modules/gke-backup-plan/README.md` | Component README | Reusable Terraform module for the scheduled Backup for GKE BackupPlan covering the release namespace; opt-in for cost reasons. | BackupPlan, retention, CMEK, cost | Infrastructure engineers |
| `terraform/modules/drift-pubsub/README.md` | Component README | Reusable Terraform module for the drift detector's audit-log ingress: Log Router sink, drift-audit topic and pull subscription, sink-writer and subscriber IAM; not yet part of the full-install composition. | Audit-log sink, Pub/Sub, writer identity | Infrastructure engineers |
| `tests/e2e/README.md` | Component README | The pytest E2E suite for the Google Chat integration and its hybrid auth flow (service-account posting + test-account polling via Pub/Sub event injection). | Hybrid auth, Pub/Sub injection, CI setup | CI maintainers |
| `tests/integration/README.md` | Component README | The integration seam tier: real components wired together with the agent replaced by a fake — the contract, how to add a seam test, and what changed when the tier left probation to gate inside `PYTHON_TEST_DIRS`. | Seam tests, `make test-integration`, expectedFailure pins | Developers adding seam tests |

## 5. Keeping this map fresh

- This file is **hand-maintained**. When a PR adds, moves, renames, or deletes
  a Markdown document that no existing glob covers, update the tree and the
  inventory row in the same PR. A file that lands inside a collapsed family
  needs nothing here — only `make docs-generate`, which re-snapshots the
  roster.
- **The edit a PR makes here should be the rows it actually adds — nothing
  else.** No count to bump, no column to re-align: a document-adding PR's diff
  against this file is normally a single inserted line, which is what lets two
  such PRs merge without conflicting. A diff that rewrites rows it did not
  author is a review finding, not a tidy-up.
- `make docs-check` mechanically guards **presence and shape**:
  `docs-check-map` fails CI when a tracked `.md`/`.mdx` file outside a
  root-level dot-directory has no inventory entry here, when a path in the
  inventory's path column no longer exists, or when a table row has been
  re-padded. It deliberately checks no counts — see section 1. The prose
  summaries, key-topic cells, and the identifier-sources table have no
  mechanical guard; PR review (and the drift skill) is the only check on their
  honesty.
- **A file deleted from inside a collapsed family is caught by
  [`family-roster.txt`](family-roster.txt), not by this file.** A glob keeps
  matching its survivors, so the inventory row still reads true after one of
  its documents disappears. The roster is the snapshot that notices:
  `docs-check-generated` fails until it is regenerated, and the removed path
  then appears as a deleted line in the PR diff. A PR that removes a family
  member runs `make docs-generate` and says whether the row still describes
  what is left.
- The `review-docs-drift` skill in `.agents/skills/` consults this map to find
  which docs a code change should have updated, and checks the map itself for
  staleness — keep the summaries honest. CI's docs-freshness workflow
  additionally nudges PRs that change code without touching any docs.
- The map carries **no "last verified at commit X" stamp, deliberately** — a
  stored hash is a second source of truth that goes stale the moment anything
  merges without it, and it conflicts on every concurrent PR. Git already
  records when the map was last touched; to see everything that changed since
  then, derive the delta instead of trusting a stamp:

  ```bash
  git diff --name-status "$(git log -1 --format=%H -- docs/README.md)"..HEAD -- '*.md' '*.mdx'
  ```
