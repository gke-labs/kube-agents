# SOP - GitOps & Drift Reconciliation

You must execute this procedure exactly when triggered by the `gitops-drift-reconcile` cron job:

1. **Context Check**: Verify your namespace scope from `/opt/data/SETTINGS.md`.
2. **Reconciliation Sequence**:
   (Make sure to navigate inside the `./repo/` subdirectory to execute Git operations, while reading and writing state files at your root workspace)
   - Navigate inside your repository: run `cd repo`.
   - Run `git fetch origin` to retrieve remote updates.
   - Read the previously reconciled commit hash (`gitCommit` field) from the root-level state file `../memory/heartbeat-state.json`.
   - Get the latest fetched remote `HEAD` commit hash (run `git rev-parse origin/main` inside `./repo/`).
   - Compare the remote `HEAD` hash with the previously reconciled hash, and check GKE namespace manifests:
     - **If the hash has changed** (new commit merged on GitHub):
       - Fast-forward the local branch inside the repository: run `git merge origin/main`.
       - Wait for the external GitOps pipeline (or CI/CD runner) to deploy the updates: monitor the rollout status using read-only queries (e.g., run `kubectl rollout status deployment/<deployment-name> -n <namespace>` or query Pod statuses using `kubectl get pods -n <namespace>`). Do **NOT** run `kubectl apply` or other write commands.
       - Once GKE reaches the expected state, update the root state file `../memory/heartbeat-state.json` setting `gitCommit` to the new `HEAD` hash, and `reconciled` to `true`.
     - **If GKE namespace manifests/resources have been changed/drifted from Git**:
       - If anyone has manually modified the namespace out-of-band (drift detected), you are restricted from overwriting GKE directly. You **must immediately output a high-priority warning in the chat window** detailing the drifted resources, the expected Git state, and the instructions for the human operator to reconcile it.
     - **If the hash is unchanged AND no live namespace changes/drift are detected**:
       - You **must skip** any rollout or verify checks to optimize cluster resource operations.
   - Navigate back to your root workspace: run `cd ..` to resume standard operations.
3. **State Integrity**: Always ensure `memory/heartbeat-state.json` is updated on each turn according to the heartbeat state schema.
