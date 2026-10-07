---
title: Web Console
description: A browser page for the agent on installs with no chat platform, laid out as channels and threads, reached through kubectl port-forward.
sidebar:
  order: 9
---

The web console is a browser page for the agent that runs inside the cluster. It is for installs where Slack or Google Chat is not set up yet, such as an evaluation cluster, a workshop seat or a demo. It is off by default.

## The layout

The page has a rail on the left, a centre pane, and a right pane you can close. A banner above the two panes shows the model, the token and cost counters, and the cluster.

The rail lists two channels and your threads:

- `# alerts` has one post per incident the event watcher handed to the agent. Each post is the event triage session the watcher opened for that incident.
- `# scheduled` has one post per scheduled job per UTC day. Every run of a job on the same day goes into that day's session.
- **Threads** are your own conversations with the agent. **New chat** starts one.
- **Chat** lists Slack and Google Chat sessions by title, taken from the agent's 20 most recent sessions. It appears only when there are any, and its rows cannot be opened.

A channel shows up to 50 posts, chosen from the agent's 200 most recently active sessions that were created through the gateway API. The most recent post is at the bottom. A session becomes a post only when its ID has the exact shape the event watcher or the scheduler gives it and its title is exactly `Triage <id>`. A session someone else created with a similar title does not appear.

Each post shows its subject, the agent's latest reply, the reply count and when it was last active. For an event triage post, the subject is the card title the triage prompt names, such as `Triage shop/Pod/web-7 (BackOff) on prod`. The reply count covers the session's newest 50 messages, so on a longer session it is a lower bound. A session the agent has not started yet holds no messages. Its post reads "Event <id>: the agent has not started on this yet" (or "Check <id>" in `# scheduled`) in grey, with no reply count. If you scroll up and a post arrives, a **New posts** button appears instead of moving the feed.

The reply shown on an `# alerts` post is often a short routing line. The session holds the Planning Agent's turn, which files a kanban card for the cluster's specialist. The diagnosis itself runs on that card, and its report reaches the session only when the card's result lands there.

**View thread** opens the post's session in the right pane. The first message in an `# alerts` or `# scheduled` thread is the prompt the event watcher or scheduler sent to the agent. The pane shows that message as an **Event watcher** or **Scheduler** card with the resource, reason and warning message lifted out, and keeps the full routing prompt folded under **Show routing prompt**. When a specialist agent completes a kanban card subscribed to the thread, its `[kanban] Task ...` wake message appears as a **Specialist report** card naming the task ID, the shortened specialist name (for example `Cluster agent · prod`) and the one-line summary from the card's completion. You can reply in the pane. The reply goes into that session, and the agent answers in it. **About this agent** opens in the same pane. It shows the model, the cluster, the agent's Kubernetes service account, and the Google Cloud service account and the roles it was granted on the host project.

## Threads

Each thread is one agent session. A message you type reaches the Planning Agent, the same front door a Slack or Google Chat message reaches. It answers directly, or it files a kanban card and hands the work to the Platform Agent. A turn that calls tools can take several minutes. The console follows a streamed turn for up to 15 minutes. On the plain fallback route, it waits up to five minutes for a reply. When the agent proposes a change, it opens a pull request, as it does from any other chat surface.

Several threads can run turns at once. You can send in one thread and switch to another while it works. A thread's title is its first message.

Work handed to a card finishes after the turn's reply is already on the page. The card's result lands on the same session as a new message. One loop checks every thread for new messages every ten seconds. A result for the thread on screen appears tagged `update`. A result for another thread gives that thread an unread dot in the rail.

The thread list is kept in the browser's local storage. It survives a reload and is shared by the tabs of that browser. It holds at most 12 threads and drops the least recently used. Removing a thread with its × button removes it from this list only; the agent keeps the session. Threads opened in another browser are not listed.

## Live status

While a turn runs, a single line under it shows what the agent is doing and how long the turn has been running: thinking, running a named tool, waiting for the model (when three seconds pass between steps without a new event), or writing the reply. Below that line, a step list records each thinking excerpt and tool call (`running`, `done`, or `failed`, up to 50 steps per turn) with Hermes' short preview of the tool's arguments, and streams the reply text as `delta` chunks arrive. When the turn finishes, the step list folds into a **Worked for N s · M steps** summary above the reply. Reasoning lines stop once the reply starts. Full tool arguments and tool output are not forwarded.

If you close or reload the page during a streamed turn, the console keeps reading the agent's stream to its end, so the agent finishes the turn. When you come back, the reply appears in the thread once it lands. If the page loses the stream while it stays open, it says the agent is still finishing the turn, and the reply arrives the same way. A turn still running after 15 minutes is stopped: the console closes the stream, the agent interrupts the run, and the pane shows that the run was interrupted.

The page reads the line from `POST /api/chat/stream`, which relays the agent's event stream. If that route is missing, as on an older console, the page uses `POST /api/chat` and shows a plain "working" line until the reply.

## Unread counts and notifications

The rail shows an unread count for each channel and bolds a thread with news. A post counts as unread when it is new or has more messages than when you last saw it. Seen state is kept per post in local storage. On a first visit, every existing post is marked seen. The page title shows the total, for example `(3) kube-agents`.

