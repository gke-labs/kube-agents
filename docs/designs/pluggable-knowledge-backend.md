# Pluggable Knowledge Backend

> **STATUS — proposed design; not implemented.** Tracks
> [#5043](https://github.com/gke-labs/kube-agents/issues/5043) (part of
> [#5048](https://github.com/gke-labs/kube-agents/issues/5048), shared with
> [#2903](https://github.com/gke-labs/kube-agents/issues/2903) and
> [#4053](https://github.com/gke-labs/kube-agents/issues/4053)). Builds on the
> version-control broker and provider seam in
> [`version-control-support.md`](version-control-support.md), the Open Knowledge Format (OKF) v0.2
> contract in [`docs/architecture/06-api-and-data-contracts.md` §5](../architecture/06-api-and-data-contracts.md#5-okf-knowledge-contract),
> Layer 2 decision memory in [`memory.md`](memory.md), and the brokered report store in
> [`fleet-audit-report-store.md`](fleet-audit-report-store.md).

**Scope:** how `kube-agents` connects to customer-chosen operational knowledge stores (a GitOps
repository's `knowledge/` root, a dedicated knowledge repository, Google Drive, SharePoint /
OneDrive, and Confluence) and to a separate agent output store behind one credential-brokered
interface.

**Does not cover:** per-user OAuth brokering (declined under Decision D12; see
[#1720](https://github.com/gke-labs/kube-agents/issues/1720)), direct agent writes to curated
runbooks without pull-request review, media/OCR conversion of images or video, or replacing
Hindsight Layer 1 conversational memory.

## Summary

Platform teams keep runbooks, playbooks, cluster baselines, and SOPs in Git repositories, Google
Drive folders, SharePoint libraries, and Confluence spaces. Today `kube-agents` recognizes only
`knowledge/` inside a registered Git repository (`06` §5). An install without a GitOps repository
has nowhere to read curated knowledge at all, and an install whose runbooks live outside the
GitOps repository has no brokered path to reach them. Pasting runbooks into chat fails on
staleness, length limits, and missing citations
([#2296](https://github.com/gke-labs/kube-agents/issues/2296)). At the same time, scheduled
audits, investigations, and watches generate unreviewed reports that chat should link to rather
than paste ([#5047](https://github.com/gke-labs/kube-agents/issues/5047)), and those unreviewed
outputs must stay separated from human-curated runbooks.

This design introduces a **knowledge broker** inside `<agent>-credential-proxy` backed by modular
build-time **knowledge providers**, mirroring the version-control seam in
[`version-control-support.md`](version-control-support.md):

- **Neutral verbs at the credential boundary.** Agents, skills, profile workers, and the Curator
  call `/v1/knowledge/<verb>` (`capabilities`, `list`, `read`, `search`, `propose`, `write`) naming
  a configured logical store rather than a backend URL or SDK. Credentials remain inside the
  credential broker pod.
- **Two store classes with distinct permissions and trust.** **Curated stores** (`role: curated`)
  hold human-reviewed OKF knowledge; agents read them and may only `propose` changes through a pull
  or merge request that a human merges. **Agent output stores** (`role: output`) hold unreviewed
  machine output (`generated` set, `verified` absent, `stale_after` set); agents may `write` to
  bound prefixes there, and nothing in an output store is ever followed as a procedure or enforced
  as a guard. External non-Git sources (`role: external`) are read-only by default and feed the
  PR-mediated OKF converter ([#5045](https://github.com/gke-labs/kube-agents/issues/5045)).
- **One directory per provider.** Adding a backend adds one package under
  `agents/platform/scripts/knowledge_providers/` and one entry in its `registry.py`, enforced by
  import-boundary and provider-name CI tests rather than runtime plugin loading.
- **Zero disruption to existing installs.** When `spec.integration.knowledge` is omitted, an
  install with a `role: gitops` repository defaults its curated store to that repository's
  `knowledge/` root and its output store to today's brokered report store; an install with no
  GitOps repository disables curated reads and keeps the local output store.

| Layer                  | Proposed home                                                                    | Modeled on                                                     |
| ---------------------- | -------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| Sandbox / agent client | `agents/platform/scripts/knowledge_client.py` (stdlib HTTP client + CLI)         | `agents/platform/scripts/vcs_client.py` / `vcs.py`             |
| Broker routes & policy | `agents/platform/scripts/knowledge_broker.py` (zero provider names)              | `agents/platform/scripts/vcs_broker.py`                        |
| Shared provider seam   | `agents/platform/scripts/knowledge_providers/{base,validate,errors,credentials}` | `agents/platform/scripts/providers/`                           |
| Provider packages      | `knowledge_providers/{git_repo,local_store,google_drive,sharepoint,confluence}/` | `providers/{github,gitlab,gitea,bitbucket}/`                   |
| Provider registration  | `agents/platform/scripts/knowledge_providers/registry.py` (`AVAILABLE`)          | `agents/platform/scripts/providers/registry.py`                |
| Declarative surface    | `spec.integration.knowledge` on `PlatformAgent` + `KNOWLEDGE_STORES_CONFIG`      | `spec.integration.{forges,repositories}` + `VCS_FORGES_CONFIG` |

---

## 1. Why

### The gap on `main`

Three adjacent systems on `main` already depend on operational knowledge, yet each stops at the
same boundary:

1. **OKF contract (`06` §5) and CI validation.** `docs/architecture/06-api-and-data-contracts.md`
   §5 aligns `knowledge/` with the published
   [Open Knowledge Format v0.2 `SPEC.md`](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md)
   and `scripts/check_okf_knowledge.py` validates `knowledge/` trees in CI. That contract still
   assumes `knowledge/` sits inside the GitOps repository and leaves a dedicated repository for
   later.
2. **Layer 2 decision memory (`docs/designs/memory.md`).** `sweep_repo_memory` and `a2a/memory`
   (`curator`) snapshot `managed_repos` and `context_repos` via `/v1/vcs/snapshot` and index
   `knowledge/`, ADRs, RFCs, and Markdown runbooks into `kube-agents-decisions`. They cannot read
   a Google Drive folder, a SharePoint library, or a Confluence space, and no skill yet looks up a
   matching runbook before acting (`agents/platform/docs/autoops-architecture.md` Contract 4 still
   lists `Runbook / knowledge retrieval` as `Future`).
3. **Report storage (`docs/designs/fleet-audit-report-store.md`).** `audit_report_store.py` and
   `/v1/audit-reports` store structured fleet-audit JSON envelopes on the gateway pod's `/opt/data`
   volume so ephemeral profile workers can write and the A2A gateway can read them. There is no
   general, backend-pluggable store for non-audit reports and investigation notes that chat posts
   can link to ([#4053](https://github.com/gke-labs/kube-agents/issues/4053),
   [#5047](https://github.com/gke-labs/kube-agents/issues/5047)).

Every install on `main` runs the A2A stack (NATS bus, A2A gateway, session pods, profile workers,
and the optional Curator pod; `PlatformAgentSpec.Mode` is deprecated and always reconciled to
`"next"`). Because profile workers and session pods mount no shared `/opt/data` volume and hold no
external credentials, any knowledge backend that works across the Platform Agent shell, Cluster
Agent sessions, profile workers, and Curator has to cross the credential broker over HTTP.

### Why a broker + provider seam instead of per-backend skills or direct MCP

Threading Drive, SharePoint, and Confluence APIs directly into skills would duplicate
authentication, redaction, egress control, and OKF handling across every skill and would place
provider credentials inside agent pods, violating
[`docs/credential-isolation-design.md`](../credential-isolation-design.md).

Using remote MCP servers as the sole integration mechanism fails for the same two reasons recorded
in [`version-control-support.md` §9](version-control-support.md#9-why-an-mcp-server-is-an-addition-not-the-mechanism):

- **Non-agent callers need the same backend.** `sweep_repo_memory`, the Go Curator pod
  (`a2a/cmd/curator`), scheduled conversion jobs ([#5045](https://github.com/gke-labs/kube-agents/issues/5045)),
  and the A2A gateway's report link retrieval run with no LLM in the loop. They need a typed HTTP
  broker route and library client, not an interactive MCP tool surface.
- **Credential and policy enforcement belong in one broker.** Putting authentication, root-scope
  checks, query sanitization, redaction, and read-only enforcement in `<agent>-credential-proxy`
  means every caller — interactive chat, Cluster Agent, profile worker, and Curator — goes through
  the same chokepoint. An optional MCP tool wrapper can sit in front of `/v1/knowledge/*` later
  without duplicating provider code.

---

## 2. Store Classes, Trust Separation, and Addressing

### Two store classes plus read-only external sources

Curated team knowledge and agent-generated output share the same protocol verbs, yet they have
opposite review, trust, and permission requirements
([#3045](https://github.com/gke-labs/kube-agents/issues/3045),
[#5047](https://github.com/gke-labs/kube-agents/issues/5047)):

| Store role | What lives there                                                       | Permitted verbs                                                     | Trust level & runtime rules                                                                                                         |
| ---------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `curated`  | Human-reviewed OKF runbooks, playbooks, blueprints, decisions, lessons | `capabilities`, `list`, `read`, `search`, `propose` (never `write`) | Trusted when Git-backed and `status` is `stable`/`active` (not `deprecated` or past `stale_after`). `propose` opens a PR/MR only.   |
| `external` | Team-owned non-Git sources (Drive folder, SharePoint site, Confluence) | `capabilities`, `list`, `read`, `search` (read-only by default)     | Tagged `trust:untrusted` in recall and tool output. Primary v1 use is feeding PR-mediated OKF conversion (#5045).                   |
| `output`   | Unreviewed agent reports, audit summaries, investigation drafts        | `capabilities`, `list`, `read`, `search`, `write` (bound prefix)    | Tagged `trust:untrusted`, `generated` required, `verified` forbidden, `stale_after` required. Never followed as a runbook or guard. |

Three invariants follow from this split:

1. **No direct writes to curated knowledge.** `POST /v1/knowledge/write` always returns `403
KNOWLEDGE_WRITE_FORBIDDEN` for any store whose role is `curated` or `external`. Curated
   knowledge changes exclusively via `propose`, which opens a reviewable pull or merge request on a
   Git-backed store.
2. **Output never becomes guidance automatically.** Runbook lookup (#5044), `declares:` posture
   resolution, and `decision`/`lesson` guard evaluation never query `role: output` stores. Turning
   an investigation report into a runbook or lesson requires an explicit promotion flow that calls
   `propose` against the `curated` store with `sources` pointing to the output record's stable
   reference.
3. **Read-only by default.** Every declared store defaults to `mode: read-only`. Enabling `propose`
   on a `curated` store or `write` on an `output` store requires explicit configuration (or the
   default local output store's narrow built-in schedule bindings).

### Store addressing and server-side root confinement

Callers never pass raw external URLs, hostnames, Drive IDs outside a configured root, or
Confluence base URLs to the broker. Instead, the administrator declares each store by a DNS-1123
logical `name` (for example `curated`, `output`, `sre-drive`, `platform-confluence`), and every
broker request names `{store, ...}`:

- **Canonical document identifier:** `<store>:<opaque-relative-id>`, where the suffix is a
  normalized relative path (for Git and local stores, checked against traversal, leading slashes,
  null bytes, and symlinks) or a provider item ID whose ancestry under the configured root is
  verified by the broker on every `read`.
- **Canonical citation URL:** Every `list`, `read`, and `search` response carries a broker-composed
  `webUrl` (for example the forge's `fileUrl` at the resolved commit SHA, a Drive file URL, a
  SharePoint item URL, or a Confluence page URL) so agents cite exact locations in PRs and chat
  cards without constructing URLs in the sandbox.

---

## 3. The Seam: `/v1/knowledge/*` Protocol

All knowledge routes live on the **stateful credential broker** (`<agent>-credential-proxy`, port
`8765`, alongside `/v1/vcs/*` and `/v1/audit-reports`). They are **not** served on the exec tier
(`credential-broker-exec-tier.md`), which handles `/v1/exec` and `/v1/gcp/`.

Like `/v1/vcs/*`, every knowledge route is stateless to its caller: no server-side session handle
and no shared checkout across requests.

### Verbs

| Route (`POST`)               | Request payload                                                                  | Response payload                                                                                                                                  | Notes                                                                                                     |
| ---------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `/v1/knowledge/capabilities` | `{store?}`                                                                       | `{stores: [{name, provider, role, mode, verbs, maxReadBytes, supportsSearch, configured, missing?}]}`                                             | Zero network/credential spend. Omit `store` to list every configured store on the install.                |
| `/v1/knowledge/list`         | `{store, prefix?, types?, statuses?, cursor?, limit?}`                           | `{store, provider, role, items: [{id, path, title, type?, status?, staleAfter?, mimeType, revision, updatedAt, webUrl}], nextCursor?, truncated}` | Lists entries under the declared store root. Excludes reserved `index.md` / `log.md` when `types` is set. |
| `/v1/knowledge/read`         | `{store, id, revision?, format?}`                                                | `{store, id, path, title, revision, updatedAt, webUrl, mimeType, content, frontmatter?, trust, stale?, redacted}`                                 | Enforces `maxReadBytes` (default `4 MiB` text). `format: "markdown"\|"text"\|"raw"` (provider-bounded).   |
| `/v1/knowledge/search`       | `{store, query?, types?, tags?, resource?, includeStale?, limit?}`               | `{store, hits: [{id, path, title, type?, status?, stale?, snippet, revision, webUrl, trust}], truncated}`                                         | Frontmatter filters + sanitized plain-text query. Default excludes `deprecated` and past-`stale_after`.   |
| `/v1/knowledge/propose`      | `{store, files: [{path, content}], title, body, branch?, baseBranch?, sources?}` | `{store, proposal: {url, number, branch, revision, forge, repo}}`                                                                                 | Curated Git stores only. Validates OKF v0.2 + `okf_guard.py`, then delegates to `vcs_broker` primitives.  |
| `/v1/knowledge/write`        | `{store, path, content, frontmatter}`                                            | `{store, id, path, revision, webUrl, writer, writtenAt}`                                                                                          | Output stores only. Requires `generated` and `stale_after`, forbids `verified`, stamps server `writer`.   |

### Error contract

Provider HTTP failures are translated in `knowledge_providers/errors.py` into actionable guidance
for the calling agent rather than forwarding raw upstream bodies, matching `providers/errors.py`:

| HTTP | Code                               | Meaning and caller action                                                                          |
| ---- | ---------------------------------- | -------------------------------------------------------------------------------------------------- |
| 400  | `KNOWLEDGE_INVALID_REQUEST`        | Malformed path, disallowed search operator, or invalid OKF frontmatter (lists exact field errors). |
| 401  | `KNOWLEDGE_UNAUTHENTICATED`        | Provider credential rejected or expired; stop and report to operator naming the store's Secret.    |
| 403  | `KNOWLEDGE_FORBIDDEN`              | Service identity lacks access to the configured folder, site, space, or repo; do not retry.        |
| 403  | `KNOWLEDGE_WRITE_FORBIDDEN`        | Caller attempted `write` on a `curated`/`external` store, or outside its permitted output prefix.  |
| 403  | `KNOWLEDGE_SCOPE_VIOLATION`        | Item ID resolved outside the store's configured root folder, SharePoint drive, or space allowlist. |
| 404  | `KNOWLEDGE_NOT_FOUND`              | Store or document not found (notes that missing share permissions can answer the same way).        |
| 409  | `KNOWLEDGE_CONFLICT`               | Proposal branch diverged or `okf_guard.py` / OKF validation blocked the change.                    |
| 413  | `KNOWLEDGE_DOC_TOO_LARGE`          | Document or export exceeded `maxReadBytes`; caller should narrow scope or skip binary payload.     |
| 429  | `KNOWLEDGE_RATE_LIMITED`           | Upstream API throttled; returns `retryAfterSeconds` when supplied and advises broader queries.     |
| 501  | `KNOWLEDGE_VERB_UNSUPPORTED`       | Provider or store mode does not serve this verb (for example `propose` on `google-drive`).         |
| 502  | `KNOWLEDGE_TLS_UNTRUSTED`          | Self-hosted endpoint TLS failed verification; check `caBundleRef` Secret.                          |
| 503  | `KNOWLEDGE_CREDENTIAL_UNAVAILABLE` | Mounted credential Secret or WIF token is absent; broker stays healthy for other stores.           |
| 503  | `KNOWLEDGE_UNAVAILABLE`            | Upstream 5xx or timeout; safe to retry once unchanged, or degrade gracefully for triage.           |

Per-provider `error_overrides` allow a provider to refine status guidance (for example, Microsoft
Graph `403` when `Sites.Selected` site grant was not granted versus tenant admin consent missing,
or Drive `404` when a folder was not shared with the service account).

---

## 4. Provider Interface and Modularity

Following [`version-control-support.md` §5](version-control-support.md#5-modularity), adding a
knowledge backend must be **one new package directory and one entry in `registry.py`**, touching no
shared broker file and no other provider's code.

### Package layout

```text
agents/platform/scripts/
  knowledge_client.py            # sandbox/agent stdlib HTTP client + CLI
  knowledge_broker.py            # /v1/knowledge/* routes, policy gates, OKF check, redaction
  knowledge_providers/
    __init__.py                  # public surface: KnowledgeProvider, KnowledgeRegistry
    base.py                      # KnowledgeProvider ABC, normalized dataclasses, StubKnowledgeProvider
    validate.py                  # store-name, path, query-grammar, size-cap, and OKF validators
    errors.py                    # shared status-to-guidance table
    transport.py                 # HttpTransport (host allowlist, no redirects, per-store CA, byte cap)
    credentials.py               # WIF/SA impersonation, OAuth2 client_credentials, static token file
    registry.py                  # AVAILABLE, load_store_entries(KNOWLEDGE_STORES_CONFIG), build_stores()
    git_repo/                    # Provider 1 & 2: GitOps knowledge/ and dedicated knowledge Git repo
    local_store/                 # Output fallback: brokered on-volume store (beside audit_report_store)
    google_drive/                # Provider 3: Google Drive folder / Shared Drive (read-only)
    sharepoint/                  # Provider 4: Microsoft SharePoint / OneDrive site (read-only)
    confluence/                  # Provider 5: Confluence Cloud / Data Center spaces (read-only)
  testdata/
    knowledge_providers/
      git_repo/  local_store/  google_drive/  sharepoint/  confluence/
```

### `KnowledgeProvider` contract

Each provider class declares:

| Member                                       | Responsibility                                                                                  |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `name`                                       | Provider discriminator (`git-repo`, `local-store`, `google-drive`, `sharepoint`, `confluence`). |
| `allowed_hosts(cfg)`                         | Exact outbound FQDNs this store instance may dial (feeds `HttpTransport` and egress policy).    |
| `supported_roles`                            | Subset of `{"curated", "external", "output"}` the provider can serve.                           |
| `verbs(cfg)`                                 | Supported verb set for this store instance, narrowed by `role` and `mode`.                      |
| `credential(cfg)`                            | Credential strategy object (`ensure`, `headers`), read fresh per call or cached with expiry.    |
| `for_config(config)`                         | Classmethod building `0..n` configured instances from `KNOWLEDGE_STORES_CONFIG`.                |
| `list`, `read`, `search`, `propose`, `write` | Request descriptors + response translators returning normalized dataclasses.                    |
| `error_overrides`                            | Optional per-provider status-to-guidance overrides.                                             |

### Prohibitions, boundary enforcement, and why no runtime plugins

Knowledge providers execute inside `<agent>-credential-proxy`, the same process that holds cluster,
forge, and chat credentials. Therefore:

- **Build-time packages only.** No runtime plugin loader or dynamic import path is allowed in the
  broker container, for the same credential-exfiltration reason stated in
  [`version-control-support.md` §5](version-control-support.md#why-not-a-runtime-plugin).
- **No subprocesses or direct network sockets.** A provider may not call `subprocess`, open raw
  sockets, pick scratch paths, set timeouts, or bypass byte ceilings. Git providers delegate
  repository operations to the broker's existing `VcsBroker` (`snapshot`, `clone`, `publish`,
  `proposal-create`); HTTP providers emit request descriptors executed by the shared `HttpTransport`
  with `allow_redirects=False` and strict host pinning.
- **Two CI tests enforce the seam.** An AST import-boundary test verifies that no module outside
  `knowledge_providers/` imports a concrete provider package (only `registry.py` imports classes
  listed in `AVAILABLE`) and that a provider imports only `knowledge_providers.{base,validate,errors,transport,credentials}`
  and the standard library (plus `vcs_broker` for `git_repo` alone). A grep guard fails CI if any
  provider name (`google_drive`, `sharepoint`, `confluence`, `git_repo`) appears in
  `knowledge_broker.py` or shared contract modules.
- **Parameterized contract suite.** One pytest suite runs over `AVAILABLE` using recorded fixtures
  under `testdata/knowledge_providers/<provider>/`, verifying normalized shapes, root-confinement
  refusals, query-injection refusals, byte caps, and redaction for every provider.

---

## 5. Providers in Suggested Delivery Order

### 1. GitOps repository `knowledge/` root (`git-repo`, default)

Reuses the install's existing `role: gitops` repository from `spec.integration.repositories` with
zero new credentials and zero new egress hosts. `list`, `read`, and `search` operate on a
short-lived `VcsBroker.snapshot` of the configured `rootPath` (default `knowledge/`) at the
repository's pinned `baseBranch` (or default branch), cached briefly by `(repo, head_sha)` on the
broker's scratch `emptyDir` with bounded LRU eviction so repeated runbook searches in one turn do
not re-clone. `propose` validates OKF frontmatter and `okf_guard.py`, then creates a branch under
`platform-agent/knowledge/` and opens a proposal through `VcsBroker` (`clone`, `publish`,
`proposal-create`).

### 2. Dedicated knowledge Git repository (`git-repo`)

Points at a separate repository declared on any forge in `spec.integration.forges` (GitHub, GitHub
Enterprise Server, GitLab, Gitea, Bitbucket Cloud, Bitbucket Data Center), with a configurable
`rootPath` (default `knowledge/`, or `.` for a dedicated OKF-only repo). When the repository has
`role: context` (or the store sets `mode: read-only`), the broker reads it with the forge's
read-only credential (`read_credential` / `read-token`) and refuses `propose`. When declared with
write access (`role: managed`) and `mode: propose`, the agent can open PRs/MRs against the
knowledge repository without mixing runbook churn into cluster-manifest history.

### 3. Google Drive folder (`google-drive`, read-only)

- **Identity (respecting D12):** Uses a Google Service Account — either the install's Workload
  Identity GSA or a dedicated impersonated GSA (matching `spec.integration.googleChat.serviceAccountEmail`)
  scoped to `https://www.googleapis.com/auth/drive.readonly`. The customer shares one specific
  Drive folder or Shared Drive (`viewer` role) with that service account email. No per-user OAuth
  browser flow or user refresh tokens exist anywhere in the cluster.
- **Root confinement:** Every `list` and `search` constrains Drive API v3 queries to descendants of
  the configured `folderId` (and optional `driveId` with `supportsAllDrives=true`). Every `read`
  walks the file's `parents` chain up to `folderId` before returning bytes, refusing files outside
  the declared root (`403 KNOWLEDGE_SCOPE_VIOLATION`) even if the service account was granted
  access elsewhere.
- **Export & format handling:** Native Google Docs (`application/vnd.google-apps.document`) export
  via `files.export` to `text/markdown` (or `text/plain` fallback); plain Markdown/text files read
  via `files.get?alt=media`; PDF/DOCX return bounded raw bytes for the conversion pipeline (#5045)
  when `format: "raw"` is requested by an authorized converter caller. Images, audio, and video
  MIME types are skipped.
- **Universe domain:** Respects `spec.harness.universeDomain` (`GOOGLE_CLOUD_UNIVERSE_DOMAIN`),
  composing the Drive endpoint under the install's universe rather than hardcoding
  `www.googleapis.com`.

### 4. OneDrive / SharePoint (`sharepoint`, read-only)

- **Identity (respecting D12):** App-only Microsoft Entra ID OAuth 2.0 `client_credentials` grant
  against `https://login.microsoftonline.com/<tenantId>/oauth2/v2.0/token` for scope
  `https://graph.microsoft.com/.default`, reusing the exact token-exchange and in-memory caching
  pattern already shipped in `TeamsRelay` (`agents/platform/scripts/credential_proxy.py`). The
  Entra app registration holds `Sites.Selected` (never tenant-wide `Sites.Read.All`), and the
  SharePoint site admin grants that app read-only (`read`) permission to the single declared site.
  Credentials live in a Kubernetes Secret (`client-id`, `client-secret`) mounted only into
  `<agent>-credential-proxy`.
- **Root confinement:** The broker resolves the declared `(siteUrl, driveName, folderPath)` to
  immutable `(siteId, driveId, rootItemId)` and restricts every Graph `/drives/{driveId}/items`
  call to descendants of `rootItemId`, verifying `parentReference.path` on `read`.

### 5. Confluence (`confluence`, read-only, reusing #1987)

- **Identity:** Reuses the service-identity and broker boundary designed for Atlassian integrations
  ([#1987](https://github.com/gke-labs/kube-agents/issues/1987)): a dedicated read-only Atlassian
  service account API token (Confluence Cloud, `Basic` auth over HTTPS) or Personal Access Token
  (Confluence Data Center, `Bearer` auth over HTTPS with optional `caBundleRef`), stored in a
  Kubernetes Secret mounted only into `<agent>-credential-proxy`.
- **Root confinement:** Declares an explicit non-empty `spaceKeys` list (and optional `rootPageId`
  ancestor filter). Every CQL search query is constructed server-side by the provider with
  `space in (...)` appended outside the sanitized plain-text query, and every `read` checks the
  returned page's `spaceId`/`spaceKey` (and ancestor list when `rootPageId` is set) before
  returning content.
- **Conversion:** Reads Confluence REST API v2 `body.storage` (XHTML) or `body.atlas_doc_format`
  and returns bounded text/HTML for the converter (#5045) or deterministic XHTML-to-Markdown
  conversion on read. Write endpoints are never called.

### Agent output store backends (`role: output`)

1. **Default local brokered store (`local-store`):** Available on every install, including installs
   with no GitOps repository. Stores OKF Markdown reports under a dedicated root beside
   `audit_report_store.py` (never inside `fleet-audit/reports/runs/`, preserving audit ring
   invariants in `fleet-audit-report-store.md` §2), served through the same authenticated gateway
   listener with fd-relative no-symlink writes, per-writer stamping (`X-Knowledge-Writer`), and
   `stale_after` pruning.
2. **Optional external output stores (Phase 4):** A dedicated output Git repository/branch, a GCS
   bucket (via Workload Identity), or a dedicated Drive/SharePoint folder can back `role: output`
   when a team wants reports directly linkable outside the cluster console. Curated stores remain
   strictly separate regardless of which backend backs `output`.

---

## 6. Security and Credential-Proxy Boundary

### Caller-role authorization (`ROUTE_ROLES` and `ROLE_KNOWLEDGE_VERBS`)

`<agent>-credential-proxy` enforces caller identity via projected Kubernetes ServiceAccount tokens
with per-pod audiences (`credential_proxy.py` `ROUTE_ROLES`, `platformagent_broker_split.go`). The
knowledge routes extend that exact model with `KNOWLEDGE_ROUTE_PREFIX = "/v1/knowledge/"` and a
per-role verb allowlist (`ROLE_KNOWLEDGE_VERBS`):

| Caller role   | Audience / source pod                      | Allowed `/v1/knowledge/*` verbs                                                      | Store & write constraints                                                                                                                                                                       |
| ------------- | ------------------------------------------ | ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `shell`       | Platform Agent shell sandbox               | `capabilities`, `list`, `read`, `search`, `propose`, `write`                         | `propose` on `curated` Git stores (when enabled); `write` on `output` stores only.                                                                                                              |
| `profile`     | Ephemeral profile workers (`AgentProfile`) | `capabilities`, `list`, `read`, `search`; `propose` and `write` gated by task claims | Requires verified task credential. `propose` requires `vcs:proposal-create` (or `knowledge:propose`) permit; `write` is bound to the task's own stream/prefix (mirroring `audit_report_store`). |
| `curator`     | Go Curator pod (`a2a/cmd/curator`)         | `capabilities`, `list`, `read`                                                       | Read-only across configured `curated` and opted-in `external` stores. Never `propose` or `write`.                                                                                               |
| `session`     | Per-conversation A2A session pod           | `capabilities`, `list`, `read`, `search` (opt-in / curated read-only)                | Redacted reads of `curated` stores only. Never `propose`, never `write`.                                                                                                                        |
| `a2a-reports` | A2A gateway report relay                   | `read` (exact `role: output` ID only)                                                | Redacted single-document fetch for chat/console/MCP report links (#4056, #5047). No `list`/`search`/`write`.                                                                                    |
| `mcp`         | A2A MCP server pod (`a2a/cmd/mcp`)         | `read` (exact `role: output` ID only, if exposed)                                    | Same narrow read-only boundary as `a2a-reports`; no curated write or arbitrary search.                                                                                                          |

Roles absent from this table (`chat`, `a2a-chat`, `watcher`, `scope`, `teams-inbound`) receive
`403 CALLER_ROLE_FORBIDDEN` on `/v1/knowledge/*`.

### Query sanitization, redaction, and egress

- **Search-grammar allowlist:** Free-text `query` inputs to `/v1/knowledge/search` pass the same
  plain-word allowlist used by `profile_issue_query_refusal` in `credential_proxy.py` (length cap
  256 characters, refusing colons, quotes, backslashes, parentheses, and boolean operators
  `AND`/`OR`/`NOT`) so untrusted model prompts cannot inject provider search operators (Drive
  `parents in`, Confluence CQL `space =`, SharePoint KQL `path:`) to escape the declared root.
- **Inbound and outbound redaction:** Every document body and search snippet returned to
  `REDACTED_OUTPUT_ROLES` (`profile`, `session`, `a2a-reports`, `mcp`), and every broker log line,
  passes through `credential_shapes.py` redaction (masking private keys, cloud tokens, GitHub/GitLab
  PATs, and Bearer/Basic headers). Outbound search queries sent to external SaaS APIs pass through
  the same redactor before leaving the cluster so accidental tokens in a cluster error message are
  never transmitted to Google Drive, SharePoint, or Confluence.
- **Egress allowlist alignment:** Today `buildFQDNNetworkPolicy`
  (`k8s-operator/internal/controller/platformagent_manifests.go`) selects `<agent>-gateway` only,
  while `<agent>-credential-proxy` has open egress (tracked in
  [`version-control-support.md` §12 #5](version-control-support.md#12-open-questions)). Each
  knowledge provider implements `allowed_hosts()` and the operator exposes
  `KnowledgeEgressPatterns(integration)` beside `ForgeEgressPatterns(integration)` so that:
  1. `HttpTransport` inside the broker enforces the exact host allowlist and refuses all HTTP
     redirects (`allow_redirects=False`) from day one, preventing SSRF and credential forwarding on
     any cluster (including clusters without Dataplane V2).
  2. When a broker-scoped `FQDNNetworkPolicy` is enabled, external knowledge provider hosts are
     derived automatically from `spec.integration.knowledge` without manual allowlist edits.
- **Nothing leaves the install automatically:** External providers are contacted only when an
  agent, user, or explicitly configured schedule invokes a verb on that store. No cluster
  telemetry, pod logs, or unrequested data is pushed outward.
- **Secret handling:** Provider Secrets are projected read-only (`0400`, `Optional: true`) under
  `/var/run/kube-agents/knowledge-credentials/<store>/` into `<agent>-credential-proxy` alone,
  never into the gateway, sandbox, session, profile, or Curator pods. `KNOWLEDGE_STORES_CONFIG`
  (`/etc/credential-proxy/knowledge-stores.json`) is added to `SensitiveEnvVars` in
  `k8s-operator/api/v1alpha1/common_types.go` and dropped by `mergeCredentialProxyEnv` so
  `spec.deployment.env` cannot redirect the broker's store registry.

---

## 7. OKF v0.2 Contract Alignment

This backend enforces the published
[Open Knowledge Format v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md)
contract codified in `docs/architecture/06-api-and-data-contracts.md` §5 and
`scripts/check_okf_knowledge.py`:

- **Reserved filenames (`index.md`, `log.md`) and root overviews:** `index.md` and `log.md` are
  reserved at every directory level (and `STATE_OF_BEING.md` / `README.md` at the `knowledge/`
  root). `list` and `search` with `types` filters skip them; `propose` rejects any file named
  `index.md` or `log.md` that declares a `type` key, and rejects `type: index` or `type: log`.
- **Required frontmatter & status vocabularies:** Every curated OKF entry must carry valid YAML
  frontmatter with a non-empty string `type`. Standard types (`runbook`, `cluster-blueprint`,
  `tenancy-model`, `metric-definition`, `escalation`, `observation`) use OKF v0.2 `status` values
  (`draft`, `stable`, `deprecated`); `decision` and `lesson` use the `kube-agents` lifecycle
  values (`proposed`, `active`, `rejected`, `superseded`, `retired`). As aligned in
  `a2a/memory/types.go` and `decision_memory.py` (`EXTERNAL_DOC_STATUS_MAP`), `status: stable`
  maps to `active` for retrieval and recall.
- **Provenance and freshness (`sources`, `generated`, `verified`, `stale_after`):**
  - Converted runbooks (#5045) arrive via `propose` with `type: runbook`, `sources` containing the
    upstream document URL and revision, `generated: {by, at}`, `status: draft`, and no `verified`
    field until human PR review.
  - Agent output entries (#5047) arrive via `write` with `generated: {by, at}`, `stale_after`
    within the store's retention cap (default 30 days), and no `verified` field (the broker
    rejects `write` requests that set `verified`).
  - Runbook readers (#5044) check `status` and `stale_after`: entries with `status: deprecated` or
    `stale_after < today (UTC)` are flagged `stale: true` in `read`/`search` responses and are
    reported as stale rather than followed.
- **Posture declarations (`declares:`):** `declares:` exceptions and `.kube-agents/intent.yaml` are
  honored strictly from Git-backed curated/context repositories (`git-repo` provider). External
  SaaS stores (`google-drive`, `sharepoint`, `confluence`) and `role: output` stores never grant
  audit posture declarations or `severity: block` guards.

---

## 8. Integration with Existing Subsystems

### Layer 2 organizational memory (`a2a/memory`, Curator, `decision_memory.py`, #2903)

Today `sweep_repo_memory` (`agents/platform/scripts/github_scan_gate.py`) and `a2a/memory/curator.go`
pull Git snapshots via `CALLER_ROLE_CURATOR` on `/v1/vcs/snapshot` and index `knowledge/` plus
`EXTERNAL_DOC_DIRS` into Hindsight bank `kube-agents-decisions`. With the knowledge broker:

- **Curator read unification:** `Curator` keeps `/v1/vcs/snapshot` for GitOps manifest/diff checks
  and adds `/v1/knowledge/list` + `/v1/knowledge/read` (under the same `curator` role) to index
  configured `curated` stores (including a dedicated knowledge repository) and any external stores
  explicitly marked `indexInMemory: true`. Every indexed chunk carries `store:<name>` and
  `provider:<provider>` tags alongside `scope:shared`.
- **Untrusted provenance preservation:** Records indexed from `role: external` stores or
  `role: context` repositories are tagged `trust:untrusted`, have `guard.expect` cleared, have
  `severity: block` capped at `warn`, and are wrapped in `<untrusted_memory_data source="...">` on
  recall (reusing the exact sanitizer in `a2a/memory/curator.go` and `decision_memory.py`).
- **Deterministic pre-push guards stay fast and local to Git:** `okf_guard.py` inside
  `/v1/vcs/publish` continues to evaluate `decision` and `lesson` guards strictly against Git
  objects, with zero Hindsight calls and zero network calls to external SaaS knowledge providers.

### Runbook lookup and citation (#5044, #2296, AutoOps Contract 4)

- **Contract 4 closure:** Adding `knowledge_client.py` (and its skill wrapper) wires the
  `Runbook / knowledge retrieval` row of
  [`agents/platform/docs/autoops-architecture.md`](../../agents/platform/docs/autoops-architecture.md#contract-4--context-reach)
  without changing the AutoOps ingestion, session, judgment, or remediation spine.
- **Lookup behavior:** Before acting, Platform and Cluster agents call `knowledge search` against
  the `curated` store filtering `type: runbook` plus matching `tags` / `resource` / plain-text
  terms, starting with deterministic frontmatter and text search (adding Hindsight semantic recall
  only where evals show text search misses, per `02-agent-personas.md` and #5044). When a runbook
  applies, the plan names it, the PR or chat card cites its `webUrl` (and upstream `sources` when
  converted), and every action still passes normal read-only tools, RBAC, and human approval gates.
- **Replacing chat-pasted SOPs (#2296):** When no curated store is configured, onboarding and triage
  never ask users to paste runbooks into chat; they inform the user that curated knowledge is
  unconfigured and point to the CR / installer configuration.

### Document conversion pipeline (#5045)

Non-Markdown runbooks in Google Drive, SharePoint, or Confluence are converted into reviewed OKF
v0.2 Markdown files via PR rather than read as authoritative instructions live in v1:

1. A scheduled or user-requested conversion task calls `/v1/knowledge/list` and `/v1/knowledge/read`
   against a configured `role: external` store.
2. The converter tracks `(sourceId, revision)` against existing `sources` entries in the `curated`
   store so unchanged documents are skipped and modified upstream documents generate a diffable
   update PR rather than silently overwriting curated text.
3. The converter emits an OKF v0.2 `type: runbook` document (`sources`, `generated`, `status:
draft`, no `verified`) and submits it via `/v1/knowledge/propose` against the `curated` Git
   store. A human reviews and merges (and promotes `status` to `stable` / sets `verified`).

### Agent output store and linkable reports (#4053, #5047)

[`docs/designs/fleet-audit-report-store.md`](fleet-audit-report-store.md) keeps structured audit
envelopes (`latest.json`, 14-run ring, `run-starts/`) as the fleet-audit delta engine. That
structured JSON ring stays intact. The knowledge broker's `role: output` store complements it by
giving scheduled jobs, investigations, and audits a uniform `write` / `read` surface for
human-readable OKF reports that chat posts link to across Slack, Google Chat, Teams, the web
console, and the MCP door (#4056, #5047).

---

## 9. Declarative Surface and Installer Integration

### `PlatformAgent` CR (`spec.integration.knowledge`)

`IntegrationSpec` (`k8s-operator/api/v1alpha1/common_types.go`) gains an optional `knowledge` block
beside `forges` and `repositories`:

```yaml
spec:
  integration:
    forges:
      - name: github
        provider: github
        namespace: acme
    repositories:
      - forge: github
        repository: platform-gitops
        role: gitops
      - forge: github
        repository: sre-runbooks
        role: managed
    knowledge:
      curatedStore: sre-runbooks-kb # optional; defaults to gitops repo's knowledge/ when omitted
      outputStore: local-reports # optional; defaults to built-in local-store when omitted
      stores:
        - name: sre-runbooks-kb
          role: curated
          mode: propose # read-only (default) | propose
          provider: git-repo
          gitRepo:
            forge: github
            repository: sre-runbooks
            rootPath: knowledge
            baseBranch: main
        - name: team-drive-sops
          role: external
          mode: read-only
          provider: google-drive
          googleDrive:
            folderId: 1AbCdEfGhIjKlMnOpQrStUvWxYz
            serviceAccountEmail: ka-drive-reader@my-project.iam.gserviceaccount.com # optional; defaults to broker GSA
        - name: corp-sharepoint
          role: external
          mode: read-only
          provider: sharepoint
          sharePoint:
            tenantId: 00000000-0000-0000-0000-000000000000
            siteUrl: https://acme.sharepoint.com/sites/platform-ops
            driveName: Documents
            folderPath: Runbooks
            credentialsRef:
              name: sharepoint-knowledge-creds
        - name: platform-confluence
          role: external
          mode: read-only
          provider: confluence
          confluence:
            host: acme.atlassian.net
            spaceKeys: [PLAT, SRE]
            credentialsRef:
              name: confluence-knowledge-creds
            # caBundleRef optional for self-hosted Confluence Data Center
```

**CRD validation rules (CEL + admission webhook, so rules hold even with `ENABLE_WEBHOOKS=false`):**

- `stores[].name` is unique, DNS-1123 lowercase (`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, max 63 chars).
- `mode: write` is rejected unless `role == "output"`; `mode: propose` is rejected unless
  `role == "curated"` and `provider == "git-repo"`.
- `gitRepo.forge` must reference a declared entry in `spec.integration.forges`.
- `confluence.spaceKeys` must be non-empty; `googleDrive.folderId` must be non-empty;
  `sharePoint.tenantId` and `siteUrl` must be valid HTTPS host/path values with no query or
  fragment.
- `curatedStore` and `outputStore`, when set, must name entries in `stores` with matching `role`.

**Backward compatibility and no-GitOps installs:**

- **GitOps repo present, `spec.integration.knowledge` omitted:** The operator synthesizes one
  implicit `curated` store (`git-repo` at `<gitops-repo>/knowledge/`, `mode: propose`) and one
  implicit `output` store (`local-store`). Existing installs require zero CR edits.
- **No GitOps repo, `spec.integration.knowledge` omitted:** Curated knowledge is disabled
  (`capabilities` reports `curatedStore: null`) and `output` resolves to `local-store` (matching
  today's no-forge report store behavior in `fleet-audit-report-store.md` §2).

### Installer (`install.sh`, Terraform, and Helm)

Per `AGENTS.md`, `terraform/examples/full-install` + the `kube-agents` Helm chart remain the single
installation engine; `install.sh` and `upgrade.sh` only collect inputs and generate
`terraform.tfvars`:

- During interactive `install.sh`, if no GitOps repository is configured (or when the admin opts
  into a separate knowledge location), the installer prompts for a knowledge backend (`git-repo`,
  `google-drive`, `sharepoint`, `confluence`, or `none`).
- Non-interactive installs accept declarative keys in `install.env` (for example
  `KNOWLEDGE_CURATED_PROVIDER`, `KNOWLEDGE_CURATED_REPO`, `KNOWLEDGE_DRIVE_FOLDER_ID`) and pipe any
  static credentials (`--knowledge-token-file` or no-echo prompt) directly into a Kubernetes Secret
  in the agent namespace, keeping tokens out of `install.env`, Helm values, and Terraform state.
- Changing the knowledge location post-install is a declarative update to `PlatformAgent` (or
  `install.env` + `upgrade.sh`), which re-renders `knowledge-stores.json` in the broker's policy
  ConfigMap and rolls only `<agent>-credential-proxy`.

---

## 10. Failure Modes and Operational Bounds

| Failure mode                                   | System behavior                                                                                                                                                                                                                                    |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Missing or rotated provider Secret             | Optional volume mount (`Optional: true`) lets broker start normally; calls to that store return `503 KNOWLEDGE_CREDENTIAL_UNAVAILABLE`. Updating the Secret takes effect on the next call without restart (per-call file read).                    |
| Revoked Drive share / missing SharePoint grant | Provider returns `403 KNOWLEDGE_FORBIDDEN` (or `404` with explicit share-check guidance); other stores and cluster operations stay unaffected.                                                                                                     |
| External provider outage or latency spike      | Per-request timeout (default `15s` for `list`/`read`/`search`) caps broker worker tie-up; triage degrades gracefully to built-in skills and notes that runbook lookup was unavailable. `/v1/vcs/publish` never calls external knowledge providers. |
| Oversized file or binary export (large PDF)    | `HttpTransport` aborts reads above `maxReadBytes` (`4 MiB` default, `16 MiB` converter cap) with `413 KNOWLEDGE_DOC_TOO_LARGE` before buffering whole payloads in broker memory.                                                                   |
| Prompt injection inside a runbook or wiki page | External and unverified outputs carry `trust:untrusted` and `<untrusted_memory_data>` framing; curated runbooks shape plans only and never bypass RBAC, read-only guards, or human approval.                                                       |
| Malformed OKF frontmatter on `propose`         | Broker runs `check_okf_knowledge.py` rules before pushing any branch; returns `400 KNOWLEDGE_INVALID_REQUEST` naming the exact key/line so broken PRs are never opened.                                                                            |
| Secret accidentally committed to a wiki or doc | Broker redacts `credential_shapes.py` patterns on responses to `REDACTED_OUTPUT_ROLES` and on all logs, setting `redacted: true` in the response metadata.                                                                                         |
| Broker memory / concurrency exhaustion         | Knowledge routes run on the stateful broker with bounded concurrency slots and memory reservations, never competing with exec-tier `kubectl`/`gcloud` slots once the exec split is active.                                                         |

Observability follows the existing credential-proxy metrics surface (`kubeagents_*` scraped by
`<name>-credential-proxy-monitoring`): the broker exports
`kubeagents_knowledge_requests_total{store,provider,verb,status}` and
`kubeagents_knowledge_request_duration_seconds{store,provider,verb}` (label cardinality bounded to
declared store names).

---

## 11. Phased Delivery Plan

| Phase | Deliverables                                                                                                                                                                                                                                    | Unblocks                                                             |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| **1** | `knowledge_broker.py`, `knowledge_providers/{base,validate,errors,transport,credentials,registry,git_repo,local_store}`, `knowledge_client.py`, `spec.integration.knowledge` CRD + operator Secret/ConfigMap render, AST + contract test suite. | #5044 (runbook lookup & citation), #2296, #5047 (local output store) |
| **2** | `knowledge_providers/google_drive/` (service account + `folderId` confinement + Docs export), PR-mediated OKF converter workflow (#5045) for Google Docs and PDFs.                                                                              | #5045 (Drive + PDF conversion)                                       |
| **3** | `knowledge_providers/sharepoint/` (Entra `client_credentials` + `Sites.Selected`) and `knowledge_providers/confluence/` (reusing #1987 boundary), plus Curator `/v1/knowledge/*` indexing for dedicated knowledge repos.                        | #5045 (Confluence), #2903 multi-repo knowledge                       |
| **4** | Externally linkable `role: output` store (console link surface and/or optional GCS/folder backend) and broker-scoped `FQDNNetworkPolicy` wiring.                                                                                                | #4056 / #5047 clickable report links                                 |

---

## 12. Open Questions

1. **Cross-repository `decision`/`lesson` guard evaluation on `/v1/vcs/publish` (`@dshnayder`).**
   When an install keeps curated knowledge in a dedicated knowledge repository separate from the
   target GitOps repository, should deterministic `okf_guard.py` checks on `/v1/vcs/publish` stay
   scoped strictly to the target repository's own `knowledge/` tree, or also fetch and evaluate
   `decision`/`lesson` records from the dedicated knowledge repository?
   - _Trade-off:_ evaluating only the target repository preserves sub-100ms, single-repo atomic
     publish checks; evaluating a second repository adds a second snapshot fetch on every publish
     and introduces cross-repo version skew.
   - _Recommendation:_ keep `/v1/vcs/publish` blocking guards scoped to the target repository in
     Phase 1, index the dedicated knowledge repository into `kube-agents-decisions` for recall and
     `why` queries, and add cross-repo blocking guards only if multi-repo fleet teams ask for them.

2. **Where untrusted binary conversion (PDF / DOCX) executes (`@dshnayder`, `@bradhoekstra`).**
   Google Docs export and Confluence XHTML are API-level text conversions, whereas parsing
   arbitrary customer PDFs and Word documents (#5045) invokes complex binary parsers that are a
   poor fit inside the credentialed `<agent>-credential-proxy` pod. Which boundary should parse
   binary attachments: a credential-free conversion profile worker receiving bounded raw bytes from
   `/v1/knowledge/read?format=raw`, or a separate unprivileged converter sidecar/Job?
   - _Recommendation:_ keep `<agent>-credential-proxy` free of binary document parsers; return
     bounded raw bytes to a credential-free profile Job pod that extracts text, drafts the OKF v0.2
     Markdown, and submits it via `/v1/knowledge/propose`.

3. **First clickable URL surface for `role: output` reports (`@bradhoekstra`).**
   Chat posts need a URL a human can open from Slack or Google Chat (#5047). The built-in
   `local-store` lives inside the cluster on `/opt/data` and has no external URL unless fronted by
   the A2A web console. Which linkable backend should Phase 4 prioritize: authenticated report
   routes on the A2A web console reading `/v1/knowledge/read` via `a2a-reports`, a customer GCS
   bucket, or a dedicated Git/Drive output folder?
   - _Recommendation:_ front `local-store` through the A2A web console first (zero new cloud
     storage or IAM grants on default installs), and offer a customer GCS bucket as the first
     optional external `output` provider.

4. **Dedicated Git knowledge repository declaration shape (`@dshnayder`, `@bradhoekstra`).**
   Should a dedicated Git knowledge repository be registered only inside
   `spec.integration.knowledge.stores[]` (referencing a declared forge in `spec.integration.forges`),
   or also appear in `spec.integration.repositories` with a new `role: knowledge`?
   - _Trade-off:_ reusing `spec.integration.repositories` keeps all GitHub App minter policy and
     `managed_repos`/`context_repos` seeding in one list; keeping knowledge stores exclusively in
     `spec.integration.knowledge` avoids splitting knowledge configuration across two CR lists when
     mixing Git and non-Git backends.
   - _Recommendation:_ declare every knowledge store uniformly under `spec.integration.knowledge`,
     and have the operator automatically include any `git-repo` store's repository in the token
     minter policy and broker repository gate with the permission matching its `mode` (`read-only`
     versus `propose`).

5. **Broker-scoped `FQDNNetworkPolicy` gating for external SaaS providers (`@bradhoekstra`).**
   Shared with [`version-control-support.md` §12 #5](version-control-support.md#12-open-questions).
   Adding Google Drive, SharePoint, and Confluence expands the broker's outbound surface to
   additional internet endpoints. What is the required egress gate before Phase 2/3 providers leave
   experimental status: the broker's in-process `HttpTransport` host allowlist +
   `allow_redirects=False` alone, or a companion broker `FQDNNetworkPolicy` derived from
   `ForgeEgressPatterns` + `KnowledgeEgressPatterns`?

---

## Related

- [`version-control-support.md`](version-control-support.md) — the forge-neutral broker and
  provider seam this design follows.
- [`docs/architecture/06-api-and-data-contracts.md` §5](../architecture/06-api-and-data-contracts.md#5-okf-knowledge-contract) —
  the Open Knowledge Format v0.2 contract and `knowledge/` rules.
- [`memory.md`](memory.md) — Layer 1 conversational memory and Layer 2 decision/lesson memory
  (`a2a/memory`, Curator, `decision_memory.py`).
- [`fleet-audit-report-store.md`](fleet-audit-report-store.md) — the brokered on-volume report
  store that serves as the default local output fallback.
- [`../credential-isolation-design.md`](../credential-isolation-design.md) — the three-pod
  credential isolation invariant.
- [`../../agents/platform/docs/autoops-architecture.md`](../../agents/platform/docs/autoops-architecture.md#contract-4--context-reach) —
  AutoOps Contract 4 (`Runbook / knowledge retrieval`).
- Issues [#5043](https://github.com/gke-labs/kube-agents/issues/5043),
  [#5048](https://github.com/gke-labs/kube-agents/issues/5048),
  [#5044](https://github.com/gke-labs/kube-agents/issues/5044),
  [#5045](https://github.com/gke-labs/kube-agents/issues/5045),
  [#5047](https://github.com/gke-labs/kube-agents/issues/5047),
  [#2296](https://github.com/gke-labs/kube-agents/issues/2296),
  [#2903](https://github.com/gke-labs/kube-agents/issues/2903),
  [#4053](https://github.com/gke-labs/kube-agents/issues/4053), and
  [#1987](https://github.com/gke-labs/kube-agents/issues/1987).
