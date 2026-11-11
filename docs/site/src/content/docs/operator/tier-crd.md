---
title: Tier CRD
description: Custom resource naming what a task may do at a tier. Defined but not yet enforced.
sidebar:
  order: 7
---

A `Tier` names what a task may do. An `AgentProfile` lists the tiers it allows in `allowedTiers`, and a task runs at its requester's tier.

**A `Tier` is loaded but not yet usable.** Under `spec.mode: next`, the operator renders every `Tier` in the install's namespace into the ConfigMap `<agent>-a2a-tiers`. The capability verifier, the gateway and the auth callout read it and add each tier to their tier table, at the verb set its `mode` gives: `interactive` gets what `read` permits, and `unattended` what `unattended` permits.

The built-in `read` and `unattended` tiers, and the legacy ones, are compiled in: a `Tier` with one of their names changes nothing, and a missing or unreadable file leaves the built-in set. `allowedTiers` still accepts only `read` and `unattended`, and every person still runs at `read`, so a new tier changes nothing an install does yet. (The full name is `tiers.kubeagents.x-k8s.io`, since Calico has a `tiers` resource of its own.) Later changes let profiles name new tiers, map people to tiers, and enforce `writeMode`.

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

- `mode` is whether people or triggers run at the tier. Once tiers are enforced, an `interactive` tier will run interactive profile tasks, and an `unattended` tier, a trigger's (the event watcher, a named cron), will only start profile tasks. What a tier may do will follow from its mode, so a tier can't name an action of its own.
- `writeMode` is whether a task at the tier may propose a change it finds (`propose`, the default) or only explain it (`none`). It has no effect yet.

A tier's name is a token in the task submission subject, so it must be a DNS-1123 label.

With `platformAgent.enabled`, the chart creates `read` (`interactive`) and `unattended`, and owns them: an edit to either is reverted on the next upgrade. It creates them only when the API server serves the `Tier` kind, so apply `charts/kube-agents/crds/` before a hand `helm upgrade`, as `upgrade.sh` does.

**Who may write tiers.** A tier is authority, so only cluster administrators should write one. No agent ServiceAccount gets a verb on `tiers`. The operator reads them and writes their status, and nothing in the install aggregates `tiers` into the `admin` or `edit` roles, so a namespace admin doesn't get them by default. The operator reports each tier it has read with an `Accepted` condition. It looks for the `Tier` kind once, at startup: if the CRD isn't installed or its role can't read tiers, it skips the Tier controller and keeps running everything else, until it restarts.
