---
title: User interfaces
description: The browser surfaces an install can offer beside chat — the A2A console, the web console, the Hermes dashboard and the local admin console — with the port, the switch and the way in for each.
sidebar:
  order: 9
---

Chat is the primary way to talk to kube-agents ([ChatOps](/kube-agents/concepts/chatops/)). Beside it, an install can offer the browser pages below. One is on by default on a first install; the others are opt-in or run on your own machine. None is published outside the cluster: every in-cluster page is reached through `kubectl`, and the local one binds loopback.

## The surfaces

| Surface                               | Where it runs                                                        | Port   | Default                                                        | How you reach it                                                                                              |
| ------------------------------------- | -------------------------------------------------------------------- | ------ | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| [A2A console](#a2a-console)           | The `<name>-a2a-console` pod, part of the `spec.mode: next` stack    | `8080` | On, on every `next` install; absent under `today`              | `kubectl -n kubeagents-system port-forward svc/<name>-a2a-console 8080:8080`, then `http://localhost:8080`    |
| [Web console](#web-console)           | The chart's `<release>-web-console` pod                              | `8080` | Off; `install.sh --enable-web-console` or `webConsole.enabled` | `kubectl -n kubeagents-system port-forward svc/<release>-web-console 8080:8080`, then `http://localhost:8080` |
| [Hermes dashboard](#hermes-dashboard) | The `platform-agent-dashboard` container of the `<name>-gateway` pod | `9119` | Off in `install.sh`; `--enable-hermes-dashboard` turns it on   | `./scripts/hermes-dashboard-tunnel.py`, then `http://127.0.0.1:9119`                                          |
| [Admin console](#admin-console)       | Your workstation, from a repository checkout                         | `8501` | Not deployed; started by hand                                  | `./scripts/admin_portal.sh`, then `http://127.0.0.1:8501`                                                     |

`<name>` is the `PlatformAgent`'s name, `platform-agent` on a stock install, and `<release>` the Helm release name, `kube-agents` on a stock install. The A2A console and the web console both use local port 8080, so forward one at a time or give the web console another local port. The A2A console accepts no other local port; see its Host check below.

## A2A console

The A2A console is a status page over the install's message bus with a chat pane under it. It is served by the `<name>-a2a-console` pod, which the operator renders on every `spec.mode: next` install beside the bus, the auth callout, the verifier and the A2A gateway ([Architecture](/kube-agents/overview/architecture/#deployment-topology)). Under `today` there is no bus, so there is no console.

The page shows what the bus knows: sessions and their liveness, recent tasks and why they failed, conversations by chat backend, the standing topics, stream capacity and protocol anomalies. The chat pane talks to the A2A gateway through its console door, the same way a Google Chat or Slack message does, `/session` and `/feedback` included ([Conversations under `spec.mode: next`](/kube-agents/concepts/chatops/#conversations-under-specmode-next)). A message typed there reaches the Planning Agent, which answers or files kanban cards as it does for any chat message.

Forward the Service and open the page:

```bash
kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-console 8080:8080
# then open http://localhost:8080
```

The console server answers only when the browser's address is `localhost:8080` or `127.0.0.1:8080`, the two Host values the documented port-forward produces. A forward to any other local port is refused with HTTP 421 and a message naming the port to use. The check is what keeps a page served from another name from reading the console's bus credential through a rebinding DNS name. The page loads that credential from the server, so there is no password to paste, and the server proxies the page's websocket to the bus's TLS listener. Everything the page does on the bus it does as the shared `console` principal, which can read the bus and publish on the console door and nowhere else.

The Service is `ClusterIP` and the pod's NetworkPolicy denies all ingress, so the port-forward is the only way in. Whoever holds `pods/portforward` on the namespace can use the console; that Kubernetes grant is the access gate, and the page has no login of its own. [`a2a/web/README.md`](https://github.com/gke-labs/kube-agents/blob/main/a2a/web/README.md) is canonical for the page and its posture.

## Web console

The web console is a browser chat page for installs with no chat platform: an evaluation cluster, a workshop seat or a demo. It shows event triage and scheduled checks as channels you can reply in, holds your own chat threads, and shows the model and LiteLLM usage. The chart renders it when `webConsole.enabled` is true, which `install.sh --enable-web-console` sets; it is off by default. It works in either mode, since it talks to the agent's own API rather than the bus. [Web console](/kube-agents/deploy/web-console/) is canonical: the port-forward, the layout, the threads, and what the page holds and does not.

## Hermes dashboard

The Hermes dashboard is the runtime's own debugging view of the gateway pod: the `default` profile's sessions and the shared kanban board. It runs as the `platform-agent-dashboard` container in the `<name>-gateway` pod when `spec.harness.hermes.dashboardEnabled` is true. The CRD's default is on, but `install.sh` records it off unless you pass `--enable-hermes-dashboard`.

The dashboard binds loopback inside the pod, and on a stock install the agent pod runs under GKE Sandbox (gVisor), where `kubectl port-forward` cannot see a loopback listener. So the way in is the relay over `kubectl exec` in the table, which finds the gateway pod by label and prints the URL; on a node pool without the sandbox, `kubectl port-forward svc/<name> 9119:9119` works too. [`PlatformAgent` CRD → `spec.harness`](/kube-agents/operator/platformagent-crd/#specharness) is canonical for the field, the access path and why the listener stays on loopback.

## Admin console

The admin console is a Streamlit prototype for one operator's machine. `./scripts/admin_portal.sh` starts it from a repository checkout; nothing in the cluster serves it. It reads the install's resources through your own gcloud login and kubeconfig: a Chat page that reaches the Planning Agent, the kanban board and cron roster read-only, Cloud Logging and Cloud Trace activity, and the Google Chat setup checklist. [Admin console](/kube-agents/reference/admin-console/) is canonical for starting it, its pages, its boundary and the files it keeps.

## Other ways in

Two doors are not browser pages. The Hermes CLI runs inside the gateway pod over `kubectl exec` ([When no chat platform is enabled](/kube-agents/concepts/chatops/#when-no-chat-platform-is-enabled)). The opt-in [MCP server](/kube-agents/reference/a2a-mcp/) lets an external coding agent or IDE talk to a `next` install over the same bus the A2A console uses.
