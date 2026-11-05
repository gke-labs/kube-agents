---
name: review-chat-ux
description: Reviews a diff that changes what users read in Slack or Google Chat against the chat UX rules, and reports each violation with file:line and the rule it breaks.
---

# Task

Review a diff range for chat output that breaks [`.agents/rules/chat_ux.md`](../../rules/chat_ux.md).
The rule file owns the rules and their reasons; this skill holds the method. When they disagree, the
rule file wins and this skill needs fixing.

Run it in a context that did not write the change, as `review-preflight` arranges. Report; edit
nothing.

# Scope

The diff touches chat output when a non-test file it changes matches one of these:

- `agents/*/SOUL.md`, `agents/chat/defaults/**`
- `agents/platform/scripts/slack_*.py`, `agents/platform/scripts/chat_*.py`,
  `agents/platform/scripts/*_presenter.py`
- `a2a/gateway/slack_*.go`, `a2a/gateway/gchat*.go`, `a2a/gateway/*chat*.go`, `a2a/gateway/relay*.go`,
  `a2a/hermes-bridge/chat*.go`, `a2a/lib/chatmsg/**`
- `deploy/docker/patches/kanban_notifier.py`, `deploy/docker/patches/kanban_progress_lines.py`,
  `deploy/docker/patches/kanban_chat_notify.py`, `deploy/docker/patches/slack_*.py`

Outside this list, still review a hunk that adds or rewords a string a user reads in chat. Inside
it, skip hunks that change no user-visible string, layout or posting decision.

# Workflow

## 1. Collect the user-facing surface

- Run `git diff <range>` and list every hunk that adds or changes text a user reads: a string
  constant, a format string, a fixture's expected post, a Block Kit or Chat card builder, an
  instruction in a `SOUL.md` that shapes a reply.
- Record for each: file:line, the exact text, and the moment it is read (ack, status, answer,
  question, failure, notice, button).
- Follow a changed constant to its call sites; a reworded constant can break a rule only where it
  posts.

## 2. Check each rule

Read `chat_ux.md` first, then test every surface item against every rule:

- **Answer first**: Flag an answer whose first line is context, a heading or a restatement; flag a
  change that moves the verdict below the fold or drops the headline's bold.
- **Ack names its target**: Flag an ack that is generic (`on it`, `working on it`), capitalised,
  multi-sentence, or carries a task id, attribution or plan.
- **One live line**: Flag a new `post` or `chat.postMessage` for a progress step where an edit of the
  existing message would do; flag a progress line that survives the answer.
- **No plumbing**: Flag task ids, card ids, session keys, file paths, tool names, JSON, CLI flags,
  exit codes and internal agent names in user text. Do not flag a cluster, workload or linked pull
  request.
- **Buttons**: Flag "reply X", "type X" or "say X" in a message body; flag more than 5 buttons in one
  row. Do not flag the text fallback listing the options.
- **One-line failure**: Flag an apology, a second explanation, or a retry promise. For each retry
  promise, find the code that retries; flag it when none exists or it does not run on that path.
- **Speak up only when it matters**: Flag a new message of its own that is not a pull request
  opened, a question only the user can answer, or done.
- **Destructive confirm**: Flag a destructive action reachable without the asker's explicit
  confirm, or a confirm or control another participant can satisfy.
- **Renderers never compose**: Flag a renderer that writes words of its own, reads text to fill a
  field, rewords, clips or repairs a message. Allow what the rule file lists as the backend's own.

## 3. Verify every candidate

- Re-read the code path that posts the text; drop a candidate that never reaches a user (a log
  line, a metric label, an error returned to an API caller).
- Check the `SOUL.md` section the rule file names; a wording the section requires is not a
  violation of this skill, and a conflict between them is a finding against the rule file.
- Check both backends: a rule broken only on Google Chat is still broken.
- Drop anything you cannot tie to a line in the diff.

# Output

One row per surviving violation, ordered by how often a user meets the text:

| file:line | rule (`chat_ux.md` heading) | text as posted | fix |
| --------- | --------------------------- | -------------- | --- |

Above the table, list which rules were checked and which surface items were reviewed. "No
violations" is complete only alongside that list.
