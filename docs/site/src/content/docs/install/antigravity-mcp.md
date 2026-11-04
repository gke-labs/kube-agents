---
title: Using kube-agents from Antigravity
description: Connect Google Antigravity to a spec.mode next install through the MCP server and the kube-agents-mcp bridge, send a first message, and fix the common connection errors.
---

This guide connects [Google Antigravity](https://antigravity.google) to a kube-agents install, so the agent in your editor can ask the install questions, hand it work and read the answers. The path has two pieces. The [MCP server](/kube-agents/reference/a2a-mcp/) runs in the cluster beside the A2A console. The `kube-agents-mcp` bridge runs on your workstation as a local MCP server that Antigravity starts. The bridge finds the install through your kubeconfig, reads the server's token and reaches it over a port-forward of its own, so the only Antigravity setting it needs is the path to the binary.

The steps work the same from any MCP host that starts local servers over standard input and output; only the configuration file in [Configure Antigravity](#configure-antigravity) is specific to Antigravity.

## Before you start

You need four things:

- **An install in `spec.mode: next`.** The MCP server is a client of the `next` message bus and renders nothing under `today`. Check with `kubectl get platformagent -A -o custom-columns=NAME:.metadata.name,NAMESPACE:.metadata.namespace,MODE:.spec.mode`.
- **The MCP server turned on.** It is off by default; the next section turns it on.
- **A kubeconfig context for the cluster** the install runs on, which works from a terminal on the same machine (`kubectl --context <context> get pods -n kubeagents-system`).
- **Go 1.27 or later**, to build the bridge.

## Turn on the MCP server

The server has no `PlatformAgent` field. It is rendered while the operator's controller manager runs with the environment variable `A2A_MCP_DOOR=true`, on top of `spec.mode: next`; any other value leaves it off. Set the variable in the chart's `operator.extraEnv` list, as [Operator settings](/kube-agents/install/quickstart-gke/#operator-settings) describes. On an install made with `install.sh`, that is one `TF_VAR_extra_helm_values` line in `install.env`, followed by an `install.sh` or `upgrade.sh` run:

```bash
TF_VAR_extra_helm_values='{operator={extraEnv=[{name="A2A_MCP_DOOR",value="true"}]}}'
```

The variable applies to every `PlatformAgent` the operator manages. If the line already carries other `extraEnv` entries, add this one to the same list, since there is one such line. On a bare Helm install, add the entry to `operator.extraEnv` in your values file.

Once the operator has restarted and reconciled, the install's namespace holds three objects named after the `PlatformAgent` (`platform-agent` on a default install):

```bash
kubectl -n kubeagents-system get deployment,service platform-agent-a2a-mcp
kubectl -n kubeagents-system get secret platform-agent-a2a-mcp-token
```

If they do not appear, read the `A2AMCPServer` condition on the `PlatformAgent` ([status conditions](/kube-agents/operator/platformagent-crd/#status)). It is set when a ServiceAccount or Secret of those names already exists and the `PlatformAgent` does not own it.

## Grant your account access

The bridge acts with your kubeconfig credentials. Each person who connects needs these permissions on the install's cluster:

| Permission                                                                              | Where                  | Why                                                        |
| --------------------------------------------------------------------------------------- | ---------------------- | ---------------------------------------------------------- |
| `list` on `platformagents.kubeagents.x-k8s.io`                                          | Cluster-wide           | Finding the install; the bridge lists across namespaces.   |
| `list` on `agentprofiles.kubeagents.x-k8s.io`                                           | Cluster-wide, optional | Showing the install's profiles; without it they are blank. |
| `get` on the Secret `<name>-a2a-mcp-token`                                              | The install namespace  | Reading the bearer token.                                  |
| `get` on the Service `<name>-a2a-mcp`, `list` on `pods`, `create` on `pods/portforward` | The install namespace  | Opening the port-forward to the server's pod.              |

Reading the token Secret and opening the port-forward are the access gate to the server: anyone with both can send the install messages as a console user. A Role and ClusterRole for a default install look like this; bind them to your users or group:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: kube-agents-mcp-user
  namespace: kubeagents-system
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["platform-agent-a2a-mcp-token"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["services"]
    resourceNames: ["platform-agent-a2a-mcp"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["list"]
  - apiGroups: [""]
    resources: ["pods/portforward"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kube-agents-mcp-discovery
rules:
  - apiGroups: ["kubeagents.x-k8s.io"]
    resources: ["platformagents", "agentprofiles"]
    verbs: ["list"]
```

The `create` on `pods/portforward` is not limited to the MCP server's pod, because the pod name changes on every rollout. It reaches every pod in the namespace, so grant it to people you would trust with that.

## Build the bridge

The bridge is a Go program in the `a2a` module of the kube-agents repository. Install it into `$(go env GOPATH)/bin`:

```bash
go install github.com/gke-labs/kube-agents/a2a/cmd/kube-agents-mcp@main
```

The module needs Go 1.27; an older `go` command fetches the right toolchain by itself unless `GOTOOLCHAIN=local` is set. To build from a clone instead, run `go build -o kube-agents-mcp ./cmd/kube-agents-mcp` in its `a2a` directory. Check the binary:

```bash
kube-agents-mcp --version
```

It prints `kube-agents` and the bridge's version. Note the binary's full path (`command -v kube-agents-mcp`): Antigravity starts it by that path.

## Configure Antigravity

Antigravity reads MCP servers from an `mcp_config.json` file: `~/.gemini/config/mcp_config.json` for every project, or `.agents/mcp_config.json` in a workspace for that workspace alone. In the editor, the agent panel's menu opens it under **Manage MCP Servers**, then **View raw config**. Add a `kube-agents` entry to its `mcpServers` object:

```json
{
  "mcpServers": {
    "kube-agents": {
      "command": "/Users/you/go/bin/kube-agents-mcp",
      "args": [],
      "env": {
        "PATH": "/Users/you/google-cloud-sdk/bin:/usr/local/bin:/usr/bin:/bin"
      },
      "timeoutSeconds": 360
    }
  }
}
```

Replace both paths with your own. The entry carries no token: the bridge reads it from the cluster.

- **`command`** is the bridge's full path.
- **`env.PATH`** must include the directory of any credential plugin your kubeconfig runs, such as `gke-gcloud-auth-plugin` for a GKE context. An editor started from the desktop does not always inherit your shell's `PATH`, and without the plugin every cluster call fails.
- **`timeoutSeconds`** raises Antigravity's per-call limit, 180 seconds by default. A `send_message` call waits up to 150 seconds for an answer by default and 300 at most, plus up to 40 seconds for the gateway to take the turn. If Antigravity gives up on a call and cancels it, the bridge stops the turn in the cluster, so keep this above 340.

Three optional settings go in `env` or `args`:

| Setting                                                  | What it does                                                                                                                                               |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `KUBECONFIG`                                             | The kubeconfig the bridge reads, as for `kubectl`. Point it at a file holding only the contexts you want scanned.                                          |
| `KUBE_AGENTS_MCP_CONFIG`, or `--config <path>` in `args` | The bridge's state file, `~/.config/kube-agents/mcp-config.json` by default. A separate file per workspace keeps each workspace's saved installs apart.    |
| `GH_TOKEN` or `GITHUB_TOKEN`                             | The GitHub token the bridge's `inspect_pr` tool uses to fetch a pull request; without one it falls back to the `github.com` entry in `~/.git-credentials`. |

Save the file, then reload it: type `/mcp` in the agent panel, or refresh the list under **Manage MCP Servers**. The `kube-agents` server should list eight tools.

## What the bridge does for you

On its first call the bridge connects by itself. It scans every context in your kubeconfig for `PlatformAgent` resources, eight contexts at a time, and saves what it finds to its state file. With exactly one install found, it connects to that one. With several, it asks you to name one (see [Several installs](#several-installs-or-namespaces)).

To connect, it reads the token from `<name>-a2a-mcp-token` (key `token`) and holds it in memory; the state file never stores it. It then opens a port-forward to the pod behind the `<name>-a2a-mcp` Service, inside its own process, through the Kubernetes API server. Nothing listens on a local port, so you do not run `kubectl port-forward`, and no other process on the machine can reach the server through the bridge. When the pod is replaced, the bridge finds the new one on the next call.

What stays with you: the RBAC above, the server turned on, a working kubeconfig context, and picking an install when there are several.

## A first session

Ask Antigravity's agent to use the `kube-agents` tools, or name a tool directly. These are the ones to start with.

**`connection_status`** connects if needed and returns a table titled **kube-agents A2A bus status**. It names the install and its cluster and the bus endpoint and identity, then lists any problems. It also shows the size of the four message streams, whether the gateway and the Platform Agent are consuming, and the agents registered on the directory. A healthy install shows no **Problems** section.

**`send_message`** sends one message to the install as a console user, the same path the A2A console's chat pane takes, so the install answers it or files kanban cards as for any chat message. Try the install check from [First prompts](/kube-agents/install/first-prompts/):

```text
Use the kube-agents send_message tool to ask: Which GKE clusters do you manage, and can you reach each one?
```

The answer names the conversation and its token, quotes any gateway notices, and gives the turn's task and its answer. If the turn filed cards, the reports they posted while the call waited follow under **Card reports on this turn**. Each is quoted as the reporting agent's text. A later message on the same `conversation` continues it; the default conversation is `default`.

`wait_timeout_seconds` (default 150, at most 300) bounds the wait for the turn itself. When the wait ends first, the answer says the turn is still running and gives its task as `<addressee>/<task-id>`. Once the turn ends, the call waits about 15 seconds more for card reports and then returns, whatever the wait setting, so a card that takes longer is not in the answer.

**`task_control`** follows that task. `get` with `task_id` reads its state and answer, and `wait_timeout_seconds` waits for it to finish. `stop` stops a conversation's running task: pass `conversation`, or a `task_id` the bridge started in this session and is still running. A stop ends the turn; a card the turn already filed can still finish. If the host cancels a running `send_message` call, the bridge sends the stop for you.

The other tools are `read_topics` (the install's standing topics and agent cards) and `manage_session` (named conversations and `/session` workers). There is also `discover_deployments`, which rescans your kubeconfig, and `connect_deployment`, which connects to a named install. `inspect_pr` fetches a pull request the agent filed and writes its diff to local files. The [MCP server reference](/kube-agents/reference/a2a-mcp/#the-tools) describes what each one does in the cluster.

Card reports that arrive after a `send_message` call returns are not shown by this bridge, which does not offer the server's `poll_events` tool. A question the install hands to a card, which is common for anything that reads a cluster, then comes back as a short acknowledgement such as "checking namespaces on the host cluster." A later `status` message on the same conversation lists the cards and whether each is done, but not what they found.

## Troubleshooting

The bridge answers a failed call with an error that names the tool, followed by the cause quoted below. It writes warnings to standard error. To see both outside Antigravity, run the bridge in a terminal with the same environment and send it a status call:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"connection_status","arguments":{}}}' \
  | kube-agents-mcp
```

### No install found

`not connected to a kube-agents deployment: could not scan any kubeconfig context: <context>: <cause>` means no context could be scanned. A cause of `listing PlatformAgents: ...` is usually missing cluster-wide `list` on `platformagents`, or a credential plugin missing from `PATH`. `no kubeconfig file found (looked for ...)` means the bridge found no kubeconfig at all; set `KUBECONFIG` in the entry's `env`.

`no deployments configured or discovered` means every context was scanned and none holds a `PlatformAgent`.

### Connected to the install, but the server is missing

`fetching MCP token Secret <namespace>/<name>-a2a-mcp-token: ... not found` means the MCP server is not turned on for that install, or the operator has not rendered it yet. `... forbidden` means your account lacks `get` on the Secret. The bridge connects to any install it finds, including `today` installs and ones without the server, so with several installs make sure the one it picked is the one you turned on.

### Port-forward errors

`finding the pod behind service <namespace>/<name>-a2a-mcp: none of the <n> pod(s) matching <selector> is running and not terminating: ...; retry once the replacement pod is running` appears during a rollout; retry after the new pod starts. `finding the pod behind service ...: get service: ...; no pods match ...` means the Service or its pod is missing, or you lack `get` on the Service or `list` on pods. `opening port-forward to pod <namespace>/<pod>: ...` with a forbidden error means you lack `create` on `pods/portforward`.

The bridge's port-forward does not go stale: a call that fails on a dropped connection closes it, and the next call opens a new one to whichever pod then runs. If calls keep failing after the pod is back, restart the server from `/mcp`. A forward you started yourself with `kubectl port-forward` is separate and not used by the bridge, unless you set a direct URL in its state file (`directMcpUrl` on a saved deployment). That forward goes stale on every pod restart, and you restart it by hand.

### Several installs or namespaces

With more than one install saved and none chosen, a call fails with `more than one saved deployment matches: no deployment is active; name one of <a>, <b>`. Run `discover_deployments` to list the saved names, then `connect_deployment` with one of them. The choice is saved, and later sessions reconnect to it.

A saved name is the install's `spec.harness.clusterName`, or the `PlatformAgent` name when that is empty. Two installs with the same name in one context are saved as `<namespace>/<name>`. When another context holds an install of the same name, the bridge saves it as `<context>/<name>`. `connect_deployment` also matches a cluster name, `PlatformAgent` name, project ID or kube context, and a part of one when only one install matches.

To pin a workspace to one install for good, give its entry a `KUBECONFIG` holding only that cluster's context, or its own state file, and connect once. The `kube_context` and `namespace` arguments of `connect_deployment` override the saved target for that connection only. They are not saved and do not change the active install.

### The token goes nowhere

You do not need `KUBE_AGENTS_MCP_TOKEN` when your account can read the token Secret. Set it in the entry's `env` only when someone gave you the token instead of `get` on the Secret. Setting it puts the token in the file in plain text. The bridge then sends it under strict rules, and logs one warning when it does not:

- With one install saved and `KUBE_AGENTS_MCP_TOKEN_DEPLOYMENT` unset, the token goes to that install.
- With several saved, set `KUBE_AGENTS_MCP_TOKEN_DEPLOYMENT` to the exact saved name the token belongs to, as `discover_deployments` shows it. Unset, the warning is `KUBE_AGENTS_MCP_TOKEN is not sent to any deployment: more than one is configured, and KUBE_AGENTS_MCP_TOKEN_DEPLOYMENT is not set to name one of them`.
- A value that names no saved install sends the token nowhere, with the warning `... KUBE_AGENTS_MCP_TOKEN_DEPLOYMENT is not a saved deployment key; set it to the exact key discover_deployments shows`. This happens after the bridge renames an install to `<context>/<name>` or forgets it, so update the value then.
- A `connect_deployment` call that overrides `kube_context` or `namespace` never gets the token, with the warning `KUBE_AGENTS_MCP_TOKEN is not sent: the connection overrides its deployment's saved target`. The bridge reads that target's own Secret instead.

When the token is set and belongs to the install, the bridge does not read the Secret. A wrong or old value fails every call with `a2a-mcp HTTP 401: unauthorized`.

### Calls fail after the token is rotated

The bridge reads the Secret when it connects and keeps the token for the session. After the token is rotated, calls fail with `a2a-mcp HTTP 401: unauthorized` until the bridge connects again. Run `connect_deployment` for the install, or restart the server from `/mcp`.

### Unsupported options

Some arguments the bridge describes need a newer in-cluster server and are refused with a message saying so. Examples are `send_message` modes other than `auto`, `task_control` `list`, `status` and `steer`, and `manage_session` `transcript` and `clear_killed`. Start a `/session` worker with `manage_session` `session_on`, and read a task with `task_control` `get`.
