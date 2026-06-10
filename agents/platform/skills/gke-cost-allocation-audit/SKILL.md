---
name: gke-cost-allocation-audit
description: Audit GKE cost allocation, query billing exports, calculate namespace/workload resource costs, and analyze idle capacity resource waste. Use when setting up GKE Cost Allocation, auditing GCP billing datasets, running SQL queries on GKE billing tables, or tracking cost trends over time.
---

# gke-cost-allocation-audit - GKE Cost Allocation & Billing Audits

This skill equips the agent to configure, verify, analyze, and optimize Google Kubernetes Engine (GKE) cluster costs using GKE Cost Allocation telemetry exported to BigQuery.

## When to Use

- **Cost Allocation Setup**: Triggered when a user needs to enable GKE Cost Allocation or set up Billing Export.
- **Cost Attribution**: Triggered when a user requests the cost breakdown of a GKE cluster by namespace, workload, or custom Kubernetes labels.
- **Cost Diagnostics & Waste Analysis**: Triggered when investigating high billing charges or analyzing idle cluster capacity/unallocated resources.
- **Historical Resource Utilization**: Triggered when asking for historical usage trends (e.g., CPU/memory consumption over N days) combined with financial costs.

## Execution Instructions

---

### Step 1: Set Up & Verify GKE Cost Allocation

Before you can run cost reports, GKE Cost Allocation must be enabled on the cluster and detailed billing exports must be configured.

#### A. Enable GKE Cost Allocation on Cluster
To enable cost allocation on an existing GKE cluster:

- **Zonal Cluster**:
  ```bash
  gcloud container clusters update <CLUSTER_NAME> \
      --zone=<ZONE> \
      --enable-cost-allocation \
      --project=<PROJECT_ID>
  ```
- **Regional Cluster**:
  ```bash
  gcloud container clusters update <CLUSTER_NAME> \
      --region=<REGION> \
      --enable-cost-allocation \
      --project=<PROJECT_ID>
  ```

To verify if GKE Cost Allocation is active:
```bash
gcloud container clusters describe <CLUSTER_NAME> \
    --zone=<ZONE> \
    --format="value(costManagementConfig.enabled)" \
    --project=<PROJECT_ID>
```
*Expected output should be `True`.*

#### B. Set Up Detailed Billing Export to BigQuery
GKE Cost Allocation telemetry is delivered via the Detailed Billing Export. This must be set up in the GCP Billing Console:
1. Navigate to the **Billing** section of the Google Cloud Console.
2. Select **Billing export**.
3. Under the **Detailed cost data export** tab, click **Edit settings**.
4. Specify the **Target Project** and **BigQuery Dataset** where logs should be exported.
5. Save the configuration.
*Note: Data can take up to 24 hours to begin populating in BigQuery after enabling the export.*

---

### Step 2: Locate the BigQuery Billing Table

GKE Cost Allocation data is written to the standard GCP billing export table in BigQuery.

1. **List available datasets** in your billing project:
   ```bash
   bq ls --project_id=<BILLING_PROJECT_ID>
   ```
2. **Locate the billing table** (usually starts with `gcp_billing_export_resource_v1_` or `gcp_billing_export_v1_`):
   ```bash
   bq ls --project_id=<BILLING_PROJECT_ID> <DATASET_ID>
   ```
   *Note: Save the fully qualified table name as `<PROJECT_ID>.<DATASET_ID>.<TABLE_NAME>`.*

---

### Step 3: Namespace-level Cost Breakdown (Past N Days)

To calculate the GKE infrastructure cost attributed to each namespace over the past `<DAYS_LIMIT>` days (using scalar subqueries to avoid duplicate row expansion):

```sql
SELECT
  (SELECT value FROM UNNEST(t.labels) WHERE key = 'k8s-namespace') AS namespace,
  SUM(t.cost) AS gke_infra_cost,
  SUM(t.cost) + SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(t.credits) AS c), 0)) AS net_cost
FROM
  `<BQ_TABLE_NAME>` AS t
WHERE
  t.project.id = '<CLUSTER_PROJECT_ID>'
  AND t.usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL <DAYS_LIMIT> DAY)
  AND EXISTS(SELECT 1 FROM UNNEST(t.labels) WHERE key = 'k8s-namespace')
GROUP BY
  namespace
ORDER BY
  net_cost DESC;
```

---

### Step 4: Top CPU-Consuming Workloads and Cost Audit (Past N Days)

To find the top 5 workloads that consumed the most CPU resources (in vCPU-hours) and their associated infrastructure cost over the past `<DAYS_LIMIT>` days:

```sql
SELECT
  -- Extract workload name and namespace from the labels array
  (SELECT value FROM UNNEST(t.labels) WHERE key = 'k8s-workload-name') AS workload_name,
  (SELECT value FROM UNNEST(t.labels) WHERE key = 'k8s-namespace') AS namespace,
  -- Calculate infrastructure cost allocated to this workload
  SUM(t.cost) AS infra_cost,
  -- Calculate vCPU-hours
  SUM(CASE WHEN t.sku.description LIKE '%Core%' THEN t.usage.amount ELSE 0 END) / 3600 AS vcpu_hours
FROM
  `<BQ_TABLE_NAME>` AS t
WHERE
  t.project.id = '<CLUSTER_PROJECT_ID>'
  AND t.usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL <DAYS_LIMIT> DAY)
  -- Filter records that have workload annotations
  AND EXISTS(SELECT 1 FROM UNNEST(t.labels) WHERE key = 'k8s-workload-name')
GROUP BY
  workload_name,
  namespace
ORDER BY
  vcpu_hours DESC
LIMIT 5;
```

---

### Step 5: Audit Unallocated Node Waste (Idle VM Capacity)

GKE Cost Allocation assigns VM cost to namespaces based on pod resource requests. Any remaining VM capacity that was not requested by any pod is billed to `kube:unallocated`.

To calculate the cost of unallocated (idle) compute capacity over the past `<DAYS_LIMIT>` days:

```sql
SELECT
  (SELECT value FROM UNNEST(t.labels) WHERE key = 'k8s-namespace') AS namespace,
  SUM(t.cost) AS unallocated_cost
FROM
  `<BQ_TABLE_NAME>` AS t
WHERE
  t.project.id = '<CLUSTER_PROJECT_ID>'
  AND EXISTS(SELECT 1 FROM UNNEST(t.labels) WHERE key = 'k8s-namespace' AND value = 'kube:unallocated')
  AND t.usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL <DAYS_LIMIT> DAY)
GROUP BY
  namespace;
```
*If unallocated cost is high (e.g. > 40% of total cluster cost), recommend cluster scale-down, configuring autoscaling limits to scale to zero, or right-sizing pod requests.*

---

### Step 6: Execute Queries via BigQuery CLI

To run any of the SQL queries above from the command-line, format the query string and execute using the `bq` CLI tool:

```bash
bq query --use_legacy_sql=false \
  "SELECT (SELECT value FROM UNNEST(t.labels) WHERE key = 'k8s-namespace') AS namespace, SUM(t.cost) AS cost FROM \`<BQ_TABLE_NAME>\` AS t WHERE t.project.id = '<CLUSTER_PROJECT_ID>' GROUP BY namespace ORDER BY cost DESC LIMIT 10"
```
