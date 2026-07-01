# Operator SOP - CVE Scan

This procedure outlines the steps for conducting the hourly container image vulnerability scan.

## Procedure

1. **Prerequisite Checks**:
   - Verify if the Container Scanning API is enabled:
     ```bash
     gcloud services list --enabled | grep containerscanning.googleapis.com
     ```
   - If not enabled, suggest enabling it:
     ```bash
     gcloud services enable containerscanning.googleapis.com
     ```
   - _Note_: Artifact Registry vulnerability scanning is a paid feature. Ensure automatic scanning is enabled for the Artifact Registry repository.

2. **Enumerate Running Images**:
   - Query all Pods in all namespaces to extract the list of unique container images:
     ```bash
     kubectl get pods --all-namespaces -o jsonpath="{.items[*].spec.containers[*].image}" | tr ' ' '\\n' | sort -u
     ```

3. **Check Vulnerability Status**:
   - **Method A (GKE Security Posture)**: If GKE Security Posture is active, query for vulnerability findings.
   - **Method B (Artifact Registry)**: For images hosted in Artifact Registry, query scan results:
     ```bash
     gcloud artifacts docker images list-vulnerabilities <IMAGE_URI>
     ```
   - **Method C (Fallback)**: If an in-cluster scanner is used, query its custom resources (e.g., `kubectl get vulnerabilityreports`).

4. **Differential Analysis**:
   - Compare findings with the historical state stored in `memory/heartbeat-state.json` (`lastChecks.cve_scan`).

5. **Alerting**:
   - Only alert the human operator on _new_ `CRITICAL` or `HIGH` severity findings.
