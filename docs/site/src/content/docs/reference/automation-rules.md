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
([credential isolation](/kube-agents/reference/credential-isolation/) lists where it reaches). With
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
  deny:
    - fix: "*"
      environments: [prod]
```

`version` is required and must be `1`. `automerge` holds two optional lists, `allow` and `deny`. A
file with only `version: 1` is valid and allows nothing.

Each rule has exactly two keys:

- `fix`: the kind of fix, named by the audit check that finds the problem. The one kind today is
  `no-pdb`, which adds a missing PodDisruptionBudget. A rule naming any other kind is kept but never
  matches, and the check command below warns about it.
- `environments`: a non-empty list of environment names, written as GKE label values (lowercase
  letters, digits, `_` and `-`, at most 63 characters).

`*` matches every fix or every environment, in deny rules only. An allow rule must name its fix
and its environments, so a new kind of fix, or a new environment, never merges unattended because
of a rule written before it existed.

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
4. The cluster has no environment label: not allowed.
5. A deny rule matches the fix and the environment: not allowed.
6. An allow rule matches: allowed.
7. Otherwise: not allowed.

## When the file is refused

Any one of these refuses the whole file, never just the rule it is in, so a typo in a deny rule
cannot leave the allow rules beside it in force:

- It is not YAML, is larger than 64 KiB, or is not UTF-8.
- `.kube-agents` or the file is a symbolic link.
- `version` is missing or is not the number `1`.
- A key is unknown or appears twice, at any level. YAML merge keys (`<<`) are not supported.
- A rule is missing `fix` or `environments`, an environment is not a label value, or an allow rule
  uses `*`.

## Check a file

In the install's shell sandbox (the `<name>-shell` Pod), where the agent's checkouts live:

```bash
python3 /opt/defaults/scripts/automation_rules.py <checkout> --fix no-pdb --environment dev
```

It prints the parsed rules, any warnings, the reason a file was refused, and the decision for the
fix and environment given, as JSON. It exits 1 when the file is refused. With the switch `off` it
reports `switch-off` without reading the file.

The parser is
[`agents/platform/scripts/automation_rules.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/scripts/automation_rules.py),
and the reference repository layout carries an
[example file](https://github.com/gke-labs/kube-agents/blob/main/examples/gitops-repo/.kube-agents/automation.yaml).

Protect the file the way the reference layout protects all of `.kube-agents/`: a CODEOWNERS entry
that requires a human review for every change to it (`examples/gitops-repo/CODEOWNERS.example`).
