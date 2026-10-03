# Inspect experiment

Evaluates skills independently of the harness with
[Inspect](https://inspect.aisi.org.uk): the same task runs under Gemini CLI or
Claude Code (through [`inspect_swe`](https://meridianlabs-ai.github.io/inspect_swe/)),
with `skills/` installed or left out as a control. `skills/fleet-inventory` is a
stand-in written for these tasks, not a skill kube-agents ships. Nothing here
touches `bench/`, and no CI job runs these tasks: `bench/tasks` evaluates a
deployed Platform Agent, while these tasks hold the skill fixed and vary the
harness, which the bench case format has no field for.

The harness's model calls go through Inspect's model bridge, so the harness and
the model are separate choices (Claude Code can drive Gemini), and every run
lands in the same Inspect log format whichever harness produced it.

Each sample runs in a pod that `k8s_pod.py` creates from a plain manifest,
`environments/*/sandbox.yaml`, in the `kind-kube-agents` cluster
(`INSPECT_CONTEXT` names another kubectl context): every Pod in the manifest
is created per sample and deleted after it, and every other object is applied
once per task and left in place.
Each task is its own file; `harness.py` picks the harness and `scoring.py`
checks the answer.

- `clusters_from_memory`: the pod has no Kubernetes credential; the inventory a
  background sync would have written is a ConfigMap mounted at
  `/var/lib/kube-agents`, which only the `fleet-inventory` skill mentions.
  Without the skill an agent can still find it by searching the filesystem;
  the control measures what that search costs.
- `namespaces`: the pod runs as a ServiceAccount that can only list
  namespaces; the scorer compares the answer with the namespaces listed at
  scoring time. Its image adds kubectl to Ubuntu and has to be loaded into
  the cluster first.

```bash
docker build -t kube-agents-inspect-kubectl:v1.33.1 environments/kubectl
kind load docker-image kube-agents-inspect-kubectl:v1.33.1 --name kube-agents
export GOOGLE_API_KEY=...
M="--model google/gemini-3.7-flash --epochs 3 --retry-on-error=2"
uv run inspect eval clusters_from_memory.py $M
uv run inspect eval clusters_from_memory.py $M -T skills=false   # control
uv run inspect eval clusters_from_memory.py $M -T harness=claude -T identity=claude-sonnet-5
uv run inspect eval namespaces.py $M
uv run inspect view   # logs/: transcripts, events, tokens, scores
```

`identity` is the Claude model Claude Code presents itself as; it exits with
`unrecognized_model` under a model name it does not recognise, so set it when the bridged
model is not an Anthropic one. Harness versions are pinned in `harness.py`.
