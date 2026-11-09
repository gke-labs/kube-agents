---
name: update-kubectl-cost-table
description: Re-profile the sandbox shell's kubectl cost table (deploy/sandbox/kubectl-costs.json), which the kubectl admission charges each large get from. Use after a kubectl version bump, when the admission logs `other` events for commands the agents run, when a tier's reference fleet changes, or when adding a sizing tier.
---

# Task

Keep `deploy/sandbox/kubectl-costs.json` true. The shell's kubectl admission
(`deploy/sandbox/kubectl-admit.py`) charges a `get` the peak memory this table lists for its
command key, in the install's tier, and refuses a read the shell cannot hold before it starts.
It estimates nothing at run time, so a stale table charges the wrong figure: too low lets the
memory guard stop the command mid-run; too high refuses a read that would fit.

The table is written only by `scripts/dev/profile_kubectl_costs.py`, never by hand.

# When

- `KUBECTL_VERSION` in `deploy/sandbox/Dockerfile` changes: re-profile every tier.
- The admission logged `other` events: a command the agents run has no entry. Read them from a
  live shell with `kubectl -n <ns> exec <name>-shell-0 -c shell -- cat
/run/kube-agents-sandbox/kubectl-admission/events.log`. Add each key's command to `COMMANDS` in
  the profiler and re-profile.
- A tier's reference fleet changes size, or a new tier is added.

# Key and tiers

- Key: `get <kinds> <cluster|namespace|object> <json|table>`.
  - Kinds are canonical plurals, sorted. - `cluster` means `-A`, or kinds that are all cluster-scoped. Only `cluster` keys are charged:
    one namespace and named objects run unchanged, so the profiler does not measure them.
  - `json` covers every output that decodes full objects: `-o json`, `yaml`, `jsonpath`,
    `custom-columns`, `go-template`, `name`. `table` is the server-built table.
  - `command_key()` in `kubectl-admit.py` is the definition.
- Tier: the install picks one with `KUBEAGENTS_KUBECTL_COST_TIER` in `spec.deployment.env`;
  unset is `default`. A key a tier does not list is charged the sum of its kinds' entries, and the
  tier's `other` only when none of its kinds is listed.
- Each tier's `reference` field names the cluster its figures were measured on. A tier must
  stand for the largest cluster an install on that tier reads.

# Procedure

1. **Reference cluster.** Use one cluster you own, of the tier's size, that no eval reads (not
   the seeded fleet: the run creates a namespace, a ServiceAccount, a ClusterRole and its binding there).
   - `default`: a small cluster of a few hundred Pods at most.
   - `fleet`: about 3,000 KWOK nodes (Node objects with no VM behind them) and 100,000 Pods across 300
     namespaces on a small GKE cluster, the shape issue #4896 plants on the pool.
2. **Run it as a Job on that cluster.** This gives native `linux/amd64` and the `kubectl` the
   sandbox pins (version and SHA256 read from `deploy/sandbox/Dockerfile`), with nothing else in
   the pod:
   ```bash
   scripts/dev/profile_kubectl_costs_job.sh <context> <tier> "<cluster: Pods, nodes, namespaces>" [memory]
   ```
   - Give the `fleet` tier `20Gi`; its largest list peaks near 15 GiB, and the nodes must have that
     much free. The `default` tier
     needs about `1Gi`.
   - The script deletes everything it created and rewrites only that tier's column of
     `deploy/sandbox/kubectl-costs.json`.
   - Profile only with the Linux `kubectl` the sandbox runs. The macOS build of the same version
     used about 35 % less memory on the large lists, so its figures charge too little.
   - The largest lists' peaks vary by about 20 % between runs, so the script keeps each entry's
     higher figure of this run and the current column (`--keep-higher`). Run it twice for a new
     tier. To lower figures (a new `kubectl` version, a new reference cluster), delete the tier's
     column first and say why in the pull request.
3. **Read the log.** The profiler prints one line per command, with peak, time and exit code.
   A command that fails (exit code not 0) gets no new figure, because the reference cluster does
   not serve that kind; check that this is the reason. `--keep-higher` keeps its figure from the
   previous column, if it had one. To add a command the admission logged as
   `other`, add it to `COMMANDS` in `scripts/dev/profile_kubectl_costs.py` and re-run.
4. **Check:**
   - Every listed key's `example` must key back to it (`test_the_shipped_table`).
   - Run `make test-python PYTHON_TEST_FILES=deploy/sandbox/test_kubectl_admit.py`.
   - Compare with the previous column. A large-list entry that moved by more than 25% needs a
     reason in the pull request: the kubectl version, the reference size, or the machine.
5. **Pull request:** the profiler's output lines, the reference cluster, the kubectl version and
   platform, and the old and new figures for every key that changed.

# Rules

- Never hand-edit `peak_mib`. A figure the profiler did not measure is not a measurement.
- Keep `other` above every small command and below the fleet-wide lists. The profiler sets it;
  change `OTHER_MIB` there with a reason.
- A new tier needs its reference cluster in its `reference` field and a `default` fallback that
  stays: the admission falls back to `default` for an unknown tier name.
- The refusal text the admission prints is read by the model. Changing it needs the eval loop
  (`.agents/rules/eval_driven_development.md`); changing only figures does not.
