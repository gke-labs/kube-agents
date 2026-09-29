# Harbor experiment

Evaluates skills independently of the harness: the same
[Harbor](https://harborframework.com) task runs under Claude Code, Gemini CLI,
Hermes or any other adapter Harbor ships, with `skills/` installed into each or
left out as a control. `skills/fleet-inventory` is a stand-in written for these
tasks, not a skill kube-agents ships. Nothing here touches `bench/`, which
evaluates the deployed kube-agents install, and no CI job runs these tasks.

A task is a directory: `instruction.md`, `task.toml`, `environment/` (the
container the agent works in), `tests/test.sh` (writes 0 or 1 to
`/logs/verifier/reward.txt`), `solution/solve.sh` (a reference solution).

- `tasks/kube-namespaces`: the cluster is reachable; name its namespaces.
- `tasks/kube-clusters-from-memory`: no cluster is reachable; the inventory a
  background sync would have written is in `/var/lib/kube-agents`, which
  only the `fleet-inventory` skill mentions. Without the skill an agent can
  still find it by searching the filesystem; the control measures the cost of
  that search, and how often it gives up.

```bash
uv tool install harbor
kind get kubeconfig --internal --name kube-agents > tasks/kube-namespaces/environment/kubeconfig  # hack/kind-up.sh's cluster

export GEMINI_API_KEY=...            # gemini-cli
export CLAUDE_CODE_OAUTH_TOKEN=$(claude setup-token) CLAUDE_FORCE_OAUTH=1   # claude-code on a subscription
harbor run -y -p tasks/kube-namespaces -a oracle   # the reference solution; validates the task
harbor run -y -k 3 -p tasks/kube-namespaces -a gemini-cli -m gemini/gemini-3.7-flash --skills ./skills
harbor run -y -k 3 -p tasks/kube-clusters-from-memory -a gemini-cli -m gemini/gemini-3.7-flash --skills ./skills
harbor run -y -k 3 -p tasks/kube-clusters-from-memory -a gemini-cli -m gemini/gemini-3.7-flash   # control: no skills
harbor run -y -k 3 -p tasks/kube-clusters-from-memory -a claude-code -m claude-opus-5-5 --skills ./skills
harbor view jobs   # trajectories, tokens, cost
```

Harbor bind-mounts the jobs directory (`-o`, default `./jobs`) into the task
container as `/logs`, so the Docker daemon must be able to write it. Lima mounts
`~` read-only; run Harbor inside the VM instead (`limactl shell docker`, with
`uv` and Harbor installed there) and pass `-o` a path in the VM's own home.
