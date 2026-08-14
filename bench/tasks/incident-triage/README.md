# Incident triage tasks

Report-contract coverage for the incident triage surface: `k8s-event-watcher`
sees a Kubernetes warning, `session_kv_server` turns it into a prompt, the agent
posts an Incident Triage report to chat, and a human reply of `apply` opens a
GitOps pull request.

Every task here is `deployer: noop`. The event payload travels in the prompt
exactly as [`_build_agent_query()`][query] assembles it, so the agent reasons
from the payload rather than live cluster state and the group provisions no
infrastructure. Cost is roughly 2.5 minutes and a few KB of session rows per
task.

[query]: ../../../agents/platform/scripts/session_kv_server.py

## What each task covers

| Task | Covers |
| --- | --- |
| `report` | The base contract on a `FailedScheduling` event. |
| `oomkilled`, `crashloop`, `imagepull`, `pdb-eviction`, `node-notready` | The same contract across event classes, so template drift and boilerplate root-causing show up. |
| `single-option` | The branch where one sound fix means no Recommended line and a bare-`apply` call-to-action. |
| `apply-recommended`, `apply-explicit-option` | A bare `apply` resolves to the option marked Recommended; an explicit `apply Option B` overrides it. |
| `prompt-injection` | The warning message is workload-controlled and interpolated straight into the prompt, so it must be treated as data. |
| `no-live-mutation` | The ban on imperative live-cluster mutation holds even when the user pushes for it. |
| `session-routing` | The notification carries the exact session id, without which the report posts as a fresh message instead of a threaded reply. |
| `degraded-payload`, `ambiguous-evidence` | Say what is missing rather than invent a root cause. |

## Running them

```bash
cd bench
BENCH_USE_MCP=false \
  PLATFORM_AGENT_TOKEN="$(kubectl get secret platform-agent-secrets -n kubeagents-system -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)" \
  GEMINI_API_KEY="$(kubectl get secret platform-agent-secrets -n kubeagents-system -o jsonpath='{.data.GEMINI_API_KEY}' | base64 --decode)" \
  JUDGE_PROVIDER=google JUDGE_MODEL=gemini-3.1-pro-preview \
  uv run devops-bench ./tasks/incident-triage/report/task.yaml --no-infra --agent-type kubeagents
```

`BENCH_USE_MCP=false` for everything except `no-live-mutation` and
`session-routing`, whose real assertions are on tool-call arguments. The payload
is self-contained, so with MCP on the agent burns roughly 3x the tokens probing
`tool_search` and `ToolInvocation` scores a trajectory these tasks never ask for.

## Two things to know before editing a task

**Checklist bullets must each be one line.** `extract_checklist_items()` only
collects lines that begin with `-`, so a wrapped bullet silently loses its
continuation and the judge scores a truncated criterion — which reads as an
agent failure.

**Criteria are LLM-judged, so assert substance, not vocabulary.** A criterion
that names an incidental phrase fails a correct report for not using that
phrase. Three checks here had to be reworded for exactly that reason. Judge cost
is about 16 seconds per bullet, serially, so keep them to 5-8 per task.
