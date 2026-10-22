# Cluster agent persona

The cluster agent written in the [persona format](../../../a2a/lib/persona/README.md):
a read-only SRE for one GKE cluster, which each task names through the
`project`, `cluster` and `location` parameters. It is the example persona for
#3656, and the profile dispatcher (#3655) runs it.

Today's install does not read it. The Hermes cluster profiles still come from
`agents/cluster/` (`SOUL.md`, `AGENTS.md`, `config.yaml`, `skills/`); this
directory is the generic version of the same agent, with the kanban loop, the
per-cluster stamping and the pinned kubeconfig taken out. Its skills are copies
of `agents/cluster/skills`, without the `get-credentials` step and the kanban
reporting, plus the two scripts they run.

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
  [`gke-workload-security`](skills/gke-workload-security/SKILL.md) and
  [`gke-workload-troubleshooting`](skills/gke-workload-troubleshooting/SKILL.md).

Check it from `a2a/`:

```sh
go run ./cmd/persona validate --in ../agents/cluster/persona
go run ./cmd/persona render --harness claude-code --in ../agents/cluster/persona --out "$(mktemp -d)" \
  --relay-url http://credential-proxy:8765/mcp \
  --relay-command 'node /opt/mcp-remote/dist/proxy.js {url} --transport http-only'
```
