# .github/workflows/ — the actuation pipeline (customer's CI/CD)

kube-agents is **unopinionated** about CI/CD (06 §4). This directory holds the customer's pipeline
that, **on merge**, applies changed paths to the target — `kubectl apply` for K8s/KCC YAML,
`terraform apply` for HCL — using **least-privilege deploy credentials scoped per target**. Agents
hold no write credentials; the pipeline is the sole privileged writer (03 §4).

Reference workflows (added in later phases):

- `apply.yml` — apply merged `clusters/**` / `fleet/**` artifacts to their target (Phase 1).
- `review-gate.yml` — run the `review-security-k8s-*` suite via a headless harness runner on PRs
  touching guarded paths; block merge on unmitigated high/critical findings (Phase 5, 06 §7).

GitHub Actions is the reference; CircleCI/Jenkins/Argo/Flux/Atlantis are equally valid (06 §4).
