---
title: Admin console
description: The Kube Agents Console, a local Streamlit prototype started from a repository checkout that chats with the agent, browses its activity, and sets up the inference gateway. The install does not deploy it.
sidebar:
  order: 9
---

The Kube Agents Console is a web interface for one operator's machine. `./scripts/admin_portal.sh` starts it from a repository checkout; it listens on loopback only, and the gcloud login the launcher verifies is its authentication boundary. It is a prototype: the install does not deploy it, and nothing reaches it from another machine. [`admin_console/README.md`](https://github.com/gke-labs/kube-agents/blob/main/admin_console/README.md) is the operating guide and [`docs/designs/admin-console.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/admin-console.md) the design of record; this page is the summary.

## Starting it

From the root of a checkout, with an active gcloud login:

```bash
./scripts/admin_portal.sh
```

The launcher checks that gcloud has an active account whose login still mints a token, creates `.venv` under the checkout and installs the console's dependencies into it when they are missing (with `uv` when it is on the path, otherwise `python3 -m venv` and pip), then prints the local URL, `http://127.0.0.1:8501` by default. If it reports no active account or an expired login, run `gcloud auth login` and start it again. To use another port:

```bash
ADMIN_PORTAL_PORT=8601 ./scripts/admin_portal.sh
```

Streamlit runs on a second, private loopback port, the public port plus one by default; `ADMIN_PORTAL_STREAMLIT_PORT` overrides it when that one is taken. Ctrl-C stops the console.

Beyond what the launcher checks, you need Python 3 or `uv` to create the environment and an account with read access to the project and the cluster. The Connection page verifies the rest with bounded read-only checks and reports each one: project access and the required APIs, GKE discovery, recent Cloud Logging and Cloud Trace data, and the agent runtime. Cloud Trace is read with Application Default Credentials, so a failing Trace check is fixed with `gcloud auth application-default login`.

## The pages

In navigation order:

- **Setup → Connection.** Pick the project, then the cluster. Project Connect verifies the gcloud identity, project access and GKE discovery; Cluster Connect verifies the kube-agents runtime. A single cluster labelled `kube-agents-host=true` is preselected. Every other page reads the target verified here and points back to this page until both levels connect.
- **Setup → LLM Gateway.** Reads the install's LiteLLM resources and runs one bounded request from the Platform Agent through `model-default`, the same path the agents use. Its configuration form changes the provider and patches the API key Secret only for a LiteLLM the console manages; the LiteLLM this repository's install deploys is Helm-owned, so on a stock install the page shows live status with the form disabled, and provider changes go through the installer, Terraform or Helm ([Inference gateway](/kube-agents/concepts/inference-gateway/)).
- **Setup → Chat.** A table of recent Hermes sessions, with the selected session's transcript and a composer below it. A message sent here goes to the Planning Agent, the front door Google Chat and Slack use, so it is planned and delegated the same way, and the kanban tasks it spawns render inline in the thread. Google Chat and Slack sessions are visible but read-only; a follow-up from the console opens a separate `portal_*` session rather than posting into someone else's thread. When the agent asks for a tool approval, the choices are Approve once or Deny.
- **Observability → Overview.** Activity volume, the split between human-initiated and autonomous work, attention items, attribution coverage and recent outcomes for a time window chosen on the page.
- **Observability → Activity Explorer.** Structured audit events from Cloud Logging and Hermes traces from Cloud Trace for the selected window: an aggregate flow diagram, a per-interaction timeline, and a paginated ledger with a Google Cloud evidence link on every record. Truncated or failed source reads are shown as partial rather than treated as complete.
- **Observability → Task Kanban.** The live shared kanban board, read-only: filter by status and assignee, select a task for its request, runs, linked chat session, comments and lifecycle events. The page never claims, retries or comments on a task.
- **Observability → Scheduled Cron.** Every profile's cron jobs: definitions, scheduler heartbeat, one execution table per job title, and a UTC calendar of recent runs and the occurrences projected for the next 21 days. An enabled job whose profile has no live ticker is reported as unable to run automatically.
- **Integration → Google Chat.** A read-only checklist derived from the live `PlatformAgent` resource: the required APIs, topic, subscription, routing and IAM, plus the Google Chat sessions Hermes has seen in the last 30 days. The first failed check comes with fix steps and the values to copy.

## Boundary

- **Loopback only.** Both listeners bind `127.0.0.1`. The launcher-verified gcloud login is the authentication boundary; a console served to other machines would need application-level authentication that the prototype does not have.
- **Read-only, with one exception.** Connection, Observability and Integration pages never grant IAM, enable APIs or change Kubernetes resources. The LLM Gateway page may apply repository-managed LiteLLM manifests, patch that Deployment's credential Secret and restart it, and refuses a Helm-owned release. Chat sends prompts to the agent, which acts within its own permissions ([Security and IAM](/kube-agents/reference/security-and-iam/)).
- **Credentials stay with gcloud.** The console mints a short-lived token per check and discards it, prepares cluster credentials in a process-private temporary kubeconfig that is removed when it exits, and never reads the Platform Agent API key: Chat runs a fixed client inside the gateway container, which reads the key from its own environment for a loopback request.
- **What it keeps on disk.** Two owner-only files. The connection target (gcloud account, project, cluster, location, namespace and verification time, never a token or kubeconfig) is in `~/.kube-agent/state/admin-portal-connection.json`, and Disconnect deletes it; [`admin_console/CONNECTION_SECURITY.md`](https://github.com/gke-labs/kube-agents/blob/main/admin_console/CONNECTION_SECURITY.md) is its contract. Console chat interactions, prompts and replies included, are in `~/.local/state/kube-agents/admin-portal-interactions.db` (or under `$XDG_STATE_HOME`), bounded to the newest 1,000 records and seven days.
