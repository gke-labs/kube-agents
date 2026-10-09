#!/usr/bin/env python3
"""Syncs GKE agent skills from the upstream google/skills repository (skills/cloud).

The platform image build runs the shell blocks of every synced SKILL.md through
deploy/docker/check_skill_commands.py, so a sync that brings in a command Tirith
refuses, or changes any line of a block listed in its KNOWN_FINDINGS, comments
included, fails that build until the list is updated. So does a shell block whose Markdown does not parse as one; that
fix goes in SKILL_SUBSTITUTIONS below, or in a patch for a skill with an upstream.lock.

Skills with an upstream.lock under agents/platform/skill-overlays/ are mirrored by
scripts/skill_overlay.py (docs/designs/upstream-skill-overlays.md) and skipped here;
the rest are still synced from the registries below.
"""

import os
import shutil
import subprocess
import sys
import tempfile

UPSTREAM_REPO = "https://github.com/google/skills.git"
UPSTREAM_SKILLS_PATH = os.path.join("skills", "cloud")
SKILL_PREFIX = "gke-"

# Target agents where upstream GKE skills should be synced.
#
# Upstream skills from google/skills (skills/cloud) target the Platform Agent (agents/platform/).
# Cluster Agent skills (agents/cluster/skills/) are not synced from upstream: they are repo-native
# templates tailored specifically for single-cluster runtime debugging and operations (see AGENTS.md),
# with cluster-specific personas and diagnostic tooling that an upstream overwrite would wipe.
# Consequently, DEFAULT_TARGET_AGENTS is ["platform"] and cluster skills are maintained independently
# in this repository.
DEFAULT_TARGET_AGENTS = ["platform"]
SKILL_AGENT_OVERRIDES = {
    # Per-skill target agent overrides if specific skills should go to additional/alternative agents.
}

SKILL_MD_FILENAME = "SKILL.md"

# A skill whose overlay holds this lock is mirrored by scripts/skill_overlay.py instead
# (docs/designs/upstream-skill-overlays.md). This script skips it: no prune, no copy, and a
# registry entry for it is an error. Its changes live as patch files beside the lock.
SKILL_OVERLAY_ROOT = os.path.join("agents", "platform", "skill-overlays")
SKILL_OVERLAY_LOCK = "upstream.lock"
# Where an overlay-mirrored skill is generated; the recovery pathspecs exclude it.
SKILL_OVERLAY_SKILLS = "agents/platform/skills"
UTF_8_ENCODING = "utf-8"
SUBSTITUTION_COUNT = 1

# What classify_substitution() returns: apply the pair, skip it because upstream already reads
# the way the pair would make it read, or refuse because neither of those is true.
SUBSTITUTION_APPLY = "apply"
SUBSTITUTION_SKIP = "skip"
SUBSTITUTION_UNDECIDABLE = "undecidable"


class UpstreamDriftError(Exception):
    """A registered local correction can no longer be applied to what upstream now ships.

    Each registry below names an upstream skill and the text they expect to find in it. When
    upstream edits that text — even by a bullet marker — renames the skill, or drops it, the
    correction stops being applied. Warning and carrying on published the uncorrected upstream
    content with the run still reporting success, so the sync refuses instead.

    Raised by verify_local_corrections, which runs before the first write, so a sync that fails
    this way has changed nothing.
    """


class LocalCorrectionLost(UpstreamDriftError):
    """A correction could not be applied to a skill already copied into the tree.

    verify_local_corrections checks the same conditions against the clone the copy is made
    from, so this is unreachable unless the two disagree. It exists so that the uncorrected
    upstream content cannot reach the tree even then; the tree is left partly refreshed.
    """


GKE_WORKLOAD_SECURITY_OLD_NETPOL_SNIPPET = """**Enable Network Policy Enforcement:**

```bash
gcloud container clusters update <cluster-name> \\
    --update-addons=NetworkPolicy=ENABLED \\
    --region <region>
```

> [!NOTE] If your cluster uses Dataplane V2 (`--enable-dataplane-v2`), Network
> Policy enforcement is built-in and this step is not required (and may fail)."""

GKE_WORKLOAD_SECURITY_NEW_NETPOL_SNIPPET = """**Check Network Policy Enforcement & Dataplane:**

Before modifying cluster networking, inspect whether NetworkPolicy enforcement
is already active or provided natively by Dataplane V2:

```bash
gcloud container clusters describe <cluster-name> \\
    --location <location> \\
    --format='value(networkConfig.datapathProvider,networkPolicy.enabled)'
```

- If `datapathProvider` is `ADVANCED_DATAPATH` (Dataplane V2), NetworkPolicy
  enforcement is built-in natively via eBPF/Cilium from cluster creation. Calico
  addons cannot be enabled and are not needed.
- If `networkPolicy.enabled` is `True`, Calico enforcement is already enabled on nodes.
- If neither is active, enable Calico network policy enforcement using the two-step
  sequence below.

**Enable Network Policy Enforcement (non-DPv2 clusters):**

Enabling network policy enforcement on clusters without Dataplane V2 requires
two sequential commands in this order: first enable the Calico addon on the
control plane, then enable network policy enforcement on the nodes. GKE rejects
`--enable-network-policy` with HTTP 400 until the addon is enabled, and `gcloud`
rejects both flags in a single invocation.

```bash
# Step 1: Enable the NetworkPolicy addon on the control plane
gcloud container clusters update <cluster-name> \\
    --update-addons=NetworkPolicy=ENABLED \\
    --region <region>

# Step 2: Enable NetworkPolicy enforcement on the nodes (node pools may be recreated; this can take several minutes)
gcloud container clusters update <cluster-name> \\
    --enable-network-policy \\
    --region <region>
```"""

# gke-manifest-generation's frontmatter description is what the router reads to pick a skill, and
# the routing has to name gcp-config-connector, a skill this repository has and upstream does not.
# The description is a folded YAML scalar, so the whole sentence has to be replaced rather than an
# appended footer.
GKE_MANIFEST_GENERATION_OLD_ROUTING_SNIPPET = (
    "pod troubleshooting (use gke-workload-troubleshooting), or cluster infrastructure provisioning "
    "(use gke-cluster-creation)."
)

GKE_MANIFEST_GENERATION_NEW_ROUTING_SNIPPET = (
    "pod troubleshooting (use gke-workload-troubleshooting), cluster infrastructure provisioning "
    "(use gke-cluster-creation), or Google Cloud resources as Config Connector manifests "
    "(use gcp-config-connector)."
)

# gke-manifest-generation's example ServiceAccount name upstream is `devteam-agent-sa`, a name from
# this repository's retired multi-CR era (issue #340). The example is neutral here so the skill does
# not suggest a DevTeamAgent exists.
GKE_MANIFEST_GENERATION_OLD_SERVICE_ACCOUNT_SNIPPET = "(e.g., `devteam-agent-sa`)"

GKE_MANIFEST_GENERATION_NEW_SERVICE_ACCOUNT_SNIPPET = "(e.g., `checkout-sa`)"

# gke-manifest-generation's inference-manifest step upstream passes `--output-path` to gcloud. Here
# gcloud runs in the credential proxy's container, which refuses that flag, so the skill has to
# redirect stdout instead (#723). The replacement keeps the fence and adds the paragraph saying why.
GKE_MANIFEST_GENERATION_OLD_OUTPUT_PATH_SNIPPET = """          --output-path={output_file_path}
        ```
"""

GKE_MANIFEST_GENERATION_NEW_OUTPUT_PATH_SNIPPET = """          > {output_file_path}
        ```

        Redirect stdout rather than passing `--output-path`: `gcloud` runs in
        the credential proxy's container, so that flag writes the manifest
        next to the credentials instead of in your workspace, and the proxy
        refuses it.
"""

# gke-basics' cluster credentials example upstream runs `gcloud container clusters get-credentials`
# without isolating KUBECONFIG, which overwrites the default kubeconfig context and breaks the Platform
# Agent's ambient host cluster context. The replacement isolates credentials to a per-target KUBECONFIG
# under $HERMES_HOME/.kubeconfigs/, in the form agents/platform/AGENTS.md ("Cluster Credentials")
# gives: `export`, so the pin survives to the kubectl calls that follow rather than scoping to the
# gcloud, and one file per project/cluster/location, the naming _thread_kubeconfig_path in
# agents/platform/scripts/platform_mcp_server.py builds and is the source of truth for.
GKE_BASICS_OLD_CREDENTIALS_SNIPPET = """4. **Cluster Credentials:**
   * Always explicitly specify `--region` (for regional clusters) or `--zone` (for zonal clusters) when fetching credentials:
     ```bash
     gcloud container clusters get-credentials CLUSTER_NAME --region=REGION --quiet
     ```"""

