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
│   │   eval-gate-roster.md, ci-health.md, testing-map.md, ownership.md,
│   │   pull-request-workflow.md                   standalone docs
│   ├── chatops/, samples/                         the Teams integration and its
│   │                                              sample manifests
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

The artifacts below are **generated, not hand-written** — regions inside
hand-written documents (a region may be spliced into several pages), and the
line-number pins inside the governance cron prompts. `scripts/generate_docs.py`
(run via `make docs-generate`) rewrites everything between the markers;
`scripts/generate_sop_geography.py` (run first by the same target) rewrites the
pins' digits and nothing else. Everything outside the markers and the pins is
hand-written. Never edit inside the markers — edit the source and regenerate.

<!-- prettier-ignore -->
| Generated file or region | Block marker | Source of truth |
| --- | --- | --- |
| `docs/site/src/content/docs/reference/cron-jobs.md` | `<!-- BEGIN GENERATED: cron-jobs -->` | `agents/chat/defaults/cron/jobs.json` and `agents/platform/cron/jobs.json` |
| `docs/site/src/content/docs/concepts/autonomous-watchdogs.md`, `docs/site/src/content/docs/concepts/skills.md`, `docs/site/src/content/docs/reference/cron-jobs.md` | `<!-- BEGIN GENERATED: cron-job-example -->` | The `compliance-audit` entry of `agents/platform/cron/jobs.json`, rendered as fenced JSON |
| `docs/site/src/content/docs/skills/index.mdx` | `{/* BEGIN GENERATED: skill-catalog */}` (MDX comment syntax) | `name`/`description` frontmatter of every `agents/platform/skills/*/SKILL.md` and `agents/cluster/skills/*/SKILL.md` |
| `docs/site/src/content/docs/deploy/docker-images.md` | `<!-- BEGIN GENERATED: container-images -->` | `images.json` |
| `agents/platform/cron/jobs.json` — the SOP length and checks-section line range each governance prompt cites | none: `scripts/generate_sop_geography.py` rewrites the digits in place (`make docs-generate` runs it first) | The `agents/platform/governance/*_sop.md` each prompt names |

CI enforcement: `make docs-check` runs the same checks as
`.github/workflows/docs-check.yml` —

- `docs-check-generated` — `scripts/generate_sop_geography.py --check` and
  `scripts/generate_docs.py --check`; fails if a generated region or prompt
  line pin no longer matches its source.
