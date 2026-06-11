# Operator SOP - Weekly Cost Report

This procedure outlines the steps for generating the weekly cost optimization report.

## Procedure

1. **Verify GKE Cost Allocation**:
   - Ensure GKE Cost Allocation is enabled on the target clusters to allow fine-grained metric tracking.

2. **Query Cost Data**:
   - Use the `gke-cost-analysis` skill to query BigQuery for GKE costs over the last 7 days.
   - Aggregate and break down cost data by:
     - GCP Project
     - GKE Cluster
     - Kubernetes Namespace

3. **Analyze and Optimize**:
   - Identify top-spending namespaces and workloads.
   - Spot anomalies or sudden cost spikes.
   - Identify potential savings (e.g., underutilized node pools, over-provisioned workloads).

4. **Compile and Save Report**:
   - Summarize total cost and top spenders in a clean markdown report.
   - Write the detailed report to `memory/reports/cost-report-YYYYMMDD.md` (replacing YYYYMMDD with the current date).
