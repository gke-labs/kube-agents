#!/usr/bin/env python3
"""Syncs GKE agent skills from the upstream google/skills repository (skills/cloud).

The platform image build runs the shell blocks of every synced SKILL.md through
deploy/docker/check_skill_commands.py, so a sync that brings in a command Tirith
refuses, or changes any line of a block listed in its KNOWN_FINDINGS, comments
included, fails that build until the list is updated. So does a shell block whose Markdown does not parse as one; that
fix goes in SKILL_SUBSTITUTIONS below.

docs/designs/upstream-skill-overlays.md is the design, not yet implemented, for
replacing the string registries below with a pinned upstream copy and a patch
overlay per skill.
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
UTF_8_ENCODING = "utf-8"
SUBSTITUTION_COUNT = 1

# What classify_substitution() returns: apply the pair, skip it because upstream already reads
# the way the pair would make it read, or refuse because neither of those is true.
SUBSTITUTION_APPLY = "apply"
SUBSTITUTION_SKIP = "skip"
SUBSTITUTION_UNDECIDABLE = "undecidable"


class UpstreamDriftError(Exception):
    """A registered local correction can no longer be applied to what upstream now ships.

    Both registries below name an upstream skill and the text they expect to find in it. When
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

# gke-workload-troubleshooting's Step 5 upstream ends every crashloop walk by opening a pull
# request, unconditionally ("do not wait for human merge"). This repository's platform persona
# opens one only when the request asked for the fix (agents/platform/SOUL.md §3, item 3: a request to
# investigate or report gets the manifest in the reply); under the A2A bridge the persona reads the
# user's request directly and loads this skill for exactly that walk, so the unconditional step
# re-created the write the persona's rule forbids (#2037). The replacement conditions item 3 on the
# request and adds item 4 for the diagnostic case.
GKE_WORKLOAD_TROUBLESHOOTING_OLD_STEP5_SUBMIT_SNIPPET = """3.  Check if a branch or Pull Request (PR) already exists for this
    workload/failure. If so, update the existing branch/PR or notify the user
    instead of creating a duplicate. Otherwise, create a branch, commit the
    change, open a Pull Request (PR) on GitHub, and conclude the workflow (do
    not wait for human merge)."""

GKE_WORKLOAD_TROUBLESHOOTING_NEW_STEP5_SUBMIT_SNIPPET = """3.  If the request asked for the fix to be submitted or applied ("fix it",
    "open a PR", a card whose task says so), check whether a branch or Pull
    Request (PR) already exists for this workload/failure. If so, update the
    existing branch/PR or notify the user instead of creating a duplicate.
    Otherwise, create a branch, commit the change, open a Pull Request (PR) on
    GitHub, and conclude the workflow (do not wait for human merge).
4.  If the request asked you to investigate, diagnose or report, the manifest
    from step 2 goes in your reply as a recommendation and the pull request is
    one message away; do not open one (`SOUL.md` §3, item 3)."""

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
    ],
    "gke-workload-troubleshooting": [
        (
            GKE_WORKLOAD_TROUBLESHOOTING_OLD_STEP5_SUBMIT_SNIPPET,
            GKE_WORKLOAD_TROUBLESHOOTING_NEW_STEP5_SUBMIT_SNIPPET,
        ),
    ],
}

# Marker that identifies our auto-injected footer, so injection is idempotent and
# the footer can be recognized/stripped later if needed.
FOOTER_MARKER = "<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->"

# Upstream skills are copied over verbatim on every sync (the local dir is rmtree'd first), so any
# local edits are wiped. Anything this repository needs an upstream skill to say therefore belongs
# here rather than in the skill file: these footers are the single source of truth for it and are
# re-appended after each sync. Four things need saying today — the GKE create/lifecycle skills must
# keep pointing at this repo's Cluster Agent profile lifecycle, which upstream knows nothing about
# (see agents/platform/skills/cluster-agent-lifecycle/SKILL.md for the mechanics they reference),
# gke-networking must not present `--dns-endpoint` as unconditionally safe, gke-upgrades must
# point at this repo's fleet-upgrade-verification skill for executed per-member version checks, and
# gke-batch-hpc and gke-workload-scaling must preflight GPU/TPU and large-shape requests into
# capacity-obtainability.
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


def target_agents():
    """Every agent directory this script writes into, as directory names."""
    return sorted(
        set(DEFAULT_TARGET_AGENTS + [a for agents in SKILL_AGENT_OVERRIDES.values() for a in agents])
    )


def written_pathspecs():
    """Git pathspecs covering everything this script writes, and nothing else.

    A recovery instruction is only safe if it names what the run could have touched. The sync
    writes prefixed skills under the target agents and nowhere else: `agents/cluster/skills/gke-*`
    is maintained in this repository rather than synced, and the unprefixed skills beside the
    synced ones under a target agent are too, so a pathspec one level broader than this discards
    uncommitted work no run of this script could have produced.
    """
    return [f"agents/{agent}/skills/{SKILL_PREFIX}*" for agent in target_agents()]