GKE_BASICS_NEW_CREDENTIALS_SNIPPET = """4. **Cluster Credentials:**
   - Always explicitly specify the cluster's location (`--region` for regional clusters, `--zone` for zonal, or `--location` for either) when fetching credentials, and `export` a per-target `KUBECONFIG` under `$HERMES_HOME/.kubeconfigs/` first, so the pin survives to every `kubectl` that follows and concurrent reads of different clusters do not race on one `current-context`:
     ```bash
     PROJECT="$GKE_PROJECT_ID"   # CLUSTER and LOCATION come from the request
     export KUBECONFIG="${HERMES_HOME:-/opt/data}/.kubeconfigs/kubeconfig_${PROJECT}_${CLUSTER}_${LOCATION}.yaml"
     gcloud container clusters get-credentials "$CLUSTER" --location="$LOCATION" --project="$PROJECT" --quiet
     ```"""

# gke-basics' Reference Directory upstream describes cli-reference.md and mcp-usage.md as covering
# the cluster-operation tools alone. The cli-reference.md correction in SKILL_FILE_SUBSTITUTIONS
# adds the knowledge-lookup hierarchy, and mcp-usage.md documents the Developer Knowledge server,
# so the two entries name what the agent finds when it opens them.
GKE_BASICS_OLD_CLI_REFERENCE_ENTRY_SNIPPET = (
    "Tool preference hierarchy (MCP vs gcloud vs kubectl)"
)

GKE_BASICS_NEW_CLI_REFERENCE_ENTRY_SNIPPET = (
    "Tool preference hierarchies (Knowledge lookups vs Cluster Operations)"
)

GKE_BASICS_OLD_MCP_USAGE_ENTRY_SNIPPET = (
    "Connecting to and using the 23 structured GKE MCP tools for cluster management, K8s "
    "resources, and diagnostics."
)

GKE_BASICS_NEW_MCP_USAGE_ENTRY_SNIPPET = (
    "Connecting to and using the GKE MCP and Developer Knowledge MCP tools for cluster "
    "management, K8s resources, and authoritative documentation."
)

# gke-manifest-generation's grounding step upstream prefers Developer Knowledge's `answer_query`,
# whose default quota is 50 requests per day per project (developers.google.com/knowledge/quota),
# shared by every agent in an install; once spent, every lookup 429s for the rest of the day.
# `search_documents` reads the same corpus at 100 requests per minute, so the skill starts there
# and never calls `answer_query` (#1765). `get_document` is also not the tool's name.
GKE_MANIFEST_GENERATION_OLD_DEVELOPER_KNOWLEDGE_SNIPPET = """        -   **`answer_query`**: Use this to ask direct questions (e.g., *"How to
            configure GCS Fuse CSI driver in GKE"*). This is the preferred tool
            for general queries.
        -   **`search_documents`**: Use this to search for relevant GKE guides
            or examples when you don't have a specific question.
        -   **`get_document`**: Use this to fetch full document contents when
            you have a specific document ID."""

GKE_MANIFEST_GENERATION_NEW_DEVELOPER_KNOWLEDGE_SNIPPET = """        -   **`search_documents`**: Start every lookup here (e.g., *"configure
            GCS Fuse CSI driver in GKE"*). It takes only `query`.
        -   **`get_documents`**: Use this to fetch full document contents when
            a returned chunk needs its surrounding page.
        -   Do not call **`answer_query`**: its quota is 50 requests per day per
            project, shared by every agent in the install, and it reads the
            same corpus as `search_documents`. Never retry its `429`."""

# The Workload Identity split moved WI routing out of gke-workload-security in the skills'
# descriptions only; the bodies an agent reads after routing still send it there, and
# gke-workload-security's §2 still teaches GSA impersonation as the recommended path, which
# gke-workload-identity calls legacy. These pairs point the bodies at gke-workload-identity.
GKE_WORKLOAD_SECURITY_OLD_SCOPE_SNIPPET = """covers security auditing, Identity and Access Management (Workload Identity),
Network Security (Network Policies), and Node Security."""

GKE_WORKLOAD_SECURITY_NEW_SCOPE_SNIPPET = """covers security auditing, Network Security (Network Policies), and Node Security.
For Workload Identity setup, use the `gke-workload-identity` skill."""

GKE_WORKLOAD_SECURITY_OLD_WI_INTRO_SNIPPET = """### 2. Configure Workload Identity

Workload Identity allows Kubernetes Service Accounts (KSAs) to impersonate
Google Service Accounts (GSAs). This is the recommended method for workloads to
access Google Cloud APIs.
"""

GKE_WORKLOAD_SECURITY_NEW_WI_INTRO_SNIPPET = """### 2. Configure Workload Identity (legacy GSA impersonation)

Load the `gke-workload-identity` skill first: binding IAM roles to the
Kubernetes Service Account (KSA) principal directly is the current default, and
that skill diagnoses both models. Use the impersonation setup below, where a KSA
impersonates a Google Service Account (GSA), only when a GSA must be the
identity: an existing GSA already holds the roles the workload needs, or the
Google Cloud API it calls does not accept Workload Identity Federation
principals (the GKE documentation lists those limitations).
"""

# Upstream's description claims pod securityContext coverage the body does not carry; container
# security contexts are gke-manifest-generation's, and the description is what the agent routes on.
GKE_WORKLOAD_SECURITY_OLD_DESCRIPTION_SNIPPET = (
    "Security Standards (`restricted` labeling) and pod securityContext, and mounting"
)

GKE_WORKLOAD_SECURITY_NEW_DESCRIPTION_SNIPPET = "Security Standards (`restricted` labeling), and mounting"

GKE_PLATFORM_SECURITY_OLD_SCOPE_SNIPPET = """controls (such as Workload Identity Service Account bindings,
SecretProviderClass volume mounts, Network Policies, and Pod Security
Standards), refer to the `gke-workload-security` skill."""

GKE_PLATFORM_SECURITY_NEW_SCOPE_SNIPPET = """controls (such as SecretProviderClass volume mounts, Network Policies, and Pod
Security Standards), refer to the `gke-workload-security` skill; for Workload
Identity Service Account bindings, the `gke-workload-identity` skill."""

GKE_PLATFORM_SECURITY_OLD_WI_IAM_SNIPPET = """    Service Accounts (`roles/iam.workloadIdentityUser`), refer to the
    `gke-workload-security` skill."""

GKE_PLATFORM_SECURITY_NEW_WI_IAM_SNIPPET = """    Service Accounts (`roles/iam.workloadIdentityUser`), refer to the
    `gke-workload-identity` skill."""

GKE_PRODUCTIONIZE_OLD_SECURITY_ACTION_SNIPPET = """-   **Action**: You MUST run the `gke-platform-security` and
    `gke-workload-security` skills for Workload Identity, Network Policies, and
    Shielded Nodes."""

GKE_PRODUCTIONIZE_NEW_SECURITY_ACTION_SNIPPET = """-   **Action**: You MUST run the `gke-workload-identity` skill for Workload
    Identity, and the `gke-platform-security` and `gke-workload-security` skills
    for Network Policies and Shielded Nodes."""

# gke-compute-classes upstream breaks two passages: a blank line splits the large-shape bullet
# mid-sentence, so the rest renders as a separate paragraph, and the balanced-zonal bullet ends on
# "Asset:" with the asset's name deleted. The replacements rejoin the sentence and restore the
# name the previous upstream version carried.
GKE_COMPUTE_CLASSES_OLD_LARGE_SHAPE_SNIPPET = """are scarcer than

    smaller ones"""

GKE_COMPUTE_CLASSES_NEW_LARGE_SHAPE_SNIPPET = """are scarcer than
    smaller ones"""

GKE_COMPUTE_CLASSES_OLD_ZONAL_ASSET_SNIPPET = """(GKE 1.35.2+). Asset:
-   **Stockout cooldown cascade"""

GKE_COMPUTE_CLASSES_NEW_ZONAL_ASSET_SNIPPET = """(GKE 1.35.2+). Asset:
    `balanced-reserved-zonal-compute-class.yaml`.
-   **Stockout cooldown cascade"""

