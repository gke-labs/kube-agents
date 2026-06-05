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

3. **Trigger Diagnostics**:
   - If a failing deployment or pod is detected, invoke the **`gke-workload-troubleshooting`** skill.
   - Execute the diagnostic workflow to identify the precise root cause (such as a misspelled image version/tag, resource constraint, or missing secret).

4. **Locate and Analyze Source Manifests**:
   - Navigate to the local Git repository clone (`./repo/`).
   - Find the YAML manifest source file corresponding to the failing GKE workload.

5. **Check for Existing Fixes**:
   - Check if a branch or Pull Request (PR) already exists for this workload/failure. If so, update the existing branch/PR or notify the user instead of creating a duplicate.
   - Run `git branch -r | grep fix/<workload-name>-deployment-failure` or search on GitHub using `gh pr list --state open --search "<workload-name>"`.
   - If a duplicate is found, abort creation of a new branch/PR and notify the platform agent instead of creating a duplicate.

6. **Prepare the GitOps Correction**:
   - Create a new Git branch locally:
     ```bash
     git checkout -b fix/<workload-name>-deployment-failure
     ```
   - Generate the corrected YAML manifest patch (e.g. roll back to the last known working image tag found in `git log`, increase resources, or correct the typo).
   - Apply the change to the manifest file in `./repo/`.

7. **Commit, Push, and Propose PR**:
   - Add the changes and commit. **ONLY stage the modified manifest files, DO NOT stage temporary files or run `git add .`**:
     ```bash
     git add <manifest-file-path>
     git commit -m "fix(<namespace>): correct <workload-name> deployment failure due to <root-cause>"
     ```
   - Push the branch:
     ```bash
     git push origin fix/<workload-name>-deployment-failure
     ```
   - Open a draft Pull Request (PR) on GitHub against the upstream repository:
     ```bash
     gh pr create --draft --title "fix(<namespace>): resolve <workload-name> deployment failure" --body-file .tmp_pr_body.md
     ```
   - **Delete the temporary PR body file immediately to avoid committing or leaving dirty files**:
     ```bash
     rm -f .tmp_pr_body.md
     ```

8. **Notify the Platform Agent**:
   - Send the failure notification and PR link back to the Platform Agent completions API using curl:
     ```bash
     curl -s -X POST $PLATFORM_API_URL/chat/completions \
       -H "Content-Type: application/json" \
       -H "Authorization: Bearer $PLATFORM_API_KEY" \
       -d "{\"model\": \"hermes-agent\", \"messages\": [{\"role\": \"user\", \"content\": \"[IMPORTANT: Call the send_notification tool immediately to announce this alert to the Google Chat space.] Alert: Deployment <workload-name> in namespace <namespace> is failing on cluster <cluster-name> due to <root-cause>. Corrective PR has been proposed: <PR-URL>\"}]}"
     ```
