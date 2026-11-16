---
title: Tier CRD
description: Custom resource naming what a task may do at a tier, and how people are mapped to one.
sidebar:
  order: 7
---

A `Tier` names what a task may do. An `AgentProfile` lists the tiers it allows in `allowedTiers`, and a task runs at its requester's tier.

**Profiles run at a `Tier`, and people are mapped to one in the PlatformAgent** (see [Mapping people to a tier](#mapping-people-to-a-tier)). Under `spec.mode: next`, the operator renders every `Tier` in the install's namespace into the ConfigMap `<agent>-a2a-tiers`. The capability verifier, the gateway and the auth callout read it and add each tier to their tier table, at the verb set its `mode` gives: `interactive` gets what `read` permits, and `unattended` what `unattended` permits.

The built-in `read` and `unattended` tiers, and the legacy ones, are compiled in: a `Tier` with one of their names changes nothing, and a missing or unreadable file leaves the built-in set. A profile can list a tier in `allowedTiers` (see [AgentProfile](/kube-agents/operator/agentprofile-crd/)), and it renders worker pools for an `interactive` tier and Jobs for an `unattended` one. A person not mapped to a tier runs at `read`. (The full name is `tiers.kubeagents.x-k8s.io`, since Calico has a `tiers` resource of its own.) A later change enforces `writeMode`.

**Deleting a `Tier` is not a revoke.** A profile that served it drains it: nothing new starts there, but tasks already running at it finish, and the tier stays in the tiers file at its mode until the drain ends, so they're still judged at it. There's no lever yet to end it all at once; [AgentProfile](/kube-agents/operator/agentprofile-crd/) says how to end one running task.

**A tier's `mode` can't change.** A grant already minted at the tier would gain or lose verbs with it, so the API server refuses the edit: a tier that needs the other mode is a new tier. Deleting a tier and recreating it under the same name with the other mode still re-scopes live grants, until they expire (`GrantTTL`, an hour). A `Tier` the components would skip, such as one named after a legacy tier or a built-in tier under another mode, reports `Accepted=False` with the reason.

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

- `mode` is whether people or triggers run at the tier. A profile renders worker pools for an `interactive` tier and Jobs for an `unattended` one, a trigger's (the event watcher, a named cron), and a profile serves at most one `unattended`-mode tier. What a tier may do will follow from its mode, so a tier can't name an action of its own.
- `writeMode` is whether a task at the tier may propose a change it finds (`propose`, the default) or only explain it (`none`). It has no effect yet.

A tier's name is a token in the task submission subject, so it must be a DNS-1123 label.

With `platformAgent.enabled`, the chart creates `read` (`interactive`) and `unattended`, and owns them: an edit to either is reverted on the next upgrade. It creates them only when the API server serves the `Tier` kind, so apply `charts/kube-agents/crds/` before a hand `helm upgrade`, as `upgrade.sh` does.

**Who may write tiers.** A tier is authority, so only cluster administrators should write one. No agent ServiceAccount gets a verb on `tiers`. The operator reads them and writes their status, and nothing in the install aggregates `tiers` into the `admin` or `edit` roles, so a namespace admin doesn't get them by default. The operator reports each tier it has read with an `Accepted` condition. It looks for the `Tier` kind once, at startup: if the CRD isn't installed or its role can't read tiers, it skips the Tier controller and the Tier watch, and keeps running everything else until it restarts. Its tiers file is then an empty list, so the built-in tiers serve.

## Mapping people to a tier

The PlatformAgent's `spec.tiers.membership` lists the people whose profile tasks run at a tier other than `read`:

```yaml
spec:
  tiers:
    membership:
      - backend: slack # gchat, slack, discord, a2a or a2a-google
        user: U0ABC123 # the id the backend's allowedUsers takes
        tier: oncall
```

- **Anyone not listed runs at `read`.** The console runs at `read` whatever the list says, since everyone on it shares one principal.
- **The tier must be `interactive`.** Admission refuses a member mapped to an `unattended`-mode tier or a legacy tier, and a person listed twice. Google Chat ids are compared without case, as the gateway compares them.
- **A tier no `Tier` defines yet is allowed.** That member's profile tasks are refused with a notice until the `Tier` exists. They don't fall back to `read`.
- **A thread runs at one tier.** A profile task runs at its authors' tier: the requester's, everyone who has written in the thread, and anyone who has steered the task. If they're at different tiers, the turn is refused with a notice naming the tiers. A steer from an author at another tier than the running task's is refused the same way, and isn't sent.
- **The tier must stay inside the gateway's authority.** Apart from running a profile, everything a tier permits must be in the gateway's own authority tier, or its tasks are refused.
- The profile must also list the tier in `allowedTiers`, or the task is refused as before.

Groups aren't supported yet: list each person. A change to the list rolls the gateway.

**Rolling back** to a release without membership runs everyone at `read` again.