# gke-compute-classes upstream tells the agent to verify undocumented behavior by cloning the
# autoscaler repository or fetching raw files from GitHub. Neither works from this install's
# terminal (no git transport, no egress to GitHub), so the replacement sends the user to the code
# instead of the agent.
GKE_COMPUTE_CLASSES_OLD_CODE_FIRST_SNIPPET = """**VERIFY BEHAVIOR DIRECTLY IN CODE** (via local repository clone or fetching raw files from GitHub). Check `git log -S` and `git blame` to identify the exact commit and date when behavior changed, and communicate version/date ranges to the user (e.g. *"This behavior changed on July 20, 2026 in upstream commit 129daa3756..."*)."""

GKE_COMPUTE_CLASSES_NEW_CODE_FIRST_SNIPPET = """**SAY SO AND POINT AT THE CODE**: this install's terminal cannot clone or fetch from GitHub, so do not try. Name the package or symbol from the code index for the user to check with `git log -S` and `git blame`, and do not state a version or date range you have not read."""

# gke-cluster-autoscaler still calls the stockout cooldown global with no version qualifier, while
# gke-compute-classes, which it xrefs for the same cascade, says it is zonal from
# 1.36.3-gke.1244000. The replacement carries the version split so the two skills agree.
GKE_CLUSTER_AUTOSCALER_OLD_COOLDOWN_SNIPPET = """puts the **entire affected priority tier on a ~5-min GLOBAL cooldown**. During that window all pending pods"""

GKE_CLUSTER_AUTOSCALER_NEW_COOLDOWN_SNIPPET = """puts the **entire affected priority tier on a ~5-min cooldown**: across all zones before GKE `1.36.3-gke.1244000`, in the stocked-out zone only from it (quota errors stay regional; see `gke-compute-classes`). During a cross-zone cooldown all pending pods"""
# gke-backup-dr upstream fuses the end of best practice 5 into the next heading: the sentence
# stops at "Service**," and "## Golden Path Backup Defaults" follows on the same line, so the
# heading does not render and the sentence never says why the distinction matters. The
# replacement restores the clause the previous upstream version ended it with.
GKE_BACKUP_DR_OLD_TERMINOLOGY_SNIPPET = """    Service**, ## Golden Path Backup Defaults
"""

GKE_BACKUP_DR_NEW_TERMINOLOGY_SNIPPET = """    Service**, as **Backup for GKE** is built specifically for GKE.

## Golden Path Backup Defaults
"""

# The skill's restore plan leaves out --volume-data-restore-policy, whose default is
# no-volume-data-restoration: the restore brings back PVCs bound to blank volumes even though the
# backup plan above it includes volume data. The replacement restores from the backup.
GKE_BACKUP_DR_OLD_RESTORE_POLICY_SNIPPET = """  --all-namespaces \\
  --cluster-resource-conflict-policy=use-existing-version \\
"""

GKE_BACKUP_DR_NEW_RESTORE_POLICY_SNIPPET = """  --all-namespaces \\
  --volume-data-restore-policy=restore-volume-data-from-backup \\
  --cluster-resource-conflict-policy=use-existing-version \\
"""

# gke-backup-dr's Notes upstream tell the agent to run `gcloud components install beta`. Here gcloud
# runs through the credential proxy, which refuses component installs, and the image's
# apt-installed gcloud already carries the beta track. The replacement says so.
GKE_BACKUP_DR_OLD_BETA_COMPONENT_SNIPPET = """-   The `backup-restore` command group requires the `gcloud beta` component
    (`gcloud components install beta`).
"""

GKE_BACKUP_DR_NEW_BETA_COMPONENT_SNIPPET = """-   The `backup-restore` command group is on the `gcloud beta` track
    (`gcloud beta container backup-restore ...`), which this image's gcloud
    already includes. Do not run `gcloud components install`: the credential
    proxy refuses it.
"""

# gke-upgrades' SKILL.md tells a runbook to make "re-applying" the PDB backup its restore step, the
# re-apply the runbook template and troubleshooting corrections below replace because the API server
# rejects it with a conflict. The replacement names the restore those references now give.
GKE_UPGRADES_OLD_PDB_RESTORE_RULE_SNIPPET = "make re-applying them a numbered step"

GKE_UPGRADES_NEW_PDB_RESTORE_RULE_SNIPPET = "make restoring them by patch a numbered step"

# gke-upgrades' SKILL.md says every upgrade pauses when its maintenance window closes, while the
# troubleshooting reference it routes stuck upgrades to says blue-green node-pool upgrades run past
# the window to completion. The replacement scopes the pause to surge upgrades.
GKE_UPGRADES_OLD_WINDOW_PAUSE_SNIPPET = (
    "If a maintenance window closes before an upgrade (auto or manual) completes, GKE intentionally"
    " pauses the rollout to prevent disruption outside allowed times."
)

GKE_UPGRADES_NEW_WINDOW_PAUSE_SNIPPET = (
    "If a maintenance window closes before a surge upgrade (auto or manual) completes, GKE"
    " intentionally pauses the rollout to prevent disruption outside allowed times. A blue-green"
    " node-pool upgrade does not pause: it continues past the window to completion"
    " (`references/troubleshooting.md` §11), so for a blue-green pool the window is not the cause"
    " and the rest of this section does not apply."
)

# In-place content substitutions applied to freshly-synced skills to correct upstream defects
# where an appended footer is insufficient (e.g. multi-step remediation commands), to route to a
# skill only this repository has from a passage upstream cannot know about, or to drop a name this
# repository has retired. Every pair here is also applied by hand to the in-tree copy, and
# scripts/test_sync_upstream_skills.py checks that copy already reads as the next sync leaves it.
SKILL_SUBSTITUTIONS = {
    "gke-workload-security": [
        (
            GKE_WORKLOAD_SECURITY_OLD_NETPOL_SNIPPET,
            GKE_WORKLOAD_SECURITY_NEW_NETPOL_SNIPPET,
        ),
        (
            GKE_WORKLOAD_SECURITY_OLD_SCOPE_SNIPPET,
            GKE_WORKLOAD_SECURITY_NEW_SCOPE_SNIPPET,
        ),
        (
            GKE_WORKLOAD_SECURITY_OLD_WI_INTRO_SNIPPET,
            GKE_WORKLOAD_SECURITY_NEW_WI_INTRO_SNIPPET,
        ),
        (
            GKE_WORKLOAD_SECURITY_OLD_DESCRIPTION_SNIPPET,
            GKE_WORKLOAD_SECURITY_NEW_DESCRIPTION_SNIPPET,
        ),
    ],
    "gke-platform-security": [
        (
            GKE_PLATFORM_SECURITY_OLD_SCOPE_SNIPPET,
            GKE_PLATFORM_SECURITY_NEW_SCOPE_SNIPPET,
        ),
        (
            GKE_PLATFORM_SECURITY_OLD_WI_IAM_SNIPPET,
            GKE_PLATFORM_SECURITY_NEW_WI_IAM_SNIPPET,
        ),
    ],
    "gke-productionize": [
        (
            GKE_PRODUCTIONIZE_OLD_SECURITY_ACTION_SNIPPET,
            GKE_PRODUCTIONIZE_NEW_SECURITY_ACTION_SNIPPET,
        ),
    ],
    "gke-manifest-generation": [
        (
            GKE_MANIFEST_GENERATION_OLD_ROUTING_SNIPPET,
            GKE_MANIFEST_GENERATION_NEW_ROUTING_SNIPPET,
        ),
        (
            GKE_MANIFEST_GENERATION_OLD_SERVICE_ACCOUNT_SNIPPET,
            GKE_MANIFEST_GENERATION_NEW_SERVICE_ACCOUNT_SNIPPET,
        ),
        (
            GKE_MANIFEST_GENERATION_OLD_OUTPUT_PATH_SNIPPET,
            GKE_MANIFEST_GENERATION_NEW_OUTPUT_PATH_SNIPPET,
        ),
        (
            GKE_MANIFEST_GENERATION_OLD_DEVELOPER_KNOWLEDGE_SNIPPET,
            GKE_MANIFEST_GENERATION_NEW_DEVELOPER_KNOWLEDGE_SNIPPET,
        ),
    ],
    "gke-basics": [
        (
            GKE_BASICS_OLD_CREDENTIALS_SNIPPET,
            GKE_BASICS_NEW_CREDENTIALS_SNIPPET,
        ),
        (
            GKE_BASICS_OLD_CLI_REFERENCE_ENTRY_SNIPPET,
            GKE_BASICS_NEW_CLI_REFERENCE_ENTRY_SNIPPET,
        ),
        (
            GKE_BASICS_OLD_MCP_USAGE_ENTRY_SNIPPET,
            GKE_BASICS_NEW_MCP_USAGE_ENTRY_SNIPPET,
        ),
    ],
    "gke-compute-classes": [
        (
            GKE_COMPUTE_CLASSES_OLD_LARGE_SHAPE_SNIPPET,
            GKE_COMPUTE_CLASSES_NEW_LARGE_SHAPE_SNIPPET,
        ),
        (
            GKE_COMPUTE_CLASSES_OLD_ZONAL_ASSET_SNIPPET,
            GKE_COMPUTE_CLASSES_NEW_ZONAL_ASSET_SNIPPET,
        ),
        (
            GKE_COMPUTE_CLASSES_OLD_CODE_FIRST_SNIPPET,
            GKE_COMPUTE_CLASSES_NEW_CODE_FIRST_SNIPPET,
        ),
    ],
    "gke-cluster-autoscaler": [
        (
            GKE_CLUSTER_AUTOSCALER_OLD_COOLDOWN_SNIPPET,
            GKE_CLUSTER_AUTOSCALER_NEW_COOLDOWN_SNIPPET,
        ),
    ],
    "gke-backup-dr": [
        (
            GKE_BACKUP_DR_OLD_TERMINOLOGY_SNIPPET,
            GKE_BACKUP_DR_NEW_TERMINOLOGY_SNIPPET,
        ),
        (
            GKE_BACKUP_DR_OLD_RESTORE_POLICY_SNIPPET,
            GKE_BACKUP_DR_NEW_RESTORE_POLICY_SNIPPET,
        ),
        (
            GKE_BACKUP_DR_OLD_BETA_COMPONENT_SNIPPET,
            GKE_BACKUP_DR_NEW_BETA_COMPONENT_SNIPPET,
        ),
    ],
    "gke-upgrades": [
        (
            GKE_UPGRADES_OLD_PDB_RESTORE_RULE_SNIPPET,
            GKE_UPGRADES_NEW_PDB_RESTORE_RULE_SNIPPET,
        ),
        (
            GKE_UPGRADES_OLD_WINDOW_PAUSE_SNIPPET,
            GKE_UPGRADES_NEW_WINDOW_PAUSE_SNIPPET,
        ),
    ],
}

