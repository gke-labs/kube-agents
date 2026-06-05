# DevTeam SOP - Deployment Failure Resolver

This procedure outlines the steps for autonomously detecting, diagnosing, and proposing fixes for failing deployments.

## Procedure

1. **Acquire GKE Cluster Context**:
   - Read the cluster name (`<cluster_name>`) and location (`<cluster_location>`) from `/opt/data/SETTINGS.md`.
   - Retrieve the credentials and context for the target GKE cluster:
     ```bash
     gcloud container clusters get-credentials <cluster_name> --region <cluster_location>
     ```

2. **Monitor Workload Health**:
   - Enumerate all workloads in the assigned GKE namespace (read from `/opt/data/SETTINGS.md` or `USER.md`):
     ```bash
     kubectl get deployments -n <namespace>
     ```
   - Check if any deployment has mismatched replica counts (e.g. `AVAILABLE` != `READY` or `UPDATED` != `REPLICAS`) or if any pods are in `CrashLoopBackOff`, `ImagePullBackOff`, `ErrImagePull`, or `CreateContainerConfigError`:
     ```bash
     kubectl get pods -n <namespace>
     ```

2. **Trigger Diagnostics**:
   - If a failing deployment or pod is detected, invoke the **`gke-workload-troubleshooting`** skill.
   - Execute the diagnostic workflow to identify the precise root cause (such as a misspelled image version/tag, resource constraint, or missing secret).

3. **Locate and Analyze Source Manifests**:
   - Navigate to the local Git repository clone (`./repo/`).
   - Find the YAML manifest source file corresponding to the failing GKE workload.

4. **Prepare the GitOps Correction**:
   - Create a new Git branch locally:
     ```bash
     git checkout -b fix/<workload-name>-deployment-failure
     ```
   - Generate the corrected YAML manifest patch (e.g. roll back to the last known working image tag found in `git log`, increase resources, or correct the typo).
   - Apply the change to the manifest file in `./repo/`.

5. **Commit, Push, and Propose PR**:
   - Add the changes and commit with a structured commit message:
     ```bash
     git add <manifest-file-path>
     git commit -m "fix(<namespace>): correct <workload-name> deployment failure due to <root-cause>"
     ```
   - Push the branch to the personal fork:
     ```bash
     git push fork fix/<workload-name>-deployment-failure
     ```
   - Open a draft Pull Request (PR) on GitHub against the upstream repository:
     ```bash
     gh pr create --draft --title "fix(<namespace>): resolve <workload-name> deployment failure" --body "Resolves deployment failure by correcting manifest. Root cause: <root-cause>"
     ```

6. **Notify the User**:
   - Post a high-signal notification in the chat with the PR URL and a concise summary of the diagnostic analysis.
