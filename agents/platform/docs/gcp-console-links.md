# GCP Console Link Templates

Shared reference for the Platform and Cluster agents. When discussing telemetry, tracing, logs,
or debugging, construct direct Google Cloud Console links for the user's **active GCP project**
(and, where possible, scope them to the relevant cluster). Always format them as clickable
Markdown links. Substitute `{project_id}` with the active project ID.

- **Cloud Logging (Logs Explorer):**
  `https://console.cloud.google.com/logs/query;query=resource.type%3D%22k8s_container%22%0Aresource.labels.project_id%3D%22{project_id}%22?project={project_id}`
- **Cloud Trace (Trace Explorer):**
  `https://console.cloud.google.com/traces/list?project={project_id}`
- **Cloud Monitoring (Metrics Explorer):**
  `https://console.cloud.google.com/monitoring/metrics-explorer?project={project_id}`
- **GKE Workloads Console:**
  `https://console.cloud.google.com/kubernetes/workload/overview?project={project_id}`