# gke-basics' tool-preference reference upstream ranks only the interfaces for live cluster
# operations, so a GKE fact the agent needs (a field, a version lifecycle, a quota) falls through
# to web search. This repository ranks Developer Knowledge first for those lookups and web search
# as the fallback; the replacement adds that hierarchy above upstream's, which it renames.
GKE_BASICS_CLI_REFERENCE_OLD_TOOL_PREFERENCE_SNIPPET = """## Tool Preference

Default preference order:
"""

GKE_BASICS_CLI_REFERENCE_NEW_TOOL_PREFERENCE_SNIPPET = """## Tool Preference

Tool usage follows two distinct, domain-specific preference hierarchies:

### 1. Knowledge & Documentation Lookups (GKE Facts, Schemas, Best Practices)

Default preference order:

```
1. Developer Knowledge MCP  (preferred — authoritative, curated first-party documentation)
2. Web Search               (fallback — third-party tooling, open-source CVEs, or DK cache miss)
```

| Interface                                               | When to Use                                                                                                           | Examples                                                                                                      |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| **Developer Knowledge MCP** (`mcp-developer_knowledge`) | Default for all GKE/GCP facts, API schemas, version lifecycles, and configuration semantics.                          | GKE Autopilot constraints, Ingress/Gateway API spec fields, release version deprecations, quota requirements. |
| **Web Search** (`web_search`)                           | Third-party software documentation, non-Google helm charts, community error discussions, or when DK returns no match. | Investigating an open-source operator error, third-party CNI details, or external blog posts.                 |

### 2. Live Cluster Operations & State Management

Default preference order:
"""

GKE_BASICS_CLI_REFERENCE_OLD_WHEN_TO_USE_SNIPPET = "### When to use each\n"

GKE_BASICS_CLI_REFERENCE_NEW_WHEN_TO_USE_SNIPPET = "### When to use each (Cluster Operations)\n"

# gke-upgrades' troubleshooting reference offers four node-pool fixes with no word that they apply
# to Standard clusters only, so fleet audits on Autopilot recommended surge and driver changes GKE
# does not let the user make. The replacements restore the qualifiers this repository had added by
# hand before references could be registered here.
GKE_UPGRADES_TROUBLESHOOTING_OLD_SURGE_SNIPPET = "**Fix — increase surge capacity:**\n"

GKE_UPGRADES_TROUBLESHOOTING_NEW_SURGE_SNIPPET = (
    "**Fix — increase surge capacity (Standard clusters only; Autopilot manages node upgrades "
    "automatically):**\n"
)

GKE_UPGRADES_TROUBLESHOOTING_OLD_STRATEGY_SNIPPET = "1. **Change Upgrade Strategy**: "

GKE_UPGRADES_TROUBLESHOOTING_NEW_STRATEGY_SNIPPET = (
    "1. **Change Upgrade Strategy (Standard clusters only)**: "
)

GKE_UPGRADES_TROUBLESHOOTING_OLD_DRIVER_SNIPPET = "1. **Pin Driver Version**: "

GKE_UPGRADES_TROUBLESHOOTING_NEW_DRIVER_SNIPPET = "1. **Pin Driver Version (Standard clusters only)**: "

# Its §10 resume fix and §11 blue-green update are node-pool operations too, which Autopilot does
# not expose, and they need the same qualifier.
GKE_UPGRADES_TROUBLESHOOTING_OLD_RESUME_SNIPPET = (
    "**Fix — resume a canceled/partially-completed node pool upgrade** by re-issuing"
)

GKE_UPGRADES_TROUBLESHOOTING_NEW_RESUME_SNIPPET = (
    "**Fix — resume a canceled/partially-completed node pool upgrade (Standard clusters only)** "
    "by re-issuing"
)

GKE_UPGRADES_TROUBLESHOOTING_OLD_BLUE_GREEN_UPDATE_SNIPPET = "**Update an existing node pool:**\n"

GKE_UPGRADES_TROUBLESHOOTING_NEW_BLUE_GREEN_UPDATE_SNIPPET = (
    "**Update an existing node pool (Standard clusters only):**\n"
)

# The same reference rolls a node pool back with `gcloud container node-pools upgrade`, a command
# that does not exist; the skill's own runbook template says node pools upgrade through
# `clusters upgrade --node-pool`. The replacement also restores the Standard-only qualifier.
GKE_UPGRADES_TROUBLESHOOTING_OLD_ROLLBACK_SNIPPET = """3. **Rollback Node Pool**: If production is blocked, roll back the node pool to the previous GKE version:
   ```bash
   gcloud container node-pools upgrade NODE_POOL_NAME \\
     --cluster CLUSTER_NAME \\
     --zone ZONE \\
     --cluster-version PREVIOUS_VERSION
   ```
"""

GKE_UPGRADES_TROUBLESHOOTING_NEW_ROLLBACK_SNIPPET = """3. **Rollback Node Pool (Standard clusters only)**: If production is blocked, roll back the node pool to the previous GKE version:
   ```bash
   gcloud container clusters upgrade CLUSTER_NAME \\
     --node-pool NODE_POOL_NAME \\
     --zone ZONE \\
     --cluster-version PREVIOUS_VERSION
   ```
"""

# gke-upgrades' runbook template relaxes a blocking PDB by merging maxUnavailable into it, which
# the API server rejects on a PDB that sets minAvailable ("minAvailable and maxUnavailable cannot be
# both set"). The replacement clears minAvailable in the same patch.
GKE_UPGRADES_RUNBOOK_OLD_PDB_RELAX_SNIPPET = """kubectl patch pdb PDB_NAME -n NAMESPACE \\
  --type merge -p '{"spec":{"maxUnavailable":"100%"}}'"""

GKE_UPGRADES_RUNBOOK_NEW_PDB_RELAX_SNIPPET = """kubectl patch pdb PDB_NAME -n NAMESPACE \\
  --type merge -p '{"spec":{"minAvailable":null,"maxUnavailable":"100%"}}'"""

# Its restore step then re-applies a `kubectl get pdb -A -o yaml` dump. The dump carries each PDB's
# resourceVersion, which the relax patch has since changed, so the API server answers the apply with
# a 409 conflict; and a three-way apply would not remove the maxUnavailable the relax step set, so a
# PDB that used minAvailable would end with both fields set. The replacement restores by patch: the
# recorded value back, the other field cleared.
GKE_UPGRADES_RUNBOOK_OLD_PDB_RESTORE_SNIPPET = """# 3. RESTORE the original PDB — mandatory, not optional. Re-apply from the
#    backup rather than retyping the values.
kubectl apply -f /tmp/pdb-backup-TIMESTAMP.yaml"""

