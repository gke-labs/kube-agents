---
title: Web Console
description: A browser chat page for the agent on installs with no chat platform, reached through kubectl port-forward.
sidebar:
  order: 9
---

The web console is a chat page for the agent that runs inside the cluster. It is for installs where Slack or Google Chat is not set up yet — an evaluation cluster, a workshop seat, a demo — so you can talk to the agent from a browser. It is off by default.

## What it does

Each browser tab opens its own agent session. A message you type reaches the Planning Agent, the same front door a Slack or Google Chat message reaches. It answers directly, or it files a kanban card and hands the work to the Platform Agent. A turn that calls tools can take several minutes; the console waits up to five minutes for a reply. When the agent proposes a change, it opens a pull request, as it does from any other chat surface.

Work handed to a card finishes after the turn's reply is already on the page. The card's result lands on the same session as a new message, and the page checks for new messages every ten seconds between turns. It shows them tagged `update`. The page remembers what it has shown for as long as the tab is open, so a reload does not repeat them.

The side panel lists the agent's recent sessions by title, including the triage sessions the event watcher opens. Each entry shows the session's title, its source, its message count and when it was last active. It does not show messages, but Hermes writes most titles from a session's first exchange, so a title can summarize what someone asked in Slack or Google Chat. You cannot open or post into a session the console did not create.

## How it is reached

The console is reachable through `kubectl port-forward` and no other way. The chart renders its Service as `ClusterIP`, and a NetworkPolicy refuses every connection to the console pod from the pod network. The policy takes effect only on a cluster that enforces NetworkPolicy, which clusters the installer creates do. A port-forward enters from the node, which NetworkPolicy does not govern. In practice, whoever holds `pods/portforward` on the install namespace can use the console.

The console holds the agent's API key, from the same Secret the agent reads, and attaches it to the requests it sends the agent. The key does not reach the browser. There is no login page and no per-user identity: every turn reaches the agent as the console.

Do not expose the console through a LoadBalancer, an Ingress or a NodePort. Anyone who could reach it would be able to drive the agent. The chart does not offer a way to change the Service type, and apart from its health check the console refuses any request whose `Host` is not `localhost`, `127.0.0.1` or `::1`.

## Turning it on

Pass `--enable-web-console` to the installer, on a new install or a re-run of an existing one:

```bash
./install.sh --enable-web-console
```

On a first install, the installer writes the choice into the `install.env` it creates, as `WEB_CONSOLE_ENABLED=true`, so later runs keep it. The installer never rewrites an existing `install.env`. On a re-run, the flag applies to that run only, and the installer warns you: set `WEB_CONSOLE_ENABLED=true` in `install.env` yourself, or the next `install.sh`, `upgrade.sh` or `--menu` run removes the console again. `--enable-web-console=false` and `WEB_CONSOLE_ENABLED=false` turn it off. If you drive the Terraform composition in `terraform/examples/full-install` directly, set `web_console_enabled = true`.

The console needs the Platform Agent; the chart refuses to render it on an install with `platformAgent.enabled: false`. Its image follows the agent's image tag and the install's image registry, so a mirrored install pulls it from the mirror.

When the install finishes, the installer prints the command to reach the console. Its Service is `kube-agents-web-console` in the install namespace, `kubeagents-system` by default:

```bash
kubectl port-forward -n kubeagents-system svc/kube-agents-web-console 8080:8080
```

Then open `http://localhost:8080`. Both commands assume the chart's default `webConsole.service.port`, 8080. The console pod does not run under gVisor, so this works on a sandboxed install as well.

## What to expect when something is wrong

The badge in the header shows whether the console can reach the agent. It reads "Agent unreachable" while the agent pod is starting. A failed turn shows its error in the chat, including the agent's own message when the agent returned one. The console does not fall back to answering from the model directly, so every reply in the page came from the agent: the Planning Agent's answer, or the result of a card it handed to the Platform Agent.

If you send a second message before the first has been answered, the console refuses it. Wait for the reply, then send again.
