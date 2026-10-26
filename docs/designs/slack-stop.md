# Slack Stop under `next`: end the thread's work, then say what changed

**Status:** step 1 shipped; steps 2 and 3 are designed, not built. Step 2's checked replies marked
approved are the maintainer's wording; the rest are pending. This design covers `spec.mode: next`,
where the Go gateway (`a2a/gateway`) serves Slack. `today`'s Hermes Slack adapter is out of scope.

Slack puts a ■ Stop button on a thread's Working… line. Step 1 made it end the thread's work: the
running turn and every kanban card its turns filed, with one reply. What is left is a reply that
says what changed, "Stopped. I didn't change anything in seeded-a.", which needs a record of every
write a card makes (step 2), and a Stop that also ends the turn still running in Hermes (step 3).

## What runs where under `next`

- **The Slack connection.** The gateway is its own Deployment and holds Slack's Socket Mode
  connection itself (`SlackAdapter.Run`, `a2a/gateway/slack.go`). Its event switch handles
  messages, `assistant_thread_started` and `app_home_opened`, and acks and drops everything else.
  slack-go v0.30.1, the pinned version, already parses `agent_session_stopped` into
  `slackevents.AgentSessionStoppedEvent` (`channel`, `thread_ts`, `user`, `streaming_message_ts`,
  `event_ts`), so the event arrives today, typed, and is dropped.
- **The turn.** The gateway publishes each message as an A2A task. The `hermes-bridge` sidecar in
  the agent pod runs it as a turn on the pod's Hermes API server, in the session `a2a-<contextId>`
  under the Planning Agent's profile (`a2a/docs/hermes-bridge.md`). A cancel ends the bridge's HTTP
  request, but the bridge posts to `/v1/chat/completions` without streaming, and that route
  interrupts its agent only when a _streaming_ client disconnects (`api_server_openai_routes.py`,
  Hermes v2026.9.14). So a turn still running when the user stops keeps running.
- **The cards.** The Planning Agent hands work to Hermes kanban cards, which run as worker
  processes of the agent container. Their posts reach the thread over the gateway's chat.notify
  route, and the plan is drawn by `deploy/docker/patches/slack_ux_status.py` in the Hermes process.
  A card's commands run in the `<agent>-shell-0` sandbox over ssh, credentialed through the broker,
  or as `gke` MCP calls.

**The card stop.** A typed `stop` cancels the conversation's running task and, with the operator
flag `A2A_CARD_STOP` on (off by default), also sends the bridge a `cardStop` control
(`a2a/gateway/cardstop.go`, `a2a/hermes-bridge/cardstop.go`). The bridge asks session-kv's
card-stop route to archive every open card the conversation's turns filed, leaves first, with their
fan-out, and to stop their marked workers; the result names what it stopped
(`lib.CardStopResult`). The gateway words the reply ("🛑 stopped 2 cards: …", or "⚠️ couldn't
confirm …"). `spec-a2a-payloads.md`, "Stopping the cards", is the contract. Step 1 builds Slack's
Stop on this; it adds no second way to stop a card.

The gateway already holds the thread's Agent Session at `processing` from the acknowledgement until
the cards settle (`slackSession.wanted` in `a2a/gateway/slack_sessions.go`). Slack shows Stop only
on a `processing` session, so the button is there while the turn or any card runs. It is not there
while the only open cards wait on the user, when the session is `suspended`; typed `stop` covers
that window.

## Step 1: Stop ends the cards (shipped)

#3577 routes Slack's Stop to the card stop, with the requester rule and the one-reply wording;
`spec-chatops-gateway.md` and the site's ChatOps page describe the behaviour. #3830 adds the eval
case `chat-stop-halts-delegated-remediation`. A live press on 2026-10-26 stopped a running card
with one "Stopped."; a press during the opening turn cancelled the turn, but the turn still filed a
card, which is step 3.

## Step 2: "I didn't change anything in `<target>`"

The approved line is a claim about writes, so it needs a record of them. Cards run in the sandbox,
through the broker and the `gke` MCP server, so the record is:

- **A caller label on every broker request.** The `ssh` wrapper forwards the worker's card id as
  `HERMES_CALLER_LABEL` (`SendEnv`/`AcceptEnv`, `session-command.sh`); `sandbox_exec.py` callers
  send `pod:<caller>`; the broker refuses a shell-role exec, vcs or workspace request with no label
  or a malformed one.
- **A fence.** The card-stop route adds the stopped cards' labels to a broker stopped set on a new
  chat-role-only `/v1/stop/` route; the broker refuses new requests carrying one and kills the
  process group of in-flight ones. A `pre_tool_call` hook refuses a stopped card's MCP call.
- **A ledger and an MCP record.** The broker keeps an in-memory ledger per label of every request
  that can change something outside the pod (vcs `WRITE_VERBS`, workspace `push`, a non-read
  `kubectl` or `gcloud` exec), with its outcome; a `pre_tool_call`/`post_tool_call` plugin records
  non-read `gke` MCP calls the same way.

The reply says "I didn't change anything" only when read-only enforcement was on, the fence
drained, every card terminated, and neither record holds a write or an unknown for any of the
thread's cards over a window the in-memory ledger fully covers. Anything else gets a reply naming
the write, or saying it could not check. `<target>` is the cluster of the Cluster Agent profile a
stopped card ran under; a Platform card names none, and the clause drops.

| Case                                | Reply                                                                                                                           | Approved |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | -------- |
| Checked, nothing changed            | Stopped. I didn't change anything in seeded-a.                                                                                  | yes      |
| Checked, nothing changed, no target | Stopped. I didn't change anything.                                                                                              | yes      |
| A write was found                   | Stopped. Before stopping, I `<opened #412 / pushed a branch / asked GKE to update node pool pool-1>` for seeded-a.              | no       |
| Could not check                     | Stopped. I couldn't check whether anything changed in seeded-a, so look for a pull request from me before you assume it didn't. | no       |
| Read-only enforcement off           | Stopped. Cluster writes are switched on for me, so I can't vouch for seeded-a. Check it before you rely on it.                  | no       |

No reply other than the checked one may contain "didn't change anything", because the eval's phrase
match is case-insensitive.

Files: `deploy/docker/ssh-wrapper.sh`, `deploy/docker/ssh_config.d/`, `deploy/sandbox/sshd_config`
and `session-command.sh`; `credential_proxy.py`, its shims and `ROUTE_ROLES`; `sandbox_exec.py`; a
`gke` MCP hook plugin; the session-kv route. The case gains a "truthfully what changed" objective
and `github_writes` a `since: stop` window, so a write the stop could not prevent is reported rather
than charged. Live validation: stop a card before it writes and read the checked line; stop one
after it opened a pull request and read the write line; restart the broker mid-run and read the
could-not-check line.

## Step 3: interrupt the running turn

Tracked in #3263.

A Stop pressed during the opening turn cancels the bridge's wait while Hermes keeps running the
turn, which can still file a card. A Hermes patch adds a route to the API server, authenticated
with `API_SERVER_KEY`, that finds the session's running agent in `_shutdown_interruptible_agents`
(by `session_id`, following a compressed session's lineage) and calls upstream's
`request_hard_interrupt` and process reap, the pair `/v1/runs/{run_id}/stop` uses; the bridge calls
it before the card stop. Files: a new `deploy/docker/patches/` pair and `a2a/hermes-bridge/`. Live
validation: press Stop during the opening turn and see no card filed.

## Open before the build

- **Which users Slack shows Stop to** in a shared channel thread. The 2026-10-26 press showed the
  button in a channel thread without agent view; the requester gate holds either way.
