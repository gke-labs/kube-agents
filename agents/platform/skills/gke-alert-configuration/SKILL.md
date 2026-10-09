---
name: gke-alert-configuration
metadata:
  version: "1.0.0"
  category: CloudInfrastructure
  canonical_source: https://github.com/google/skills/tree/main/skills/cloud/gke-alert-configuration
description: >-
  Configures alerting policies in Terraform for Google Kubernetes Engine (GKE)
  clusters, workloads, and services using PromQL and Google Cloud Managed Service
  for Prometheus. Use when writing, analyzing, validating, or deploying Terraform
  alerting policies to monitor GKE service latency, traffic, error rates using
  Multi-Window Multi-Burn-Rate SLO alerts, memory saturation, and cluster health
  such as CrashLoopBackOff and Node NotReady conditions.
  Don't use for non-GKE compute runtimes such as standalone Compute Engine VMs or
  standalone Cloud Run services without GKE.
---

# GKE Alert Configuration

This skill provides guidelines and best practices for creating robust,
high-signal alerting policies for Google Kubernetes Engine workloads using
Google Cloud Managed Service for Prometheus and Terraform. It ensures
comprehensive coverage of the **4 Golden Signals** and key cluster health
metrics while minimizing alert noise.

--------------------------------------------------------------------------------

## Critical Rules

*   **Negative Triggers and Scope Redirection for Non-GKE Standalone Runtimes**:
    *   This skill is strictly scoped to Google Kubernetes Engine (GKE)
        workloads, clusters, and services using PromQL and Google Cloud Managed
        Service for Prometheus.
    *   **Do not use for non-GKE compute runtimes**, such as standalone Compute
        Engine virtual machines or standalone Cloud Run services without GKE.
    *   **STOP AND RESPOND DIRECTLY (Do Not Edit Files)**: When the user
        requests alert configuration for non-GKE compute infrastructure:
        1.  **Do not write, create, edit, or validate any Terraform files on
            disk**.
        2.  **Immediately stop and respond directly to the user in chat**:
            *   **Explicitly Clarify Out-of-Scope**: State clearly that
                standalone Compute Engine virtual machine monitoring or
                standalone Cloud Run monitoring is out of scope for this
                GKE-specific PromQL alerting skill, which is designed
                specifically for GKE workloads using Google Cloud Managed
                Service for Prometheus and PromQL.
            *   **Do Not Generate GKE PromQL Alerts**: Do not create or generate
                Kubernetes PromQL alert policies or fabricate Kubernetes
                container, pod, or node resources for non-GKE infrastructure.
            *   **Redirect the User**: Guide and redirect the user to standard
                Google Cloud Monitoring metrics, such as
                `compute.googleapis.com/instance/cpu/utilization` or
                `run.googleapis.com/request_latencies`, using standard
                `google_monitoring_alert_policy` with `condition_threshold` or
                MQL, or recommend the relevant specialized Cloud observability
                skill.
*   **Mandatory `kube-state-metrics` (KSM) Cost Guardrail**:
    *   Deploying open-source `kube-state-metrics` in Google Cloud Managed
        Service for Prometheus incurs billable metric ingestion costs.
    *   **STOP AND ASK PERMISSION FIRST (Do Not Edit Files)**: When a requested
        alert rule relies on **Tier 2 KSM metrics** (such as `kube_cronjob_*`,
        `kube_pod_status_phase`, `kube_persistentvolume_*`, `kube_deployment_*`,
        `kube_statefulset_*`, `kube_job_*`, or `kube_daemonset_*`), **do not
        write, create, edit, or validate any Terraform files or generate alert
        policies before obtaining user approval**.
    *   Instead, you **must immediately stop and respond directly to the user**
        to:
        1.  **Alert the user** that the requested alert requires
            `kube-state-metrics`.
        2.  **Explain the cost impact**: Detail that `kube-state-metrics` incurs
            billable sample ingestion costs in Google Cloud Managed Service for
            Prometheus.
        3.  **Ask for explicit permission**: Ask the user for explicit
            permission before assuming, enabling, or generating KSM-dependent
            alert configurations.
        4.  **Recommend filtering or allowlisting**: Suggest and recommend
            filtering or allowlisting only the specific required metrics, such
            as using a `PodMonitoring` resource with `metricRelabeling`
            (`action: keep`) or KSM `--metric-allowlist` to minimize ingestion
            costs. Provide a concrete allowlist example.
    *   **Always prefer Non-KSM Native Alternatives** (Tier 1 cAdvisor or native
        GKE metrics documented in
        [metrics_and_alerts_catalog.md](references/metrics_and_alerts_catalog.md))
        whenever possible, such as using `container_memory_working_set_bytes`
        and `container_spec_memory_limit_bytes` instead of
        `kube_pod_container_resource_limits`.
    *   **Explicit Tier and Cost Surcharge Identification in Response**: In
        every response where you generate or recommend an alerting policy, you
        **must explicitly state its classification tier and cost impact**:
        *   **Tier 1 native or standard metric** (GKE built-in metrics, cAdvisor
            `container_*`, kubelet volume stats, native
            `kubernetes.io/node/status_condition`, and control-plane metrics.
            Every `kube_*` series the references name
            (`kube_node_status_condition`, `kube_node_spec_taint` and
            `kube_pod_container_status_restarts_total` among them) is a
            kube-state-metrics series and therefore Tier 2, whatever the
            KSM-required or tier columns in the references say; see
            [metrics_and_alerts_catalog.md](references/metrics_and_alerts_catalog.md)):
            State that it is a **Tier 1 native or standard metric with zero KSM
            cost surcharge**.
        *   **Tier 2 KSM metric**: State that it is a **Tier 2 KSM-dependent
            metric** and follow the permission and allowlisting guardrail above.
            *(Tip: Generally, metrics with the `kube_` prefix that represent
            resource state or metadata belong to Tier 2).*