GKE_UPGRADES_RUNBOOK_NEW_PDB_RESTORE_SNIPPET = """# 3. RESTORE the original PDB — mandatory, not optional. Patch back the value
#    the backup records for PDB_NAME and clear the field step 1 set. Do not
#    `kubectl apply` the backup: its stale resourceVersion is rejected with a
#    conflict. If the PDB originally set minAvailable:
kubectl patch pdb PDB_NAME -n NAMESPACE \\
  --type merge -p '{"spec":{"maxUnavailable":null,"minAvailable":ORIGINAL_MIN_AVAILABLE}}'
#    If it originally set maxUnavailable:
kubectl patch pdb PDB_NAME -n NAMESPACE \\
  --type merge -p '{"spec":{"maxUnavailable":ORIGINAL_MAX_UNAVAILABLE}}'
#    (an integer as-is, a percentage quoted, e.g. "50%")"""

# troubleshooting.md §1 relaxes a PDB with no backup and leaves the restore as a closing remark,
# against SKILL.md's own rule that a runbook relaxing a safety control restores it as a numbered,
# verified step; its Option B re-applies a dump the same way the runbook template did. The
# replacement records, relaxes, restores by patch and verifies, in order.
GKE_UPGRADES_TROUBLESHOOTING_OLD_PDB_FIX_SNIPPET = """**Fix — temporarily relax the PDB:**
```bash
# Option A: Allow all disruptions temporarily
kubectl patch pdb PDB_NAME -n NAMESPACE \\
  -p '{"spec":{"minAvailable":null,"maxUnavailable":"100%"}}'

# Option B: Back up and edit
kubectl get pdb PDB_NAME -n NAMESPACE -o yaml > pdb-backup.yaml
# Edit minAvailable/maxUnavailable, then:
kubectl apply -f pdb-backup.yaml
```

Restore original PDB after upgrade completes."""

GKE_UPGRADES_TROUBLESHOOTING_NEW_PDB_FIX_SNIPPET = """**Fix — temporarily relax the PDB, and restore it in the same procedure:**
```bash
# 1. Record the original values before touching anything
kubectl get pdb PDB_NAME -n NAMESPACE -o yaml > pdb-backup.yaml

# 2. Allow all disruptions temporarily
kubectl patch pdb PDB_NAME -n NAMESPACE \\
  --type merge -p '{"spec":{"minAvailable":null,"maxUnavailable":"100%"}}'

# 3. Once the upgrade completes, RESTORE (mandatory): patch back the value
#    pdb-backup.yaml records and clear the other field. If it set minAvailable:
kubectl patch pdb PDB_NAME -n NAMESPACE \\
  --type merge -p '{"spec":{"maxUnavailable":null,"minAvailable":ORIGINAL_MIN_AVAILABLE}}'
#    If it set maxUnavailable:
kubectl patch pdb PDB_NAME -n NAMESPACE \\
  --type merge -p '{"spec":{"maxUnavailable":ORIGINAL_MAX_UNAVAILABLE}}'

# 4. Verify the restore: the spec matches pdb-backup.yaml again
kubectl get pdb PDB_NAME -n NAMESPACE -o yaml | grep -E 'minAvailable|maxUnavailable'
```"""

# The same corrections for a file in a skill other than its SKILL.md, keyed by skill and then by
# the file's path inside the skill directory. A skill's reference files are wiped and re-copied
# with the rest of it, so an edit made to one by hand lasts until the next sync unless it is
# registered here. Every rule SKILL_SUBSTITUTIONS follows applies.
SKILL_FILE_SUBSTITUTIONS = {
    "gke-basics": {
        os.path.join("references", "cli-reference.md"): [
            (
                GKE_BASICS_CLI_REFERENCE_OLD_TOOL_PREFERENCE_SNIPPET,
                GKE_BASICS_CLI_REFERENCE_NEW_TOOL_PREFERENCE_SNIPPET,
            ),
            (
                GKE_BASICS_CLI_REFERENCE_OLD_WHEN_TO_USE_SNIPPET,
                GKE_BASICS_CLI_REFERENCE_NEW_WHEN_TO_USE_SNIPPET,
            ),
        ],
    },
    "gke-upgrades": {
        os.path.join("references", "runbook-template.md"): [
            (
                GKE_UPGRADES_RUNBOOK_OLD_PDB_RELAX_SNIPPET,
                GKE_UPGRADES_RUNBOOK_NEW_PDB_RELAX_SNIPPET,
            ),
            (
                GKE_UPGRADES_RUNBOOK_OLD_PDB_RESTORE_SNIPPET,
                GKE_UPGRADES_RUNBOOK_NEW_PDB_RESTORE_SNIPPET,
            ),
        ],
        os.path.join("references", "troubleshooting.md"): [
            (
                GKE_UPGRADES_TROUBLESHOOTING_OLD_PDB_FIX_SNIPPET,
                GKE_UPGRADES_TROUBLESHOOTING_NEW_PDB_FIX_SNIPPET,
            ),
            (
                GKE_UPGRADES_TROUBLESHOOTING_OLD_SURGE_SNIPPET,
                GKE_UPGRADES_TROUBLESHOOTING_NEW_SURGE_SNIPPET,
            ),
            (
                GKE_UPGRADES_TROUBLESHOOTING_OLD_STRATEGY_SNIPPET,
                GKE_UPGRADES_TROUBLESHOOTING_NEW_STRATEGY_SNIPPET,
            ),
            (
                GKE_UPGRADES_TROUBLESHOOTING_OLD_DRIVER_SNIPPET,
                GKE_UPGRADES_TROUBLESHOOTING_NEW_DRIVER_SNIPPET,
            ),
            (
                GKE_UPGRADES_TROUBLESHOOTING_OLD_ROLLBACK_SNIPPET,
                GKE_UPGRADES_TROUBLESHOOTING_NEW_ROLLBACK_SNIPPET,
            ),
            (
                GKE_UPGRADES_TROUBLESHOOTING_OLD_RESUME_SNIPPET,
                GKE_UPGRADES_TROUBLESHOOTING_NEW_RESUME_SNIPPET,
            ),
            (
                GKE_UPGRADES_TROUBLESHOOTING_OLD_BLUE_GREEN_UPDATE_SNIPPET,
                GKE_UPGRADES_TROUBLESHOOTING_NEW_BLUE_GREEN_UPDATE_SNIPPET,
            ),
        ],
    },
}

# Marker that identifies our auto-injected footer, so injection is idempotent and
# the footer can be recognized/stripped later if needed.
FOOTER_MARKER = "<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->"

