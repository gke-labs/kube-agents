---
title: Declarative workflow
description: All infrastructure changes route through Git. How submit-suggestion and Minty enforce it.
sidebar:
  order: 6
---

The Platform Agent's `SOUL.md` forbids direct infrastructure mutations. When the agent has a fix in mind — a policy update, a node pool tweak, a security patch, a namespace addition — it doesn't `kubectl apply`. It writes the change into your **GitOps repo** as a **pull request** via the `submit-suggestion` skill, using a short-lived GitHub token minted on demand by **Minty**.

## Why

- **Human review.** Every infrastructure change gets seen before it hits prod. The PR is the audit trail.
- **Rollback via revert.** A bad remediation is one revert away from undone.
- **Compatibility with your existing GitOps.** ArgoCD, Flux, RootSync — whichever reconciler you already run applies the merged change. The agent doesn't compete with your reconciler.
- **Enforced least privilege.** The agent's cluster identity holds no general write. Even if the persona were misled, it lacks the RBAC to mutate infra directly.

## The `submit-suggestion` skill

Source: [`agents/platform/skills/submit-suggestion/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/submit-suggestion).

The agent invokes this skill whenever an SOP or on-request task decides "propose a change". The skill:

1. Reads the target GitOps repo URL from `/opt/data/SETTINGS.md` (the dynamic per-install setting).
2. Clones the repo (or uses a cached checkout) into a working directory.
3. Applies the change (file writes, YAML patches).
4. Creates a topic branch, commits, and pushes.
5. Opens a PR against the repo's default branch using the GitHub App identity minted by Minty.
6. Returns the PR URL to the agent, which posts it to Chat.

The skill body also defines commit-message conventions, PR-body structure, and safety red lines (e.g. no changes outside the declared scope of the invoking SOP).

## Minty (GitHub Token Minter)

Source: [`k8s-operator/config/integrations/github/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github).

Minty is a small in-cluster service that brokers GitHub App installation tokens without any long-lived secret ever touching the agent's pod.

### How it works

1. A GitHub App is created (once, by you) with the needed permissions (`contents:write`, `pull_requests:write`) and installed on the target repo.
2. The App's private key is wrapped in a **GCP KMS key** (created by `provision_10_deploy_github_minter.sh`) — the raw key material never lives outside KMS.
3. When `submit-suggestion` needs a token, it calls Minty via the agent's Workload Identity.
4. Minty asks KMS to sign a JWT with the wrapped private key.
5. Minty exchanges the JWT with GitHub for a **1-hour installation token**.
6. Minty returns the token to the caller.

### Recovery

If a git operation fails with an auth error (expired token, revoked installation), `SOUL.md §4` requires the agent to run:

```bash
./scripts/github_token_refresh.py <owner>/<repo>
```

which triggers a fresh mint from Minty and caches it. The recovery ladder (§5) retries the failed op up to 5 times before escalating.

## Complementary integrations

Alongside GitHub PR flows, the persona explicitly names other declarative pipelines it will use when they're the active workflow:

- **Config Connector** — for GCP resources modeled as Kubernetes CRs.
- **ArgoCD / Flux** — inspecting `RootSync` state and Application health as part of diagnostics.
- **GKE Hub fleet membership / Connect Gateway** — for multi-cluster targeting.

`SOUL.md §5` requires the agent to inspect these before manual intervention.

## Anti-patterns

Explicitly called out as forbidden in `SOUL.md`:

- Running raw `kubectl apply` against a live cluster for infrastructure changes.
- Configuring `git` credential helpers manually.
- Running ad-hoc `git clone` against the GitOps repo for change submission (must use `submit-suggestion`).
- Outputting raw tool schemas, JSON payloads, or exit codes in user-facing messages.

## Where to go next

- [Deploy → Token minter](/kube-agents/deploy/token-minter/) — Minty install details.
- [Concepts → Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks that invoke `submit-suggestion`.
- [Reference → Attribution](/kube-agents/reference/attribution/) — how a PR ties back to the authenticated human who requested it.
