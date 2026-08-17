# policy/

Cluster admission policies that enforce the security model at apply time — the runtime backstop for
attenuation (03 §4, §11).

## The agent-RBAC policies are not kept here

They used to be, as `vap-agent-readonly.yaml`, and nothing applied them. They now ship with the
harness itself:

- the Helm chart applies them, as `charts/kube-agents/templates/agent-rbac-admission-policy.yaml`
  (turn off with `admissionPolicy.enabled=false`);
- the provisioning scripts apply them, in `provision_03_gcp_gke_operator.sh`;
- the manual `make install && make deploy` path does **not** — that install has to apply the source
  file itself (INSTALL.md Method 2, Step 4).

Both are built from one source, `k8s-operator/config/admission/agent-rbac-policy.yaml`. **Read its
header before citing the policies as a control** — it states what they do and do not enforce. The
short version: they deny write and privilege-escalation verbs, Secrets, and a cluster-scoped binding
to a namespace-tier agent ServiceAccount; they cannot check the rules of a _referenced_ Role, and
the content policy only selects manifests carrying the `kube-agents/tier` label. A hand-written
manifest that omits the label is caught by the review gate, not by admission.

If your cluster is reconciled from this repository rather than installed by the chart or the
scripts, vendor the policies here by committing that source file and letting your reconciler apply
it — but keep one copy, not two.

## What does belong here

Optional Gatekeeper/Kyverno policies, and admission policies specific to your own fleet. Applied by
CI/CD on merge; human-reviewed.