# Upstream skills are copied over verbatim on every sync (the local dir is rmtree'd first), so any
# local edits are wiped. Anything this repository needs an upstream skill to say therefore belongs
# here rather than in the skill file: these footers are the single source of truth for it and are
# re-appended after each sync. Today: the GKE create/lifecycle skills must
# keep pointing at this repo's Cluster Agent profile lifecycle, which upstream knows nothing about
# (see agents/platform/skills/cluster-agent-lifecycle/SKILL.md for the mechanics they reference),
# gke-networking must not present `--dns-endpoint` as unconditionally safe, gke-upgrades must
# point at this repo's fleet-upgrade-verification skill for executed per-member version checks,
# gke-batch-hpc and gke-workload-scaling must preflight GPU/TPU and large-shape requests into
# capacity-obtainability, gke-platform-security must carry the secrets-encryption and Security
# Posture procedures its description and the gke-basics and gke-workload-security routing notes
# send those flags to, which its body upstream does not have, and gke-workload-identity must say
# which of its IAM and exec reads this install's command gate withholds.
SKILL_FOOTERS = {
    "gke-cluster-creation": f"""{FOOTER_MARKER}

## Required final step: provision the Cluster Agent profile

Creating a cluster is **not complete** until it has a Cluster Agent. A managed cluster and its
Cluster Agent profile are **created together** — never leave a newly created cluster without a
profile. Immediately after `create_cluster` succeeds and the cluster is reachable, create its
dedicated **Cluster Agent** profile (this is what makes the cluster delegable for runtime
debugging). Use the [cluster-agent-lifecycle](../cluster-agent-lifecycle/SKILL.md) skill:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py create \\
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

The command is idempotent, so it is safe to re-run. This gives the new cluster an agent
immediately. (The `cluster-agent-reconcile` cron would also pick it up on its next run — it
manages every cluster in every project in scope, so no labeling is required.)

## Cluster Agent Profile Teardown

A managed cluster and its Cluster Agent profile are **deleted together**. When a cluster is
decommissioned/deleted, also remove its dedicated **Cluster Agent** profile (created at onboarding).
Use the [cluster-agent-lifecycle](../cluster-agent-lifecycle/SKILL.md) skill:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py delete \\
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

Do not delete a Cluster Agent profile while its cluster still exists.

Deleting the profile here is the immediate, preferred path. As a backstop, the hourly
`cluster-agent-reconcile` job auto-prunes any profile whose cluster is definitively gone, so a
profile missed during teardown is cleaned up on the next reconcile cycle.

## Before recommending GPU/TPU or large-shape capacity

Before recommending capacity for a GPU/TPU or large-shape design, load the
[capacity-obtainability](../capacity-obtainability/SKILL.md) skill and run its diagnostics:
verify the regional quota for the exact accelerator metric (e.g. `NVIDIA_A100_GPUS`), then gather
capacity obtainability advice (`gcloud beta compute advice capacity`) for the requested machine
shape and count across the region's zones, for the Spot and Flex-Start provisioning models the
advice API accepts. That skill owns the rules for what to probe and how to report it; follow it
rather than restating them here.
""",
    "gke-networking": f"""{FOOTER_MARKER}

## Before you pass `--dns-endpoint`

The `get-credentials --dns-endpoint` example above works only on a cluster that publishes a DNS
endpoint **and** has `controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic` set to
true. Check first:

```bash
gcloud container clusters describe {{cluster_name}} --region {{region}} \\
  --format='value(controlPlaneEndpointsConfig.dnsEndpointConfig.endpoint,controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic)'
```

Do not infer support from the command succeeding. When external traffic is disabled, a caller that
Google treats as internal gets a warning rather than an error, plus a kubeconfig pointing at the
DNS endpoint that then returns HTTP 403 on first use — a failure that surfaces one step later than
its cause. `gcloud container clusters update {{cluster_name}} --enable-dns-access` turns the
setting on.

The Platform Agent's own tooling makes this decision per cluster in
`/opt/data/scripts/gke_endpoint.py`, so `switch_kube_context` and the Cluster Agent profile
scaffolding already pass the flag exactly when it applies; the check above is for the times you
run `get-credentials` by hand. That decision is re-read about once a minute per cluster, so after
enabling the setting, wait a moment before retrying rather than concluding it did not work.
""",
    "gke-upgrades": f"""{FOOTER_MARKER}

## Executed version checks: the fleet-upgrade-verification skill

This skill plans one upgrade at a time; its references read one cluster at a time. When the
question is which clusters in a fleet lag a target version, by how many minors, and whether the
control plane or a node pool is the laggard, run the
[fleet-upgrade-verification](../fleet-upgrade-verification/SKILL.md) skill's script and paste its
table rather than reasoning from memory:

```bash
./skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py --target-version <version> \\
  --output /opt/data/scratch/fleet_versions.json
```

Without `--target-version` it measures each cluster against its own release channel's default and
prints that baseline per member. Run again during a rollout, it says which members started,
completed or stalled since the previous run. Without `--readiness` (below) it reads with
`gcloud container` only and changes nothing in GCP; the only thing it then writes is its own
record of each run under `/opt/data/state/fleet-upgrade-verification/`. The plan, runbook and
checklist for the members it flags are this skill's job.

The same script's `--readiness` flag executes three items of this skill's pre-upgrade checklist
per member, against the same target: PodDisruptionBudgets that would block a node drain
(`maxUnavailable: 0`, or `minAvailable` demanding every expected pod), maintenance exclusions and
the maintenance window at a given instant (`--at`, default now), and node-pool version skew
against the target control plane. Run it before writing the plan and carry its `blocked` rows into
the checklist rather than asking the operator to check those three by hand. The PDB read costs one
`get-credentials` and one `kubectl get` per member and leaves a per-member kubeconfig under
`${{HERMES_HOME:-/opt/data}}/.kubeconfigs/`; an exclusion is reported as holding back automatic
upgrades only.

When the checklist's deprecated-API item comes up, the same skill's `api_deprecation_scan.py` scans
the linked GitOps repositories' manifests for apiVersions the target removes and reports each with
its replacement and the commit it read; run it with `--target-version` and the version report's
`--output`. It reads Git only: point at GKE Deprecation Insights for live client usage.
""",
    "gke-platform-security": f"""{FOOTER_MARKER}

## Application-layer secrets encryption and Security Posture

The description and the gke-basics and gke-workload-security routing notes send these two flags
here.

**Secrets encryption (`--database-encryption-key`)** envelope-encrypts Secrets in etcd with a
Cloud KMS key. The key must be in the cluster's region (`KMS_LOCATION`; for a zonal cluster, the
region that contains its zone, since Cloud KMS has no zonal locations), and the GKE service agent
of the cluster's project (`CLUSTER_PROJECT_NUMBER`, not the key project's number) needs
`roles/cloudkms.cryptoKeyEncrypterDecrypter` on it before the cluster can use it:

```bash
gcloud kms keys add-iam-policy-binding KEY_NAME \\
  --keyring KEYRING_NAME --location KMS_LOCATION --project KMS_PROJECT_ID \\
  --member serviceAccount:service-CLUSTER_PROJECT_NUMBER@container-engine-robot.iam.gserviceaccount.com \\
  --role roles/cloudkms.cryptoKeyEncrypterDecrypter

gcloud container clusters update CLUSTER_NAME --location LOCATION \\
  --database-encryption-key projects/KMS_PROJECT_ID/locations/KMS_LOCATION/keyRings/KEYRING_NAME/cryptoKeys/KEY_NAME

# CURRENT_STATE_ENCRYPTED once encryption completes (`state` is the requested setting, not the observed one)
gcloud container clusters describe CLUSTER_NAME --location LOCATION \\
  --format="value(databaseEncryption.currentState)"
```

**Security Posture (`--security-posture`)** turns on configuration auditing (`standard`) and,
with `--workload-vulnerability-scanning`, OS vulnerability scanning of running workloads:

```bash
gcloud container clusters update CLUSTER_NAME --location LOCATION \\
  --security-posture=standard --workload-vulnerability-scanning=standard
```
""",
    "gke-workload-identity": f"""{FOOTER_MARKER}

## Reads this install withholds

The Platform Agent's command gate refuses three reads this skill uses:
`gcloud iam service-accounts get-iam-policy` (Step 3), `gcloud asset search-all-iam-policies`
(Step 4) and `kubectl exec` (Step 5). Do not retry them in another spelling. Run the steps you can,
then hand the refused commands to the user with the placeholders filled in, say which output
decides the diagnosis, and ask for the result. A role granted on the whole project is readable
here: `gcloud projects get-iam-policy PROJECT_ID --format=json` shows it, so search its bindings
for the principal before handing Step 4 over.
""",
    "gke-batch-hpc": f"""{FOOTER_MARKER}

## Before scheduling a GPU/TPU batch job with a deadline

Before recommending a start time, zone, or capacity path for a GPU/TPU or large-shape batch job —
especially one that must finish inside a horizon — load the
[capacity-obtainability](../capacity-obtainability/SKILL.md) skill and run its **Future windows**
section: verify the regional quota for the exact accelerator metric, probe
`gcloud beta compute advice calendar-mode` once per candidate region for the job's shape, count,
duration, and horizon, and rank the returned windows. That skill owns the probe's flags, the
chips-per-node arithmetic, the ranking rule, and the paired ProvisioningRequest + LocalQueue
shapes; follow it rather than restating them here.
""",
    "gke-compute-classes": f"""{FOOTER_MARKER}

## Node upgrades during a PDB-guarded canary

The "Node auto-upgrades" row in `references/compute-class-lifecycle.md` overstates the protection:
GKE's node-upgrade drain respects a PodDisruptionBudget for up to one hour, then evicts the pods
anyway. A node auto-upgrade can therefore still evict the green pods of a multi-hour canary guarded
by a `maxUnavailable: 0` PDB. When the rollout must not be disrupted, run it outside the cluster's
maintenance window or add a maintenance exclusion for its duration (see `gke-upgrades`).

## Reading the code index from this install

`references/compute-class-code-index.md` opens by telling its reader to clone the repository, and
its "Code verification instructions" section runs `git log` and `git blame`. Those steps are for the
user's own terminal: this install cannot reach GitHub, so follow the CRITICAL RULE above and hand
them to the user. The index's `groupRulesByScore` entry names a function the upstream source does
not have; do not cite it.
""",
    "gke-workload-scaling": f"""{FOOTER_MARKER}

## Before recommending GPU/TPU or large-shape capacity for a scale-up

Before recommending capacity for a GPU/TPU or large-shape scale-up, load the
[capacity-obtainability](../capacity-obtainability/SKILL.md) skill and run its diagnostics: the
regional quota for the exact accelerator metric, then live obtainability advice for the requested
shape across zones and provisioning models — and, for a deadline-bound batch scale-up, its
**Future windows** section (`gcloud beta compute advice calendar-mode`). That skill owns what to
probe and how to report it; follow it rather than restating it here.
""",
}


