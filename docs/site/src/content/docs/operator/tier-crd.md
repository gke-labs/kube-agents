---
title: Tier CRD
description: Custom resource naming what a task may do at a tier. Defined but not yet enforced.
sidebar:
  order: 7
---

A `Tier` names what a task may do. An `AgentProfile` lists the tiers it allows in `allowedTiers`, and a task runs at its requester's tier.

**Nothing enforces a `Tier` yet.** The capability verifier and the gateway still use their built-in `read` and `unattended` tiers, and `allowedTiers` still accepts only those two names. Editing a `Tier` changes what `kubectl get tiers` shows and nothing else. Later changes make the tier set data, map people to tiers, and enforce `writeMode`.

A `Tier` lives in the install's namespace:

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Tier
metadata:
  name: read
  namespace: kubeagents-system
spec:
  mode: interactive # or unattended
  writeMode: propose # or none; not enforced yet
```

- `mode` is whether people or triggers run at the tier. An `interactive` tier runs interactive profile tasks. An `unattended` tier, a trigger's (the event watcher, a named cron), only starts profile tasks. What a tier may do follows from its mode, so a tier can't name an action of its own.
- `writeMode` is whether a task at the tier may propose a change it finds (`propose`, the default) or only explain it (`none`). It isn't enforced yet; until it is, only the install-level setting applies.

A tier's name is a token in the task submission subject, so it must be a DNS-1123 label.

The chart creates `read` (`interactive`) and `unattended`, and owns them: an edit to either is reverted on the next upgrade. It creates them only when the API server serves the `Tier` kind, so apply `charts/kube-agents/crds/` before a hand `helm upgrade`, as `upgrade.sh` does.

**Who may write tiers.** A tier is authority, so only cluster administrators should write one. No agent ServiceAccount gets a verb on `tiers`. The operator reads them and writes their status, and nothing in the install aggregates `tiers` into the `admin` or `edit` roles, so a namespace admin doesn't get them by default. The operator reports each tier it has read with an `Accepted` condition.
