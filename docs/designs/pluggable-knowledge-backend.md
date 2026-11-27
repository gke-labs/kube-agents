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

**Does not cover:** per-user OAuth token brokering (excluded so headless cluster pods never store,
refresh, or act on per-user interactive OAuth grants; see
[#1720](https://github.com/gke-labs/kube-agents/issues/1720)), direct agent writes to curated
runbooks without pull-request review, media or OCR conversion of images and video, or replacing
Hindsight Layer 1 conversational memory.

## Summary

Platform teams keep runbooks, playbooks, cluster baselines, and SOPs in Git repositories, Google
Drive folders, SharePoint libraries, and Confluence spaces. Today `kube-agents` recognizes only
`knowledge/` inside a registered Git repository (`06` §5). An install without a GitOps repository
has nowhere to read curated knowledge at all, and an install whose runbooks live outside the
GitOps repository has no brokered path to reach them. Pasting runbooks into chat fails on
staleness, length limits, and missing citations
([#2296](https://github.com/gke-labs/kube-agents/issues/2296)). Scheduled audits, investigations,
and watches also produce unreviewed reports that chat should link to rather than paste
([#5047](https://github.com/gke-labs/kube-agents/issues/5047)), and those unreviewed outputs must
stay separate from human-curated runbooks.

This design adds a **knowledge broker** inside `<agent>-credential-proxy` backed by build-time
**knowledge providers**, mirroring [`version-control-support.md`](version-control-support.md).
Agents, skills, profile workers, and the Curator call neutral `/v1/knowledge/<verb>` routes
(`capabilities`, `list`, `read`, `search`, `propose`, `write`) naming a declared logical store
rather than a backend URL or SDK. Every credential stays inside the credential broker pod.

Curated team knowledge (`role: curated`) and unreviewed agent output (`role: output`) share the
same protocol with opposite permissions. Agents read curated stores and may only `propose` changes
through a pull or merge request that a human reviews and merges. Agents may `write` to bound
prefixes of an output store (with OKF `generated` and `stale_after` set and `verified` forbidden),
and nothing in an output store is ever followed as a procedure or enforced as a guard. External
non-Git sources (`role: external`) are read-only by default and feed the PR-mediated OKF converter
([#5045](https://github.com/gke-labs/kube-agents/issues/5045)).

Adding a backend means adding one directory under `agents/platform/scripts/knowledge_providers/`
and one entry in its `registry.py`, guarded by AST import-boundary and provider-name checks in CI
rather than a runtime plugin loader. Existing installs need no configuration change: when
`spec.integration.knowledge` is omitted, an install with a `role: gitops` repository defaults its
curated store to `<gitops-repo>/knowledge/` and its output store to a brokered local store beside
`audit_report_store`; an install with no GitOps repository disables curated reads and keeps the
local output store.

| Layer                  | Proposed home                                                                                                      | Modeled on                                                                                       |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| Sandbox / agent client | `agents/platform/scripts/knowledge_client.py` (stdlib HTTP client + CLI)                                           | `agents/platform/scripts/vcs_client.py`, `agents/platform/skills/version-control/scripts/vcs.py` |
| Broker routes & policy | `agents/platform/scripts/knowledge_broker.py` (zero provider names)                                                | `agents/platform/scripts/vcs_broker.py`                                                          |
| Shared provider seam   | `agents/platform/scripts/knowledge_providers/{base,validate,errors,credentials}` + shared `providers/transport.py` | `agents/platform/scripts/providers/`                                                             |
| Provider packages      | `knowledge_providers/{git_repo,local_store,google_drive,sharepoint,confluence}/`                                   | `providers/{github,gitlab,gitea,bitbucket,bitbucket_datacenter}/`                                |
| Gateway local listener | Companion routes in `agent-api-auth` on `/opt/data/knowledge-output/` (for `local-store`)                          | `agents/platform/scripts/audit_report_store.py` (port `8700`)                                    |
| Provider registration  | `agents/platform/scripts/knowledge_providers/registry.py` (`AVAILABLE`)                                            | `agents/platform/scripts/providers/registry.py`                                                  |
| Declarative surface    | `spec.integration.knowledge` on `PlatformAgent` + `KNOWLEDGE_STORES_CONFIG`                                        | `spec.integration.{forges,repositories}` + `VCS_FORGES_CONFIG`                                   |

---

## 1. Why

### The gap on `main`

Three adjacent systems on `main` already depend on operational knowledge, yet each stops at the
same boundary.

First, `docs/architecture/06-api-and-data-contracts.md` §5 aligns `knowledge/` with the published
[Open Knowledge Format v0.2 `SPEC.md`](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md),
and `scripts/check_okf_knowledge.py` validates `knowledge/` trees in CI. That contract still
assumes `knowledge/` sits inside the GitOps repository and leaves dedicated repositories and
external stores undesigned.

Second, Layer 2 decision memory (`docs/designs/memory.md`) indexes `knowledge/`, ADRs, RFCs, and
Markdown runbooks from registered Git repositories into `kube-agents-decisions`, yet it cannot read
Google Drive, SharePoint, or Confluence. No skill looks up a matching runbook before acting:
[`agents/platform/docs/autoops-architecture.md`](../../agents/platform/docs/autoops-architecture.md#contract-4--context-reach)
Contract 4 still lists `Runbook / knowledge retrieval` as `Future`.

Third, `audit_report_store.py` and `/v1/audit-reports` (`docs/designs/fleet-audit-report-store.md`)
store structured fleet-audit JSON envelopes on the gateway pod's `/opt/data` volume so ephemeral
profile workers can write and the A2A gateway can read them. There is no general,
backend-pluggable store for non-audit reports and investigation notes that chat posts can link to
([#4053](https://github.com/gke-labs/kube-agents/issues/4053),
[#5047](https://github.com/gke-labs/kube-agents/issues/5047)).

Every install on `main` runs the A2A stack (NATS bus, A2A gateway, session pods, profile workers,
and the optional Curator pod; `PlatformAgentSpec.Mode` is deprecated and always reconciled to
`"next"`). Because profile workers and session pods mount no shared `/opt/data` volume and hold no
external credentials, any knowledge backend that serves the Platform Agent shell, Cluster Agent
sessions, profile workers, and Curator must cross the credential broker over HTTP.

### Why a broker + provider seam instead of per-backend skills or direct MCP

Threading Drive, SharePoint, and Confluence APIs directly into skills duplicates authentication,
redaction, egress control, and OKF handling across every skill and puts provider tokens inside
agent pods, breaking [`docs/credential-isolation-design.md`](../credential-isolation-design.md).

Using remote MCP servers as the primary integration mechanism fails for the same two reasons
recorded in [`version-control-support.md` §9](version-control-support.md#9-why-an-mcp-server-is-an-addition-not-the-mechanism).
Half the consumers run with no LLM in the loop: `sweep_repo_memory`, the Go Curator pod
(`a2a/cmd/curator`), scheduled conversion jobs
([#5045](https://github.com/gke-labs/kube-agents/issues/5045)), and the A2A gateway's report link
retrieval need a typed HTTP route and library client, not an interactive MCP surface. Keeping
authentication, root-scope checks, query sanitization, redaction, and read-only enforcement in
`<agent>-credential-proxy` routes every caller through one chokepoint. An optional MCP tool wrapper
can sit in front of `/v1/knowledge/*` later without duplicating provider code.

---

## 2. Store Classes, Trust Separation, and Addressing

### Two store classes plus read-only external sources

Curated team knowledge and agent-generated output share the same protocol verbs, yet they carry
opposite review, trust, and permission rules
([#3045](https://github.com/gke-labs/kube-agents/issues/3045),
[#5047](https://github.com/gke-labs/kube-agents/issues/5047)):

| Store role | What lives there                                                       | Permitted verbs                                                     | Trust level & runtime rules                                                                                                         |
| ---------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `curated`  | Human-reviewed OKF runbooks, playbooks, blueprints, decisions, lessons | `capabilities`, `list`, `read`, `search`, `propose` (never `write`) | Trusted when Git-backed and `status` is `stable`/`active` (not `deprecated` or past `stale_after`). `propose` opens a PR/MR only.   |
| `external` | Team-owned non-Git sources (Drive folder, SharePoint site, Confluence) | `capabilities`, `list`, `read`, `search` (read-only by default)     | Tagged `trust:untrusted` in recall and tool output. Primary v1 use is feeding PR-mediated OKF conversion (#5045).                   |
| `output`   | Unreviewed agent reports, audit summaries, investigation drafts        | `capabilities`, `list`, `read`, `search`, `write` (bound prefix)    | Tagged `trust:untrusted`, `generated` required, `verified` forbidden, `stale_after` required. Never followed as a runbook or guard. |

Three invariants hold across every backend:

1. **No direct writes to curated knowledge.** `POST /v1/knowledge/write` always returns `403
KNOWLEDGE_WRITE_FORBIDDEN` for any store with `role: curated` or `role: external`. Curated
   knowledge changes exclusively via `propose`, which opens a reviewable pull or merge request on a
   Git-backed store.
2. **Output never becomes guidance automatically.** Runbook lookup, `declares:` posture resolution,
   and `decision`/`lesson` guard evaluation never query `role: output` stores. Promoting an
   investigation report into a runbook or lesson requires an explicit flow that calls `propose`
   against the `curated` store with `sources` pointing to the output record's stable reference.
3. **Read-only by default.** Every explicitly declared store defaults to `mode: read-only`.
   Enabling `propose` on a `curated` store or `write` on an `output` store requires setting `mode`
   explicitly. Two narrow implicit defaults preserve existing install behavior when
   `spec.integration.knowledge` is omitted: a `role: gitops` repository synthesizes an implicit
   `curated` store at `<gitops-repo>/knowledge/` with `mode: propose` (matching `06` §5
   curate-as-code), and the default `local-store` output backend allows `write` within the caller's
   permitted prefix.

### Store addressing and server-side root confinement

Callers never pass raw external URLs, hostnames, Drive IDs outside a configured root, or
Confluence base URLs to the broker. The administrator declares each store by a DNS-1123 logical
`name` (for example `curated`, `output`, `sre-drive`, `platform-confluence`), and every broker
request names `{store, ...}`.

Each document carries a canonical identifier `<store>:<opaque-relative-id>`, where the suffix is a
normalized relative path (checked against traversal, leading slashes, null bytes, and symlinks) or
a provider item ID whose ancestry under the configured root is verified by the broker on every
`read`. Every `list`, `read`, and `search` response also includes a broker-composed `webUrl` (for
example the forge's `fileUrl` at the resolved commit SHA, a Drive file URL, a SharePoint item URL,
or a Confluence page URL) so agents cite exact locations without building URLs in the sandbox.

---

## 3. The Seam: `/v1/knowledge/*` Protocol

All knowledge routes live on the **stateful credential broker** (`<agent>-credential-proxy`, port
`8765`, alongside `/v1/vcs/*` and `/v1/audit-reports`). Once the exec-tier split proposed in
[`credential-broker-exec-tier.md`](credential-broker-exec-tier.md) lands, knowledge routes remain on
the stateful broker rather than moving to the exec tier.

Like `/v1/vcs/*`, every knowledge route is stateless to its caller: no server-side session handle
and no shared checkout across requests.

### Verbs

| Route (`POST`)               | Request payload                                                                  | Response payload                                                                                                                                  | Notes                                                                                                                  |
| ---------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `/v1/knowledge/capabilities` | `{store?}`                                                                       | `{curatedStore, outputStore, stores: [{name, provider, role, mode, verbs, maxReadBytes, supportsSearch, configured, missing?}]}`                  | Zero network/credential spend. `curatedStore` / `outputStore` are `null` when unconfigured.                            |
| `/v1/knowledge/list`         | `{store, prefix?, types?, statuses?, cursor?, limit?}`                           | `{store, provider, role, items: [{id, path, title, type?, status?, staleAfter?, mimeType, revision, updatedAt, webUrl}], nextCursor?, truncated}` | Lists entries under the declared store root. Excludes reserved `index.md` and `log.md` when `types` is set.            |
| `/v1/knowledge/read`         | `{store, id, revision?, format?}`                                                | `{store, id, path, title, revision, updatedAt, webUrl, mimeType, content, frontmatter?, trust, stale?, redacted}`                                 | Enforces `maxReadBytes` (default `4 MiB` text). `format` is `"markdown"`, `"text"`, or `"raw"` (provider-bounded).     |
| `/v1/knowledge/search`       | `{store, query?, types?, tags?, resource?, includeStale?, limit?}`               | `{store, hits: [{id, path, title, type?, status?, stale?, snippet, revision, webUrl, trust}], truncated}`                                         | Frontmatter filters + strict plain-text query. Default excludes `deprecated` and past-`stale_after` entries.           |
| `/v1/knowledge/propose`      | `{store, files: [{path, content}], title, body, branch?, baseBranch?, sources?}` | `{store, proposal: {url, number, branch, revision, forge, repo}}`                                                                                 | Curated Git stores only. Validates OKF v0.2 + `okf_guard.py`, then commits via `VcsBroker.publish_files` and opens PR. |
| `/v1/knowledge/write`        | `{store, path, content, frontmatter}`                                            | `{store, id, path, revision, webUrl, writer, writtenAt}`                                                                                          | Output stores only. Requires `generated` and `stale_after`, forbids `verified`, stamps server `writer`.                |

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

Per-provider `error_overrides` allow a provider to refine status guidance, such as distinguishing a
missing Microsoft Graph `Sites.Selected` site grant from missing tenant admin consent on `403`, or
a Drive `404` caused by a folder not shared with the service account.

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
    validate.py                  # store-name, path, query-grammar, size-cap, and mirrored OKF v0.2 checks
    errors.py                    # shared status-to-guidance table
    credentials.py               # WIF/SA impersonation, OAuth2 client_credentials; reuses StaticFileCredential
    registry.py                  # AVAILABLE, load_store_entries(KNOWLEDGE_STORES_CONFIG), build_stores()
    git_repo/                    # Providers 1 & 2: GitOps knowledge/ and dedicated knowledge Git repo
    local_store/                 # Output fallback: relays to gateway agent-api-auth listener (/opt/data/knowledge-output)
    google_drive/                # Provider 3: Google Drive folder / Shared Drive (read-only)
    sharepoint/                  # Provider 4: Microsoft SharePoint / OneDrive site (read-only)
    confluence/                  # Provider 5: Confluence Cloud / Data Center spaces (read-only)
  testdata/
    knowledge_providers/
      git_repo/  local_store/  google_drive/  sharepoint/  confluence/
```

External HTTP providers reuse `HttpTransport` from `agents/platform/scripts/providers/transport.py`
(which already enforces host pinning, `_RefuseRedirect`, per-call CA bundles, timeouts, and byte
caps) and `StaticFileCredential` from `agents/platform/scripts/providers/credentials.py` rather
than duplicating transport or token-file code. `local_store` relays to the gateway pod's
`agent-api-auth` listener over the same authenticated in-cluster HTTP pattern as `ReportStoreRelay`
in `credential_proxy.py` (port `8700`, projected ServiceAccount token), because
`<agent>-credential-proxy` mounts no `/opt/data` volume.

### `KnowledgeProvider` contract

| Member                                       | Responsibility                                                                                         |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `name`                                       | Provider discriminator (`git-repo`, `local-store`, `google-drive`, `sharepoint`, `confluence`).        |
| `allowed_hosts(cfg)`                         | Exact outbound FQDNs this store instance may dial (feeds `HttpTransport` and egress policy).           |
| `supported_roles`                            | Subset of `{"curated", "external", "output"}` the provider can serve.                                  |
| `verbs(cfg)`                                 | Supported verb set for this store instance, narrowed by `role` and `mode`.                             |
| `credential(cfg)`                            | Credential strategy object (`ensure`, `headers`), read fresh per call or cached in memory with expiry. |
| `for_config(config)`                         | Classmethod building `0..n` configured instances from `KNOWLEDGE_STORES_CONFIG`.                       |
| `list`, `read`, `search`, `propose`, `write` | Request descriptors and response translators returning normalized dataclasses.                         |
| `error_overrides`                            | Optional per-provider status-to-guidance overrides.                                                    |

### Prohibitions, boundary enforcement, and why no runtime plugins

Knowledge providers execute inside `<agent>-credential-proxy`, the same process that holds cluster,
forge, and chat credentials. Build-time packages are mandatory: adding a runtime plugin loader to
the broker container would create a code-execution and credential-exfiltration surface beside live
tokens, for the exact reason stated in
[`version-control-support.md` §5](version-control-support.md#why-not-a-runtime-plugin).

A knowledge provider may not spawn subprocesses, open raw sockets, choose scratch paths, set
timeouts, or bypass byte ceilings. External HTTP providers emit request descriptors executed by the
shared `HttpTransport` (`allow_redirects=False`, pinned hosts). The `git_repo` provider delegates
reads to `VcsBroker.snapshot` and proposals to a new `VcsBroker.publish_files` helper owned by
`vcs_broker.py` (see §5.1), so `git_repo` runs no `git` subprocesses of its own.

Two CI boundary tests enforce this layout, mirroring `agents/platform/scripts/test_providers_boundary.py`.
An AST import-boundary test verifies that no module outside `knowledge_providers/` imports a
concrete provider package (only `registry.py` imports classes listed in `AVAILABLE`) and that each
provider imports only `knowledge_providers.{base,validate,errors,credentials}`,
`providers.{transport,credentials}`, and the standard library (plus `vcs_broker` for `git_repo` and
`ReportStoreRelay` primitives for `local_store`). A grep guard derives provider package names
dynamically from `AVAILABLE` and fails CI if any provider name appears in `knowledge_broker.py` or
shared contract modules.

One parameterized `unittest` contract suite under `agents/platform/scripts/` (picked up by
`PYTHON_TEST_FILES` in `Makefile`, matching `test_providers_contract.py`) runs over `AVAILABLE`
against recorded fixtures in `testdata/knowledge_providers/<provider>/`. It verifies normalized
response shapes, root-confinement refusals, search-query injection refusals, byte caps, and
redaction for every provider.

---

## 5. Providers in Suggested Delivery Order

### 1. GitOps repository `knowledge/` root (`git-repo`, default)

This provider reuses the install's existing `role: gitops` repository from
`spec.integration.repositories` with zero new credentials and zero new egress hosts. `list`,
`read`, and `search` operate on a short-lived `VcsBroker.snapshot` of the configured `rootPath`
(default `knowledge/`) at the repository's pinned `baseBranch` or default branch, cached briefly by
`(repo, head_sha)` on the broker's scratch `emptyDir` with bounded LRU eviction so repeated runbook
searches in one turn do not re-clone.

Because `VcsBroker.publish` takes a caller-built git bundle and checks out nothing on the broker
side (`agents/platform/scripts/vcs_broker.py`), `propose` requires one new broker-owned helper:
`VcsBroker.publish_files(repo, files, target, branch, commit_message)`. That helper runs entirely
inside `vcs_broker.py` against a bare scratch clone using plumbing commands (`git hash-object -w`,
`git read-tree`, `git update-index --cacheinfo`, `git write-tree`, `git commit-tree`) with zero
working-tree checkout, runs `okf_guard.py` and mirrored OKF v0.2 validation on the proposed files,
pushes the branch under `platform-agent/knowledge/`, and delegates proposal creation to
`VcsBroker.proposal_create`. No repository contents are ever checked out beside forge tokens, and
`git_repo` itself remains subprocess-free.

### 2. Dedicated knowledge Git repository (`git-repo`)

This reuses the `git_repo` provider against a separate repository on any forge declared in
`spec.integration.forges` (GitHub, GitHub Enterprise Server, GitLab, Gitea, Bitbucket Cloud, or
Bitbucket Data Center), with a configurable `rootPath` (default `knowledge/`, or `.` for a
dedicated OKF-only repo). Under `mode: read-only`, the broker reads it with the forge's read-only
credential (`read_credential` / `read-token`) and refuses `propose`. Under `mode: propose`, the
broker opens PRs/MRs against the knowledge repository without mixing runbook churn into
cluster-manifest history. How the repository is declared relative to `spec.integration.repositories`
is tracked in Open Question 4 (§12).

### 3. Google Drive folder (`google-drive`, read-only)

The Google Drive provider uses a service identity rather than per-user OAuth so headless cluster
pods hold no interactive user tokens. The install uses either the broker's Workload Identity GSA or
a dedicated impersonated GSA (matching `spec.integration.googleChat.serviceAccountEmail`) scoped to
`https://www.googleapis.com/auth/drive.readonly`. The customer shares one Drive folder or Shared
Drive (`viewer` role) with that service account email.

Every `list` and `search` request restricts Drive API v3 queries to descendants of the configured
`folderId` (plus optional `driveId` with `supportsAllDrives=true`), wrapping the provider root
predicate and escaped user query as `(<root_clause>) and (<escaped_user_clause>)`. Every `read`
walks the file's `parents` chain up to `folderId` before returning bytes, refusing any file outside
the declared root (`403 KNOWLEDGE_SCOPE_VIOLATION`) even if the service account can see other
folders. Native Google Docs (`application/vnd.google-apps.document`) export via `files.export` to
`text/markdown` (or `text/plain` fallback); Markdown and plain text files read via
`files.get?alt=media`; PDF and DOCX return bounded raw bytes when an authorized converter caller
sends `format: "raw"` in the `POST /v1/knowledge/read` JSON body. Images, audio, and video MIME
types are skipped. Endpoints respect `spec.harness.universeDomain` (`GOOGLE_CLOUD_UNIVERSE_DOMAIN`)
rather than hardcoding `www.googleapis.com`.

### 4. OneDrive / SharePoint (`sharepoint`, read-only)

The SharePoint provider uses an app-only Microsoft Entra ID OAuth 2.0 `client_credentials` grant
against `https://login.microsoftonline.com/<tenantId>/oauth2/v2.0/token` for scope
`https://graph.microsoft.com/.default`, reusing the token-exchange and in-memory caching pattern
already shipped in `TeamsRelay` (`agents/platform/scripts/credential_proxy.py`). The Entra app
registration holds `Sites.Selected` (never tenant-wide `Sites.Read.All`), and the SharePoint site
administrator grants that app read-only (`read`) access to the single declared site. Credentials
live in a Kubernetes Secret (`client-id`, `client-secret`) mounted only into
`<agent>-credential-proxy`.

The broker resolves the declared `(siteUrl, driveName, folderPath)` to immutable
`(siteId, driveId, rootItemId)` and restricts every Microsoft Graph `/drives/{driveId}/items` call
to descendants of `rootItemId`, verifying `parentReference.path` on `read`.

### 5. Confluence (`confluence`, read-only)

Issue [#1987](https://github.com/gke-labs/kube-agents/issues/1987) tracks Atlassian's hosted
Cloud-only MCP server (`https://mcp.atlassian.com/v1/mcp/authv2`), which has not yet settled a
non-interactive service credential and does not cover Confluence Data Center. Rather than waiting
on hosted MCP or limiting installs to Confluence Cloud, this provider speaks Confluence's REST API
through the same broker credential, egress, and redaction posture described in #1987: a dedicated
read-only Atlassian service account API token (`Basic` auth over HTTPS on Confluence Cloud) or a
Personal Access Token (`Bearer` auth over HTTPS with optional `caBundleRef` on Confluence Data
Center), stored in a Kubernetes Secret mounted only into `<agent>-credential-proxy`. If #1987 later
ships an interactive Atlassian MCP server, it remains an additive interactive tool surface.

Every Confluence store declares a non-empty `spaceKeys` allowlist (and optional `rootPageId`
ancestor filter). The provider builds CQL queries server-side as
`(space in (...)) AND (<escaped_user_clause>)` and verifies the returned page's `spaceKey` (and
ancestor chain when `rootPageId` is set) on every `read`. It reads REST API v2 `body.storage`
(XHTML) or `body.atlas_doc_format` and returns bounded text/XHTML for the converter (#5045) or
deterministic XHTML-to-Markdown conversion on read. Write endpoints are never called.

### Agent output store backends (`role: output`)

The default `local-store` backend works on every install, including installs with no GitOps
repository. Because `<agent>-credential-proxy` mounts no `/opt/data` PVC, `local-store` follows the
exact two-hop architecture of [`fleet-audit-report-store.md` §3](fleet-audit-report-store.md#3-where-it-lives):
a companion route group on the gateway pod's `agent-api-auth` listener (port `8700`) stores OKF
Markdown reports under a sibling root `/opt/data/knowledge-output/` — strictly outside
`/opt/data/fleet-audit/reports/` so it cannot collide with `<audit-id>` stream enumeration or per-stream
`runs/` and `run-starts/` ring invariants. `agent-api-auth` authenticates the broker via its
projected ServiceAccount token (`kubeagents-audit-report-store` or a dedicated audience), enforces
fd-relative no-symlink atomic writes, records server-stamped writer provenance
(`X-Knowledge-Writer`), and prunes entries past `stale_after`.

Optional external output backends (Phase 4) — such as a customer GCS bucket via Workload Identity
or a dedicated output Git/Drive folder — can back `role: output` when a team wants reports directly
linkable outside the cluster console. Curated stores stay separate regardless of which backend
backs `output`.

---

## 6. Security and Credential-Proxy Boundary

### Caller-role authorization (`ROUTE_ROLES` and `ROLE_KNOWLEDGE_VERBS`)

`<agent>-credential-proxy` authenticates callers using projected Kubernetes ServiceAccount tokens
with per-pod audiences (`credential_proxy.py` `ROUTE_ROLES`, `platformagent_broker_split.go`). The
knowledge routes extend that model with `KNOWLEDGE_ROUTE_PREFIX = "/v1/knowledge/"` and a per-role
verb allowlist (`ROLE_KNOWLEDGE_VERBS`):

| Caller role   | Audience / source pod                      | Allowed `/v1/knowledge/*` verbs                                                      | Store & write constraints                                                                                                                                                                       |
| ------------- | ------------------------------------------ | ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `shell`       | Platform Agent shell sandbox               | `capabilities`, `list`, `read`, `search`, `propose`, `write`                         | `propose` on `curated` Git stores (when enabled); `write` on `output` stores only.                                                                                                              |
| `profile`     | Ephemeral profile workers (`AgentProfile`) | `capabilities`, `list`, `read`, `search`; `propose` and `write` gated by task claims | Requires verified task credential. `propose` requires `vcs:proposal-create` (or `knowledge:propose`) permit; `write` is bound to the task's own stream/prefix (mirroring `audit_report_store`). |
| `curator`     | Go Curator pod (`a2a/cmd/curator`)         | `capabilities`, `list`, `read`                                                       | Read-only across configured `curated` and opted-in `external` stores. Never `propose` or `write`.                                                                                               |
| `session`     | Per-conversation A2A session pod           | `capabilities`, `list`, `read`, `search` (opt-in / curated read-only)                | Redacted reads of `curated` stores only. Never `propose`, never `write`.                                                                                                                        |
| `a2a-reports` | A2A gateway report relay                   | `read` (exact `role: output` ID only)                                                | Redacted single-document fetch for chat/console/MCP report links (#4056, #5047). No `list`, `search`, or `write`.                                                                               |
| `mcp`         | A2A MCP server pod (`a2a/cmd/mcp`)         | `read` (exact `role: output` ID only, if exposed)                                    | Same narrow read-only boundary as `a2a-reports`; no curated write or arbitrary search.                                                                                                          |

Roles absent from this table (`chat`, `a2a-chat`, `watcher`, `scope`, `teams-inbound`) receive
`403 CALLER_ROLE_FORBIDDEN` on `/v1/knowledge/*`.

### Query sanitization, redaction, egress, and secret confinement

Search queries sent to `/v1/knowledge/search` cannot reuse `profile_issue_query_refusal` from
`credential_proxy.py` as-is: that helper omits single quotes (`'`), comparison operators (`=`, `~`,
`<`, `>`), and lowercase boolean words (`or`, `and`, `not`, `in`, `contains`), which would allow
root-escape injection in Google Drive `q` and Confluence CQL. Instead, `knowledge_providers/validate.py`
enforces a dedicated plain-text allowlist (`KNOWLEDGE_QUERY_MAX_LENGTH = 256`, permitting only
alphanumerics, spaces, and `._/-`, refusing quotes, backslashes, colons, brackets, comparison
operators, and case-insensitive query keywords `and`, `or`, `not`, `in`, `contains`), and every
provider escapes literals and parenthesizes `(<root_clause>) AND (<escaped_user_clause>)`.

For response redaction, `credential_proxy.py` defines a dedicated
`REDACTED_KNOWLEDGE_READ_ROLES = REDACTED_OUTPUT_ROLES | {CALLER_ROLE_A2A_REPORTS, CALLER_ROLE_MCP}`,
mirroring `REDACTED_VCS_READ_ROLES` and `REDACTED_REPORT_READ_ROLES` without altering
`REDACTED_OUTPUT_ROLES` (`profile` and `session` only). Every document body and search snippet
returned to those roles, every broker log line, and every outbound search query sent to an external
SaaS provider passes through `credential_shapes.py` redaction (private keys, GitHub `gh*` /
`github_pat_` tokens, AWS/Google/Slack/`sk-` keys, Bearer/Basic headers, JWTs, and URL passwords).
As a Phase 1 prerequisite, `credential_shapes.py` also adds GitLab PAT (`glpat-`) and provider
token shapes so secrets accidentally pasted into a wiki or runbook are masked consistently.

Egress control aligns with `buildFQDNNetworkPolicy`
(`k8s-operator/internal/controller/platformagent_manifests.go`). Today that policy selects
`<agent>-gateway` only while `<agent>-credential-proxy` has open egress
([`version-control-support.md` §12 #5](version-control-support.md#12-open-questions)). Each
knowledge provider implements `allowed_hosts()` and the operator adds
`KnowledgeEgressPatterns(integration)` beside `ForgeEgressPatterns(integration)`. From day one,
`HttpTransport` inside the broker enforces the per-store host allowlist and refuses all HTTP
redirects (`allow_redirects=False`), preventing SSRF and token forwarding even on clusters without
Dataplane V2. When a broker-scoped `FQDNNetworkPolicy` is enabled, external knowledge hosts are
derived automatically from `spec.integration.knowledge`. External providers are contacted only on
explicit request or administrator-configured schedule; nothing leaves the install automatically.

Provider Secrets are projected read-only (`0400`, `Optional: true`) under
`/var/run/kube-agents/knowledge-credentials/<store>/` (and optional CA bundles under
`/etc/kube-agents/knowledge-ca/<store>/`) into `<agent>-credential-proxy` alone — never into the
gateway, sandbox, session, profile, or Curator pods. To preserve parity with forge credentials
(`k8s-operator/internal/controller/vcs_forges.go` and `k8s-operator/api/v1alpha1/common_types.go`),
the operator adds `KNOWLEDGE_STORES_CONFIG` (`/etc/credential-proxy/knowledge-stores.json`) to
`SensitiveEnvVars` (and drops it in `mergeCredentialProxyEnv`) and adds `knowledge-credentials-`
and `knowledge-ca-` to `ReservedVolumePrefixes` (enforced in both the validating webhook and
render-time stripping across `extraVolumes`, `sidecarVolumes`, `extraVolumeMounts`,
`sidecars[].volumeMounts`, and `initContainers[].volumeMounts`).

---

## 7. OKF v0.2 Contract Alignment

This backend enforces the published
[Open Knowledge Format v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md)
contract codified in `docs/architecture/06-api-and-data-contracts.md` §5 and
`scripts/check_okf_knowledge.py`. Because `scripts/check_okf_knowledge.py` is repository CI tooling
not copied into the broker image, Phase 1 mirrors its frontmatter and reserved-filename checks in
`agents/platform/scripts/knowledge_providers/validate.py` with a unit parity test holding the two
in step.

`index.md` and `log.md` are reserved at every directory level (alongside root overviews
`STATE_OF_BEING.md` and `README.md` at the `knowledge/` root). `list` and `search` with `types`
filters skip them; `propose` rejects any `index.md` or `log.md` carrying a `type` key and rejects
`type: index` or `type: log`.

Every curated OKF entry requires valid YAML frontmatter with a non-empty string `type`. Standard
types (`runbook`, `cluster-blueprint`, `tenancy-model`, `metric-definition`, `escalation`,
`observation`) use OKF v0.2 `status` values (`draft`, `stable`, `deprecated`); `decision` and
`lesson` use the `kube-agents` lifecycle values (`proposed`, `active`, `rejected`, `superseded`,
`retired`). Matching `a2a/memory/types.go` and
`agents/platform/skills/decision-memory/scripts/decision_memory.py` (`EXTERNAL_DOC_STATUS_MAP`),
`status: stable` maps to `active` for retrieval and recall.

Provenance and freshness fields enforce trust boundaries across workflows. Converted runbooks
([#5045](https://github.com/gke-labs/kube-agents/issues/5045)) arrive via `propose` with
`type: runbook`, `sources` pointing at the upstream document URL and revision, `generated: {by, at}`,
`status: draft`, and no `verified` field until human PR review. Agent output entries
([#5047](https://github.com/gke-labs/kube-agents/issues/5047)) arrive via `write` with
`generated: {by, at}`, `stale_after` within the store's retention cap (default 30 days), and no
`verified` field (the broker rejects any `write` setting `verified`). Runbook readers check
`status` and `stale_after`: entries with `status: deprecated` or `stale_after < today (UTC)` return
`stale: true` so agents report staleness rather than following stale procedures.

Audit posture declarations (`declares:`) and `.kube-agents/intent.yaml` are honored strictly from
Git-backed curated and context repositories (`git-repo` provider). External SaaS stores
(`google-drive`, `sharepoint`, `confluence`) and `role: output` stores never grant posture
declarations or `severity: block` guards.

---

## 8. Integration with Existing Subsystems

### Layer 2 organizational memory (`a2a/memory`, Curator, `decision_memory.py`)

On `main`, two distinct code paths ingest Git knowledge into Hindsight bank `kube-agents-decisions`:
`sweep_repo_memory` (`agents/platform/scripts/github_scan_gate.py`) runs in the sandbox under
`CALLER_ROLE_SHELL`, snapshots both `managed_repos` and `context_repos` via `forge.call("snapshot", ...)`,
and invokes the local `curator index` binary; the standalone Curator pod (`a2a/memory/curator.go`)
authenticates to `/v1/vcs/snapshot` under `CALLER_ROLE_CURATOR`, which `credential_proxy.py`
currently restricts to `managed` repositories only.

With the knowledge broker, `Curator` keeps `/v1/vcs/snapshot` for GitOps manifest and diff checks
and adds `/v1/knowledge/list` + `/v1/knowledge/read` under `CALLER_ROLE_CURATOR` to index declared
`curated` stores (including a dedicated knowledge repository) and any `external` stores explicitly
marked `indexInMemory: true`. Every indexed chunk carries `store:<name>` and `provider:<provider>`
tags alongside `scope:shared`. Records indexed from `role: external` stores or `role: context`
repositories carry `trust:untrusted`, have `guard.expect` cleared, have `severity: block` capped at
`warn`, and return wrapped in `<untrusted_memory_data source="...">` on recall (reusing the
sanitizer in `a2a/memory/curator.go` and `decision_memory.py`).

Deterministic pre-push guards (`okf_guard.py` inside `/v1/vcs/publish`) remain fast and strictly
local to Git, making zero Hindsight calls and zero network calls to external SaaS knowledge
providers.

### Runbook lookup, citation, and AutoOps Contract 4

Adding `knowledge_client.py` and its runbook-lookup skill wires the `Runbook / knowledge retrieval`
row of [`agents/platform/docs/autoops-architecture.md`](../../agents/platform/docs/autoops-architecture.md#contract-4--context-reach)
Contract 4 without changing the AutoOps ingestion, session, judgment, or remediation spine.

Before acting, Platform and Cluster agents call `knowledge search` on the `curated` store filtering
`type: runbook` alongside `tags`, `resource`, and plain-text terms. Per
[`docs/architecture/02-agent-personas.md` §2.3](../architecture/02-agent-personas.md#23-coordination-is-indirect-durable-state-not-synchronous-calls),
OKF retrieval must work under `--memory=file` and `--memory=off` without Hindsight; per
[#5044](https://github.com/gke-labs/kube-agents/issues/5044), lookup starts with deterministic
frontmatter and text search and adds Hindsight semantic recall only where evaluations show text
search misses. When a runbook applies, the plan names it, the PR or chat card cites its `webUrl`
(and upstream `sources` when converted), and every action still runs through normal read-only
tools, RBAC, and human approval gates. When no curated store is configured, onboarding and triage
never ask users to paste runbooks into chat ([#2296](https://github.com/gke-labs/kube-agents/issues/2296));
they point the operator to the CR or installer configuration.

### Document conversion pipeline

Non-Markdown runbooks in Google Drive, SharePoint, or Confluence convert into reviewed OKF v0.2
Markdown files via PR rather than acting as live authoritative instructions in v1
([#5045](https://github.com/gke-labs/kube-agents/issues/5045)). A scheduled or user-requested
conversion task calls `/v1/knowledge/list` and `/v1/knowledge/read` against a configured
`role: external` store, compares `(sourceId, revision)` against existing `sources` entries in the
`curated` store so unchanged files are skipped and changed upstream docs produce an update PR, and
submits the resulting OKF v0.2 `type: runbook` document (`sources`, `generated`, `status: draft`,
no `verified`) via `/v1/knowledge/propose`. A human reviews and merges the PR before the agent
follows it.

### Agent output store and linkable reports

[`docs/designs/fleet-audit-report-store.md`](fleet-audit-report-store.md) keeps structured audit
envelopes (`latest.json`, 14-run ring, `run-starts/`) as the fleet-audit delta engine. That
structured JSON ring stays intact. The knowledge broker's `role: output` store complements it by
giving scheduled jobs, investigations, and audits a uniform `write` / `read` surface for
human-readable OKF reports that chat posts link to across Slack, Google Chat, Teams, the web
console, and the MCP door
([#4056](https://github.com/gke-labs/kube-agents/issues/4056),
[#5047](https://github.com/gke-labs/kube-agents/issues/5047)).

---

## 9. Declarative Surface and Installer Integration

### `PlatformAgent` CR (`spec.integration.knowledge`)

`IntegrationSpec` (`k8s-operator/api/v1alpha1/common_types.go`) gains an optional `knowledge` block
beside `forges` and `repositories`. The example below illustrates the recommended single-block
declaration from Open Question 4 (§12), where a dedicated Git knowledge repository is declared
directly inside `spec.integration.knowledge.stores[]` referencing a forge in `spec.integration.forges`:

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
            baseBranch: main # allowed when mode is propose; omitted or ref-pinned when read-only
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

Schema validation uses CEL rules on the CRD alongside the validating webhook so every invariant
holds even when `ENABLE_WEBHOOKS=false`:

- `stores[].name` is unique and matches DNS-1123 lowercase (`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, max
  63 chars).
- `mode: write` is rejected unless `role == "output"`; `mode: propose` is rejected unless
  `role == "curated"` and `provider == "git-repo"`.
- `gitRepo.forge` must reference a declared entry in `spec.integration.forges`; `gitRepo.baseBranch`
  is rejected when `mode == "read-only"` (mirroring `RepositorySpec.BaseBranch`).
- `confluence.spaceKeys` must be non-empty; `googleDrive.folderId` must be non-empty.
- `sharePoint.tenantId` must be a UUID or single-label Entra tenant domain (`^[A-Za-z0-9.-]+$`, no
  `/`, `?`, `#`, or `..`); `sharePoint.siteUrl` must be an `https://` URL with host and path only
  and no query string or fragment.
- `curatedStore` and `outputStore`, when set, must name entries in `stores` with matching `role`.

When `spec.integration.knowledge` is omitted and a `role: gitops` repository exists, the operator
synthesizes one implicit `curated` store (`git-repo` at `<gitops-repo>/knowledge/`, `mode: propose`)
and one implicit `output` store (`local-store`), so existing installs need zero CR edits. When no
GitOps repository exists and `spec.integration.knowledge` is omitted, curated reads are disabled
(`/v1/knowledge/capabilities` returns `curatedStore: null`) and `outputStore` resolves to
`local-store` (matching today's no-forge report store behavior in `fleet-audit-report-store.md` §2).

### Installer (`install.sh`, Terraform, and Helm)

Per `AGENTS.md`, `terraform/examples/full-install` and the `kube-agents` Helm chart remain the
single installation engine; `install.sh` and `upgrade.sh` only collect inputs and generate
`terraform.tfvars`. During interactive `install.sh`, if no GitOps repository is configured (or when
the admin opts into a separate knowledge location), the installer prompts for a knowledge backend
(`git-repo`, `google-drive`, `sharepoint`, `confluence`, or `none`). Non-interactive installs
accept declarative keys in `install.env` (such as `KNOWLEDGE_CURATED_PROVIDER`,
`KNOWLEDGE_CURATED_REPO`, `KNOWLEDGE_DRIVE_FOLDER_ID`) and pipe any static credentials
(`--knowledge-token-file` or no-echo prompt) directly into a Kubernetes Secret in the agent
namespace, keeping tokens out of `install.env`, Helm values, and Terraform state. Changing the
knowledge location post-install updates the CR (or `install.env` + `upgrade.sh`), re-rendering
`knowledge-stores.json` in the broker's policy ConfigMap and rolling only `<agent>-credential-proxy`.

---

## 10. Failure Modes and Operational Bounds

| Failure mode                                   | System behavior                                                                                                                                                                                                                         |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Missing or rotated provider Secret             | Optional volume mount (`Optional: true`) lets the broker start normally; calls to that store return `503 KNOWLEDGE_CREDENTIAL_UNAVAILABLE`. Updating the Secret takes effect on the next call without restart (per-call file read).     |
| Revoked Drive share / missing SharePoint grant | Provider returns `403 KNOWLEDGE_FORBIDDEN` (or `404` with explicit share-check guidance); other stores and cluster operations stay unaffected.                                                                                          |
| External provider outage or latency spike      | Per-request timeout (default `15s` for `list`/`read`/`search`) caps broker worker tie-up; triage degrades to built-in skills and notes that runbook lookup was unavailable. `/v1/vcs/publish` never calls external knowledge providers. |
| Oversized file or binary export (large PDF)    | `HttpTransport` aborts reads above `maxReadBytes` (`4 MiB` default, `16 MiB` converter cap) with `413 KNOWLEDGE_DOC_TOO_LARGE` before buffering whole payloads in broker memory.                                                        |
| Prompt injection inside a runbook or wiki page | External and unverified outputs carry `trust:untrusted` and `<untrusted_memory_data>` framing; curated runbooks shape plans only and never bypass RBAC, read-only guards, or human approval.                                            |
| Malformed OKF frontmatter on `propose`         | Broker runs the mirrored OKF v0.2 checks in `knowledge_providers/validate.py` before pushing any branch; returns `400 KNOWLEDGE_INVALID_REQUEST` naming the exact key/line so broken PRs are never opened.                              |
| Secret accidentally committed to a wiki or doc | Broker redacts `credential_shapes.py` patterns on responses to `REDACTED_KNOWLEDGE_READ_ROLES` and on all logs, setting `redacted: true` in the response metadata.                                                                      |
| Broker memory / concurrency exhaustion         | Knowledge routes run on the stateful broker with bounded concurrency slots and memory reservations, staying separate from exec-tier `kubectl`/`gcloud` slots once the exec split lands.                                                 |

Observability follows the existing credential-proxy metrics surface (`kubeagents_*` scraped by
`<name>-credential-proxy-monitoring`): the broker exports
`kubeagents_knowledge_requests_total{store,provider,verb,status}` and
`kubeagents_knowledge_request_duration_seconds{store,provider,verb}`, with label cardinality
bounded to declared store names.

---

## 11. Phased Delivery Plan

| Phase | Deliverables                                                                                                                                                                                                                                                                                                                                                                                                                         | Unblocks                                                             |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| **1** | `knowledge_broker.py`, `knowledge_providers/{base,validate,errors,credentials,registry,git_repo,local_store}`, `VcsBroker.publish_files`, gateway `agent-api-auth` listener routes on `/opt/data/knowledge-output/`, `knowledge_client.py`, `glpat-` pattern in `credential_shapes.py`, `spec.integration.knowledge` CRD + operator Secret/ConfigMap/volume-prefix reservations, `unittest` boundary + contract + OKF-parity suites. | #5044 (runbook lookup & citation), #2296, #5047 (local output store) |
| **2** | `knowledge_providers/google_drive/` (service account + `folderId` confinement + Docs export), PR-mediated OKF converter workflow (#5045) for Google Docs and PDFs.                                                                                                                                                                                                                                                                   | #5045 (Drive + PDF conversion)                                       |
| **3** | `knowledge_providers/sharepoint/` (Entra `client_credentials` + `Sites.Selected`) and `knowledge_providers/confluence/` (REST API + `spaceKeys` confinement + `caBundleRef`), plus Curator `/v1/knowledge/*` indexing for dedicated knowledge repos.                                                                                                                                                                                 | #5045 (Confluence), #2903 multi-repo knowledge                       |
| **4** | Externally linkable `role: output` store (console link surface and/or optional GCS/folder backend) and broker-scoped `FQDNNetworkPolicy` wiring.                                                                                                                                                                                                                                                                                     | #4056 / #5047 clickable report links                                 |

---

## 12. Open Questions

1. **Cross-repository `decision`/`lesson` guard evaluation on `/v1/vcs/publish` (`@dshnayder`).**
   When an install keeps curated knowledge in a dedicated knowledge repository separate from the
   target GitOps repository, should deterministic `okf_guard.py` checks on `/v1/vcs/publish` stay
   scoped strictly to the target repository's own `knowledge/` tree, or also fetch and evaluate
   `decision`/`lesson` records from the dedicated knowledge repository?
   Evaluating only the target repository preserves sub-100ms, single-repo atomic publish checks,
   whereas evaluating a second repository adds a second snapshot fetch on every publish and
   introduces cross-repo version skew. _Recommendation:_ keep `/v1/vcs/publish` blocking guards
   scoped to the target repository in Phase 1, index the dedicated knowledge repository into
   `kube-agents-decisions` for recall and `why` queries, and add cross-repo blocking guards only if
   multi-repo fleet teams need centralized enforcement.

2. **Where untrusted binary conversion (PDF / DOCX) executes (`@dshnayder`, `@bradhoekstra`).**
   Google Docs export and Confluence XHTML are API-level text conversions, whereas parsing
   arbitrary customer PDFs and Word documents (#5045) invokes binary parsers that do not belong
   inside the credentialed `<agent>-credential-proxy` pod. Which boundary should parse binary
   attachments: a credential-free conversion profile worker requesting `POST /v1/knowledge/read`
   with `format: "raw"` for bounded raw bytes, or a separate unprivileged converter Job?
   _Recommendation:_ keep `<agent>-credential-proxy` free of binary document parsers; return
   bounded raw bytes to a credential-free profile Job pod that extracts text, drafts the OKF v0.2
   Markdown, and submits it via `POST /v1/knowledge/propose`.

3. **First clickable URL surface for `role: output` reports (`@bradhoekstra`).**
   Chat posts need a URL a human can open from Slack or Google Chat (#5047). The built-in
   `local-store` lives inside the cluster on `/opt/data/knowledge-output/` and has no external URL
   unless fronted by the A2A web console. Which linkable backend should Phase 4 prioritize:
   authenticated report routes on the A2A web console reading `/v1/knowledge/read` via
   `a2a-reports`, a customer GCS bucket, or a dedicated Git/Drive output folder?
   _Recommendation:_ front `local-store` through the A2A web console first (zero new cloud storage
   or IAM grants on default installs), and offer a customer GCS bucket as the first optional
   external `output` provider.

4. **Dedicated Git knowledge repository declaration shape (`@dshnayder`, `@bradhoekstra`).**
   Should a dedicated Git knowledge repository be declared exclusively inside
   `spec.integration.knowledge.stores[]` (referencing a forge in `spec.integration.forges`, as
   shown in §9), or also appear in `spec.integration.repositories` with a new `role: knowledge`?
   Reusing `spec.integration.repositories` keeps all GitHub App minter policy and
   `managed_repos`/`context_repos` seeding in one list, while declaring every store exclusively
   under `spec.integration.knowledge` avoids splitting knowledge configuration across two CR lists
   and avoids duplicate `baseBranch` fields when mixing Git and non-Git backends.
   _Recommendation:_ declare every knowledge store uniformly under `spec.integration.knowledge`,
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
  store whose gateway listener pattern `local-store` reuses.
- [`credential-broker-exec-tier.md`](credential-broker-exec-tier.md) — proposed broker split
  keeping stateful routes off the exec tier.
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
  [#3045](https://github.com/gke-labs/kube-agents/issues/3045),
  [#4053](https://github.com/gke-labs/kube-agents/issues/4053),
  [#4056](https://github.com/gke-labs/kube-agents/issues/4056),
  [#1720](https://github.com/gke-labs/kube-agents/issues/1720), and
  [#1987](https://github.com/gke-labs/kube-agents/issues/1987).
