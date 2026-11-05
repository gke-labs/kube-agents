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

The `paths:` frontmatter of `chat_ux.md` is the list of chat output files. The diff touches chat
output when it changes a non-test file matching one of those globs, adds or rewords a string a user
reads in chat anywhere else, or changes who may confirm, stop or press a button. Inside scope, skip
hunks that change no user-visible string, layout, posting decision or click gate.

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

Test every surface item against each rule's paragraph in `chat_ux.md`, by its heading. The cues
below say where violations hide; the paragraph is the test.

- **The answer comes first, as one bold line**: Flag an answer whose first line is context, a
  heading or a restatement; flag a change that moves the verdict below the fold or drops the
  headline's bold; flag reasoning, a table or long detail above the fold.
- **The acknowledgement names its target**: Flag an ack that is generic (`on it`), capitalised,
  multi-sentence, not ended by a period, a restatement, named by the change instead of what is
  checked, or carrying a task id, attribution or plan.
- **One live status line, updated in place**: Flag a new post (Slack
  `chat.postMessage`, Google Chat `messages.create`) for a progress step where an edit would do;
  flag a progress line that survives the answer.
- **No plumbing in user text**: Flag task ids, card ids, session keys, file paths, tool names,
  JSON, CLI flags, exit codes and roster ids used as names. Allow a cluster, a workload, a linked
  pull request, a specialist's name where `SOUL.md` asks for attribution, and a board read's short
  task id.
- **Choices are buttons**: Flag a choice offered without buttons, a button that does not send its
  answer, "reply X", "type X" or "say X" in a message body; flag more than 5 buttons in one row.
  Allow the text fallback listing the options.
- **A failure is one plain line**: Flag an apology, a failure that does not lead with one plain
  line, a gateway notice longer than that line, a failure that does not say what would work instead,
  a retry or re-route offered as text rather than a button, or a retry promise; for each promise, find the code that retries and flag it when none runs on that path.
- **Speak up only when it matters**: Flag a new message of its own that is not the
  acknowledgement, a pull request opened, a question only the user can answer, a failure, or done.
- **A destructive action needs the asker's confirm**: Flag a destructive action reachable without
  stating the resolved target back and the asker's explicit confirm, or a confirm another
  participant can satisfy.
- **Renderers never compose**: Flag a renderer that writes words of its own, reads text to fill a
  field or rewords; flag an invalid message rendered clipped or repaired instead of posting its
  text fallback whole. Allow what the rule's paragraph names as the backend's own.

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
