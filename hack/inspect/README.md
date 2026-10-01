# Inspect experiment

Evaluates skills independently of the harness with
[Inspect](https://inspect.aisi.org.uk): the same task runs under Gemini CLI or
Claude Code (through [`inspect_swe`](https://meridianlabs-ai.github.io/inspect_swe/)),
or under Hermes from the platform image this repository ships, with `skills/`
installed or left out as a control. `skills/fleet-inventory` is a
stand-in written for these tasks, not a skill kube-agents ships. Nothing here
touches `bench/`, and no CI job runs these tasks: `bench/tasks` evaluates a
deployed Platform Agent, while these tasks hold the skill fixed and vary the
harness, which the bench case format has no field for.

The harness's model calls go through Inspect's model bridge, so the harness and
the model are separate choices (Claude Code can drive Gemini), and every run
lands in the same Inspect log format whichever harness produced it.

Each sample runs in a pod that `k8s_pod.py` creates from a plain manifest,
`environments/*/sandbox.yaml` (`hermes.yaml` for Hermes), in the `kind-kube-agents` cluster
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
  scoring time. Its image adds kubectl to Ubuntu.

`run.sh` builds both images (the platform image from `deploy/docker/Dockerfile`),
loads them into the cluster when it is a kind cluster, and passes its arguments
to `inspect eval`:

```bash
export GOOGLE_API_KEY=...
M="--model google/gemini-3.7-flash --epochs 3 --retry-on-error=2"
./run.sh clusters_from_memory.py $M
./run.sh clusters_from_memory.py $M -T skills=false   # control
./run.sh clusters_from_memory.py $M -T harness=claude -T identity=claude-sonnet-5
./run.sh clusters_from_memory.py $M -T harness=hermes
./run.sh namespaces.py $M
uv run inspect view   # logs/: transcripts, events, tokens, scores
```

`harness=hermes` runs the `platform` profile one-shot (`hermes chat -Q`) in the
platform image, from `environments/*/hermes.yaml`, with `skills/` added to the
profile's shipped skills. Its commands run in the agent's own container, where
an install sends them to the shell sandbox, and the image has no kubectl, so
only `clusters_from_memory` has a `hermes.yaml`. Hermes reaches the model
through the bridge's Gemini API, the one wire on which it replays Gemini's
thought signatures.

`identity` is the Claude model Claude Code presents itself as; it exits with
`unrecognized_model` under a model name it does not recognise, so set it when the bridged
model is not an Anthropic one. Harness versions are pinned in `harness.py`.
