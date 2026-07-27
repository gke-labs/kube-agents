# Reference GitOps repository layout

This is the **template customers fork** as their kube-agents GitOps repo — the single source of truth
for desired state (`05` C13). It is separate from the kube-agents source tree; agents check it out
via `integration.github.gitRepo` and `submit-suggestion` opens PRs against it. Layout defined in
[`docs/design/06-api-and-data-contracts.md` §3](../../docs/design/06-api-and-data-contracts.md).

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
  `**/policy/**` require human review (see `CODEOWNERS.example` — copy to `CODEOWNERS` and fill in real
  teams when forking) + the security review gate (06 §7).

`cluster-a/` and its `namespaces/team-x/` are illustrative scaffolding, not a live target.
