---
# Claude Code loads this rule only beside files matching `paths`; Antigravity uses `trigger` and `description`.
paths:
  - "agents/*/SOUL.md"
  - "agents/chat/defaults/**"
  - "agents/platform/scripts/slack_*.py"
  - "agents/platform/scripts/chat_*.py"
  - "agents/platform/scripts/*_presenter.py"
  - "a2a/gateway/slack_*.go"
  - "a2a/gateway/gchat*.go"
  - "a2a/gateway/*chat*.go"
  - "a2a/gateway/relay*.go"
  - "a2a/gateway/busy.go"
  - "a2a/gateway/stopgate.go"
  - "a2a/hermes-bridge/chat*.go"
  - "a2a/lib/chatmsg/**"
  - "deploy/docker/patches/kanban_notifier.py"
  - "deploy/docker/patches/kanban_progress_lines.py"
  - "deploy/docker/patches/kanban_chat_notify.py"
  - "deploy/docker/patches/slack_*.py"
trigger: model_decision
description: "Chat UX rules for anything a user reads in Slack or Google Chat: answer first, named acks, one live status line, no plumbing, buttons for choices, one-line failures, speaking up only when it matters, confirmed destructive actions, and renderers that never compose."
---

# Chat UX rules

[`AGENTS.md`](../../AGENTS.md) names this file. It holds the rules for every word a user reads in
Slack or Google Chat: an agent's reply, a specialist's result, a status line, a button, a notice
the gateway posts. The yardstick is two messages for a simple ask: an acknowledgement, then the
answer. Each rule below says what to check, why, and where it is implemented today. Where a rule
is also an agent's instruction, the `SOUL.md` section is canonical for the agent's wording; this
file does not restate it.

## The answer comes first, as one bold line

The first thing in an answer is one bold line that answers the question. Evidence follows it;
reasoning, tables and long detail go in the fold. A reader who stops after the bold line has the
point.

Why: the answer is read from a notification on a phone, and a reply that opens on context makes
the reader dig for the verdict.

