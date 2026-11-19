---
title: Using kube-agents over MCP
description: Connect Claude Code, the Antigravity CLI or the Antigravity editor to an install through the MCP server and the kube-agents-mcp bridge, including reaching a private cluster, then send a first message and fix the common connection errors.
---

This guide connects an MCP host on your workstation to a kube-agents install, so the agent in your terminal or editor can ask the install questions, hand it work and read the answers. It covers [Claude Code](#claude-code), the [Antigravity CLI](#antigravity-cli) and the [Antigravity editor](#antigravity-editor); any MCP host that starts local servers over standard input and output works the same way. It replaces [Using kube-agents from Antigravity](/kube-agents/install/antigravity-mcp/), which now points here.

The path has two pieces. The [MCP server](/kube-agents/reference/a2a-mcp/) runs in the cluster beside the A2A console. The `kube-agents-mcp` bridge runs on your workstation as a local MCP server that your host starts. The bridge finds the install through your kubeconfig, reads the server's token and reaches it over a port-forward of its own, so the only host setting it needs is the path to the binary. Everything up to [Configure your MCP host](#configure-your-mcp-host) is the same for every host.

## Before you start

You need four things:

- **An installed `PlatformAgent`.** The MCP server is a client of the install's message bus. Check with `kubectl get platformagent -A`.
- **The MCP server turned on.** It is off by default; the next section turns it on.
- **A kubeconfig context for the cluster** the install runs on, which works from a terminal on the same machine. [Reach the cluster](#reach-the-cluster) checks it, including for a private cluster.
- **Go 1.27 or later**, to build the bridge, on Linux or macOS. The bridge does not build for Windows yet.

## Turn on the MCP server

The server has no `PlatformAgent` field. It is rendered while the operator's controller manager runs with the environment variable `A2A_MCP_DOOR=true`, and the chart value `operator.mcpServer.enabled: true` sets that variable. On an install made with `install.sh`, that is one `TF_VAR_extra_helm_values` line in `install.env`, followed by an `install.sh` or `upgrade.sh` run:

```bash
TF_VAR_extra_helm_values='{operator={mcpServer={enabled=true}}}'
```

The setting applies to every `PlatformAgent` the operator manages. If the line already sets other values, add `mcpServer={enabled=true}` inside its `operator` object, since there is one such line. On a bare Helm install, pass `--set operator.mcpServer.enabled=true` or set it in your values file. An install that turned the server on with an `A2A_MCP_DOOR` entry in `operator.extraEnv` keeps working, and that entry wins over the value.

Once the operator has restarted and reconciled, the install's namespace holds five objects named after the `PlatformAgent` (`platform-agent` on a default install). They are the server's Deployment, Service and ServiceAccount, its token Secret, and a NetworkPolicy that denies it all in-cluster ingress; a port-forward is not affected:

```bash
kubectl -n kubeagents-system get deployment,service platform-agent-a2a-mcp
kubectl -n kubeagents-system get secret platform-agent-a2a-mcp-token
kubectl -n kubeagents-system get networkpolicy platform-agent-a2a-mcp-netpol
```

If they do not appear, read the `A2AMCPServer` condition on the `PlatformAgent` ([status conditions](/kube-agents/operator/platformagent-crd/#status)). It is set when a ServiceAccount or Secret of those names already exists and the `PlatformAgent` does not own it.

## Grant your account access

The bridge acts with your kubeconfig credentials. Each person who connects needs these permissions on the install's cluster:

| Permission                                                                                                | Where                  | Why                                                        |
| --------------------------------------------------------------------------------------------------------- | ---------------------- | ---------------------------------------------------------- |
| `list` on `platformagents.kubeagents.x-k8s.io`                                                            | Cluster-wide           | Finding the install; the bridge lists across namespaces.   |
| `list` on `agentprofiles.kubeagents.x-k8s.io`                                                             | Cluster-wide, optional | Showing the install's profiles; without it they are blank. |
| `get` on the Secret `<name>-a2a-mcp-token`                                                                | The install namespace  | Reading the bearer token.                                  |
| `list` on `pods`, `create` on `pods/portforward`, and (recommended) `get` on the Service `<name>-a2a-mcp` | The install namespace  | Opening the port-forward to the server's pod.              |

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

## Reach the cluster

The bridge talks to the install the way `kubectl` does: through the Kubernetes API server named by a kubeconfig context, with your credentials. Its port-forward to the MCP server rides on that same connection. So before you configure any host, the context has to work from a terminal on the same machine. These commands prove it, the last one checking the permission the port-forward needs:

```bash
kubectl --context <context> get platformagent -A
kubectl --context <context> -n kubeagents-system get service platform-agent-a2a-mcp
kubectl --context <context> -n kubeagents-system auth can-i create pods/portforward
```

When all three work, skip to [Build the bridge](#build-the-bridge).

### Reaching a private cluster

A cluster whose control plane has no public endpoint needs a path to its API server first. Pick one of these, get a context through it, and run the three checks above against that context. The bridge needs nothing more: it uses the context as `kubectl` does.

Some paths carry ordinary API calls but not port-forwards. Before you configure the bridge, check that the path carries one. The bridge's port-forward uses SPDY, while recent `kubectl` tries WebSockets first, so the check turns that off to test the same protocol. It should print `Forwarding from 127.0.0.1:18089`; stop it with Ctrl-C.

```bash
KUBECTL_PORT_FORWARD_WEBSOCKETS=false kubectl --context <context> -n kubeagents-system port-forward service/platform-agent-a2a-mcp 18089:8089
```

- **DNS-based endpoint (GKE).** A cluster with the control plane's DNS-based endpoint turned on, and external traffic allowed on it, is reached by name, with access decided by IAM rather than by network. Get a context for it with `gcloud container clusters get-credentials <cluster> --location <location> --dns-endpoint`, then run the checks.
- **Connect gateway (GKE fleets).** A cluster registered to a fleet is reached through Google's connect gateway. Get a context with `gcloud container fleet memberships get-credentials <membership> --location <location>`, then run the checks, the port-forward one included.
- **A bastion or an IAP tunnel.** With neither of the above, get a context whose server is the control plane's private address (`gcloud container clusters get-credentials <cluster> --location <location> --internal-ip`), run a forward proxy on a host inside the cluster's network, and reach that proxy through a tunnel, for example `gcloud compute ssh <bastion> --tunnel-through-iap --zone <zone> -- -L 8888:localhost:8888 -N`. Then point the context at the proxy with `proxy-url: http://localhost:8888` under its `cluster` entry in the kubeconfig, and run the checks while the tunnel is open.

The bridge reads the same kubeconfig settings as `kubectl`, a `proxy-url` included, so a context that passes the checks works for the bridge too. Keep any tunnel open while your host runs the bridge.

## Build the bridge

The bridge is a Go program in the `a2a` module of the kube-agents repository. Install it into `$(go env GOPATH)/bin`:

```bash
go install github.com/gke-labs/kube-agents/a2a/cmd/kube-agents-mcp@main
```

The module needs Go 1.27; an older `go` command fetches the right toolchain by itself unless `GOTOOLCHAIN=local` is set. To build from a clone instead, run `go build -o kube-agents-mcp ./cmd/kube-agents-mcp` in its `a2a` directory. Check the binary:

```bash
kube-agents-mcp --version
```

It prints `kube-agents` and the bridge's version. Note the binary's full path (`command -v kube-agents-mcp`): your host starts it by that path.

## Configure your MCP host

### Claude Code

Add the bridge once at user scope, so it is there in every project:

```bash
claude mcp add kube-agents --scope user \
  -e PATH="$HOME/google-cloud-sdk/bin:/usr/local/bin:/usr/bin:/bin" \
  -- "$(command -v kube-agents-mcp)"
```

Then check it:

```bash
claude mcp list
```

It should show `kube-agents: <path to kube-agents-mcp> - ✔ Connected`. Inside a session, `/mcp` lists the server and its tools and reconnects it.

- **`PATH`** must include the directory of any credential plugin your kubeconfig runs, such as `gke-gcloud-auth-plugin` for a GKE context. Claude Code passes the server the environment it was started with plus the `-e` values, so your `HOME` reaches the bridge as well; credential plugins read their state from it.
- **Other settings** go in further `-e` flags: `KUBECONFIG` and `KUBE_AGENTS_MCP_CONFIG`, or the bridge's `--config <path>` after the `--`. See the table in [Antigravity editor](#antigravity-editor).
- **Timeouts:** Claude Code's default limit on a tool call is longer than a `send_message` call can take, so no setting is needed. If you set `MCP_TOOL_TIMEOUT` yourself, keep it above 340000 (milliseconds), for the reason given under `timeoutSeconds` below.

To share the setup with a team, add it at project scope instead (`--scope project`). That writes a `.mcp.json` file at the project's root, which you can commit; Claude Code asks each person to approve the server the first time they open the project:

```json
{
  "mcpServers": {
    "kube-agents": {
      "type": "stdio",
      "command": "/Users/you/go/bin/kube-agents-mcp",
      "args": [],
      "env": {
        "PATH": "/Users/you/google-cloud-sdk/bin:/usr/local/bin:/usr/bin:/bin"
      }
    }
  }
}
```

Replace the paths with your own. As in every host, the entry carries no token: the bridge reads it from the cluster.

### Antigravity CLI

Add the bridge with `agy mcp add`. Flags come before the server's name:

```bash
agy mcp add -e PATH="$HOME/google-cloud-sdk/bin:/usr/local/bin:/usr/bin:/bin" \
  kube-agents "$(command -v kube-agents-mcp)"
agy mcp list
```

`agy mcp list` should show `kube-agents` as an enabled stdio server. The `PATH` advice is the same as for [Claude Code](#claude-code); add `KUBECONFIG` or `KUBE_AGENTS_MCP_CONFIG` with further `-e` flags.

A run in print mode (`agy --print`) cannot ask you to approve a tool, so it denies every kube-agents call unless an allow rule names it. Pass the rules for the session with `--settings`, one `mcp(<server>/<tool>)` entry per tool:

```bash
agy --print "Call the kube-agents connection_status tool." \
  --settings '{"permissions":{"allow":["mcp(kube-agents/connection_status)","mcp(kube-agents/send_message)","mcp(kube-agents/task_control)","mcp(kube-agents/poll_events)"]}}'
```

:::caution
The Antigravity CLI asks the bridge for step-by-step results, and with `agy` 1.5.12 a `send_message` call then never returns its answer, although the install completes the turn. Until that is fixed, ask for the plain result: have the agent pass `stream_steps: false` to `send_message`, for example "Use the kube-agents send_message tool with stream_steps false to ask: …". The call then returns the turn's answer as in other hosts.
:::

### Antigravity editor

This configuration has not yet been checked against a live install from the editor; the bridge side is the same as for the other hosts.

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

| Setting                                                  | What it does                                                                                                                                                                                      |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `KUBECONFIG`                                             | The kubeconfig the bridge reads, as for `kubectl`. Point it at a file holding only the contexts you want scanned.                                                                                 |
| `KUBE_AGENTS_MCP_CONFIG`, or `--config <path>` in `args` | The bridge's state file, `~/.config/kube-agents/mcp-config.json` by default. A separate file per workspace keeps each workspace's saved installs apart.                                           |
| `GH_TOKEN` or `GITHUB_TOKEN`                             | The GitHub token the bridge's `inspect_pr` tool uses to fetch a pull request; without one it falls back to the `github.com` entry in `~/.git-credentials`, or to the `gh` CLI if it is on `PATH`. |

Save the file, then reload it: type `/mcp` in the agent panel, or refresh the list under **Manage MCP Servers**. The `kube-agents` server should list nine tools.

## What the bridge does for you

On its first call the bridge connects by itself. It scans every context in your kubeconfig for `PlatformAgent` resources, eight contexts at a time, and saves what it finds to its state file. With exactly one install found, it connects to that one. With several, it asks you to name one (see [Several installs](#several-installs-or-namespaces)).

To connect, it reads the token from `<name>-a2a-mcp-token` (key `token`) and holds it in memory; the state file never stores it. It then opens a port-forward to the pod behind the `<name>-a2a-mcp` Service, inside its own process, through the Kubernetes API server. Nothing listens on a local port, so you do not run `kubectl port-forward`, and no other process on the machine can reach the server through the bridge. When the pod is replaced, the bridge finds the new one on the next call.

What stays with you: the RBAC above, the server turned on, a working kubeconfig context, and picking an install when there are several.

## A first session

Ask your host's agent to use the `kube-agents` tools, or name a tool directly. These are the ones to start with.

**`connection_status`** connects if needed and returns a table titled **kube-agents A2A bus status**. It names the install and its cluster and the bus endpoint and identity, then lists any problems. It also shows the size of the four message streams, whether the gateway and the Platform Agent are consuming, and the agents registered on the directory. A healthy install shows no **Problems** section.

**`send_message`** sends one message to the install as a console user, the same path the A2A console's chat pane takes, so the install answers it or files kanban cards as for any chat message. Try the install check from [First prompts](/kube-agents/install/first-prompts/):

```text
Use the kube-agents send_message tool to ask: Which GKE clusters do you manage, and can you reach each one?
```

The answer names the conversation and its token, quotes any gateway notices, and gives the turn's task and its answer. If the turn filed cards, the reports they posted while the call waited follow under **Card reports on this turn**. Each is quoted as the reporting agent's text. A later message on the same `conversation` continues it; the default conversation is `default`.

To send the turn straight to an AgentProfile instead, name it in `addressee` and pass its task parameters in `parameters`, an object of string values. For the `cluster` profile, the parameters are `project`, `location` and `cluster`. `read_topics` with `action: cards` lists the profiles' agent cards. The gateway refuses an unknown profile, a tier the profile doesn't run at, or a target outside the install's scope, and its notice is the answer.

`wait_timeout_seconds` (default 150, at most 300) bounds the wait for the turn itself. When the wait ends first, the answer says the turn is still running and gives its task as `<addressee>/<task-id>`. Once the turn ends, the call keeps collecting card reports until none has arrived for 15 seconds or the wait runs out, whichever comes first, and returns at once when the turn handed no work to cards. A card that takes longer than that is not in the answer.

**`task_control`** follows that task. `get` with `task_id` reads its state and answer, and `wait_timeout_seconds` waits for it to finish. `stop` stops a conversation's running task: pass `conversation`, or a `task_id` the bridge started in this session and is still running. A stop ends the turn; a card the turn already filed can still finish. If the host cancels a running `send_message` call, the bridge sends the stop for you.

The other tools are `read_topics` (the install's standing topics and agent cards) and `manage_session` (named conversations and `/session` workers). There is also `discover_deployments`, which rescans your kubeconfig, and `connect_deployment`, which connects to a named install. `inspect_pr` fetches a pull request the agent filed and writes its diff to local files. The [MCP server reference](/kube-agents/reference/a2a-mcp/#the-tools) describes what each one does in the cluster.

A question the install hands to a card, which is common for anything that reads a cluster, can come back first as a short acknowledgement such as "checking namespaces on the host cluster." The card's report arrives after `send_message` has returned. The answer ends with the `conversation_token` and `since` cursor to read it with: call `poll_events` with those two, and it returns the reports posted since that cursor. Set `wait_seconds` (60 at most) to have it wait for a report when none has arrived yet, and pass the cursor it returns on the next call. A `status` message on the same conversation lists the cards and whether each is done, but not what they found.

To answer a report, send the next message with `reply_to` set to the task its heading gave as `on task`; the agent then reads that report before answering. That read happens on the [platform route](/kube-agents/concepts/chatops/#session), the install's default; a conversation on its own session pod does not read it yet.

## Troubleshooting

The bridge answers a failed call with an error that names the tool, followed by the cause quoted below. It writes warnings to standard error. To see both outside your host, run the bridge in a terminal with the same environment and send it a status call. The bridge stops, and cancels the call it is making, as soon as its input closes, so the `sleep` keeps the input open while it works:

```bash
( printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"connection_status","arguments":{}}}'
  sleep 60 ) | kube-agents-mcp
```

Without the `sleep`, a call against a real cluster fails with `context canceled`.

### No install found

`not connected to a kube-agents deployment: could not scan any kubeconfig context: <context>: <cause>` means no context could be scanned. A cause of `listing PlatformAgents: ...` is usually missing cluster-wide `list` on `platformagents`, or a credential plugin missing from `PATH`. `no kubeconfig file found (looked for ...)` means the bridge found no kubeconfig at all; set `KUBECONFIG` in the entry's `env`.

`no deployments configured or discovered` means every context was scanned and none holds a `PlatformAgent`. When some contexts could not be scanned and the rest hold none, the error is `no kube-agents deployment found in the kubeconfig contexts that were scanned; could not scan <context>: <cause>`.

### Connected to the install, but the server is missing

`fetching MCP token Secret <namespace>/<name>-a2a-mcp-token: ... not found` means the MCP server is not turned on for that install, or the operator has not rendered it yet. `... forbidden` means your account lacks `get` on the Secret. The bridge connects to any install it finds, including ones without the server, so with several installs make sure the one it picked is the one you turned on.

### Port-forward errors

`finding the pod behind service <namespace>/<name>-a2a-mcp: none of the <n> pod(s) matching <selector> is running and not terminating: ...; retry once the replacement pod is running` appears during a rollout; retry after the new pod starts. `finding the pod behind service ...: get service: ...; no pods match ...` means the Service or its pod is missing, or you lack `get` on the Service or `list` on pods. `opening port-forward to pod <namespace>/<pod>: ...` with a forbidden error means you lack `create` on `pods/portforward`.

The bridge's port-forward does not go stale: a call that fails on a dropped connection closes it, and the next call opens a new one to whichever pod then runs. If calls keep failing after the pod is back, restart the server from your host (`/mcp` in Claude Code and in Antigravity). A forward you started yourself with `kubectl port-forward` is separate and not used by the bridge, unless you set a direct URL in its state file (`directMcpUrl` on a saved deployment). That forward goes stale on every pod restart, and you restart it by hand.

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

The bridge reads the Secret when it connects and keeps the token for the session. After the token is rotated, calls fail with `a2a-mcp HTTP 401: unauthorized` until the bridge connects again. Run `connect_deployment` for the install, or restart the server from your host.

### Unsupported options

Some arguments the bridge describes need a newer in-cluster server and are refused with a message saying so. Examples are `send_message` modes other than `auto`, `task_control` `list`, `status` and `steer`, and `manage_session` `transcript` and `clear_killed`. Start a `/session` worker with `manage_session` `session_on`, and read a task with `task_control` `get`.
