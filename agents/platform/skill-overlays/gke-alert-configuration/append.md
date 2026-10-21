<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Corrections to the references

These override the reference files, and the shorthand formulas in the sections above, where
they disagree.

- **Memory-saturation query.** Filter the limit series with `> 0` before dividing wherever this
  skill gives the ratio: the Critical Rules and Technical Considerations above,
  [promql_queries.md](references/promql_queries.md) and
  [metrics_and_alerts_catalog.md](references/metrics_and_alerts_catalog.md) alike:

  ```promql
  sum(
    container_memory_working_set_bytes{
      cluster="${var.cluster_name}",
      namespace="${var.namespace}",
      container!=""
    }
  ) by (pod, container)
  /
  sum(
    container_spec_memory_limit_bytes{
      cluster="${var.cluster_name}",
      namespace="${var.namespace}",
      container!=""
    } > 0
  ) by (pod, container) > 0.90
  ```

  cAdvisor reports `container_spec_memory_limit_bytes` as `0` for a container with no limit, so
  the unfiltered ratio is `+Inf` and the alert fires for as long as that container runs. With the
  filter, unlimited containers return no series, which is what the references' warning about
  missing limits describes.
- **Enabling control-plane metrics.** Do not run the `gcloud container clusters update
  --monitoring=SYSTEM,API_SERVER,CONTROLLER_MANAGER,SCHEDULER` command in
  [gke_configuration_prerequisites.md](references/gke_configuration_prerequisites.md). It changes
  the cluster's configuration, so ask the user first, and `--monitoring` replaces the cluster's
  whole list of enabled components rather than adding to it: a list that omits `POD`, `CADVISOR`,
  `KUBELET` or `STORAGE` turns those packages off, and with them the series other alerts in this
  skill read. Read the current list first:

  ```bash
  gcloud container clusters describe CLUSTER --location LOCATION \
    --format='value(monitoringConfig.componentConfig.enableComponents)'
  ```

  It prints API names separated by `;`, and `--monitoring` takes a comma list in the flag's own
  spelling: map `SYSTEM_COMPONENTS` to `SYSTEM` and `APISERVER` to `API_SERVER` (the other names
  are spelled the same), join them with `,`, and add `API_SERVER`, `SCHEDULER` and
  `CONTROLLER_MANAGER` where absent, so nothing already enabled (`JOBSET` on a golden-path
  cluster, for one) is turned off. For a cluster that printed
  `SYSTEM_COMPONENTS;STORAGE;POD;CADVISOR;KUBELET;JOBSET`, the update to propose is:

  ```bash
  gcloud container clusters update CLUSTER --location LOCATION \
    --monitoring=SYSTEM,STORAGE,POD,CADVISOR,KUBELET,JOBSET,API_SERVER,SCHEDULER,CONTROLLER_MANAGER
  ```
- **Cluster-health queries.** `kube_node_status_condition` and
  `kube_pod_container_status_restarts_total` are kube-state-metrics series (Tier 2) wherever this
  skill uses them, including the references' "Non-KSM Alternative" sections. The Tier 1 natives
  for Node NotReady and crash-looping containers, in PromQL, carry `cluster_name` and
  `namespace_name` labels rather than `cluster` and `namespace`:

  ```promql
  kubernetes_io:node_status_condition{
    monitored_resource="k8s_node",
    cluster_name="${var.cluster_name}",
    condition="Ready",
    status!="True"
  } > 0
  ```

  `status!="True"` and not `status="False"`: a node whose kubelet stops posting status goes to
  `Unknown`, which is the usual `NotReady`.

  The references' Tier 2 allowlist template keeps no `kube_node_*` series. When a
  kube-state-metrics node alert is approved, add `kube_node_status_condition` and every other
  `kube_node_*` series the policy reads to its `keep` regex, or the relabel drops them at scrape
  and the alert never fires.

  ```promql
  increase(
    kubernetes_io:container_restart_count{
      monitored_resource="k8s_container",
      cluster_name="${var.cluster_name}",
      namespace_name="${var.namespace}"
    }[15m]
  ) > 3
  ```
- **Traffic-drop queries.** `default 0` is MetricsQL, not PromQL, and Cloud Monitoring rejects
  it when the policy is applied, although `validate_config.py` passes it. Wherever this skill or
  its references write `... default 0`, rewrite it by shape. For the per-service week-over-week
  comparison the references recommend, alert on services that had traffic a week ago and have
  none now:

  ```promql
  (
    sum(rate(http_requests_total{cluster="${var.cluster_name}", namespace="${var.namespace}"}[5m] offset 1w )) by (service) > 1
  )
  unless
  (
    sum(rate(http_requests_total{cluster="${var.cluster_name}", namespace="${var.namespace}"}[5m])) by (service) > 0
  )
  ```

  For a single total with no grouping labels, use `(sum(rate(...[5m])) or vector(0)) == 0`; for a
  whole series disappearing, `absent(...)` on the same selector. Neither joins a `by (service)`
  baseline: the `vector(0)` and `absent()` samples carry no `service` label, so an `and` against
  one never matches.
- **Duplicate targets.** Each validator mode checks duplicates differently. `--directory` infers
  a policy's signal from keywords in its resource or display name (`error`, `latency`, `memory`
  and others, first match wins) and reports a "Duplicate Target Error" when two policies share
  one. `--plan` compares the `signal_type` strings written in the plan. `--file` checks no
  duplicates at all, so a pass from it says nothing about them. A duplicate is real only when two
  policies query the same metric on the same resource and labels. Policies that read different
  series (API-server and API-client errors, P95 and P99 latency) or are burn-rate tiers of one SLO
  with their own severities stay separate: tell the user why, and do not merge them. In a plan,
  give each such policy its own `signal_type` (for example `errors_fast_burn` and
  `errors_slow_burn`), which `--plan` accepts.
- **Terraform.** The agent's shell has OpenTofu for `tofu fmt` only and no `terraform` binary. The
  prerequisites' Verification Runbook check, `tofu fmt -check -diff`, runs there; `init`,
  `validate` and `plan` are the user's or their CI's: name them as the user's step, and do not
  report them as run.
- **Catalog regexes.** The `metrics_and_alerts_catalog.md` table rows escape each `|` with a
  backslash for Markdown, and in PromQL's RE2 that backslash makes the pipe a literal, so a
  copied matcher never matches. Drop the backslashes: the regexes are `(4|5)..` (API client
  errors), `Pending|Unknown|Failed` (Pod not healthy), `Failed|Pending` (PersistentVolume error)
  and `(?:CONNECT|WATCHLIST|WATCH|PROXY)` (API server latency).
- **Offset spacing.** The validator reads an `offset` duration up to the next whitespace, so it
  rejects `offset 1w))`. Leave a space or line break after the duration (`offset 1w ))`), as the
  traffic-drop query above does.
- **Validator path.** Wherever a reference runs the validator as `scripts/validate_config.py`, run
  `python3 "$HERMES_HOME"/skills/gke-alert-configuration/scripts/validate_config.py` instead.
  Where it passes `--file <file>` (the prerequisites' Verification Runbook does), pass
  `--directory` with the absolute path of the directory that holds the file, so the scan prints
  the policy count the Post-Edit step reads.
