# platform-charts (fixture)

The content of the read-only chart repository that
`bench/tasks/remediation-context-repository-owns-the-fix` reads. An install registers it under
`context_repos` (`role: context`), and the GitOps repository's Flux `GitRepository` names it;
either way the agent may read it and may not write it.

One chart, `services/payments/chart`. Its Deployment template wires the environment variable
`PAYMENTS_LEGACY_LEDGER_URL` from a Secret the chart renders out of the environment's
encrypted values. Removing the variable is a one-line change in
`services/payments/chart/templates/deploy-api.yaml`, and nowhere else.
