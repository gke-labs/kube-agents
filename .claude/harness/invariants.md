# Invariants gate (pre-merge checklist)

The five load-bearing rules from `docs/design/README.md`. **A change that violates one is wrong even
if it compiles and passes tests.** The harness runs this checklist before opening/merging any PR.
Detailed in `docs/design/03-security-model.md`.

Answer each as PASS / FAIL / N-A with one line of evidence (a file, a test name, a command output).

1. **Read-only agents.** No change gives an agent a write verb or write-capable tool/credential on
   any cluster or cloud API. The only write path introduced is a reviewed PR.
   - Check: no new write RBAC verbs on an agent SA; no mutating MCP tool reachable by an agent; cloud
     GSA stays viewer-only. Backstop: the `ValidatingAdmissionPolicy` (Phase 0) rejects agent-SA
     write/wrong-scope RBAC at apply time.

2. **All mutation flows through GitOps.** No direct `kubectl`/`gcloud` write, no KCC/Terraform applied
   by an agent, no break-glass path. Change → PR → human approve → customer CI/CD applies.
   - Check: no `apply`/`delete`/`create` actuation in agent code paths; actuation lives in the
     customer CI/CD workflow only.

3. **Agents never call each other directly.** Coordination is only via shared state — the GitOps repo
   and the OKF. No agent-to-agent RPC/HTTP/tool call.
   - Check: no code path where one agent invokes another; escalation is a written artifact picked up
     by the parent.

4. **Each tier is scope-bounded** (project / cluster / namespace) by a per-agent read-only identity,
   not by convention.
   - Check: the agent's KSA/RBAC/WI is pre-created and scoped to its tier; controller mints no RBAC;
     `(tier,scope)` cardinality webhook holds.

5. **Every change is reviewed, attributable, and revertible.**
   - Check: change is a PR (reviewable + revertible); audit record ties it to requester + PR.

---

## Also enforce (repo mechanics, from AGENTS.md)

- Conventional Commits; scoped to the request; no unrelated formatting churn.
- `npx prettier --write` on changed md/json/yaml; `make`/`go build` if `k8s-operator/` changed;
  `docker build` the relevant Dockerfile target if the image changed.
- Use `.github/PULL_REQUEST_TEMPLATE.md`; do **not** use `gh pr create --fill`.
- Push PR branches to a fork, not upstream. Stage only targeted files.

## Destructive-test guard

Before any test that deletes/kills resources or applies deliberately-bad RBAC, confirm the target
context is **Kind** or an **ephemeral scratch GKE** cluster. If it is anything else (esp. a prod
context), **halt and surface** — do not run.