*   **Plan-Validate-Execute Loop for Approved File Edits**: When modifying,
    adding, or merging approved Terraform files on disk in a workspace, follow
    the three-phase workflow:
    1.  **Plan**: Draft a structured change plan (`changes.json`) containing
        proposed policy resource names, PromQL expressions, grouping labels, and
        durations.
    2.  **Validate**: Run the pre-edit validation script (`python3
        "$HERMES_HOME"/skills/gke-alert-configuration/scripts/validate_config.py
        --plan changes.json`) to check bracket balance, time-window and offset
        formats, duration rules, and duplicate signals. The duplicate check
        compares only entries that carry a `signal_type` (for example
        `saturation_memory`), so give each planned policy one. It does not
        parse PromQL, so a pass does not prove the query is valid.
    3.  **Execute**: After the plan passes validation, apply or merge changes
        in-place into the target Terraform configuration (`alerts.tf`).
    4.  *Note*: When answering questions or providing Terraform snippets
        directly in chat where no disk modification is requested, output the
        complete, valid Terraform HCL block in your response.
*   **Configure the 4 Golden Signals and Cluster Health**: Always ensure the
    target Kubernetes workload or service has the following alerting coverage:
    1.  **Latency** (P95 response time)
    2.  **Errors** (Multi-Window Multi-Burn-Rate SLO alerts, such as Fast Burn 1
        hour / 5 minutes with factor 14.4, Slow Burn 6 hours / 30 minutes with
        factor 6.0; do not use simple static ratios)
    3.  **Traffic** (Sudden drop or complete metric disappearance using
        `absent()` or `default 0` syntax, or overload spikes)
    4.  **Saturation (Memory Limit Utilization Only)**: When describing or
        configuring alert policies for a cluster or project, include ONLY
        **Memory Saturation** (`container_memory_working_set_bytes` /
        `container_spec_memory_limit_bytes`). Do **NOT** include CPU saturation
        alerts or list `container_cpu_usage_seconds_total` as an alert metric
        because CPU is compressible and throttled by CFS quotas rather than
        causing uncompressible fatal termination (OOM).
    5.  **Cluster Health** (Pod CrashLooping, Node NotReady)
*   **PromQL Only (Managed Prometheus)**: You must use
    `condition_prometheus_query_language` with PromQL. Do **NOT** use MQL or
    standard `condition_threshold` unless explicitly requested. Google Cloud
    Managed Service for Prometheus is the standard telemetry ingestion path for
    GKE.
*   **Terraform Only**: Write the generated observability configuration ONLY as
    Terraform (`.tf`) files, such as `alerts.tf` and `variables.tf`.
*   **Dynamic Multi-Resource Alerting (No Hardcoding)**: You must not hardcode
    specific pod names, node names, or service names in alerting conditions
    unless explicitly requested. Alerting policies must be written to cover
    resources dynamically:
    *   Always use grouping aggregations (`by (cluster, namespace, service, pod,
        container)`) instead of filtering to a single instance. This allows a
        single alert policy to dynamically track each service or pod separately.
    *   Always declare and use Terraform variables for `project_id`,
        `cluster_name`, and `namespace` (`var.project_id`, `var.cluster_name`,
        `var.namespace`) to make the configuration reusable across environments.
        Always define these variables in `variables.tf` (or within the
        configuration) and reference all three in policies or PromQL label
        matchers.
