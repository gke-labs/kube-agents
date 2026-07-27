# clusters/cluster-a/agents/

`Agent` CRs bound to `cluster-a` (cluster-admin tier) and their **pre-created** per-agent identity
manifests — KSA / read-only Role/ClusterRole + RoleBinding / Workload-Identity binding. The
kube-agents controller **references** these (`serviceAccountName`); it never mints RBAC (08 §4).
Identity manifests carry the `kube-agents/tier` label the `ValidatingAdmissionPolicy` selects on
(see `../../../policy/`). Applied by CI/CD on merge; human-reviewed (see `CODEOWNERS.example`).
