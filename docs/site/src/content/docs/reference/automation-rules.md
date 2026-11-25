---
title: Automation rules file
description: .kube-agents/automation.yaml, the file in your GitOps repository that says which kube-agents fixes may merge without a human.
sidebar:
  order: 12
---

`.kube-agents/automation.yaml` in your GitOps repository lists the kinds of kube-agents fix your
pipeline may merge without a human review, and in which environments. Nothing is allowed unless an
allow rule names it, a deny rule beats every allow rule, and a file kube-agents cannot read in full
allows nothing.

kube-agents reads the file only when the install's `KUBEAGENTS_AUTOMERGE` switch is `practice` or
`on`. The switch is an entry in the PlatformAgent's `spec.deployment.env`, and it defaults to `off`
([credential isolation](/kube-agents/reference/credential-isolation/) lists where it reaches). On a
chart, `install.sh` or Terraform install, set it through the chart value
`platformAgent.deployment.env`. On an `install.sh` install, that is a line in `install.env`:

```bash
TF_VAR_extra_helm_values='{platformAgent={deployment={env=[{name="KUBEAGENTS_AUTOMERGE",value="practice"}]}}}'
```

There is one such line for every extra chart value, so merge this entry into one already there
([Operator settings](/kube-agents/install/quickstart-gke/#operator-settings) covers the line and a
hand-driven Terraform or Helm install). A hand edit of the CR can be lost at the next chart apply. With
it off the file is never opened. Adding the file changes nothing until the switch is turned on.
kube-agents never merges a pull request itself: the file is input to your own pipeline's merge
decision.

## Format

```yaml
version: 1
automerge:
  allow:
    - fix: no-pdb
      environments: [dev]
    - max_risk: low
      environments: [staging]
    - max_risk: medium
      environments: [dev]
  deny:
    - fix: "*"
      environments: [prod]
```

`version` is required and must be `1`. `automerge` holds two optional lists, `allow` and `deny`. A
file with only `version: 1` is valid and allows nothing.

Each rule has `environments` plus one target selector:

- `fix`: the kind of fix, named by the audit check that finds the problem. The one kind today is
  `no-pdb`, which adds a missing PodDisruptionBudget. A rule naming any other kind refuses the
  whole file.
- `max_risk` (in `allow` rules only): the highest deterministic pull request risk level (`low` or
  `medium`) permitted to carry an auto-merge label when the change also has
  `disruptiveness: non_disruptive`, `revertability: easy`, `supported: true`, and
  `effective_changes: true`. `high`, `critical`, `unknown`, and `*` are refused in `allow` rules.
- `risk` (in `deny` rules only): denies risk-based auto-merge at or above that risk level (`low`,
  `medium`, `high`, `critical`, or `*`). A `deny` rule with `fix: "*"` denies both recipe fixes and
  risk-based rules in those environments.
- `environments`: a non-empty list of environment names, written as GKE label values (lowercase
  letters, digits, `_` and `-`, at most 63 characters).

`*` matches every fix, risk level, or environment, in deny rules only. An allow rule must name its
fix or `max_risk` level and its environments, so a new kind of fix, or a new environment, never
merges unattended because of a rule written before it existed.

Risk-based auto-merge stays off by default even when `KUBEAGENTS_AUTOMERGE` is `on` for `no-pdb`.
An install admin must set `KUBEAGENTS_AUTOMERGE_MAX_RISK` to `low` or `medium` to opt in (`off` or unset disables it).
The credential broker computes and applies a deterministic `risk:low`, `risk:medium`, `risk:high`,
`risk:critical`, or `risk:unknown` label to open kube-agents pull requests; callers cannot create,
add, or remove any `risk:*` label.

## The environment of a fix

A fix's environment is the target cluster's `environment` resource label. When that label is not
set, kube-agents reads `env`, then `stage`, then `tier`, the same keys the fleet-drift audit groups
clusters by. The value is compared exactly as written: fleet drift reads `production` as `prod`,
this file does not, so a rule names the label value your clusters carry. A cluster with none of
these labels has no environment, and no rule matches it.

## How a fix is decided

1. The switch is `off`: not allowed. The file is not read.
2. The file is absent: not allowed.
3. The file is refused: not allowed. See the next section.
4. The cluster has no environment label (or, for risk-based evaluation, changed files do not all sit
   under a single cluster's `clusters/<cluster>/` directory): not allowed.
5. For risk-based rules (`max_risk`), the credential broker classifies the pull request from git
   across four orthogonal axes before consulting allow/deny rules:
   - `risk_level`: `low | medium | high | critical | unknown` (only `low` or `medium` can qualify,
     capped by `KUBEAGENTS_AUTOMERGE_MAX_RISK`, which must be `low` or `medium`).
   - `blast_radius`: `low | medium | high | critical | unknown`.
   - `revertability`: `easy | moderate | difficult | irreversible | unknown` (auto-merge requires
     `easy`).
   - `disruptiveness`: `non_disruptive | disruptive | destructive | unknown` (auto-merge requires
     `non_disruptive`).
     Auto-merge also requires `supported: true`, `effective_changes: true`, and `stale_base: false`
     (if the base branch modified any of the same files since the pull request branched, a rebase is
     required). Known fix-recipe pull requests (`no-pdb`) are always evaluated under their recipe rule
     and live cluster safety checks, never via `max_risk`.
6. A deny rule matches the fix or risk level and the environment: not allowed.
7. An allow rule matches the fix or `max_risk` threshold and the environment: allowed.
8. Otherwise: not allowed.

## When the file is refused

Any one of these refuses the whole file, never just the rule it is in, so a misspelt key or fix
kind in a deny rule cannot leave the allow rules beside it in force. A misspelt environment name
cannot be caught this way: it is a valid label value that matches no cluster.

- It is not YAML, is larger than 64 KiB, or is not UTF-8.
- `.kube-agents` or the file is a symbolic link.
- `version` is missing or is not the number `1`.
- A key is unknown or appears twice, at any level. YAML merge keys (`<<`) are not supported.
- A rule is missing `environments`, omits a selector (`fix` or `max_risk` in `allow`; `fix` or `risk`
  in `deny`), or sets more than one selector on the same rule.
- A rule names an unknown fix kind or invalid risk level, has an environment that is not a label
  value, or uses `*` inside an `allow` rule.

## The auto-merge labels

With the switch `practice` or `on`, each time the fleet audit opens or refreshes a pull request for a
`no-pdb` fix, kube-agents' credential broker judges it. When all of these hold it adds a label,
`kube-agents:would-auto-merge` under `practice` and `kube-agents:auto-merge` under `on`:

1. The pull request is open and was opened by kube-agents.
2. It adds one file and changes nothing else, and that file is, byte for byte, the
   PodDisruptionBudget the audit generates: `maxUnavailable: 1` over the workload's own selector. The
   file sits in its cluster's own directory, `clusters/<cluster>/`, as in the reference layout.
3. The pull request targets the repository's base branch, and this file, read from that branch,
   allows `no-pdb` in the cluster's environment.
4. The cluster, read at that moment, passes three safety checks. The workload has at least two
   replicas and all of them are Ready. No other PodDisruptionBudget selects its pods or has the new
   one's name. The new budget's selector is the workload's live selector and selects no other
   workload's pods.

For general GitOps pull requests opened by kube-agents (`proposal-create`) or updated via `publish`,
the broker also runs deterministic git risk classification (`proposal-risk-label`):

1. The pull request is open, originates from the managed repository (not a fork), was opened by
   kube-agents, and targets the repository's base branch at a verified head commit SHA.
2. The broker applies the computed `risk:low`, `risk:medium`, `risk:high`, `risk:critical`, or
   `risk:unknown` label, removes any stale `risk:*` label, and posts a `kube-agents/risk` commit status
   on the head SHA citing the base SHA and reason codes.
3. The broker checks `KUBEAGENTS_AUTOMERGE_MAX_RISK` (`low` or `medium`) and `.kube-agents/automation.yaml`
   on the base branch for a matching `max_risk` rule in the verified cluster environment. It also requires
   `disruptiveness: non_disruptive`, `revertability: easy`, `supported: true`, `effective_changes: true`,
   and `stale_base: false`. When all hold, it records a green `kube-agents/auto-merge` commit status on
   the head SHA (`on`) and applies `kube-agents:would-auto-merge` (`practice`) or
   `kube-agents:auto-merge` (`on`, when the autonomy dial allows auto-merge).

A pull request that carries a label and no longer qualifies loses it when re-evaluated, which
includes after the switch is turned back to `off` or if a pull request is retargeted. A change of
switch value swaps one label for the other. While the autonomy dial is below Auto-merge,
`kube-agents:auto-merge` is held back and a carried one comes off. The broker reads all of this
itself, verifies the head commit SHA before and after applying labels, and refuses any caller request
to add, remove, or create `kube-agents:*` auto-merge or `risk:*` labels.

Before pushing a new commit in `publish`, the broker strips carried `risk:*` and auto-merge labels,
then re-evaluates the pushed head SHA. Your auto-merge pipeline should require the green
`kube-agents/auto-merge` commit status on the current head SHA (plus up-to-date base branches or a merge
queue), rather than merging on the `kube-agents:auto-merge` label alone.

The labels are applied only on GitHub and Gitea. Bitbucket has no labels, and on GitLab a `/label` line in a comment or a description sets a
label, so no label there is kube-agents' alone. Under `practice` the label is the whole effect:
compare the pull requests that carry it with the ones your team merged. Under `on` your pipeline may
merge a pull request that carries `kube-agents:auto-merge`. kube-agents merges nothing in either
mode.

## When an auto-merged fix is still broken

A fix that carries the `kube-agents:auto-merge` label merged without a human review. When it has
merged and kube-agents' after-merge check finds the problem still there, it opens pull requests
against the branch the fix merged into, and comments once on the fix to link them:

- `platform-agent/revert-<number>` undoes the fix's changes, whether it merged as a merge commit, a
  squash or a rebase. The label decides this, so turning the switch off after a bad auto-merge still
  gets the revert.
- With the switch `on`, `platform-agent/automerge-step-down-<number>` adds a deny rule for that kind
  of fix in that environment to this file. It edits the file in place and keeps everything else you
  wrote; in a block-style file a comment above the rule names the fix. Delete the rule later to
  allow those fixes again. Under `off` or `practice` nothing else merges unattended, so no rule is
  proposed and the comment says so.

Both go through your normal review; kube-agents merges neither. Until the rules change merges, fixes
of that kind can still merge on their own. A fix a person merged gets only the after-merge report.

When one of the two cannot be made, the comment says why and the other still opens. The revert is
skipped when the fix's changes no longer undo cleanly, for example because someone already reverted
it. The rules change is skipped when the file is absent or refused on that branch, already denies
that kind there, or is laid out in a way kube-agents cannot add a rule to. A failure that persists
is tried again on later runs, five attempts in all; then kube-agents comments once on the fix with what did and did not
open, and stops.

## Check a file or classify a pull request

In the install's shell sandbox (the `<name>-shell` Pod), where the agent's checkouts live:

```bash
# Check a recipe fix rule:
python3 /opt/defaults/scripts/automation_rules.py <checkout> --fix no-pdb --environment dev

# Check a risk-based rule across risk_level, disruptiveness, and revertability:
KUBEAGENTS_AUTOMERGE_MAX_RISK=low python3 /opt/defaults/scripts/automation_rules.py <checkout> \
  --risk low --disruptiveness non_disruptive --revertability easy --environment staging

# Deterministically classify an open or merged pull request via the credential broker (read-only):
python3 /opt/defaults/scripts/vcs_client.py proposal-classify-risk <number> --repo <owner/name> --format summary
```

`automation_rules.py` prints the parsed rules, the reason a file was refused, and the decision for
the fix or risk evaluation and environment given, as JSON. It exits 1 when the file is refused. With
the switch `off` it reports `switch-off` without reading the file. `vcs_client.py proposal-classify-risk`
returns the full 4-axis classification (`risk_level`, `blast_radius`, `revertability`,
`disruptiveness`), `risk_label`, driving `reasons`, and `automerge` eligibility as JSON (default) or
as a concise multi-line summary (`--format summary`).

The parser is
[`agents/platform/scripts/automation_rules.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/scripts/automation_rules.py),
and the reference repository layout carries an
[example file](https://github.com/gke-labs/kube-agents/blob/main/examples/gitops-repo/.kube-agents/automation.yaml).

Protect the file the way the reference layout protects all of `.kube-agents/`: a CODEOWNERS entry
that requires a human review for every change to it (`examples/gitops-repo/CODEOWNERS.example`).