*   **No Redundant Duration Windows on Lookbacks**:
    *   When PromQL expressions already use an aggregated lookback window (such
        as `increase(...[15m]) > 3` or multi-window SLO burn rates), the query
        time window already smooths out transient spikes.
    *   Adding a Terraform duration on top of a PromQL lookback window increases
        the Mean Time to Detect (MTTD) without providing additional smoothing
        benefits.
    *   In these cases, set Terraform `duration = "0s"` (or `"60s"`). Do not
        enforce `duration = "300s"` on top of `[15m]`, which delays critical
        crashloop alerts by up to 20 minutes total (15 minutes + 5 minutes).
    *   Use `duration = "300s"` only on instantaneous gauge conditions, such as
        `kube_node_status_condition == 0`.
*   **Use SLO Burn Rates Instead of Simple Ratios**: For error rate alerting,
    always generate Multi-Window Multi-Burn-Rate (MWMBR) SLO alerts (such as
    14.4x burn rate over 1 hour and 5 minute windows for a 99% SLO) rather than
    simple error rate ratios (`rate(5xx)/rate(total) > 0.05`), which produce
    excessive false alarms on low traffic.
*   **Robust Traffic Drop Detection (`absent()` / `default 0`)**: When
    monitoring for traffic drops to zero, do not use `rate(...) == 0` alone
    because Prometheus time series disappear completely when no requests occur
    (evaluating to an empty vector rather than 0). Use `default 0` syntax, such
    as `sum(rate(...[5m])) default 0 == 0`, or `absent(...) == 1`.
*   **Notification Channels**: By default, never configure any notification
    channels without user input. If the user explicitly provides a notification
    channel, configure the alerts to use it. Otherwise, you must prompt the user
    in your response to ask if they would like to configure one.
*   **Consult GKE Metrics and Open-Source Alerts Catalog**: When designing or
    generating evaluation suites or alerting policies, consult
    [metrics_and_alerts_catalog.md](references/metrics_and_alerts_catalog.md)
    for public GKE metrics (`kubernetes.io/`) and open-source Kubernetes alerts
    (`awesome-prometheus-alerts`).
*   **Plain English Response**: You must include a plain English explanation for
    what the alerts do in your response. Explain what the alert measures, what
    the threshold represents, and what a trigger indicates.
*   **User Labels**: Include a `user_labels` block in all
    `google_monitoring_alert_policy` resources to track policies created by this
    skill:

    ```terraform
    user_labels = {
      created-with-google-skill = "gke-alert-configuration"
    }
    ```

--------------------------------------------------------------------------------

## Alerting Policy Structure in Terraform

Alerting policies must be defined using the `google_monitoring_alert_policy`
resource with `condition_prometheus_query_language`. Always declare variables in
`variables.tf` for `project_id`, `cluster_name`, and `namespace`.

```hcl
# variables.tf
variable "project_id" {
  type        = string
  description = "Google Cloud Project ID"
}

variable "cluster_name" {
  type        = string
  description = "GKE Cluster Name"
}

variable "namespace" {
  type        = string
  description = "Target Kubernetes Namespace"
  default     = "default"
}

variable "slo_target" {
  type        = number
  description = "SLO Target fraction (for example 0.99 for 99%)"
  default     = 0.99
}
```

```hcl
# alerts.tf
# Example: Multi-Window Multi-Burn-Rate (MWMBR) SLO Alert (Fast Burn: 14.4x, 1h & 5m windows)
resource "google_monitoring_alert_policy" "k8s_service_error_rate_slo" {
  project      = var.project_id
  display_name = "[K8s] ${var.cluster_name} - Service Error Rate SLO Fast Burn"
  combiner     = "OR"

  conditions {
    display_name = "Error Budget Fast Burn (14.4x over 1h and 5m)"
    condition_prometheus_query_language {
      query    = <<-EOT
        (
          (
            sum(
              rate(
                http_requests_total{
                  cluster="${var.cluster_name}",
                  namespace="${var.namespace}",
                  status=~"5.."
                }[5m]
              )
            ) by (service, namespace, cluster)
            /
            sum(
              rate(
                http_requests_total{
                  cluster="${var.cluster_name}",
                  namespace="${var.namespace}"
                }[5m]
              )
            ) by (service, namespace, cluster)
          ) > (1 - ${var.slo_target}) * 14.4
        )
        and
        (
          (
            sum(
              rate(
                http_requests_total{
                  cluster="${var.cluster_name}",
                  namespace="${var.namespace}",
                  status=~"5.."
                }[1h]
              )
            ) by (service, namespace, cluster)
            /
            sum(
              rate(
                http_requests_total{
                  cluster="${var.cluster_name}",
                  namespace="${var.namespace}"
                }[1h]
              )
            ) by (service, namespace, cluster)
          ) > (1 - ${var.slo_target}) * 14.4
        )
      EOT
      duration = "0s"
    }
  }
}
```

