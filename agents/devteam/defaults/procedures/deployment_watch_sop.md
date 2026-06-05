# SOP - Deployment Watch & Autonomous Recovery

You must execute this procedure exactly when triggered by the `deployment-watch` cron job:

1. **Context Check**: Verify your namespace scope from `/opt/data/SETTINGS.md`.
2. **Rollout Audit**: Query all active Deployments in your assigned namespace (`kubectl get deployments -n <namespace> -o yaml`).
3. **Identify Failures**: If any Deployment's replica count is not fully available, query Pod statuses (`kubectl get pods -n <namespace>`). Identify if any pod is stuck in a degraded state:
   - Check if containers are stuck in `ImagePullBackOff`, `ErrImagePull`, or `CrashLoopBackOff`.
4. **RCA Diagnostics**:
   - Invoke the `gke-workload-troubleshooting` skill targeting the degraded Pod.
   - If the diagnostic results confirm an invalid container image tag/version:
     - Run `git log -p -n 5 -- <manifest_path>` inside the `./repo/` directory to inspect recent changes to the deployment manifest.
     - Locate the last working image tag before the invalid revision was introduced.
     - Verify if a correct/valid version can be resolved (e.g. by checking if it matches the previous working version or if the tag contains a simple typographical error).
5. **Autonomous PR Creation**:
   - Navigate inside `./repo/`.
   - Edit the manifest file to reference the corrected image version.
   - Create a new Git branch: `fix/image-version-<deployment-name>`.
   - Commit and push the changes:
     ```bash
     git checkout -b fix/image-version-<deployment-name>
     git add <manifest_path>
     git commit -m "fix: restore working container image version"
     git push origin fix/image-version-<deployment-name>
     ```
   - Open a Pull Request on GitHub using the `gh pr create` CLI tool.
   - Post the resulting PR URL in the chat workspace, summarizing the issue and the automatic fix.
