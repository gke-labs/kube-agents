# Documentation map

This file is the map of the Markdown documentation in the `kube-agents`
repository: what lives where, which files carry machine-generated regions and
from which sources, and which source file owns each identifier the docs state
as fact. It serves human contributors and AI agents alike — in particular, the
PR docs-drift review consults it to find which sources a code change should
have re-verified. What each document covers is the document's own business:
its title, its first paragraph, and the page that links it.

The documentation **rules** — the canonical-home table ("every fact has one
home"), the generated-regions rule, link-don't-summarise, verify identifiers
against source — are owned by [`AGENTS.md`](../AGENTS.md) at the repository
root. This file is the **map**, not the rulebook; read `AGENTS.md` before
editing any doc.

## 1. Directory overview

Dot-directories at the repository root (`.agents/`, `.github/`, `.claude/`)
hold tooling — review skills, agent rules, PR templates, agent config — not
documentation; they are out of the map's scope, and the link check's
linked-from-somewhere rule (section 2) does not require them to be linked.
`.agents/rules/` is the one the canonical-home table in `AGENTS.md`
points at, so a rule's home is found through that table rather than through
this map. `.claude/skills` and `.claude/rules` are relative symlinks into
`.agents/`, not copies, so Claude Code reads the same files every other
harness does; [`tests/test_skill_discovery.py`](../tests/test_skill_discovery.py)
holds them to that.

```text
kube-agents/
├── README.md, INSTALL.md, CONTRIBUTING.md,        project front door, install
│   AGENTS.md, CLAUDE.md                           guide, contributor/agent rules
├── a2a/                                           A2A bus: the persona README +
│                                                  the mode-gated a2a-topics skill
├── agents/                                        agent blueprints (runtime docs)
│   ├── chat/                                      Planning Agent front door: persona
│   │                                              docs, onboarding templates,
│   │                                              plugin design READMEs
│   ├── cluster/                                   Cluster Agent profile TEMPLATE:
│   │                                              persona docs + runtime-debugging
│   │                                              SKILL.md bundles
│   ├── contributor/                               Contributor-agent protocol (claim/PR/review loop)
│   └── platform/                                  Platform Agent profile
│       ├── AGENTS.md, SOUL.md, CAPABILITIES.md    persona and workspace docs
│       ├── docs/                                  runtime references (glossary,
│       │                                          console links) + design docs
│       ├── governance/                            cron-run SOP playbooks + the
│       │                                          first-run inventory-scan and
│       │                                          report-prioritization SOPs
│       └── skills/                                SKILL.md bundles + the
│                                                  gke-compute-classes references
├── a2a/docs/                                      design notes kept beside the A2A
│                                                  bus Go module they describe
├── bench/                                         devops-bench evaluation harness README
│                                                  + the task/harness authoring how-to
├── charts/                                        canonical Helm charts (kube-agents)
├── docs/                                          human documentation
│   ├── README.md                                  this map
│   ├── architecture/                              END-STATE spec set 01–09 + README
│   ├── designs/                                   per-feature design documents
│   ├── ci-pool-projects.md, environment-reconcile.md,
│   │   security-requirements.md, credential-isolation-design.md,
│   │   eval-gate-roster.md,
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

Three regions are **generated, not hand-written**. `scripts/generate_docs.py`
(run via `make docs-generate`) rewrites everything between the markers;
everything outside them is hand-written. Never edit inside the markers — edit
the source and regenerate.

<!-- prettier-ignore -->
| Generated file or region | Block marker | Source of truth |
| --- | --- | --- |
| `docs/site/src/content/docs/reference/cron-jobs.md` | `<!-- BEGIN GENERATED: cron-jobs -->` | `agents/chat/defaults/cron/jobs.json` and `agents/platform/cron/jobs.json` |
| `docs/site/src/content/docs/skills/index.mdx` | `{/* BEGIN GENERATED: skill-catalog */}` (MDX comment syntax) | `name`/`description` frontmatter of every `agents/platform/skills/*/SKILL.md` and `agents/cluster/skills/*/SKILL.md` |
| `docs/site/src/content/docs/deploy/docker-images.md` | `<!-- BEGIN GENERATED: container-images -->` | `images.json` |

CI enforcement: `make docs-check` runs the same checks as
`.github/workflows/docs-check.yml` —

- `docs-check-generated` — `scripts/generate_docs.py --check`; fails if a
  generated region no longer matches its source.
- `docs-check-links` — `scripts/check_docs_links.py`; relative links must
  resolve to **git-tracked** targets, and a `docs/designs/…` or
  `docs/architecture/…` path cited from a code or configuration file (Python,
  Go, shell, Dockerfiles, YAML, Terraform, TypeScript) must be one too. The
  same script holds the **linked-from-somewhere rule**: every tracked
  `.md`/`.mdx` outside a root-level dot-directory must be reached by a
  relative link, by a repository blob URL (the generated skill catalogue's
  form), or by such a citation. Exempt by shape, because a reader reaches them
  without a link: files at the repository root, any `README.md` (its directory
  reaches it), the published site under `docs/site/src/content/docs/`
  (Starlight's sidebar reaches every page), and the uniform families the script
  names by glob — the agents' personas, SOPs, skills and skill references, the
  onboarding templates, the forge fixture READMEs and the GitOps template's
  per-directory documents. The documents that were unlinked when the rule
  arrived are named in the script's allowlist, which only shrinks: an entry
  that gains a link or is deleted fails the check until it is dropped. A new
  document is linked from the page that owns its topic, not listed anywhere.
- `docs-check-terminology` — `hack/check-docs-terminology.sh`; identifiers in
  prose must match their source (service-account names, versions, the
  fleet-audit finding-id pattern and rendering caps, …), and a fenced roster
  entry, found by `hack/scan-cron-prompts.awk`, must carry a real job `"id"` and
  quote enough of its prompt, verbatim, to identify it in `jobs.json` — or a
  placeholder id in angle brackets, which marks an illustration and is left
  ungraded.
- `docs-check-audience` — `scripts/check_docs_audience.py`; no page under
  `docs/site/src/content/docs/` may match a shape in
  `scripts/docs_audience_denylist.txt` (workflow secret and variable names, App
  and installation IDs, the maintainers' Prow project, internal repositories),
  name a project ID `hack/ci-env.sh` exports (the evaluation pool), or name a
  service-account email in a non-placeholder project; it also fails when it
  finds no site page or derives no project ID. The rule is
  `.agents/rules/documentation.md`.
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
| Kubernetes service-account names | `scripts/installer/common.sh` |
| GCP service-account names an install creates, release namespace, GKE CMEK key ring and key | `install.defaults.env` |
| Defaults an install gets for saying nothing (region, cluster, permission set, registry prefix) | `install.defaults.env` |
| Go toolchain version | `k8s-operator/go.mod` (and `a2a/go.mod`, kept in step) for building the operator; `scripts/installer/min_versions.sh` (`MIN_GO_VERSION`) for the host that imports the GitHub App key, which builds the Minty CLI and not the operator |
| The drift audit subscription's name | `subscription_name` in `terraform/modules/drift-pubsub/variables.tf`, mirrored by `defaultSubscriptionName` in `k8s-operator/cmd/drift-detector/main.go` |
| The drift batch join budget and the ack deadline it must fit inside | `defaultBatchJoinBudget`, `batchJoinBudgetCeiling` and `maxBudgetShareOfAckDeadline` in `k8s-operator/cmd/drift-detector/subscriber.go`; `ack_deadline_seconds` in `terraform/modules/drift-pubsub/variables.tf`. The detector reads the deadline at startup and warns when the budget takes more than half of it, but does not adopt it, so a doc stating one states both |
| A2A wire constants: protocol version, stream names, size thresholds, token grammar | `a2a/lib/envelope.go` and `a2a/lib/topics.go` |
| A2A subject grammar: the task subject classes (`in`, `events`, `supervisor`), their constructors and the parse that recovers addressee, taskId and class | `a2a/lib/client.go` |
| The A2A gateway process's env (backend selection, gchat relay and allowlist, display mode, addressee) | `a2a/gateway/config.go` (`FromEnv`) |
| Credential-proxy relay env vars, audiences, and route roles | reader `agents/platform/scripts/credential_proxy.py` (`serve`, `build_authenticator`, `ROUTE_ROLES`); the audience values are written by `k8s-operator/internal/controller/platformagent_broker_split.go` |
| The bus principal set: every NATS user the A2A fabric issues or renders, which are static and which authenticate through the callout, and the subject grants each one gets | `k8s-operator/internal/controller/platformagent_a2a_identities.go` (the rendered map and the surviving static users), `k8s-operator/internal/controller/platformagent_a2a_manifests.go` (the `a2a*JetStreamGrants` functions each identity's JetStream API subjects are built from) and `a2a/authcallout/session.go` (the per-session grants, which are in no map) |
| The bus token contract: the audience a bus token must carry, the path the projected volume delivers it on, and its expiry | `a2a/lib/credentials.go` (client side); `A2ABusTokenAudience` and the bus Secret names in `k8s-operator/api/v1alpha1/common_types.go` (shared by the webhook and the render); `k8s-operator/internal/controller/platformagent_a2a_callout.go` (the path and expiry the operator projects); the two sides must agree or every client is refused at connect |
| The name of the env var carrying the agent container's bus principal (`A2A_BUS_USER`) | `a2aBusUserEnv` in `k8s-operator/internal/controller/platformagent_a2a_identities.go` (writer) and `lib.EnvBusUser` in `a2a/lib/credentials.go` (reader, via `busUser()` in `a2a/cmd/a2a/main.go`); the two must agree or the CLI finds no identity, falls through to an unset `NATS_USER`, and exits `no bus identity` before it dials |
| The A2A JetStream stream limits: each stream's subjects, retention, byte cap, `max_consumers` and `max_msgs_per_subject`, including the ones derived from `spec.harness.tuning.maxSessions` | `k8s-operator/internal/controller/platformagent_a2a_manifests.go` (the rendered provision script and the constants above it); `docs/designs/spec-nats-deployment.md` argues the numbers but does not set them |
| Minimum supported tool versions (`gcloud`, `terraform`, `go`) | `scripts/installer/min_versions.sh` |
| Toolsets, plugins, and MCP servers of an agent profile | that profile's `config.yaml` (`agents/platform/`, `agents/chat/`, `agents/cluster/`) |
| Cron job rosters and schedules | `agents/chat/defaults/cron/jobs.json` and `agents/platform/cron/jobs.json` |
| Persona rules and `§N` section numbering | the profile's `SOUL.md` |
| RBAC bindings and KSA defaults laid down per agent | `k8s-operator/internal/controller/platformagent_manifests.go` |
| `app.kubernetes.io/*` label values on installed objects | `k8s-operator/internal/controller/manifest_helpers.go`, each `kustomization.yaml`, and `a2a/gateway/spawn.go` (gateway-spawned session pods) |
| The mode switch's key, values, and skew reason (`KUBEAGENTS_MODE`, `today`/`next`, `ModeNotRecognized`) | `k8s-operator/internal/controller/mode.go` and `platformagent_manifests.go` (writer), `agents/platform/scripts/runtime_mode.py` (reader) |
| Controller permissions | `k8s-operator/config/rbac/` |
| The operator RBAC self-check: the `RBACIncomplete` reason, its re-check interval and condition message; the floating tags `make deploy` refuses and `ALLOW_MUTABLE_IMG` | `k8s-operator/internal/controller/rbac_selfcheck.go`; `k8s-operator/Makefile` |
| `make` targets | the root `Makefile` and `k8s-operator/Makefile` |
| The third-party download retry rule: which files are walked and what flags a curl line must carry | `DOWNLOAD_SOURCES`, `RETRY_COUNT` and `RETRY_ALL_ERRORS` in `tests/test_third_party_download_retry.py` |
| Harness discovery of this repository's own skills: the `.claude/*` symlink targets, and the floor a lifecycle skill's description must clear | `CLAUDE_LINKS`, `LIFECYCLE_ACTIONS` and `PRODUCT` in `tests/test_skill_discovery.py` |
| The GitHub environment variables an install is configured from, which install.env key each becomes, and which are required to reconcile a long-lived environment | `MAPPING`, `REQUIRED_ALWAYS` and `REQUIRED_STRICT` in `scripts/release/render_install_env.sh` |
| Paths baked into the agent image (`/opt/defaults/...`) | `deploy/docker/Dockerfile` |
| The maintainers' CI project IDs, which `docs-check-audience` forbids on the site | `hack/ci-env.sh` (the `PROJECT_ID` export) |
| Image-patch module names and the behaviour they add | the module's own docstring under `deploy/docker/patches/`, plus the `COPY`/`RUN` list in `deploy/docker/Dockerfile` |
| Bundled Hermes platform plugins the image installs (no patch) | the plugin's own `adapter.py` docstring under `deploy/docker/plugins/`, plus the `COPY`/`RUN` list in `deploy/docker/Dockerfile` |
| Slack bot token scopes an install must grant | upstream `_build_full_manifest` in `hermes_cli/slack_cli.py` as patched by `deploy/docker/patches/apply_slack_reactions_scope.py`; the one prose copy, in `INSTALL.md`, must match it (`scripts/installer/print_instructions_slack.sh` defers to `hermes slack manifest` and carries no copy) |
| What pod start-up force-syncs from the image vs. preserves on the PV | `deploy/shared/docker-entrypoint.sh` |
| Shared agent defaults (`approvals.*`, `security.*`) | `deploy/shared/defaults/config.yaml` and `renderConfigYAML()` in `k8s-operator/internal/controller/platformagent_manifests.go` |
| Image defaults and override env vars (`PLATFORM_AGENT_IMAGE` et al.) | `k8s-operator/internal/controller/manifest_helpers.go` |
| OTLP endpoint default, discovery candidates, and `otlpEndpointSource` values | `k8s-operator/internal/controller/telemetry.go` |
| The `secret-env-hash` pod-template annotation and its re-read interval | `k8s-operator/internal/controller/platformagent_secret_hash.go` |
| DNS/metadata-daemon defaults, the `dnsClusterIPsSource` / `metadataDaemonIPSource` values, and the `additionalEgress` prefix floors (`/12`, `/48`) | `k8s-operator/internal/controller/netpolprofile.go` and `platformagent_controller.go` |
| Agent egress-allowlist policy: metadata addresses, the `-sandbox-metadata-deny` name, the `controlPlaneCIDRs` floors (`/16`, `/32`), and the `EgressAllowlistRefused` reason | `k8s-operator/internal/controller/platformagent_egress_policy.go` and `platformagent_controller.go` |
| LiteLLM egress policy: `litellm-policy`, the `enable-litellm-network-policy` and `otlp-collector-namespace` annotations, the external-endpoint port-443 check, and the `platformAgent.annotations` conflict rule | `k8s-operator/internal/controller/platformagent_litellm_policy.go`, `charts/kube-agents/templates/_helpers.tpl`, `charts/kube-agents/templates/platform-agent-cr.yaml` |
| Scoped service-account pool: `CREDENTIAL_PROXY_SCOPED_SA_POOL{,_FILE}`, the pool file path, the scope-key spelling, and the `ka-<name>-<hash8>` account ids | `agents/platform/scripts/scoped_sa_pool.py`, `k8s-operator/internal/controller/platformagent_manifests.go`, `terraform/modules/kube-agents-iam/scoped_pool.tf` |
| Image inventory: every image an install pulls, and its upstream pin | `images.json` |
| Registry prefix defaults (`REGISTRY_PREFIX`, `THIRD_PARTY_REGISTRY_PREFIX`) | `install.defaults.env` |
| Provisioning image-tag attachment (`qualify_image_ref`) | `scripts/installer/common.sh` |
| GKE host-discovery label | `scripts/installer/common.sh` |
| GitOps clone layout (`/opt/data/gitops/...`) and leases | `agents/platform/scripts/gitops_workspace.py` |
| Repository-identity rules: accepted GitHub hostnames, path depth, segment grammar, length bound, and the `GIT_REPO_UNPARSEABLE` reason | `agents/platform/scripts/repo_ref.py` |
| The gitops-state ConfigMap's keys (`managed_repos`, `context_repos`) and which one each consumer reads | `agents/platform/scripts/gitops_workspace.py` (readers), `repository_role` in `agents/platform/scripts/credential_proxy.py` (reads `context_repos` for the clone credential), and `reconcileGitopsStateConfigMap` / `syncGithubTokenMinterConfigMap` in `k8s-operator/internal/controller/platformagent_controller.go` (the `managed_repos` seed; both keys for the minter policy) |
| Chat platforms an install posts to, the order, and the fallback | `agents/platform/scripts/chat_platforms.py` |
| Which deliverables a Google Chat thread gets pasted inline, the size ceiling, and the per-message budget | `agents/platform/scripts/google_chat_relay_patch.py` |
| Staging a sandbox-written artifact out for delivery: the per-file, per-card and total ceilings, the deadline, and the denied prefixes | `agents/platform/scripts/sandbox_artifact_patch.py` |
| fleet-audit finding-id pattern and rendering caps | `agents/platform/skills/fleet-audit/scripts/audit_report.py` |
| fleet-upgrade-verification record path, file name per target, record format version, readiness flags and exit codes, kubeconfig directory and file name, and the readiness cell strings | `agents/platform/skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py` and `upgrade_readiness.py` beside it |
| Chat-delivery watch: the `ALERT chat_delivery_watch` log prefix and file, the ledger issue's label and marker, the streak state path, and the `CHAT_DELIVERY_*` environment variables | `agents/platform/scripts/chat_delivery_watch.py` |
| Helm chart value defaults (KSA/secret names, image repos, tag rules) | `charts/kube-agents/values.yaml`; the accepted key set and types, `charts/kube-agents/values.schema.json` |
| What a default install reserves (per-workload CPU, memory, pods, claims) | `charts/kube-agents/files/footprint.yaml` for the operator-rendered pods, generated by `scripts/generate_chart_footprint.py`; `charts/kube-agents/values.yaml` for the chart's own |
| Release tag families (`rc_*`, `rc_*_validated`, `evalcand_<ts>_<sha>`, `staging_<ts>_<sha>`, GA `X.Y.Z`) and the shared lookups over them | `scripts/release/common.sh` |
| GA release gate: its conditions, exit codes, dispatch modes, and step outputs | `scripts/release/resolve_scheduled_release.sh`, `scripts/release/decide_release_gate.sh`, `.github/workflows/release-publish.yml`, and `.github/workflows/release-scheduler.yml` |
| Stock `PlatformAgent.metadata.name` used as the admin-console installation ID | `charts/kube-agents/values.yaml` (`platformAgent.name`) |
| Terraform module defaults (GSA/KSA/namespace, role set, channel) | `terraform/modules/*/variables.tf` |
| Memory bank name, scope-tag spelling, and provider name | `agents/chat/plugins/memory/kube_agents_memory/config_schema.py` |
| Per-profile Hindsight recall settings the agent uses | `agents/chat/defaults/hindsight/config.json`, `agents/platform/hindsight/config.json` |
| Hindsight endpoint (`HINDSIGHT_API_URL`, derived from the namespace) | `k8s-operator/internal/controller/platformagent_manifests.go` |
| Admission webhook server port (`--webhook-port` default) | `DefaultPort` in `k8s-operator/internal/webhook/platformagent_webhook.go` |
| Live-test lease: ConfigMap name, TTL, install-configuration keys read, which commands count as mutations | `scripts/live_test_lease.py` |
| PR evidence screenshots: publish branch, file-name provenance, caption format | `scripts/pr_evidence_screenshot.sh` |
| Unresolved-thread hold: the label, the pool condition, the sweep interval, the ownership rule | `scripts/hold_unresolved_threads.py` and `.github/workflows/hold-unresolved-threads.yml` |
| Issue triage queue: the `needs-triage` label, the `priority:` label prefix it mirrors, and when each event adds or removes it | `.github/workflows/needs-triage.yml` |
| Flaky-check tracking: the `ci:flaky` label, the watched checks and the exclusions, the one-issue-per-container key, the never-close rule | `scripts/notify_flaky_check.py` and `.github/workflows/flaky-check-notify.yml`; the exclusion list the contract test enforces is `FLAKY_CHECK_EXCLUDED_WORKFLOWS` in `scripts/test_integration_contracts.py` |
| Reviewer auto-assign: the skip reasons, the `OWNERS`-approver verdict rule, the `/request-review` reactions | `scripts/request_reviewers.py` and `.github/workflows/auto_request_review.yml` |
| Broken-main tracking: the `ci:main-broken` label, the watched workflows, the one-issue-per-episode marker, the sweep interval, the dismissal, older-episode-only and whole-read rules | `scripts/notify_broken_main.py` and `.github/workflows/main-broken-notify.yml`; the required-check roster the contract test enforces is `BROKEN_MAIN_WATCHED_WORKFLOWS` in `scripts/test_integration_contracts.py` |
| Context budget for the always-loaded agent instruction files (`AGENTS.md`, `CLAUDE.md`) | `BUDGET` in `scripts/check_context_budget.py` |
| Who may set the `approved` label on a change | `OWNERS`, `hack/OWNERS`, and `OWNERS_ALIASES` |
| Which labels Tide merges on, and which Prow presubmits gate | `prow/oss/config.yaml` and `prow/prowjobs/gke-labs/kube-agents/` in `GoogleCloudPlatform/oss-test-infra` — not a file in this repository |
| Contributor-agent merge labels (`lgtm`, `approved`, `ok-to-test`, `do-not-merge/hold`) and the `triage` permission grant | external tide automation and GitHub repo settings (not in-tree); named in `AGENTS.md` and `agents/contributor/AGENTS.md` |
| Queue-wait thresholds that justify onboarding an eval project, the window they run over, and the JUnit row names and metric property the TestGrid tab reads | `scripts/pool_pressure.py` |
| Presubmit-gate health rules (windows, thresholds, hysteresis), the Chat posting variables and the digest hour | `scripts/eval_dashboard/health.py`, `scripts/eval_dashboard/post_health.py` and `.github/workflows/ci-health.yml` |
| The eval dashboard's roster-page contract (the `demoted YYYY-MM-DD` phrase inside a `- **case-name** —` hold-out bullet), its page files and its URL parameter vocabularies | `ROSTER_ENTRY_RE` / `DEMOTED_RE` and `PAGES` in `scripts/eval_dashboard/render.py`; `linkState()` in `scripts/eval_dashboard/template/pages.js` |
| Testing-domain slugs a bench case may claim | `docs/designs/domains.yaml` |
| Seeded-fleet fixture role names and the cluster slot each lives on | `bench/tf/fleet/fixtures.json` |
| Day-N availability gate per fixture, and the project-scoped fixtures that sit on no cluster | `docs/designs/fleet-fixtures.yaml`, which overlays `fixtures.json` and may not rename a role |
| Credential-proxy refusal rule ids, refused flags, forced git config | `agents/platform/scripts/credential_proxy.py`; the `gcp.api.*` relay rule ids and the relayed-read table in `agents/platform/scripts/api_policy.py` |
| Command-policy allowlisted verbs and denied `kubectl`/`gcloud` flags | `agents/platform/scripts/command_policy.py` |
| Gateway redaction: rule actions, marker formats, pseudonym length, rule-name grammar, and the `KUBE_AGENTS_REDACTION_CONFIG` variable | `agents/chat/defaults/plugins/common/redactor.py` (canonical; `charts/kube-agents/files/redactor.py` is its checked mirror, and `GCP_OAUTH_TOKEN_PATTERN` in `deploy/docker/patches/credential_redaction.py` is a second, tested against it) and `charts/kube-agents/files/litellm_redaction_callback.py` |
| Which CI pool project maps to which GitOps repository | `gitops_repo_for_project()` in `hack/ci-deploy.sh` |
| Roles the pool verifier accepts as Artifact Registry upload rights, and the API set it requires | `scripts/verify_ci_pool_project.py`, whose `VALID_CMEK_STATES` mirrors `is_valid_cmek_encryption_state()` in `scripts/installer/installer_common.sh`, whose `PLATFORM_GSA_ROLES` mirrors `local.read_only_roles` in `terraform/examples/full-install/main.tf`, whose `FLEET_READER_TOKEN_CREATORS` mirrors `bench/tf/fleet`'s `fleet_reader_token_creators` default, and whose cluster names mirror `bench/tf/fleet` and whose fixture check parses the summary lines `hack/fleet-kubeconfigs.sh` and `hack/fleet-fixture-state.py` print, whose signing probe reads `githubMinter.kms.keyVersion` from `charts/kube-agents/values.yaml`, whose `LEDGER_APP_ID`/`LEDGER_INSTALLATION_ID` mirror the `EVAL_LEDGER_*` defaults in `hack/ci-eval-pr.sh`, and whose mapping check reads `hack/ci-deploy.sh` from `gke-labs/main` as well as the local tree |
