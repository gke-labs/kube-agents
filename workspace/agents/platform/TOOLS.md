# TOOLS.md - Platform Agent Routing Shortcuts & Checklists

Use these shortcuts and checklists during handoff and delegation.

## Routing Shortcuts
- `@devteam <task>` → delegate app build/deploy/debug tasks
- `@operator <task>` → delegate cluster capacity/incident/upgrade tasks
- `@platform <task>` → manage core provisioning and architectural policy

## DevTeam Handoff & Delivery Checklist
When routing tasks to the `devteam` agent, require the following proof before reporting success:
1. GitHub Pull Request URL and list of changed files.
2. Repository path and workspace details.
3. Target GKE namespace and deployment results.
4. Rollout status confirmation:
   - `kubectl get deploy -n <ns>`
   - `kubectl get pods -n <ns>`
   - `kubectl get svc -n <ns>`

## Operator Handoff & Delivery Checklist
When routing tasks to the `operator` agent, require:
1. Current cluster context used (`kubectl config current-context`).
2. Scope of inspection (cluster resources or namespaces).
3. Before/after state comparison:
   - `kubectl get <resource>`
4. Remediation CLI outputs and health confirmation.
5. Risk and user-facing impact notes.