def abort(message):
    """Report a fatal error after the progress log and exit non-zero.

    stdout is block-buffered when it is not a terminal, so a `> log 2>&1` run prints the whole
    stderr message above every `Syncing '...'` line unless stdout is flushed first — detaching
    the error from the skill it names.
    """
    sys.stdout.flush()
    print(message, file=sys.stderr)
    sys.exit(1)


def overlay_mirrored_skills(repo_root):
    """Names of the skills scripts/skill_overlay.py mirrors, which this script leaves alone."""
    root = os.path.join(repo_root, SKILL_OVERLAY_ROOT)
    if not os.path.isdir(root):
        return set()
    return {name for name in os.listdir(root) if os.path.isfile(os.path.join(root, name, SKILL_OVERLAY_LOCK))}


def target_agents():
    """Every agent directory this script writes into, as directory names."""
    return sorted(
        set(DEFAULT_TARGET_AGENTS + [a for agents in SKILL_AGENT_OVERRIDES.values() for a in agents])
    )


def written_pathspecs(repo_root):
    """Git pathspecs covering everything this script writes, and nothing else.

    A recovery instruction is only safe if it names what the run could have touched. The sync
    writes prefixed skills under the target agents and nowhere else: `agents/cluster/skills/gke-*`
    is maintained in this repository rather than synced, and the unprefixed skills beside the
    synced ones under a target agent are too, so a pathspec one level broader than this discards
    uncommitted work no run of this script could have produced. A skill with an overlay lock is
    excluded: scripts/skill_overlay.py writes it, and this script skips it.
    """
    included = [f"agents/{agent}/skills/{SKILL_PREFIX}*" for agent in target_agents()]
    excluded = [
        f":(exclude){SKILL_OVERLAY_SKILLS}/{name}" for name in sorted(overlay_mirrored_skills(repo_root))
    ]
    return included + excluded


def local_correction_lost_message(detail, repo_root):
    """The operator-facing text for a backstop failure, recovery commands included.

    Separate from the handler that prints it because the pre-flight makes the handler
    unreachable from a fixture: the only way to read what an operator would be told to run is
    to call this.
    """
    pathspecs = " ".join(f"'{pathspec}'" for pathspec in written_pathspecs(repo_root))
    return (
        f"\nError: Synchronization aborted. {detail}\n"
        "The clone and the copy made from it disagree, which should not happen. Skills copied "
        "before this point are already written, and a copy adds untracked files as well as "
        "modifying tracked ones, so discarding them takes both of:\n"
        f"  git checkout -- {pathspecs}\n  git clean -fd -- {pathspecs}\n"
        "Those pathspecs are everything this script writes; run nothing broader, because the "
        "skills beside them are maintained in this repository and the sync never touched them."
    )


def classify_substitution(content, target, replacement):
    """Decide whether a registered pair still applies to content.

    The pre-flight reads this against the clone and the backstop reads it against the copy made
    from it, both through substitute(), so the one place the rule is written is the only place
    it can be got wrong. Two
    earlier versions of this file stated it twice and the two statements disagreed, which is how
    an uncorrected passage shipped under exit 0.

    Returns (SUBSTITUTION_APPLY, None) when the pair applies cleanly, (SUBSTITUTION_SKIP, None)
    when upstream already reads the way applying it would leave it, and
    (SUBSTITUTION_UNDECIDABLE, reason) otherwise. Undecidable covers three shapes, and the
    reason says which: the target is gone, the target is there but so is the replacement, and
    the target occurs more than once.
    """
    occurrences = content.count(target)
    has_replacement = replacement in content

    # A pair is applied with a count of SUBSTITUTION_COUNT, so exactly that many occurrences is
    # the only count under which applying it leaves nothing uncorrected behind.
    if occurrences == SUBSTITUTION_COUNT and not has_replacement:
        return SUBSTITUTION_APPLY, None
    if occurrences == 0 and has_replacement:
        return SUBSTITUTION_SKIP, None

    if occurrences == 0:
        return SUBSTITUTION_UNDECIDABLE, (
            "target snippet not found. Upstream has rewritten the passage this repository "
            "corrects; update the target to match the current upstream text, or drop the pair "
            "if upstream has fixed the defect."
        )
    if has_replacement:
        return SUBSTITUTION_UNDECIDABLE, (
            "the target and the replacement are both present, so whether the correction still "
            "applies cannot be decided. Extend the target with surrounding context until only "
            "one of them matches."
        )
    return SUBSTITUTION_UNDECIDABLE, (
        f"target snippet occurs {occurrences} times and a pair is applied once, so every copy "
        f"after the first would ship uncorrected. Extend the target with surrounding context "
        f"until it matches one passage."
    )


def substitute(content, pairs):
    """Apply a skill's pairs in order, each classified against the text the earlier ones left.

    Returns (content, problems): the rewritten text, and one (target, reason) per pair
    classify_substitution calls undecidable, which is left unapplied. The pre-flight and
    apply_substitutions both call this, so a later pair whose verdict an earlier pair's
    replacement changes is judged the same way by each.
    """
    problems = []
    for target, replacement in pairs:
        verdict, reason = classify_substitution(content, target, replacement)
        if verdict == SUBSTITUTION_UNDECIDABLE:
            problems.append((target, reason))
        elif verdict == SUBSTITUTION_APPLY:
            content = content.replace(target, replacement, SUBSTITUTION_COUNT)
    return content, problems


def substituted_files(skill_name):
    """Every file of a skill that has pairs registered, as (path inside the skill, pairs).

    SKILL.md comes from SKILL_SUBSTITUTIONS and the rest from SKILL_FILE_SUBSTITUTIONS; the
    pre-flight and apply_substitutions both read this, so neither can cover a file the other
    skips.
    """
    files = []
    if skill_name in SKILL_SUBSTITUTIONS:
        files.append((SKILL_MD_FILENAME, SKILL_SUBSTITUTIONS[skill_name]))
    files.extend(sorted(SKILL_FILE_SUBSTITUTIONS.get(skill_name, {}).items()))
    return files


def verify_local_corrections(upstream_skills_dir, discovered_skills, skip=frozenset()):
    """Check every registered correction against the clone before anything is written.

    The sync rmtree's each destination before copying, so a correction found unappliable
    mid-loop would leave a partly-refreshed tree. Every registry entry is checked here, against
    the clone, while the tree is still untouched, and every problem is reported at once rather
    than one re-clone at a time.

    Raises UpstreamDriftError listing every registry entry that no longer matches upstream.
    """
    discovered = set(discovered_skills)
    problems = []

    for registry_name, registry in (
        ("SKILL_SUBSTITUTIONS", SKILL_SUBSTITUTIONS),
        ("SKILL_FILE_SUBSTITUTIONS", SKILL_FILE_SUBSTITUTIONS),
        ("SKILL_FOOTERS", SKILL_FOOTERS),
    ):
        for skill_name in sorted(registry):
            if skill_name in skip:
                problems.append(
                    f"{registry_name}[{skill_name!r}]: this skill has an {SKILL_OVERLAY_LOCK} and is "
                    f"mirrored by scripts/skill_overlay.py, so the entry never applies. Record the "
                    f"change as a patch (make skills-refresh) and drop the entry."
                )
                continue
            if skill_name not in discovered:
                problems.append(
                    f"{registry_name}[{skill_name!r}]: upstream no longer ships this skill "
                    f"(renamed or removed). Move the entry to the new name, or drop it."
                )
                continue
            if registry_name == "SKILL_FILE_SUBSTITUTIONS":
                files = sorted(registry[skill_name].items())
            else:
                files = [(SKILL_MD_FILENAME, registry[skill_name])]
            for relpath, pairs in files:
                path = os.path.join(upstream_skills_dir, skill_name, relpath)
                if not os.path.isfile(path):
                    problems.append(
                        f"{registry_name}[{skill_name!r}]: upstream skill has no {relpath}."
                    )
                    continue
                if registry_name == "SKILL_FOOTERS":
                    continue
                with open(path, "r", encoding=UTF_8_ENCODING) as f:
                    content = f.read()
                _, undecidable = substitute(content, pairs)
                for target, reason in undecidable:
                    problems.append(
                        f"{registry_name}[{skill_name!r}] {relpath}: {reason} "
                        f"Target begins: {target.splitlines()[0]!r}"
                    )

    if problems:
        raise UpstreamDriftError("\n".join(f"  - {p}" for p in problems))