def local_correction_lost_message(detail):
    """The operator-facing text for a backstop failure, recovery commands included.

    Separate from the handler that prints it because the pre-flight makes the handler
    unreachable from a fixture: the only way to read what an operator would be told to run is
    to call this.
    """
    recovery = "".join(
        f"  git checkout -- '{pathspec}'\n  git clean -fd '{pathspec}'\n"
        for pathspec in written_pathspecs()
    )
    return (
        f"\nError: Synchronization aborted. {detail}\n"
        "The clone and the copy made from it disagree, which should not happen. Skills copied "
        "before this point are already written, and a copy adds untracked files as well as "
        "modifying tracked ones, so discarding them takes all of:\n"
        f"{recovery}"
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


def verify_local_corrections(upstream_skills_dir, discovered_skills):
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
        ("SKILL_FOOTERS", SKILL_FOOTERS),
    ):
        for skill_name in sorted(registry):
            if skill_name not in discovered:
                problems.append(
                    f"{registry_name}[{skill_name!r}]: upstream no longer ships this skill "
                    f"(renamed or removed). Move the entry to the new name, or drop it."
                )
                continue
            skill_md = os.path.join(upstream_skills_dir, skill_name, SKILL_MD_FILENAME)
            if not os.path.isfile(skill_md):
                problems.append(
                    f"{registry_name}[{skill_name!r}]: upstream skill has no {SKILL_MD_FILENAME}."
                )
                continue
            if registry_name != "SKILL_SUBSTITUTIONS":
                continue
            with open(skill_md, "r", encoding=UTF_8_ENCODING) as f:
                content = f.read()
            _, undecidable = substitute(content, registry[skill_name])
            for target, reason in undecidable:
                problems.append(
                    f"{registry_name}[{skill_name!r}]: {reason} "
                    f"Target begins: {target.splitlines()[0]!r}"
                )

    if problems:
        raise UpstreamDriftError("\n".join(f"  - {p}" for p in problems))


def apply_substitutions(dest_path, skill_name):
    """Apply in-place string substitutions to a freshly-synced skill's SKILL.md.

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
    substitutions = SKILL_SUBSTITUTIONS.get(skill_name)
    if not substitutions:
        return False

    skill_md = os.path.join(dest_path, SKILL_MD_FILENAME)
    if not os.path.isfile(skill_md):
        raise LocalCorrectionLost(
            f"{skill_name} has substitutions configured but {skill_md} does not exist."
        )

    with open(skill_md, "r", encoding=UTF_8_ENCODING) as f:
        content = f.read()

    substituted, problems = substitute(content, substitutions)
    if problems:
        target, reason = problems[0]
        raise LocalCorrectionLost(
            f"{skill_name}/{SKILL_MD_FILENAME}: {reason} The entry is in "
            f"SKILL_SUBSTITUTIONS. Target begins: {target.splitlines()[0]!r}"
        )

    if substituted == content:
        return False
    with open(skill_md, "w", encoding=UTF_8_ENCODING) as f:
        f.write(substituted)
    return True


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

            # Nothing below this line is reversible without git, so every registered local
            # correction is checked against the clone first.
            verify_local_corrections(upstream_skills_dir, discovered_skills)

            # Prune obsolete local skill directories that were renamed/removed upstream
            for agent in target_agents():
                agent_skills_dir = os.path.join(repo_root, "agents", agent, "skills")
                if os.path.isdir(agent_skills_dir):
                    for local_name in sorted(os.listdir(agent_skills_dir)):
                        if local_name.startswith(SKILL_PREFIX) and local_name not in discovered_skills:
                            stale_path = os.path.join(agent_skills_dir, local_name)
                            print(f"Removing obsolete upstream skill: agents/{agent}/skills/{local_name}...")
                            shutil.rmtree(stale_path)
                
            print("\nSyncing skills...")
            for skill_name in discovered_skills:
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
                        print(f"  Applied substitutions to {skill_name}/{SKILL_MD_FILENAME}")

                    # Re-inject the Cluster Agent coupling footer (wiped by the copy above).
                    if inject_footer(dest_path, skill_name):
                        print(f"  Injected kube-agents footer into {skill_name}/{SKILL_MD_FILENAME}")

            print("\nSynchronization complete!")
    except LocalCorrectionLost as e:
        abort(local_correction_lost_message(e))
    except UpstreamDriftError as e:
        abort(
            f"\nError: Synchronization aborted, nothing written. Upstream has moved away from "
            f"corrections this repository registers:\n{e}\n"
            f"Update the entries in {os.path.basename(__file__)} and re-run."
        )
    except subprocess.CalledProcessError:
        abort("\nError: Synchronization failed due to command error. Details above.")
    except Exception as e:
        abort(f"\nError: An unexpected error occurred: {e}")

if __name__ == "__main__":
    main()