- `docs-check-links` — `scripts/check_docs_links.py`; relative links must
  resolve to **git-tracked** targets, and a `docs/designs/…` or
  `docs/architecture/…` path cited from a code or configuration file (Python,
  Go, shell, Dockerfiles, YAML, Terraform, TypeScript) must be one too. The
  same script holds the **reachability rule**: every tracked `.md`/`.mdx`
  outside a root-level dot-directory must be reachable from where a reader
  starts. The starting points are files at the repository root, any
  `README.md` (its directory reaches it), the tooling under the root
  dot-directories, the site pages Starlight's sidebar lists (read from
  `docs/site/astro.config.mjs`: every page under an autogenerated directory,
  every page a sidebar entry names, and the 404 page), the uniform families the script
  names by glob — the agents' personas, SOPs, skills and skill references, the
  onboarding templates, the GitOps template's per-directory documents and the
  integrity sweep's report and adjudication beside each committed bench run
  record — and every design document a code file cites. From there reach follows
  relative links, site routes and repository blob URLs (the generated skill
  catalogue's form), written as a Markdown link, a reference-style definition,
  an autolink or an `href` attribute (the site's hub pages link by
  `<LinkCard href=...>`). A document linked only from documents no reader reaches
  is reported like one linked from nowhere. The documents that were
  unreachable when the rule arrived are named in the script's allowlist, which
  only shrinks: an entry that becomes reachable, becomes exempt by shape, or is
  deleted fails the check until it is dropped. A new document is linked from
  the page that owns its topic, not listed anywhere.
- `docs-check-terminology` — `hack/check-docs-terminology.sh`; identifiers in
  prose must match their source (service-account names, versions, the
  fleet-audit finding-id pattern and rendering caps, …).
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
| Seeded-fleet reconcile report keys and outcomes, fixture-state role states, the delete/replace allowlist | `hack/fleet_reconcile.py` (`OUTCOMES`, `write_report`), `scripts/eval_dashboard/fixture_state.py` (`ROLE_STATES`), `bench/tf/fleet/reconcile-allow.json` |
| GCP service-account names an install creates, release namespace, GKE CMEK key ring and key | `install.defaults.env` |
| Defaults an install gets for saying nothing (region, cluster, permission set, registry prefix) | `install.defaults.env` |
| Content-workspace ceilings and reclaim (`CREDENTIAL_PROXY_MAX_WORKSPACES`, `CREDENTIAL_PROXY_MAX_CLONE_BYTES`, `CREDENTIAL_PROXY_WORKSPACE_IDLE_SECONDS`) | `DEFAULT_*` and `_limit` in `agents/platform/scripts/content_workspace.py` |
| The credential proxy's child memory budget terms (the broker and Envoy resident reserve, the content workspace reserve, the per-request child reserve, the output copies per command, the fewest requests a usable budget admits) | `BROKER_RESIDENT_RESERVE_BYTES`, `CONTENT_WORKSPACE_RESERVE_BYTES`, `REQUEST_CHILD_MEMORY_RESERVE_BYTES`, `OUTPUT_COPIES_PER_COMMAND` and `BUDGET_MINIMUM_ADMITTED_REQUESTS` in `agents/platform/scripts/credential_proxy.py`; the operator's `credentialProxy*ReserveBytes`, `credentialProxyOutputCopiesPerCommand` and `credentialProxyMinimumAdmittedRequests` in `k8s-operator/internal/controller/credential_proxy_manifests.go` must match them, held equal by `tests/test_credential_proxy_sizing_parity.py`, which also holds the three declared copies of the memory floor at the default output cap equal (`CHILD_MEMORY_BUDGET_FLOOR_BYTES_AT_DEFAULT_CAP`, `credentialProxyMemoryFloorBytesAtDefaultCap` and the chart's `kube-agents.credentialProxyMemoryFloorBytes`; the broker's and the operator's own tests hold theirs to their formula) and the chart's copies of the proxy's default requests and limits (`kube-agents.credentialProxyDefaults` in `charts/kube-agents/templates/_helpers.tpl`) to the operator's |
| Go toolchain version | `k8s-operator/go.mod` (and `a2a/go.mod`, kept in step) for building the operator; `scripts/installer/min_versions.sh` (`MIN_GO_VERSION`) for the host that imports the GitHub App key, which builds the Minty CLI and not the operator |
| The drift audit topic, subscription and sink names | `topic_name`, `subscription_name` and `sink_name` in `terraform/modules/drift-pubsub/variables.tf`; `drift_pubsub_topic`, `drift_pubsub_subscription` and `drift_pubsub_sink` in `terraform/examples/full-install/variables.tf` default to the same strings, and `defaultSubscriptionName` in `k8s-operator/cmd/drift-detector/main.go` mirrors the subscription's, and `EVAL_DRIFT_SUBSCRIPTION` in `hack/ci-deploy.sh` restates it for the eval install; `tests/test_drift_subscription_wiring.py` and `tests/test_ci_deploy_drift_detector.py` hold the copies together |
| The two drift switches and the rule between them: the ingress, the consumer, and the fact that the second is refused without the first | `enable_drift_pubsub` and `enable_drift_detector` in `terraform/examples/full-install/variables.tf`, and the `helm_release` preconditions in its `main.tf`; `DEFAULT_ENABLE_DRIFT_DETECTOR` in `install.defaults.env` for the single `install.env` key the front doors write both from; `driftDetectorEnabled` in `k8s-operator/internal/controller/platformagent_manifests.go` for what the operator does with the field afterwards; `tests/test_drift_subscription_wiring.py` pins the composition side |
| The drift batch join budget and the ack deadline it must fit inside | `defaultBatchJoinBudget`, `batchJoinBudgetCeiling` and `maxBudgetShareOfAckDeadline` in `k8s-operator/cmd/drift-detector/subscriber.go`; `ack_deadline_seconds` in `terraform/modules/drift-pubsub/variables.tf`. The detector reads the deadline at startup and warns when the budget takes more than half of it, but does not adopt it, so a doc stating one states both |
| A2A wire constants: protocol version, stream names, size thresholds, token grammar | `a2a/lib/envelope.go` and `a2a/lib/topics.go` |
| Executor terminal reason tokens (`reason: <token>`) and their infrastructure/persona classification | `INFRASTRUCTURE_REASONS` and `PERSONA_REASONS` in `bench/kube_agents_bench/inject_transport.py`; written by `a2a/hermes-bridge/bridge.go` (`failureReason` and the `reason:` constants), `a2a/hermes-bridge/api.go` (`runTaskAPI` and `finalizeAPIError`) and `a2a/worker-adapter/adapter.go`; the lists in `docs/designs/eval-next-transport.md`, `docs/designs/spec-chatops-gateway.md` and `bench/README.md` must agree |
| A2A payload vocabulary: the reserved artifact names (`result`, `thinking`, `activity`, `progress`, `delegate`, `turn`) and the steer-notice outcomes and refusal reason tokens | `Artifact*`, `SteerQueued`/`SteerRefused` and `SteerReason*` in `a2a/lib/payload.go`; the room's wording for each reason is `steerRefusalWhy` in `a2a/gateway/gateway.go`, except `task-ended`, which is counted into the terminal's `noticeSteersUnrun` beside it |
| A2A subject grammar: the task subject classes (`in`, `events`, `supervisor`), their constructors and the parse that recovers addressee, taskId and class | `a2a/lib/client.go` |
| The A2A gateway process's env (backend selection, Slack token pair and allowlist, gchat relay, allowlist and home channel, display mode, addressee, the relay token path it reads, and the A2A door's Google client id and allowlist) | `a2a/gateway/config.go` (`FromEnv`) |
| The operator's mirror of that backend selection for the gateway render: the Chat and Slack arming rules (`a2aChatArmed`, `a2aSlackArmed`) and their legacy complements, the `discord-bot` Secret name, the Slack token and allowlist env names, the `a2a-slack-principal-map` Secret name, the principal-map path and which one table the gateway mounts there (`a2aPrincipalMapVolumeSource`: the Secret alone when Slack is armed, the hand-made `principal-map` ConfigMap otherwise), the broker env names the Chat render sets and the relay token path, the `A2AGateway` condition and its `NoChatBackend` and `WaitingForReplica` reasons, and the `BusProvisioned` record beside them | `a2aGatewayBackend` and the constants beside it in `k8s-operator/internal/controller/platformagent_a2a_manifests.go`; it must agree with `a2a/gateway/config.go` |
| The gateway's `verifiedBy` values, the doors' principal-map key prefixes and eval value namespace, the A2A door's Google caller prefix, and the doors' HTTP paths | `verifiedByFor` in `a2a/gateway/gchat.go` and Slack's two in `a2a/gateway/slack.go`; the prefixes in `a2a/gateway/inject.go`, `a2a/gateway/a2adoor.go` and `a2a/gateway/a2adoor_google.go`; the inject door's paths in `a2a/gateway/inject.go` and the A2A door's in `a2a/gateway/a2awire.go` |
| Credential-proxy relay env vars, audiences, and route roles | reader `agents/platform/scripts/credential_proxy.py` (`serve`, `build_authenticator`, `ROUTE_ROLES`); the audience values are written by `k8s-operator/internal/controller/platformagent_broker_split.go`, and the session audience also by `a2a/gateway/spawn.go`, which projects it |
| Pull-request base pin: the `repositories[].baseBranch` grammar and its refusal on a `context` repository, the `CREDENTIAL_PROXY_PINNED_BASES` JSON shape and its reservation from `spec.deployment.env`, the host-and-path match, and the `TARGET_NOT_BASE`, `BASE_BRANCH_MISSING` and `workspace.base-branch-missing` refusals | `BaseBranch` markers on `RepositorySpec` in `k8s-operator/api/v1alpha1/common_types.go` (the chart's copy of the grammar, its old-CRD guard and the lists form it forces are in `charts/kube-agents/values.schema.json` and `templates/platform-agent-cr.yaml`); `credentialProxyBaseEnv` and `mergeCredentialProxyEnv` in `k8s-operator/internal/controller/platformagent_manifests.go`; `parse_pinned_bases` in `agents/platform/scripts/credential_proxy.py`; `pinned_base` in `agents/platform/scripts/providers/validate.py`; `agents/platform/scripts/vcs_broker.py` and `content_workspace.py` |
| The bus principal set: every NATS user the A2A fabric issues or renders, which are static and which authenticate through the callout, and the subject grants each one gets | `k8s-operator/internal/controller/platformagent_a2a_identities.go` (the rendered map and the surviving static users), `k8s-operator/internal/controller/platformagent_a2a_manifests.go` (the `a2a*JetStreamGrants` functions each identity's JetStream API subjects are built from) and `a2a/authcallout/session.go` (the per-session grants, which are in no map), `k8s-operator/internal/controller/agentprofile_identities.go` (the operator's entry and the AgentProfile entries) and `a2a/authcallout/profile_narrowing.go` (a profile pod's grants, which are in no map either) |
| The bus token contract: the audience a bus token must carry, the path the projected volume delivers it on, and its expiry | `a2a/lib/credentials.go` (client side); `A2ABusTokenAudience` and the bus Secret names in `k8s-operator/api/v1alpha1/common_types.go` (shared by the webhook and the render); `k8s-operator/internal/controller/platformagent_a2a_callout.go` (the path and expiry the operator projects); the two sides must agree or every client is refused at connect |
| The name of the env var carrying the agent container's bus principal (`A2A_BUS_USER`) | `a2aBusUserEnv` in `k8s-operator/internal/controller/platformagent_a2a_identities.go` (writer) and `lib.EnvBusUser` in `a2a/lib/credentials.go` (reader, via `busUser()` in `a2a/cmd/a2a/main.go`); the two must agree or the CLI finds no identity, falls through to an unset `NATS_USER`, and exits `no bus identity` before it dials |
| The chat.notify subjects and the agent's notify-platform env (`A2A_NOTIFY_PLATFORM`) | `a2a/lib/notify.go` (sender and answerer) and `a2aNotifySubjectGchat`/`a2aNotifyReplySubjects` in `k8s-operator/internal/controller/platformagent_a2a_identities.go` (grants); `a2aNotifyPlatformEnvVar` in `platformagent_a2a_manifests.go` (writer) and `NOTIFY_PLATFORM_ENV` in `agents/platform/scripts/chat_notify.py` and in `deploy/docker/patches/kanban_chat_notify.py` (readers); each pair must agree or proactive posts go to a subject nobody holds |
| The names the auth callout refuses as a narrowed pod name: the static principals, and the env var carrying them (`A2A_RESERVED_PRINCIPALS`), plus the users of the identity map being served | `reservedAs` in `a2a/authcallout/reserved.go` (the one check) and `ParseIdentityMap` in `a2a/authcallout/identitymap.go` (builds the map's user set at load); `a2aStaticPrincipalNames` in `k8s-operator/internal/controller/platformagent_a2a_confusers.go` (the one list behind both `auth_users` and the env value); `a2aCalloutReservedPrincipalsEnvVar` in `k8s-operator/internal/controller/platformagent_a2a_callout.go` (writer) and `envReservedPrincipals` in `a2a/cmd/authcallout/main.go` (reader, parsed by `ParseReservedPrincipals` in `a2a/authcallout/reserved.go`); the two names must agree or the callout refuses to start |
| The fixed-name addressees the auth callout refuses as a narrowed pod name, and the env var carrying them (`A2A_RESERVED_ADDRESSEES`) | `a2aReservedAddressees` in `k8s-operator/internal/controller/platformagent_a2a_callout.go` (built from `a2aBridgeAddressee` in `platformagent_a2a_identities.go`); `a2aCalloutReservedAddresseesEnvVar` there (writer) and `envReservedAddressees` in `a2a/cmd/authcallout/main.go` (reader, parsed by `ParseReservedAddressees` in `a2a/authcallout/addressees.go`, and refused by the same `reservedAs` check as the static principals); the two names must agree or the callout refuses to start, and the list must hold the gateway's `A2A_DEFAULT_ADDRESSEE` default (`a2a/gateway/config.go`) and the bridge's `defaultProfile` (`a2a/cmd/hermes-bridge/main.go`) |
| The capability envelope's identifiers: the KV bucket and its stream/subject names, the key grammar and the root/hop key builders, the depth bound, the tier names and the verb-to-tier table, and the verify/reply subject grammar | `a2a/capability/store.go` (`Bucket`, `Stream`, `SubjectPrefix`, `RootKey`, `HopKey`, `MaxDepth`, and the `Minter`), `a2a/capability/verb.go` (the `Tier` values and `verbTier`), `a2a/capability/entry.go` (the entry a verifier reads) and `a2a/capability/service.go` (`ReplyPrefix`, `ReplySubject`, `VerifySubject`). `a2a/gateway/authority.go` (`mintCapability`) is the only code that mints a root, and it fills tier and scope from the gateway's own config rather than from anything about who asked |
| The env the operator writes into an A2A sidecar, CR-authored or rendered (`POD_NAMESPACE`, `A2A_CAPABILITY_REQUIRED`), and which of the two beats a CR value (`A2A_CAPABILITY_REQUIRED` does; `POD_NAMESPACE` is only a default) | `a2aExecutorSidecarEnv` in `k8s-operator/internal/controller/platformagent_a2a_callout.go` (writer; the precedence is `mergeEnvVars` in `manifest_helpers.go`, whose second argument wins), read by `capabilityScope()` in `a2a/cmd/hermes-bridge/main.go` and by the bridge config's capability switch in `a2a/hermes-bridge/` |
| The operator-rendered Hermes bridge: the operator settings `A2A_BRIDGE_IMAGE`, `A2A_BRIDGE_CONCURRENCY` and `A2A_BRIDGE_EXECUTOR`, the image fallback, the agent env names the bridge does not inherit, and the gateway's `A2A_BUSY_NOTICE_AT` rendered from the bridge's worker count | `k8s-operator/internal/controller/platformagent_a2a_bridge.go` (the three `a2aBridge*EnvVar` constants, `a2aBridgeImage`, `a2aBridgeDroppedAgentEnv`, `a2aBusyNoticeAtEnvVar` and `a2aBusyNoticeAt`) |
| The A2A JetStream stream limits: each stream's subjects, retention, byte cap, `max_consumers` and `max_msgs_per_subject`, including the ones derived from `spec.harness.tuning.maxSessions` and the bridge's `BRIDGE_CONCURRENCY` (a declared sidecar's, or the operator's `A2A_BRIDGE_CONCURRENCY` for a rendered bridge) | `k8s-operator/internal/controller/platformagent_a2a_manifests.go` (the rendered provision script and the constants above it); `docs/designs/spec-nats-deployment.md` argues the numbers but does not set them |
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
| The version-control verb surface: which verbs exist, which are broker-side and which are collaboration, and the members of the forge provider protocol | `agents/platform/scripts/providers/base.py` (`BROKER_VERBS`, `COLLABORATION_VERBS`) and `agents/platform/scripts/forge.py` (`ForgeProvider`); the route table in `vcs_broker.py` is what the broker actually serves |
| The maintainers' CI project IDs, which `docs-check-audience` forbids on the site | `hack/ci-env.sh` (the `PROJECT_ID` export) |
| Image-patch module names and the behaviour they add | the module's own docstring under `deploy/docker/patches/`, plus the `COPY`/`RUN` list in `deploy/docker/Dockerfile` |
| Bundled Hermes platform plugins the image installs (no patch) | the plugin's own `adapter.py` docstring under `deploy/docker/plugins/`, plus the `COPY`/`RUN` list in `deploy/docker/Dockerfile` |
| Slack bot token scopes an install must grant | upstream `_build_full_manifest` in `hermes_cli/slack_cli.py` as patched by `deploy/docker/patches/apply_slack_reactions_scope.py`; the one prose copy, in `INSTALL.md`, must match it (`scripts/installer/print_instructions_slack.sh` defers to `hermes slack manifest` and carries no copy) |
| What pod start-up force-syncs from the image vs. preserves on the PV | `deploy/shared/docker-entrypoint.sh` |
| GitHub logins named as area or service owners | `OWNERS` (approvers and reviewers); `scripts/test_ownership.py` pins `docs/ownership.md` to it |
| Shared agent defaults (`approvals.*`, `security.*`) | `deploy/shared/defaults/config.yaml` and `renderConfigYAML()` in `k8s-operator/internal/controller/platformagent_manifests.go` |
| Image defaults and override env vars (`PLATFORM_AGENT_IMAGE` et al.) | `k8s-operator/internal/controller/manifest_helpers.go` |
| The `status.usage.activeInterfaces` vocabulary (`dashboard`, `googlechat`, `slack`, `teams`) and how each is resolved from the spec | `resolveActiveInterfaces` and the `interface*` constants in `k8s-operator/internal/controller/manifest_helpers.go` |
| The event watcher's metrics port, its container-port name and `EVENT_WATCHER_METRICS_PORT`; the chart's `PodMonitoring` for the gateway pod | `eventWatcherMetricsPort`, `eventWatcherMetricsPortName` and `eventWatcherMetricsPortEnv` in `k8s-operator/internal/controller/platformagent_manifests.go`; `charts/kube-agents/templates/platform-agent-monitoring.yaml`, held to the operator's port by `tests/test_chart_platform_agent_monitoring.py` |
| The audit record envelope (`event_type`, `audit_event`, `severity`, `timestamp`), the plugin and broker `status` vocabularies, the broker's `tool_execution_audit` fields, and the fluent-bit input and parser that ship the audit file | `agents/chat/defaults/plugins/common/audit_schema.py`; `_STATUS_*` in `agents/chat/defaults/plugins/tool_call_audit/audit.py`; `AUDIT_STATUS_*`, `_tool_audit` and `LOG_*_KEY` in `agents/platform/scripts/credential_proxy.py`; `fluentBitAuditTailPath` and the `audit_json` parser in `buildFluentBitConfigMap`, `k8s-operator/internal/controller/platformagent_manifests.go`; `AUDIT_FILE_NAME` in `agents/chat/defaults/plugins/common/audit_sink.py`, held equal to the Go name by `TestFluentBitAuditFileNameMatchesTheEmitters` |
| The credential broker's metrics-only port, its container-port name and `CREDENTIAL_PROXY_METRICS_PORT`; the broker's metric names, label vocabularies and histogram buckets | `credentialProxyMetricsPort`, `credentialProxyMetricsPortName` and `credentialProxyMetricsPortEnv` in `k8s-operator/internal/controller/platformagent_manifests.go`; the `*_METRIC`, `TOOL_STATUS_*`, `*_SUBCOMMANDS`/`*_SURFACES`/`*_VERBS` and `TOOL_DURATION_BUCKETS` constants in `agents/platform/scripts/credential_proxy.py`; `charts/kube-agents/templates/platform-agent-monitoring.yaml`, held to the operator's port by `tests/test_chart_platform_agent_monitoring.py`; the `KUBECTL_READ_VERBS` and `GCLOUD_READ_COMMANDS` tables in `agents/platform/scripts/command_policy.py` supply the read halves of the `subcommand` vocabularies |
| The A2A gateway's metrics-only port, its container-port name and `A2A_METRICS_PORT`; the gateway's metric names and label vocabularies | `a2aGatewayMetricsPort`, `a2aGatewayMetricsPortName` and `a2aGatewayMetricsPortEnvVar` in `k8s-operator/internal/controller/platformagent_a2a_manifests.go`; `MetricsPath`, the `*Name` metric constants, `metricsTerminalStates`, `metricsTerminalSources` and `metricsGchatOutcomes` in `a2a/gateway/metrics.go`; `metricsPortEnv` in `a2a/gateway/config.go`; `charts/kube-agents/templates/platform-agent-monitoring.yaml`, held to the operator's port by `tests/test_chart_platform_agent_monitoring.py` |
| The usage counters poller: its interval, the `<name>-usage-counters` ConfigMap and its document key, the per-poll delta ceiling, and the operator-peer rule the gateway and broker policies render on the metrics ports (`POD_NAMESPACE`, `app.kubernetes.io/name: kube-agents-operator`) | `usageCountersPollInterval`, `usageCountersConfigMapSuffix` and `usageCountersDocumentKey` in `k8s-operator/internal/controller/usage_counters_poller.go`; `usageDeltaCeiling` in `k8s-operator/internal/controller/usage_counters_fold.go`; `OperatorNamespaceEnv`, `operatorPodNameLabel`, `operatorPodNameValue` and `operatorMetricsIngressRule` in `k8s-operator/internal/controller/platformagent_manifests.go`; the variable is set on the manager container by `charts/kube-agents/templates/operator-deployment.yaml` and `k8s-operator/config/manager/manager.yaml`, and the pod label the rule selects is written by `operatorSelectorLabels` in `charts/kube-agents/templates/_helpers.tpl` and by `k8s-operator/config/manager/manager.yaml`; `tests/test_operator_pod_namespace_env.py` holds the three in step |
| OTLP endpoint default, discovery candidates, and `otlpEndpointSource` values | `k8s-operator/internal/controller/telemetry.go` |
| The `secret-env-hash` pod-template annotation and its re-read interval | `k8s-operator/internal/controller/platformagent_secret_hash.go` |
| DNS/metadata-daemon defaults, the `dnsClusterIPsSource` / `metadataDaemonIPSource` values, and the `additionalEgress` prefix floors (`/12`, `/48`) | `k8s-operator/internal/controller/netpolprofile.go` and `platformagent_controller.go` |
| Agent egress-allowlist policy: metadata addresses, the `-sandbox-metadata-deny` name, the `controlPlaneCIDRs` floors (`/16`, `/32`), and the `EgressAllowlistRefused` reason | `k8s-operator/internal/controller/platformagent_egress_policy.go` and `platformagent_controller.go` |
| LiteLLM egress policy: `litellm-policy`, the `enable-litellm-network-policy` and `otlp-collector-namespace` annotations, the external-endpoint port-443 check, and the `platformAgent.annotations` conflict rule | `k8s-operator/internal/controller/platformagent_litellm_policy.go`, `charts/kube-agents/templates/_helpers.tpl`, `charts/kube-agents/templates/platform-agent-cr.yaml` |
| Scoped service-account pool: `CREDENTIAL_PROXY_SCOPED_SA_POOL{,_FILE}`, the pool file path and version, the per-project key, and the `ka-<project>-<hash8>` account ids | `agents/platform/scripts/scoped_sa_pool.py`, `k8s-operator/internal/controller/platformagent_manifests.go`, `terraform/modules/kube-agents-iam/scoped_pool.tf` |
| Image inventory: every image an install pulls, and its upstream pin | `images.json` |
| Registry prefix defaults (`REGISTRY_PREFIX`, `THIRD_PARTY_REGISTRY_PREFIX`) | `install.defaults.env` |
| GKE host-discovery label | `scripts/installer/common.sh` |
| GitOps clone layout (`/opt/data/gitops/...`) and leases | `agents/platform/scripts/gitops_workspace.py` |
| Repository-identity rules: accepted GitHub hostnames, path depth, segment grammar, length bound, and the `GIT_REPO_UNPARSEABLE` reason | `agents/platform/scripts/repo_ref.py` |
| The gitops-state ConfigMap's keys (`managed_repos`, `context_repos`) and which one each consumer reads | `agents/platform/scripts/gitops_workspace.py` (readers), `repository_role` in `agents/platform/scripts/credential_proxy.py` (reads `context_repos` for the clone credential), and `reconcileGitopsStateConfigMap` / `syncGithubTokenMinterConfigMap` in `k8s-operator/internal/controller/platformagent_controller.go` (the `managed_repos` seed; both keys for the minter policy) |
| Chat platforms an install posts to, the order, and the fallback | `agents/platform/scripts/chat_platforms.py` |
| Which deliverables a Google Chat thread gets pasted inline, the size ceiling, and the per-message budget | `agents/platform/scripts/google_chat_relay_patch.py` |
| Staging a sandbox-written artifact out for delivery: the per-file, per-card and total ceilings, the deadline, and the denied prefixes | `agents/platform/scripts/sandbox_artifact_patch.py` |
| fleet-audit finding-id pattern and rendering caps | `agents/platform/skills/fleet-audit/scripts/audit_report.py` |
| fleet-audit report store: root, `FLEET_AUDIT_REPORTS_DIR`, ring size, stamp format, and the in-flight TTL | `agents/platform/skills/fleet-audit/scripts/audit_report.py` (`report_status.py` beside it copies the root and the TTL) |
| fleet-upgrade-verification record path, file name per target, record format version, readiness flags and exit codes, kubeconfig directory and file name, and the readiness cell strings | `agents/platform/skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py` and `upgrade_readiness.py` beside it |
| Chat-delivery watch: the `ALERT chat_delivery_watch` log prefix and file, the ledger issue's label and marker, the streak state path, and the `CHAT_DELIVERY_*` environment variables | `agents/platform/scripts/chat_delivery_watch.py` |
| Controller stall watch: the `STALL_WATCH_*` environment variables, the default kind list, the per-tick alert ceiling and the ledger path | `agents/platform/scripts/stall_watch.py` |
| Helm chart value defaults (KSA/secret names, image repos, tag rules) | `charts/kube-agents/values.yaml`; the accepted key set and types, `charts/kube-agents/values.schema.json` |
| What a default install reserves (per-workload CPU, memory, pods, claims) | `charts/kube-agents/files/footprint.yaml` for the operator-rendered pods, generated by `scripts/generate_chart_footprint.py`; `charts/kube-agents/values.yaml` for the chart's own |
| Release tag families (`rc_*`, `rc_*_validated`, `evalcand_<ts>_<sha>`, `staging_<ts>_<sha>`, GA `X.Y.Z`), the release line `release/X.Y` and the GA base found by ancestry, the shared lookups over them, and the required release image roster (`REQUIRED_RELEASE_IMAGES`), which every rung reads at its candidate's commit through `required_release_images_at` | `scripts/release/common.sh` |
| Which pushes build SHA-tagged images, and which never move `:latest` | `.github/workflows/docker-publish-ghcr.yml` (`on.push.branches` and the `tags:` guards) and `scripts/release/decide_image_publish.sh` (the release-branch rules) |
| GA release gate: its conditions, exit codes, dispatch modes, and step outputs | `scripts/release/resolve_scheduled_release.sh`, `scripts/release/decide_release_gate.sh`, `.github/workflows/release-publish.yml`, and `.github/workflows/release-scheduler.yml` |
| Stock `PlatformAgent.metadata.name` used as the admin-console installation ID | `charts/kube-agents/values.yaml` (`platformAgent.name`) |
| Terraform module defaults (GSA/KSA/namespace, role set, channel) | `terraform/modules/*/variables.tf` |
| Memory bank name, scope-tag spelling, and provider name | `agents/chat/plugins/memory/kube_agents_memory/config_schema.py` |
| Per-profile Hindsight recall settings the agent uses | `agents/chat/defaults/hindsight/config.json`, `agents/platform/hindsight/config.json` |
| Hindsight endpoint (`HINDSIGHT_API_URL`, derived from the namespace) | `k8s-operator/internal/controller/platformagent_manifests.go` |
| Gateway rollout budgets: `StartupProbe` (`agentAPIProbe`), `gatewayProgressDeadlineSeconds`, and the rollout gates in `upgrade.sh` and `scripts/release/wait_for_gke_readiness.sh` (`GATEWAY_READINESS_TIMEOUT`) | `k8s-operator/internal/controller/platformagent_manifests.go`, `upgrade.sh`, `scripts/release/wait_for_gke_readiness.sh`, and `scripts/release/platform_agent_mode.sh` (`PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS`), held in order by `tests/test_gateway_rollout_budgets.py` |
| Admission webhook server port (`--webhook-port` default) | `DefaultPort` in `k8s-operator/internal/webhook/platformagent_webhook.go` |
| Live-test lease: ConfigMap name, TTL, install-configuration keys read, which commands count as mutations | `scripts/live_test_lease.py` |
| PR evidence screenshots: publish branch, file-name provenance, caption format | `scripts/pr_evidence_screenshot.sh` |
| Unresolved-thread hold: the label, the pool condition, the sweep interval, the ownership rule | `scripts/hold_unresolved_threads.py` and `.github/workflows/hold-unresolved-threads.yml` |
| Issue triage queue: the `needs-triage` label, the `priority:` label prefix it mirrors, and when each event adds or removes it | `.github/workflows/needs-triage.yml` |
| Flaky-check tracking: the `ci:flaky` label, the watched checks and the exclusions, the one-issue-per-container key, the never-close rule | `scripts/notify_flaky_check.py` and `.github/workflows/flaky-check-notify.yml`; the exclusion list the contract test enforces is `FLAKY_CHECK_EXCLUDED_WORKFLOWS` in `scripts/test_integration_contracts.py` |
| Reviewer auto-assign: the skip reasons, the self-approval test that widens the verdict rule and narrows the draw, the robot-account exclusion, the `/request-review` reactions, the third-round hand-off and its marker | `scripts/request_reviewers.py`, `.github/workflows/auto_request_review.yml` and `reviewers.groups` and `options.robot_accounts` in `.github/auto_request_review.yml` |
| Broken-main tracking: the `ci:main-broken` label, the watched workflows, the one-issue-per-episode marker, the sweep interval, the dismissal, older-episode-only and whole-read rules | `scripts/notify_broken_main.py` and `.github/workflows/main-broken-notify.yml`; the required-check roster the contract test enforces is `BROKEN_MAIN_WATCHED_WORKFLOWS` in `scripts/test_integration_contracts.py` |
| Context budget for the always-loaded agent instruction files (`AGENTS.md`, `CLAUDE.md`) | `BUDGET` in `scripts/check_context_budget.py` |
| Who may set the `approved` and `lgtm` labels on a change | `OWNERS` (`approvers`; `reviewers` for `lgtm` only), `hack/OWNERS`, and `OWNERS_ALIASES`; the `skip_collaborators` switch that makes `lgtm` OWNERS-gated is `prow/oss/plugins.yaml` in `GoogleCloudPlatform/oss-test-infra` |
| Which labels Tide merges on, and which Prow presubmits gate | `prow/oss/config.yaml` and `prow/prowjobs/gke-labs/kube-agents/` in `GoogleCloudPlatform/oss-test-infra` — not a file in this repository |
| Contributor-agent merge labels (`lgtm`, `approved`, `ok-to-test`, `do-not-merge/hold`; who may set the first two is the "Who may set" row above) and the `triage` permission grant | external tide automation and GitHub repo settings (not in-tree); named in `AGENTS.md` and `agents/contributor/AGENTS.md` |
| Queue-wait thresholds that justify onboarding an eval project, the window they run over, and the JUnit row names and metric property the TestGrid tab reads | `scripts/pool_pressure.py` |
| Presubmit-gate health rules (windows, thresholds, hysteresis), the Chat posting variables and the digest hour | `scripts/eval_dashboard/health.py`, `scripts/eval_dashboard/post_health.py`, `scripts/eval_dashboard/periodics.py` and `.github/workflows/ci-health.yml` |
| The eval dashboard's roster-page contract (the `demoted YYYY-MM-DD` phrase inside a `- **case-name** —` hold-out bullet), its page files and its URL parameter vocabularies | `ROSTER_ENTRY_RE` / `DEMOTED_RE` and `PAGES` in `scripts/eval_dashboard/render.py`; `linkState()` in `scripts/eval_dashboard/template/pages.js` |
| Testing-domain slugs a bench case may claim | `docs/designs/domains.yaml` |
| Seeded-fleet fixture role names and the cluster slot each lives on | `bench/tf/fleet/fixtures.json` |
| Day-N availability gate per fixture, and the project-scoped fixtures that sit on no cluster | `docs/designs/fleet-fixtures.yaml`, which overlays `fixtures.json` and may not rename a role |
| Credential-proxy refusal rule ids, refused flags, forced git config | `agents/platform/scripts/credential_proxy.py`; the `gcp.api.*` relay rule ids and the relayed-read table in `agents/platform/scripts/api_policy.py` |
| Command-policy allowlisted verbs and denied `kubectl`/`gcloud` flags | `agents/platform/scripts/command_policy.py` |
| Gateway redaction: rule actions, marker formats, pseudonym length, rule-name grammar, and the `KUBE_AGENTS_REDACTION_CONFIG` variable | `agents/chat/defaults/plugins/common/redactor.py` (canonical; `charts/kube-agents/files/redactor.py` is its checked mirror, and `GCP_OAUTH_TOKEN_PATTERN` in `deploy/docker/patches/credential_redaction.py` is a second, tested against it) and `charts/kube-agents/files/litellm_redaction_callback.py` |
| Which CI pool project maps to which GitOps repository | `gitops_repo_for_project()` (GitHub) and `gitlab_project_for_project()` (GitLab) in `hack/ci-deploy.sh` |
| The GitOps fix-cycle wait's `GITOPS_*` environment names and defaults, its outcome names, the merge check it watches and the `gitops_fix_cycle` trajectory entry | `bench/kube_agents_bench/gitops.py` |
| The GitOps fix-cycle pilot's run-branch grammar (`run/<cluster>/<task>`), its task directory default and its Application name | `locals` in `bench/tf/prebuilt/gitops-fix-cycle/main.tf` |
| The App the pool sweep signs as, whose own bot login it closes pull requests for | `DEFAULT_APP_ID` in `hack/ci_sweep_agent_pulls.py`, which must be the App `hack/ci-deploy.sh` hands the agent; the login is read from `GET /app` under that key, and a wrong id fails every project's sweep at that lookup |
| What the in-job repository reset mints, owns and records | `AGENT_PULLS_RESET_PERMISSIONS` in `hack/ci-eval-pr.sh`; the `[bot]` rule and the record keys in `hack/ci_reset_agent_pulls.py` (`new_record`, mirrored for GitLab by `empty_record` in `hack/ci_gitlab_forge.py`), the suffix pinned to `hack/ci_reset_audit_ledgers.py`'s; the two audit labels, the gone-ref codes and the limit reading imported from `hack/ci_sweep_agent_pulls.py` |
| What the smoke test's step 0 reuses and how it binds a verdict: the inert-path list, the Prow bot login, the override and cancel description prefixes, the pull-request URL binding, the status page cap and the `finished.json` metadata key a reused override leaves | `REVALIDATION_INERT_PATHS`, `REVALIDATION_STATUS_POSTER`, `REVALIDATION_OVERRIDE_PREFIX`, `REVALIDATION_OVERRIDE_CANCEL_PREFIX`, `REVALIDATION_PULL_URL_PREFIX`, `REVALIDATION_STATUS_PAGE_LIMIT` and `REVALIDATION_REUSED_OVERRIDE_KEY` in `hack/ci-revalidate.sh` |
| Roles the pool verifier accepts as Artifact Registry upload rights, and the API set it requires | `scripts/verify_ci_pool_project.py`, whose `VALID_CMEK_STATES` mirrors `is_valid_cmek_encryption_state()` in `scripts/installer/installer_common.sh`, whose `HOST_OTEL_SCOPE` mirrors the same-named constant in `scripts/provision_ci_pool_project.sh`, whose `PLATFORM_GSA_ROLES` mirrors `local.read_only_roles` in `terraform/examples/full-install/main.tf`, whose `FLEET_READER_TOKEN_CREATORS` mirrors `bench/tf/fleet`'s `fleet_reader_token_creators` default, whose `POOL_STATE_READER_ROLES` mirrors `bench/tf/fleet`'s `pool_state_reader_roles` local, whose `FLEET_RECONCILER_ROLES` mirrors the reconciler grant loop in `scripts/provision_ci_pool_project.sh`, and whose cluster names mirror `bench/tf/fleet` except `seeded-d`, left out until the pool is re-applied, and whose fixture check parses the summary lines `hack/fleet-kubeconfigs.sh` and `hack/fleet-fixture-state.py` print, whose signing probe reads `githubMinter.kms.keyVersion` from `charts/kube-agents/values.yaml`, whose `LEDGER_APP_ID`/`LEDGER_INSTALLATION_ID` mirror the `EVAL_LEDGER_*` defaults in `hack/ci-eval-pr.sh` and the `DEFAULT_LEDGER_*` constants in `hack/ledger_token_mint.py`, whose `GITOPS_SEED_*` and `GITOPS_INTENT_NOTE_{PATH,MESSAGE,CONTENT}` mirror the same values in `scripts/provision_ci_pool_project.sh` (the note is read back through `parse_declarations` in `agents/platform/skills/fleet-audit/scripts/audit_report.py`), and whose mapping check reads `hack/ci-deploy.sh` from `gke-labs/main` as well as the local tree |
