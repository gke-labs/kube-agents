# Cluster agent persona

The cluster agent written in the [persona format](../../../a2a/lib/persona/README.md):
a read-only SRE for one GKE cluster, which each task names through the
`project`, `cluster` and `location` parameters. It is the example persona for
#3656, for the profile dispatcher (#3655) to run.

It ships as its own image, `persona-cluster` (`a2a/Dockerfile.persona-cluster`,
with this directory under `/files`), and the chart's `profiles.cluster.enabled`
renders the generic `cluster` AgentProfile that runs it (#3658). A profile
worker pod mounts the image at `/persona`. No pod runs it until the profile
dispatcher, worker and pool land (#4148, #4483, #4484) and the operator runs
them.

The Hermes cluster profiles don't use it. The platform-agent image also builds
it into `/opt/cluster-template/persona` with the rest of `agents/cluster/`, but
`OVERLAY_ITEMS` in `agents/platform/scripts/cluster_agent_profile.py` leaves it
out, so those profiles still come from `agents/cluster/` (`SOUL.md`,
`AGENTS.md`, `config.yaml`, `skills/`). This directory is the
generic version of the same agent, with the kanban loop, the per-cluster
stamping and the pinned kubeconfig taken out: credentials for the task's target
come from the credential proxy under the task's grant.

The profile worker sets `PROFILE_TASK_PROJECT`, `PROFILE_TASK_LOCATION` and
`PROFILE_TASK_CLUSTER` from the task's parameters; the instructions' first step
reads the target from them.

Each skill's `SKILL.md` (and `workload-triage-scan/references/reporting.md`) is
its `agents/cluster/skills` source with only the edits
`a2a/lib/persona/claudecode/cluster_persona_test.go` lists. Every other file is
a byte-identical copy: the assets of the same path under `agents/cluster/skills`,
and the three scripts of `agents/platform/scripts/stall_report.py`,
`agents/platform/scripts/workload_triage.py` and
`agents/platform/skills/gke-workload-security/scripts/audit_cluster.sh`. The
same test checks both. The skills run their scripts by skill-relative paths
(`scripts/...`), from the skill's directory.

What it holds:

- [`persona.yaml`](persona.yaml): the manifest, with the three parameters and
  the tools it grants (`read_file`, `shell`, `web_search` and the two servers);
- [`instructions.md`](instructions.md): the instructions;
- [`mcp.yaml`](mcp.yaml): the `gke` and `developer_knowledge` servers on the
  broker's relay, each limited to its read tools;
- the skills:
  [`gke-load-bearing-change`](skills/gke-load-bearing-change/SKILL.md),
  [`gke-observability`](skills/gke-observability/SKILL.md),
  [`gke-reliability`](skills/gke-reliability/SKILL.md),
  [`gke-stall-detection`](skills/gke-stall-detection/SKILL.md),
  [`gke-storage`](skills/gke-storage/SKILL.md),
  [`gke-workload-scaling`](skills/gke-workload-scaling/SKILL.md),
  [`gke-workload-security`](skills/gke-workload-security/SKILL.md),
  [`gke-workload-troubleshooting`](skills/gke-workload-troubleshooting/SKILL.md),
  [`pod-container-creating`](skills/pod-container-creating/SKILL.md),
  [`pod-crashloop-oom`](skills/pod-crashloop-oom/SKILL.md),
  [`pod-pending-scheduling`](skills/pod-pending-scheduling/SKILL.md),
  [`service-connectivity`](skills/service-connectivity/SKILL.md) and
  [`workload-triage-scan`](skills/workload-triage-scan/SKILL.md).

Check it from `a2a/`:

```sh
go run ./cmd/persona validate --in ../agents/cluster/persona
go run ./cmd/persona render --harness claude-code --in ../agents/cluster/persona --out "$(mktemp -d)" \
  --relay-url http://credential-proxy:8765/v1/mcp \
  --relay-command 'node /opt/mcp-remote/dist/proxy.js {url} --transport http-only'
```
