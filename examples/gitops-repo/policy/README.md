# policy/

Cluster admission policies that enforce the security model at apply time — the runtime backstop for
attenuation (03 §4, §11). The load-bearing one, landed in Phase 0:

- **`vap-agent-readonly.yaml`** — a `ValidatingAdmissionPolicy` that **hard-denies** any
  `Role`/`ClusterRole` selected by the `kube-agents/tier` label whose own `rules` grant an agent
  ServiceAccount a **write verb** or a **wrong-scope** grant (e.g. cluster-scoped for a namespace
  tier). CEL is scoped to the role's own `rules`. This rejects a bad-RBAC PR **at apply time even if
  it was merged** — the negative test in 03 §11.

Optional Gatekeeper/Kyverno policies may also live here. Applied by CI/CD on merge; human-reviewed.