--------------------------------------------------------------------------------

## Telemetry Metrics and PromQL Examples

For GKE metrics (`kubernetes.io/`), community open-source alerts
(`awesome-prometheus-alerts`), KSM cost guardrails, and non-KSM native
alternatives, you must read and follow:

*   [metrics_and_alerts_catalog.md](references/metrics_and_alerts_catalog.md)

For specific PromQL queries corresponding to each of the Golden Signals, you
must read and follow:

*   [promql_queries.md](references/promql_queries.md)

For GKE cluster prerequisites, enabling Google Cloud Managed Service for
Prometheus collection, configuring PodMonitoring custom scraping, and enabling
control plane metrics collection (API Server, Controller Manager, Scheduler),
you must read and follow:

*   [gke_configuration_prerequisites.md](references/gke_configuration_prerequisites.md)

--------------------------------------------------------------------------------

## Tooling Scripts and Validation Loop

Use the `validate_config.py` script to validate change plans and Terraform
configurations when working in a repository:

*   **Pre-Edit Plan Validation**: Draft a `changes.json` plan specifying the
    proposed policies, queries, and durations, and validate it before editing:
    *   Command: `python3
        "$HERMES_HOME"/skills/gke-alert-configuration/scripts/validate_config.py
        --plan changes.json`
*   **Post-Edit and Directory Validation**: Scan existing or modified Terraform
    files in a directory to ensure no duplicates or syntax errors exist:
    *   Command: `python3
        "$HERMES_HOME"/skills/gke-alert-configuration/scripts/validate_config.py
        --directory [TARGET_TF_DIR]`
    *   For a single file, run `--directory` on the absolute path of the
        directory that holds it. `--file` prints no policy count, so its
        pass cannot be read against the rules below.
    *   The script does not check label scoping or hardcoded cluster names:
        confirm each query's scope yourself. cAdvisor, kube-state-metrics
        and application series take `cluster="${var.cluster_name}"` and
        `namespace="${var.namespace}"`; `kubernetes_io:` series take
        `cluster_name` and `namespace_name`; node-level series take no
        namespace matcher.
    *   `"policies_scanned_count"` counts the alert-policy blocks the scan
        extracted, and `0` never means the files are clean. No `.tf` file
        sits directly in that path (pass the absolute path of the directory
        that holds them; the scan does not recurse), the files hold no
        alert policy, or the scanner could not parse a policy block. If the
        right path still reports `0` for a file that holds a policy, check
        that policy by eye. A non-zero count is not a clean bill either. The
        scanner reads comments as code, so a `{`, `}` or `"` in a comment can
        drop a policy from the count or cut it short so it is counted but
        not linted. It lints a `query` written as an inline `EOT` heredoc,
        and a quoted-string `query` only in a resource with no heredoc one;
        a `query` from a variable, `local`, `file()`, `templatefile()` or
        another heredoc delimiter is counted but not linted. Check by eye
        every policy with a comment inside it and every `query` the scan
        does not lint.

--------------------------------------------------------------------------------

## Technical Considerations and Gotchas

*   **Lookback Windows versus Duration Buffers**:
    *   Do not add large `duration = "300s"` buffers to alerts that already use
        aggregated lookback windows like `increase(...[15m])` or multi-window
        SLO rates.
    *   The `[15m]` window in `increase(...[15m]) > 3` already smooths spikes.
        Adding `duration = "300s"` increases MTTD by forcing the restart count
        to remain above 3 for an extra 5 continuous minutes, delaying alerts by
        up to 20 minutes total.
    *   Use `duration = "0s"` or `"60s"` when using lookback window functions.
        Reserve `duration = "300s"` for raw instantaneous gauge conditions, such
        as `kube_node_status_condition == 0`.
*   **Memory Saturation Only for Cluster Alerting**:
    *   Do not configure CPU saturation alerts for cluster or workload
        monitoring. CPU is compressible (throttled by the CFS scheduler), while
        memory is uncompressible (triggers OOMKills).
    *   Configure Memory Saturation using `container_memory_working_set_bytes` /
        `container_spec_memory_limit_bytes`.
