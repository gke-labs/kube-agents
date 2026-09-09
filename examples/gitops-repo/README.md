# Reference GitOps repository layout

This is the **template customers fork** as their kube-agents GitOps repo — the single source of truth
for desired state (`05` C13). It is separate from the kube-agents source tree; agents check it out
dynamically into the agent workspace and `submit-suggestion` opens PRs against it. Layout defined in
[`docs/architecture/06-api-and-data-contracts.md` §3](../../docs/architecture/06-api-and-data-contracts.md).

```
gitops-repo/
├── clusters/<cluster>/            # per-cluster desired state (applied by that target's pipeline)
│   ├── provisioning/              # cloud/cluster resources: KCC YAML or Terraform HCL
│   ├── namespaces/<ns>/           # Namespace, RBAC, NetworkPolicy, ResourceQuota, workloads
│   └── agents/                    # Agent CRs + per-agent identity (KSA/RBAC/WI) manifests
├── fleet/                         # project-level policy; platform-tier Agent CR + identity
├── knowledge/                     # OKF base (§5) — never applied to a cluster
├── policy/                        # admission policies (ValidatingAdmissionPolicy; Gatekeeper/Kyverno)
└── .github/workflows/             # the actuation pipeline config (customer's CI/CD)
```

## Contracts

- **Propose** (`submit-suggestion`): branch `<tier>-agent/<change_type>-<target>` → stage only
  targeted files (never `git add .`) → Conventional Commit → PR.
- **Apply:** on merge, the **customer's CI/CD** applies changed paths — `kubectl apply` for K8s/KCC
  YAML, `terraform apply` for HCL. kube-agents never calls cluster/cloud APIs directly.
- **Review gate:** PRs touching `**/provisioning/**`, `**/agents/**`, `**/namespaces/**`,
  `**/policy/**` and `knowledge/` require human review (see `CODEOWNERS.example` — copy to
  `CODEOWNERS` and fill in real teams when forking) + the security review gate (06 §7).
  `knowledge/` is in the gate because a note there can move an audit posture off the ledger
  (next bullet); a declaration is a reviewed change, not a comment.
- **Declared intent:** the `obtainability-audit` stream reads `clusters/<cluster>/provisioning/`
  and `knowledge/` before it reports a fixed replica count, a pinned HPA or a missing
  PodDisruptionBudget as a finding (`agents/platform/governance/obtainability_audit_sop.md` §4a).
  A choice HCL cannot express — a workload meant to run one replica — goes in the `knowledge/`
  root as an OKF document (`type` frontmatter, 06 §5) naming `<namespace>/<Kind>/<name>` and the
  check slug (`single-replica`); the ledger then lists it under _Declared intent_ with the file's
  path instead of reporting it. A Terraform
  repository that is not this one is registered under the `context_repos` key of the agent's
  `gitops-state` ConfigMap and is read the same way, never written to.
- **Version pins:** kube-agents artifacts referenced from this repo are pinned to immutable SemVer
  releases — Terraform modules via
  `git::https://github.com/gke-labs/kube-agents.git//terraform/modules/<name>?ref=1.2.0`, the Helm
  chart via `oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents --version 1.2.0`, container
  images via `1.2.0` tags — never `:latest` or a branch ref. Canonical rules:
  [release versioning & promotion](../../docs/site/src/content/docs/deploy/release-versioning.md).

`cluster-a/` and its `namespaces/team-x/` are illustrative scaffolding, not a live target.