Today: the agents are told so in [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md) §1 ("Keep your
replies short", "Write replies as warm, full sentences that lead with the fact") and in
[`agents/platform/SOUL.md`](../../agents/platform/SOUL.md) §0 ("Short by default") and §7. The
layout is `answer_message` in `agents/platform/scripts/chat_message.py` and its port `answerChat`
in `a2a/hermes-bridge/chat.go`: the first sentence becomes `headline`, cut at a clause to stay
within 150 characters and rendered bold, and a rest too long for `detail` goes under `fold`.

## The acknowledgement names its target

An ack is one short lowercase line naming what is being checked, in the user's words, ending with a
period: `checking checkout-gateway.` It carries no task id, no attribution, no restatement and no
plan. A request to change something is acked by what is being checked, never by the change.

Why: the ack is status, and a specific target is what tells the user they were understood.

Today: [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md) §2 step 4 is the rule. The arrival
reaction is the receipt before any model call: `arrival_reaction` in `slack_presenter.py`, ported
as `askKind` in `a2a/hermes-bridge/chat.go`.

## One live status line, updated in place

Progress is one message per task that is edited as the work moves. A new message for each step is
a violation, and so is a progress line left behind under the answer.

Why: a trail of progress posts buries the answer and notifies the user for every step.

Today: under `next`, the `progress` artifact is "the task's one live message, edited in place"
([`spec-a2a-payloads.md`](../../docs/designs/spec-a2a-payloads.md), "Chat messages"), drawn by
`updateRollingLine` in `a2a/gateway/relay.go`; the queue notice in `a2a/gateway/busy.go` replaces
the placeholder rather than following it. Under `today`, a card's heartbeats share one message
([`agents/platform/SOUL.md`](../../agents/platform/SOUL.md) §0, "Show your progress"), the plan is
one message per thread (`agents/platform/scripts/slack_status.py`), and `kanban_notifier.py`'s
`drop_superseded` drops a held failure line, before it posts, once the card recovers.

## No plumbing in user text

User-facing text carries no task id, card id, session key, file path, tool name, JSON, CLI flag,
exit code, or roster id used as a name (`cluster-foo` where "the foo cluster agent" is meant). An
identifier the user can act on — a cluster, a workload, a pull request as a link — is content, not
plumbing, and so is a specialist's name where `SOUL.md` asks for attribution. A board read is the
exception for ids: [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md) §1.5 gives each card a short
task id, because the user asked about the board and may name a card back.

Why: plumbing is noise to the person who asked, and an internal name tells them nothing they can
use.

Today: [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md) §1 ("Don't re-say what the thread already
says", "Handle pure conversation yourself") and §2 steps 4 and 5. In code,
`deploy/docker/patches/slack_boilerplate.py` strips Hermes' cron wrapper and its job id from Slack,
and `busyNotice*` in `a2a/gateway/busy.go` is worded without a task id.

## Choices are buttons

When the user has to pick, each option is a button that sends its answer; the message never asks
the user to type `reply A`. At most 5 buttons share a row. The text fallback may list the options,
since a notification or a non-chat reader has no buttons.

Why: a button is one tap on a phone, and a typed reply is a guess at the expected wording.

Today: `choices` in [`spec-a2a-payloads.md`](../../docs/designs/spec-a2a-payloads.md) ("Chat
messages"); `answerChoices` and `nextStep` in `a2a/hermes-bridge/chat_choices.go` turn an answer's
options and next step into choices; `slackChatButtonsPerRow` in `a2a/gateway/slack_chat.go` and
`BUTTONS_PER_ROW` in `slack_presenter.py` cap a row at 5. The agent's side is the `[next: …]` line
in [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md) §2 step 5.

## A failure is one plain line

A failure leads with one plain line saying what did not happen, in the agent's own voice, and says
what would work instead. A gateway's own failure notice is that one line; the Planning Agent's reply
to a blocked or failed card adds why, what the specialist needs, and the retry as a question, at the
length [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md) §2 step 5 sets. No apology. No promise to retry unless something really retries it: a `crashed` or
`timed_out` card is re-run by the dispatcher, a `blocked` or `gave_up` card is not. The retry or
re-route, when there is one, is a button.

Why: a failure is where trust is won or lost, and a promise nothing keeps loses it twice.

Today: [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md) §2 step 5 owns the agent's wording. The
gateway's own failure lines follow the same shape: `slackStopCouldntReply` in
`a2a/gateway/slack_stop.go` offers "Press Stop again" only where a second press can work, and
`slackStopCouldntFinalReply` drops it where it cannot.

## Speak up only when it matters

Beyond the acknowledgement, a message of its own, one that notifies, is for a pull request opened,
a question only the user can answer, a failure, or done. Everything else updates the live line or
stays silent.

Why: every extra post notifies, and noise trains the user to ignore the thread.

Today: [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md) §2 step 5 (the Planning Agent replies only
to a blocked or failed card, and `[SILENT]` when the specialist's question is already posted);
`deploy/docker/patches/slack_ux_moments.py`, which posts a pull request opened and a question the
work waits on as messages of their own while every other milestone stays a plan row; and
`completion_text` and `explained_by_wake` in `deploy/docker/patches/kanban_notifier.py`, which drop
the head line above a report and hold a failure line the wake will explain.

## A destructive action needs the asker's confirm

Before anything destructive — delete, drain, downscale, roll back — the target is stated back and
the person who asked confirms it explicitly. Nobody else's click or yes counts.

Why: in a shared channel, a misread possessive or a bystander's tap otherwise reaches the wrong
cluster.

Today: [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md) §1.6 ("state the resolved target back to
them and wait for a yes") and §3's `/feedback` confirmation, which shows the exact submission and waits for a yes. In the
gateway, `mayStop` in `a2a/gateway/stopgate.go` lets only a requester of the thread press Slack's
Stop button; a typed `stop`, and Stop on other backends, are not gated.

## Renderers never compose

Every word in a structured message comes from the message. A renderer lays out, escapes and
validates; it never invents a field, reads the text to fill one, or rewords. What it adds is its
backend's own, such as step markers, a side bar's colour, which emoji a mark shows, the "✓ name:
choice" receipt of an answered question and the labels on the board row buttons; the spec's "The
emitter composes, the renderer lays out" paragraph is the full list. An invalid message posts its text fallback whole rather than a clipped or
repaired layout.

Why: the emitter is the one place a word is decided, so a renderer that composes makes Slack and
Google Chat disagree and hides emitter bugs.

Today: "The emitter composes, the renderer lays out" and "The gateway renders, it doesn't compose"
in [`spec-a2a-payloads.md`](../../docs/designs/spec-a2a-payloads.md) ("Chat messages");
`a2a/lib/chatmsg` validates; the header comments of `a2a/gateway/slack_chat.go` and
`a2a/gateway/gchat_chat.go` state the same contract for each renderer.