*   **Missing Resource Limits Blind Spot (Mandatory Explanation)**: Saturation
    alerts that compare usage to limits (such as
    `container_spec_memory_limit_bytes`) will **fail to resolve** or return
    `NaN` if workloads do not have explicit Memory limits configured in their
    Kubernetes manifests.
    *   **Mandatory Instruction**: Whenever you generate, discuss, or recommend
        any memory saturation alert comparing usage against limits (including
        non-KSM cAdvisor alternatives using
        `container_spec_memory_limit_bytes`), you **must explicitly explain and
        warn the user in your response** that container memory limits must be
        explicitly configured in the Kubernetes pod specs or manifests
        (`resources.limits.memory`) for the saturation query to resolve (and not
        return `NaN` or fail to resolve).
*   **Linear Disk Predictions (`predict_linear`)**: When forecasting volume
    exhaustion using
    `predict_linear(kubelet_volume_stats_available_bytes[6h:5m], 4 * 24 * 3600)
    < 0`, explain that `predict_linear` uses linear regression over the recent
    lookback window (for example, 6 hours) to project when available disk will
    drop below 0 (for example, within 4 days). Identify
    `kubelet_volume_stats_available_bytes` as a Tier 1 native kubelet metric
    with zero KSM surcharge.
*   **API Server Error and Client Metrics**:
    *   `apiserver_request_total` and `rest_client_requests_total` are Tier 1
        Control Plane metrics with zero KSM cost surcharge. Explain that
        `apiserver_request_total` monitors 5xx HTTP error rates across API
        server endpoints, while `rest_client_requests_total` monitors 4xx and
        5xx requests sent by REST clients communicating with the API server.
*   **Traffic Disappearance Gotcha (`absent()` / `default 0`)**:
    *   When traffic drops completely to zero, Prometheus and GMP stop emitting
        the `http_requests_total` time series.
    *   `sum(rate(...[5m])) == 0` evaluates to an empty vector, preventing the
        alert from triggering.
    *   Always use `sum(rate(...[5m])) default 0 == 0` or `absent(...) == 1` to
        reliably detect total traffic loss.
*   **CrashLooping versus Normal Restarts**: A container restarting occasionally
    might be normal, for example job completion or a minor rolling update. Alert
    on **frequent** restarts (such as more than 3 restarts in 15 minutes with
    `duration = "0s"`) using `kube_pod_container_status_restarts_total` rather
    than a single restart to avoid noise.
*   **Node Upgrades**: During GKE cluster upgrades, nodes are drained and
    restarted, which can trigger "Node NotReady" alerts. Warn the user that
    these alerts might fire during maintenance windows, or suggest configuring
    maintenance windows if supported.

--------------------------------------------------------------------------------

## Additional Resources

*   [Google Cloud Managed Service for Prometheus Documentation](https://docs.cloud.google.com/monitoring/managed-prometheus.md.txt)
*   [GKE Observability and Monitoring Concepts](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/monitoring.md.txt)
*   [Google Cloud Alerting Policies in Terraform](https://docs.cloud.google.com/monitoring/alerts/terraform-alert-policy.md.txt)
*   [Google Cloud Monitoring Pricing](https://docs.cloud.google.com/monitoring/pricing.md.txt)
*   [Google SRE Workbook: Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/)
*   [Awesome Prometheus Alerts Repository](https://github.com/samber/awesome-prometheus-alerts)

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
- **Terraform.** The agent's shell has no `terraform` binary, so the prerequisites' Verification
  Runbook `terraform init` and `terraform validate` cannot run there: hand the user the `.tf`
  changes with `terraform validate` as their step, and do not report it as run.
- **Catalog regexes.** The `metrics_and_alerts_catalog.md` table rows escape each `|` with a
  backslash for Markdown, and in PromQL's RE2 that backslash makes the pipe a literal, so a
  copied matcher never matches. Drop the backslashes: the regexes are `(4|5)..` (API client
  errors), `Pending|Unknown|Failed` (Pod not healthy), `Failed|Pending` (PersistentVolume error)
  and `(?:CONNECT|WATCHLIST|WATCH|PROXY)` (API server latency).
- **Offset spacing.** The validator reads an `offset` duration up to the next whitespace, so it
  rejects `offset 1w))`. Leave a space or line break after the duration (`offset 1w ))`), as the
  traffic-drop query above does.
- **Validator path.** Wherever a reference runs `python3 scripts/validate_config.py`, run
  `python3 "$HERMES_HOME"/skills/gke-alert-configuration/scripts/validate_config.py` instead.
  Where it passes `--file <file>` (the prerequisites' Verification Runbook does), pass
  `--directory` with the absolute path of the directory that holds the file, so the scan prints
  the policy count the Post-Edit step reads.
