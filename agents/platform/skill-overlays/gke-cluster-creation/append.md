<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Required final step: provision the Cluster Agent profile

Creating a cluster is **not complete** until it has a Cluster Agent. A managed cluster and its
Cluster Agent profile are **created together** — never leave a newly created cluster without a
profile. Immediately after `create_cluster` succeeds and the cluster is reachable, create its
dedicated **Cluster Agent** profile (this is what makes the cluster delegable for runtime
debugging). Use the [cluster-agent-lifecycle](../cluster-agent-lifecycle/SKILL.md) skill:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py create \
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
python3 /opt/data/scripts/cluster_agent_profile.py delete \
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