The page sends browser notifications for new `# alerts` posts only after you click **Turn on notifications** in the rail. It never asks for permission on its own. It notifies only while the tab is in the background. Each check sends at most three post notifications, plus one that counts the rest.

## The banner

The model and provider come from the chart's LiteLLM settings at install. The token and cost counters are read from LiteLLM's own metrics, so read them with these limits in mind:

- The counters reset when a LiteLLM pod restarts. The banner says "since LiteLLM last restarted".
- They are summed across LiteLLM replicas. The console finds the replicas through a headless Service and reads each one, up to 32. When a replica does not answer, the banner says how many it read, for example "1 of 2 replicas", and the total is low.
- The cost is LiteLLM's estimate from its own price table, not your bill.
- An install without LiteLLM shows "Not available".

## Agent identity

The identity in **About this agent** is recorded at install. The Terraform composition passes the Google Cloud service account and the roles it granted on the host project into `webConsole.agentIdentity`. Access granted in other projects or folders, as described in [Multiple GCP projects](/kube-agents/deploy/multi-project/), is not listed. The console does not read IAM. A role granted or removed later is not shown until the next install or upgrade updates the values. A Helm-only install that does not set `webConsole.agentIdentity` shows "Not recorded at install".

## What it does not show

The Hermes gateway API does not expose the kanban board or a session's place in the work queue. The console cannot show which cards are open, who holds them, or how long a turn will wait. Ask the agent in a thread instead.

## Replies into agent sessions

A reply from the right pane names the event triage or scheduled session itself. Before the turn, the console looks the session up in the agent's session store. It accepts it only when the session was created through the gateway API, and its ID and title match exactly what the event watcher or the scheduler gives it. The console never creates or recreates such a session. A session the agent has no record of is refused with a 404. A Slack or Google Chat session is refused with a 403, and so is any other caller's.

A reply is also refused, with a 409 and the message "The agent is still working on this thread", while another turn looks active on the session. That turn can come from the event watcher, the scheduler or a chat reply. The agent's API does not report whether a session has a run in progress, so the console guesses. It treats the session as busy when it was active in the last 10 minutes and its newest message is not a reply from the agent. Try again once the agent has answered.

## Privacy

The console reads the text of event triage sessions, scheduled checks, and its own threads. An event triage or scheduled session can include replies people posted in the Slack or Google Chat thread where the alert or report went, because those replies continue the same session. Anyone who can open the console can read them. The console never reads the messages of a session that started in Slack or Google Chat. It lists those by title only, and Hermes writes most titles from a session's first exchange, so a title can summarize what someone asked.

This does not widen access much. Opening the console takes `pods/portforward` on the install namespace. The built-in `edit` and `admin` roles that grant it also grant `pods/exec`, which reads the agent's session store directly.

## How it is reached

The console is reachable through `kubectl port-forward` and no other way. The chart renders its Service as `ClusterIP`, and a NetworkPolicy refuses every connection to the console pod from the pod network. The policy takes effect only on a cluster that enforces NetworkPolicy, which clusters the installer creates do. A port-forward enters from the node, which NetworkPolicy does not govern. In practice, whoever holds `pods/portforward` on the install namespace can use the console.

The console holds the agent's API key, from the same Secret the agent reads, and attaches it to the requests it sends the agent. The key does not reach the browser. There is no login page and no per-user identity: every turn reaches the agent as the console. A port-forward carries no identity of the person behind it, so the page cannot show who is signed in.

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

## Routes

The page uses these routes. All of them answer only a loopback `Host`.

| Route                                | What it returns                                                                                                                                                                          |
| :----------------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/channels/{name}/posts`     | Up to 50 posts of `alerts` (event triage) or `scheduled` (scheduled checks), from the agent's 200 most recently active gateway API sessions, with summaries. Any other name is 404.      |
| `GET /api/sessions/{id}/transcript`  | The newest 200 text messages of an event triage, scheduled or console session. An ID with characters no session ID uses is 400, an unknown session is 404, and any other session is 403. |
| `GET /api/sessions/{id}/messages`    | New messages on one of the console's own sessions, after a given message ID.                                                                                                             |
| `GET /api/sessions/recent`           | The agent's 20 most recent sessions with their kind, without reading their messages. The rail's Chat section reads it.                                                                   |
| `GET /api/status`                    | Whether the console can reach the agent gateway. The top bar's badge reads it.                                                                                                           |
| `GET /api/insights`                  | The model, the LiteLLM counters, the identity recorded at install, and the cluster.                                                                                                      |
| `POST /api/chat`, `/api/chat/stream` | One turn, as a single reply or as a stream of status lines then the reply.                                                                                                               |

## What to expect when something is wrong

The badge in the top bar shows whether the console can reach the agent. It reads "Agent unreachable" while the agent pod is starting. A failed turn shows its error in the pane that sent it, including the agent's own message when the agent returned one. The console does not fall back to answering from the model directly, so every reply in the page came from the agent: the Planning Agent's answer, or the result of a card it handed to the Platform Agent.

The console sends one turn at a time into each session. If you send a second message in a thread before the first has been answered, the console refuses it. Wait for the reply, or use another thread. On an event triage or scheduled session, the console also refuses a reply while a turn it did not send looks active, as described under [Replies into agent sessions](#replies-into-agent-sessions). That check is a guess from the session's recent activity, so it can refuse a reply to a session that is idle, or let one through while a turn runs.