def apply_substitutions(dest_path, skill_name):
    """Apply in-place string substitutions to a freshly-synced skill's registered files.

    Used when an upstream defect must be corrected in-place (such as a remediation
    sequence where an appended footer would still leave the broken command in the
    body of the skill).

    Idempotent: a pair whose replacement is present and whose target is not has already been
    applied, or adopted upstream, and is skipped. Returns True if at least one substitution was
    applied, else False.

    substitute() decides which pairs apply; this raises LocalCorrectionLost on the first it calls
    undecidable. verify_local_corrections has already rejected those against the clone,
    so reaching one here means the copy and the clone disagree; the raise keeps the defect out
    of the tree.
    """
    applied = False
    for relpath, substitutions in substituted_files(skill_name):
        path = os.path.join(dest_path, relpath)
        if not os.path.isfile(path):
            raise LocalCorrectionLost(
                f"{skill_name} has substitutions configured but {path} does not exist."
            )

        with open(path, "r", encoding=UTF_8_ENCODING) as f:
            content = f.read()

        substituted, problems = substitute(content, substitutions)
        if problems:
            target, reason = problems[0]
            registry_name = (
                "SKILL_SUBSTITUTIONS" if relpath == SKILL_MD_FILENAME else "SKILL_FILE_SUBSTITUTIONS"
            )
            raise LocalCorrectionLost(
                f"{skill_name}/{relpath}: {reason} The entry is in "
                f"{registry_name}. Target begins: {target.splitlines()[0]!r}"
            )

        if substituted != content:
            with open(path, "w", encoding=UTF_8_ENCODING) as f:
                f.write(substituted)
            applied = True
    return applied


def inject_footer(dest_path, skill_name):
    """Append this repository's footer for a skill to its freshly-synced SKILL.md.

    Idempotent: does nothing if the skill has no footer configured or the footer marker is
    already present. Returns True if a footer was written, else False.

    Raises LocalCorrectionLost when a skill with a footer configured has no SKILL.md, on the
    same terms as apply_substitutions: a footer dropped in silence ships a skill missing the
    step that couples it to this repository.
    """
    footer = SKILL_FOOTERS.get(skill_name)
    if footer is None:
        return False

    skill_md = os.path.join(dest_path, SKILL_MD_FILENAME)
    if not os.path.isfile(skill_md):
        raise LocalCorrectionLost(
            f"{skill_name} has a footer configured but {skill_md} does not exist."
        )

    with open(skill_md, "r", encoding=UTF_8_ENCODING) as f:
        existing = f.read()
    if FOOTER_MARKER in existing:
        return False

    separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    with open(skill_md, "a", encoding=UTF_8_ENCODING) as f:
        f.write(separator + footer)
    return True


def run_cmd(cmd, cwd=None):
    """Runs a shell command and returns the result, raising an exception on failure."""
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error running command: {' '.join(cmd)}", file=sys.stderr)
        print(f"Stdout:\n{res.stdout}", file=sys.stderr)
        print(f"Stderr:\n{res.stderr}", file=sys.stderr)
        res.check_returncode()
    return res

def main():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    
    try:
        print("Creating temporary directory for shallow clone...")
        with tempfile.TemporaryDirectory() as tmpdir:
            print(f"Cloning upstream repository (depth 1): {UPSTREAM_REPO}...")
            run_cmd([
                "git", "clone", "--depth", "1",
                UPSTREAM_REPO, tmpdir
            ])
            
            upstream_skills_dir = os.path.join(tmpdir, UPSTREAM_SKILLS_PATH)
            if not os.path.isdir(upstream_skills_dir):
                print(f"Error: upstream skills directory not found in clone: {upstream_skills_dir}", file=sys.stderr)
                sys.exit(1)
                
            # Discover all skills that start with the prefix (e.g. 'gke-')
            discovered_skills = sorted([
                name for name in os.listdir(upstream_skills_dir)
                if name.startswith(SKILL_PREFIX) and os.path.isdir(os.path.join(upstream_skills_dir, name))
            ])
            
            # Not a warning: an empty discovery is indistinguishable from upstream having moved
            # the skills, and the prune below removes every local skill not in the list, so a
            # run that continued from here would delete all of them. The pre-flight cannot catch
            # it either, since it only reads the skills that were discovered.
            if not discovered_skills:
                abort(
                    f"\nError: Synchronization aborted, nothing written. No skills matching "
                    f"prefix '{SKILL_PREFIX}' in the clone at {UPSTREAM_SKILLS_PATH}. Upstream "
                    f"has moved or renamed them; update UPSTREAM_SKILLS_PATH or SKILL_PREFIX in "
                    f"{os.path.basename(__file__)} and re-run."
                )


            print(f"\nDiscovered {len(discovered_skills)} skills matching prefix '{SKILL_PREFIX}':")
            for name in discovered_skills:
                print(f"  - {name}")

            mirrored_elsewhere = overlay_mirrored_skills(repo_root)
            if mirrored_elsewhere:
                print(f"\nSkipping {len(mirrored_elsewhere)} skill(s) mirrored by scripts/skill_overlay.py "
                      f"(they have an {SKILL_OVERLAY_LOCK}):")
                for name in sorted(mirrored_elsewhere):
                    print(f"  - {name}")

            # Nothing below this line is reversible without git, so every registered local
            # correction is checked against the clone first.
            verify_local_corrections(upstream_skills_dir, discovered_skills, skip=mirrored_elsewhere)

            # Prune obsolete local skill directories that were renamed/removed upstream
            for agent in target_agents():
                agent_skills_dir = os.path.join(repo_root, "agents", agent, "skills")
                if os.path.isdir(agent_skills_dir):
                    for local_name in sorted(os.listdir(agent_skills_dir)):
                        if (local_name.startswith(SKILL_PREFIX) and local_name not in discovered_skills
                                and local_name not in mirrored_elsewhere):
                            stale_path = os.path.join(agent_skills_dir, local_name)
                            print(f"Removing obsolete upstream skill: agents/{agent}/skills/{local_name}...")
                            shutil.rmtree(stale_path)
                
            print("\nSyncing skills...")
            for skill_name in discovered_skills:
                if skill_name in mirrored_elsewhere:
                    continue
                src_skill_path = os.path.join(upstream_skills_dir, skill_name)
                agents = SKILL_AGENT_OVERRIDES.get(skill_name, DEFAULT_TARGET_AGENTS)
                
                for agent in agents:
                    dest_path = os.path.join(repo_root, "agents", agent, "skills", skill_name)
                    print(f"Syncing '{skill_name}' to agents/{agent}/skills/{skill_name}...")
                    
                    # Delete existing destination directory to remove stale files
                    if os.path.exists(dest_path):
                        shutil.rmtree(dest_path)
                        
                    # Re-create destination parent directories if needed
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    
                    # Copy from upstream src to dest
                    shutil.copytree(src_skill_path, dest_path)

                    # Apply in-place substitutions to correct upstream defects.
                    if apply_substitutions(dest_path, skill_name):
                        print(f"  Applied substitutions to {skill_name}")

                    # Re-inject the Cluster Agent coupling footer (wiped by the copy above).
                    if inject_footer(dest_path, skill_name):
                        print(f"  Injected kube-agents footer into {skill_name}/{SKILL_MD_FILENAME}")

            print("\nSynchronization complete!")
    except LocalCorrectionLost as e:
        abort(local_correction_lost_message(e, repo_root))
    except UpstreamDriftError as e:
        abort(
            f"\nError: Synchronization aborted, nothing written. Corrections this repository "
            f"registers can no longer be applied as written:\n{e}\n"
            f"Update the entries in {os.path.basename(__file__)} and re-run."
        )
    except subprocess.CalledProcessError:
        abort("\nError: Synchronization failed due to command error. Details above.")
    except Exception as e:
        abort(f"\nError: An unexpected error occurred: {e}")

if __name__ == "__main__":
    main()
