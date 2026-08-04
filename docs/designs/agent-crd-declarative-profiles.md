# Design: Declarative Agent Profiles for the `Agent` CRD

**Status:** Draft for review

**Context:** Fills the slot [06-api-and-data-contracts.md](../design/06-api-and-data-contracts.md)
§1.1 explicitly defers ("**v1 = a baked per-tier image** … a mounted profile is deferred"), and
supersedes the runtime-patching approach proposed in
[PR #381](https://github.com/gke-labs/kube-agents/pull/381) (`AgentExtension`).

---

## TL;DR

Today an agent's workspace — `SOUL.md`, skills, SOPs, MCP server scripts, `config.yaml` — is split
between a baked container image and ~150 lines of hardcoded Go in `renderConfigYAML()`. Changing
any of it means a new image or operator release, which creates pressure for runtime patch
mechanisms like PR #381's `AgentExtension` (raw YAML merges and file injection into the data PVC).

This design replaces both with **declarative profile compilation**: an agent is declared in a
single `agent-profile.yaml` (identity, procedures, skills, MCP servers, schedules — content inline
or by `ref`), a compiler embedded in both a CLI and the operator translates it into the physical
workspace, and the operator delivers that workspace to the pod **read-only, reassembled on every
pod start**, through one of three size-tiered mechanisms (sharded ConfigMaps → OCI artifact →
git-at-SHA). The generic `Agent` CRD carries the profile reference plus the infrastructure
envelope, and **reuses the existing `PlatformAgent` spec structure field-for-field wherever it
makes sense** — `PlatformAgent` becomes a published profile, not an API type.

---

## 1. Problem

The agent's definition is smeared across three places with three different change processes:

| Where                                                            | What lives there                                                                        | To change it            |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------- |
| Container image (`deploy/docker/Dockerfile`, `agents/platform/`) | `SOUL.md`, skills, SOPs, MCP server scripts                                             | Rebuild + push an image |
| `renderConfigYAML()` (`k8s-operator/internal/controller/…`)      | `mcp_servers`, `platform_toolsets`, plugin list, model endpoint, approvals, web backend | New operator release    |
| `PlatformAgent` CR                                               | A handful of toggles (memory, chat platforms, agentHome, replicas)                      | `kubectl apply`         |

Consequences:

- **Nothing is composable.** Adding one skill to one agent requires a full image rebuild; there is
  no way to assemble a narrow, single-purpose agent (per the focused-agents principle in
  [02-agent-personas.md](../design/02-agent-personas.md)) without forking the whole workspace.
- **Runtime patching fills the vacuum.** PR #381 (`AgentExtension`) responds to real demand, but by
  mutating the deployed artifact: unrestricted root-level `config.yaml` merges (≈ arbitrary command
  execution via `mcp_servers`), files copied irreversibly into the persistent data PVC (no GC when
  an extension is deleted), and namespace-wide targeting by default.
- **The operator knows too much.** `renderConfigYAML()` hardcodes behavior (which MCP servers, which
  plugins, which toolsets) that belongs to the agent definition, not the controller.

### Goals

- One declarative source of truth per agent (`agent-profile.yaml`), with content inline or by
  reference, compiled deterministically into the runtime workspace.
- One generic `Agent` CRD (per [06](../design/06-api-and-data-contracts.md) §1) that stays as close
  to the existing `PlatformAgent` spec as possible; prebuilt personas ship as **profiles**, not CRDs.
- Workspace delivery that requires **neither a custom image per agent nor unbounded ConfigMaps**.
- Extension by **composition at compile/assembly time**, replacing mutation at reconcile time.
- Strip `renderConfigYAML()` down to operator-owned infrastructure keys.

### Non-goals

- A universal multi-harness transpiler. Hermes is the v1 compile target; Scion is v2 (the
  `kube-agents-scion` sync gives it a real consumer). Other targets (Cloud Agents API, etc.) are
  out of scope until the schema has proven itself on two harnesses.
- Remote skill marketplaces / git registries in v1. The `ref` model and OCI delivery leave room;
  the fetch-and-verify pipeline is future work.
- Changing the security model: agents remain read-only; profiles do not grant permissions
  ([03-security-model.md](../design/03-security-model.md)).
- Redesigning `tier` / `scope` / `parentRef` — those come from
  [06](../design/06-api-and-data-contracts.md) §1.1 unchanged and are orthogonal to this design.

---

## 2. Existing components

| Component                                             | Current behavior                                                                                                                                    |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`PlatformAgent` CRD** (`k8s-operator/api/v1alpha1`) | `AgentSpec` (deployment, security) + `harness` (clusterName, location, projectId, hermes, memory) + `integration` (github, googleChat, slack)       |
| **`renderConfigYAML()`**                              | Generates `config.yaml` in Go; hardcodes MCP servers, toolsets, plugins, model endpoint; mounted via ConfigMap subPath over `/opt/data/config.yaml` |
| **Agent workspace source** (`agents/platform/`)       | ~90 files, ~457 KB, all text — `SOUL.md`, `config.yaml`, `skills/*/SKILL.md`, scripts, governance SOPs — baked into the platform image              |
| **Data PVC** (`<agent>-data`)                         | Mutable agent state (sessions, memory) _and_ — today — parts of the workspace, mounted at `agentHome` (default `/opt/data`)                         |
| **Design 06 §1.1**                                    | End-state `Agent` CRD with `tier`/`scope`/`parentRef` and a `profile` field; v1 = baked per-tier image, **mounted profile deferred → this design**  |
| **PR #381 (`AgentExtension`)**                        | Runtime patch CRD: raw config merge + `___`-encoded file ConfigMap + init-container copy into the data PVC + env injection                          |

---

## 3. The `Agent` CRD

**Principle: `Agent` is `PlatformAgent` plus a `profile`, minus nothing.** Every existing nested
block (`deployment`, `security`, `harness`, `integration`) is reused as-is — same Go types, same
JSON paths — so a `PlatformAgent` manifest converts to an `Agent` by changing `kind` and adding
`spec.profile`. The tier fields from [06](../design/06-api-and-data-contracts.md) §1.1 slot in
alongside.

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: platform-agent
  namespace: kubeagents-system
spec:
  # ---- from design 06 §1.1 (unchanged, not re-designed here) ----
  tier: platform
  scope: { projectId: my-proj }
  # parentRef: {...}                      # non-platform tiers

  # ---- NEW: the agent definition ----
  profile:
    # Exactly one source (CEL-validated oneOf):
    oci: # precompiled workspace artifact (primary production path)
      ref: ghcr.io/gke-labs/agent-profiles/platform
      digest: sha256:… # required; tags alone are rejected
    # configMapRef: { name: my-profile }  # profile YAML authored out-of-band; operator compiles
    # inline: {…}                         # full DASP document; experiments only (CR size ceiling)
    # git: { url: …, revision: <sha>, path: agents/platform }   # v2 (see §5, tier 3)

  # ---- EXISTING blocks, reused verbatim from PlatformAgent ----
  harness:
    clusterName: my-cluster
    location: us-central1
    hermes: { agentHome: /opt/data }
    memory: { memoryEnabled: false }
    model: # NEW sub-block: evicts the hardcoded LiteLLM URL
      baseURL: http://litellm.kubeagents-system.svc.cluster.local/v1
      name: model-default
  integration:
    googleChat:
      { enabled: true, projectId: …, topicName: …, subscriptionName: … }
    slack: { enabled: false }
    github: { gitRepo: … }
  deployment:
    image: … # SHARED runtime image (harness only, no workspace)
    env: # secret-ref env — already exists today (DeploymentSpec.Env)
      - name: SLACK_API_TOKEN
        valueFrom: { secretKeyRef: { name: slack-secrets, key: api-token } }
    availability: { replicas: 1 }
  security:
    serviceAccountName: platform-agent-ro
```

Notes:

- **`spec.deployment.env` already exists** (`common_types.go` `DeploymentSpec.Env`) — the env-var
  injection feature of PR #381 needs no new API surface at all.
- `harness.model` is the one net-new harness field: the model endpoint is environment-coupled
  infrastructure, so it belongs on the CR, not in the profile. It replaces the hardcoded
  `http://litellm.<ns>.svc.cluster.local/v1`.
- `PlatformAgent` is retained as a deprecated conversion shim during migration (§7); no new
  persona CRDs are ever added.

---

## 4. The agent profile (`agent-profile.yaml`)

A single YAML document declaring the entire workspace. Every content-bearing node is
**polymorphic**: `content:` (inline block string) or `ref:` (path relative to the profile file,
resolved at compile time). Schema (v1alpha1):

```yaml
schema_version: v1alpha1
metadata:
  name: platform
  version: 1.4.0
  description: GKE platform-tier operations agent

identity:
  soul: { ref: ./SOUL.md } # or content: |
  # further identity files as needed

procedures: # SOPs, mounted under procedures/
  - name: cve_scan_sop.md
    ref: ./governance/cve_scan_sop.md
  - name: emergency_scaling_sop.md
    content: |
      # Emergency Scaling SOP
      1. Inspect HPA thresholds …

skills: # each compiles to skills/<name>/…
  - name: gke-compute-class-creator
    ref: ./skills/gke-compute-class-creator # whole-directory reference
  - name: gke-cost-analysis
    mcp_servers:
      - name: cost_mcp_server.py
        runtime: python3
        content: |
          from mcp.server.fastmcp import FastMCP
          …
    scripts:
      - name: preflight.sh
        runtime: bash
        ref: ./skills/gke-cost-analysis/preflight.sh

schedules: # compiles to the harness cron config
  - name: weekly-cost-audit
    cron: "0 9 * * 1"
    trigger_prompt: |
      Execute the weekly cost audit …

harness_config: # structured Hermes config fragment (NOT raw YAML merge):
  mcp_servers: { … } # the blocks currently hardcoded in renderConfigYAML()
  platform_toolsets: { … }
  plugins: { enabled: [hermes_otel, session_store, …] }
  approvals: { cron_mode: approve }
  web: { backend: ddgs }
```

### 4.1 The compiler

`kube-agent-cli compile` is a Go **library first, CLI second** — the same package is linked into
the operator, so a profile compiles identically everywhere:

- **CI / pre-bake:** `kube-agent-cli compile --profile … --output …` (optionally `--push oci://…`)
  produces the workspace or a data-only OCI artifact. Deterministic: same input → byte-identical
  output (sorted walks, no timestamps), so artifacts are diffable and cacheable.
- **Operator:** for `inline` / `configMapRef` sources, the controller calls the library at
  reconcile time and ships the output via §5 tier 1. For `oci`, the artifact **is** the compiled
  output — the operator only mounts it.
- **Target:** `--target hermes` only in v1. The compiler emits the Hermes layout (workspace files +
  `config.yaml` fragment + cron config). `--target scion` follows, replacing the hand-rolled
  `kube-agents-scion` sync script.

### 4.2 `config.yaml` ownership

`renderConfigYAML()` shrinks to **operator-owned keys only** — things derivable from the CR or the
cluster: `model.*` (from `harness.model`), `platforms.*` + display (from `integration`),
`memory.*` (from `harness.memory`), `leader_election` (from replicas), `terminal.cwd` (from
`agentHome`). Everything else — `mcp_servers`, `platform_toolsets`, `plugins`, `approvals`, `web` —
comes from the profile's `harness_config`, merged **structured-field-wise with operator keys
winning on conflict**, and the conflict reported in `Agent.status.conditions`. There is no raw
free-form YAML merge anywhere; that is the load-bearing difference from PR #381 and from the
`hermes/extra-config` annotation (which is removed).

---

## 5. Workspace delivery — the file problem

The constraint: **no custom runtime image per agent** (the harness image stays shared) and **no
unbounded ConfigMaps** (1 MiB apiece, etcd-backed). Measured reality: the entire platform
workspace today is **~457 KB across 90 files, all text**. So delivery is tiered, all behind the
same volume contract:

| Tier | Source                   | Mechanism                                                                                                                                                                                                                                                                                                                                                                                                                        | Bounds / when                                                                                 |
| ---- | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| 1    | `inline`, `configMapRef` | Operator compiles → **sharded ConfigMaps** (~900 KB/shard), assembled with a **projected volume** using `items[].path` (paths may contain `/` — no `___` encoding hack)                                                                                                                                                                                                                                                          | Hard cap **3 MB** total; over the cap the reconcile fails with a condition pointing to tier 2 |
| 2    | `oci` (digest-pinned)    | Data-only artifact (scratch + workspace files). **Default mechanism: init container runs `oras pull` into an `emptyDir`** — works on every cluster version. Optimization when available: the native **`image:` volume source** (beta since K8s 1.33 but disabled by default until 1.35, when containerd support landed; GA in 1.36 — so on GKE this is effectively 1.35+, and gate/runtime support should be verified per fleet) | Primary production path. Registry-cached, cosign-signable, admission-controllable             |
| 3    | `git` at a pinned SHA    | Init container clones and runs the embedded compiler at pod start                                                                                                                                                                                                                                                                                                                                                                | v2 — dev/GitOps convenience; adds a boot-time network dependency, so never the default        |

### 5.1 The read-only invariant

Whatever the tier, the workspace is **read-only and reassembled on every pod start**; the pod
start sequence is:

1. Workspace volume (projected ConfigMaps / image volume / emptyDir) mounted read-only at a
   staging path.
2. An assembly init container overlays, **in deterministic order**: profile workspace → each
   `workspace.extraSources[]` entry (§6) → into the live workspace directory. If the harness needs
   the directory writable, the live directory is an `emptyDir` — wiped on every restart by
   construction.
3. Mutable agent state (Hermes memory, sessions, scratch) lives on the data PVC under a
   **separate mount** (`/opt/data/state/…`), never inside the workspace tree.

This single invariant eliminates the entire PR #381 lifecycle problem class: there is no file GC,
no drift, and no "extension deleted but skill still active," because nothing ever mutates a
persistent workspace in place. Rollouts are driven the same way config changes are today: the pod
template carries a hash annotation of the resolved workspace (shard hashes or the OCI digest).

---

## 6. Composition: what replaces `AgentExtension`

Additive composition is a CR field, not a second CRD:

```yaml
spec:
  workspace:
    extraSources: # ordered; later entries win on file conflict —
      - oci: { ref: …, digest: sha256:… } # e.g. a shared, signed skill pack
      - configMapRef: { name: stockout-handler-skill }
```

- Deterministic, ordered, **visible in one place** (the Agent CR itself — reviewable via GitOps),
  and removable by editing the CR. Conflicts between sources are surfaced in `status`.
- Targeting is explicit: a source applies to exactly the agents whose CRs list it. There is no
  namespace-wide implicit injection.
- Env/secrets: `spec.deployment.env` (exists today). Structured config: the profile's
  `harness_config` (or a fragment in an extra source, same structured merge rules). Raw YAML
  merge: intentionally impossible.

---

## 7. Migration path

Sequenced to fit [07-implementation-roadmap.md](../design/07-implementation-roadmap.md); each step
is independently shippable:

1. **Extract the compiler** (`k8s-operator/` or a sibling `cli/` module): profile schema, `ref`
   resolution, Hermes target. Golden tests: compiling `agents/platform/` reproduces today's baked
   workspace byte-for-byte.
2. **Author `agents/platform/agent-profile.yaml`** describing the existing layout (mostly `ref`s —
   the repo tree stays the source of truth for content). Move the `renderConfigYAML()` hardcoded
   blocks into its `harness_config`; shrink `renderConfigYAML()` to operator-owned keys (§4.2).
3. **Add `Agent` CRD v1alpha1** reusing the existing spec types + `profile` + `workspace` +
   `harness.model`. Implement tier 1 delivery (sharded ConfigMaps, projected volume, hash rollout)
   and the assembly init container. `PlatformAgent` continues to work, now internally converted to
   an `Agent` with the platform profile.
4. **Tier 2 delivery**: `--push oci://`, oras init-container pull (image-volume mount as a later
   optimization on 1.35+ fleets); CI publishes the platform profile artifact per release. The per-tier baked images from
   [08](../design/08-agent-runtime-and-identity.md) §2 collapse into one shared harness image.
5. **Deprecate `PlatformAgent`** (conversion webhook or documented one-line migration), repo
   refactor toward shared root-level `skills/` and `procedures/` so profiles can `ref` across
   agents.
6. **v2:** `--target scion` (retiring the `kube-agents-scion` sync script), `git` source tier,
   signed-artifact verification policy.

### Disposition of PR #381

Superseded, with credit — it identified the right requirements (dynamic skills, secrets, per-agent
targeting) and its path-traversal validation and hash-rollout wiring carry over to the tier-1
implementation. Concretely: env injection already exists (`spec.deployment.env`); file injection
becomes `workspace.extraSources` under the read-only invariant; config injection becomes
structured `harness_config` fragments. The raw-merge, PVC-mutation, and namespace-wide-default
semantics are the parts this design deliberately makes impossible. If the stockout experiment
needs an unblock before step 3 lands, the interim is a `configMapRef` extra source behind a
feature gate — not a new CRD.

---

## 8. Verification

- **Determinism:** `compile` twice → identical digests; golden test vs. today's baked workspace
  (step 1).
- **No mutation:** chaos check — delete an `extraSources` entry, roll the pod, assert the files
  are gone; write into the workspace at runtime, restart, assert it's clean.
- **Bounds:** a >3 MB tier-1 profile fails reconcile with an actionable condition; a tag-only
  (digest-less) `oci` ref is rejected by CEL validation.
- **Ownership:** a profile `harness_config` that sets an operator-owned key (e.g. `model.base_url`)
  loses, and the conflict appears in `status.conditions`.
- **Security regression:** the [03](../design/03-security-model.md) §11 negative tests pass
  unchanged; a profile cannot grant RBAC, mount host paths, or alter the pod spec.
- **Compatibility:** an existing `PlatformAgent` manifest applies and reconciles identically
  through the conversion path (step 3).

---

## 9. Alternatives considered

| Alternative                                       | Why not                                                                                                                                                                                 |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Custom image per agent** (status quo, 08 §2 v1) | Rebuild/push per skill change; couples workspace to harness releases; no composition without forking                                                                                    |
| **`AgentExtension` runtime patching** (PR #381)   | Mutates persistent state (no GC), raw config merge ≈ exec, namespace-wide defaults; a second config surface that migration would have to unwind                                         |
| **One giant ConfigMap**                           | 1 MiB hard limit is already within 2× of today's workspace; silent failure at growth                                                                                                    |
| **git-sync sidecar as the primary path**          | Boot-time network/credential dependency for every pod start; mutable refs unless SHA-pinned; kept as opt-in tier 3                                                                      |
| **CSI / custom volume driver for artifacts**      | An `oras` init container covers every cluster version today, and the native `image:` volume source becomes a drop-in optimization on 1.35+ fleets — neither requires operating a driver |
| **Per-persona CRDs** (`ClusterAdminAgent`, …)     | API sprawl; validation belongs in the profile schema + CEL on one CRD; contradicts 06 §1's single tier-discriminated `Agent`                                                            |
