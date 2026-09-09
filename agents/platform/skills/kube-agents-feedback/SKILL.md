---
name: kube-agents-feedback
description:
  File a bug report, feature request, or question about kube-agents itself on
  the user's behalf, through the project's public feedback form, exactly once,
  after the user has confirmed the exact text in chat. A submission becomes a
  public GitHub issue on gke-labs/kube-agents. Not for telling a user where to
  report — the persona bullet owns that answer without a skill.
---

# Skill: kube-agents-feedback

## When this skill applies

- A kanban card asks you to **file** a kube-agents report for the user — not to
  explain where reports go. For "how do I report a bug in kube-agents?", answer
  from the persona bullet and do not touch this skill.
- The submission target is the public feedback form behind
  <https://gke-labs.github.io/kube-agents/feedback>. It needs no GitHub or
  Google credential, so this path works even when the install's GitHub
  credential proxy is down.

## The confirmation gate

- File **only** what the user has seen and approved. The card must carry the
  exact submission text and the line `user-confirmed: yes` — the chat front
  door's assertion that it showed the user this text and got a yes.
- No `user-confirmed: yes`, or a card that asks you to compose or improve the
  text yourself: do not file. Complete the card saying the front door must show
  the user the exact text and dispatch again with the confirmation line.
- Never edit the confirmed text — no trimming, no added environment detail, no
  rewording. If it looks wrong, that is a reason to send the card back, not to
  fix it yourself.

## Redaction

- The script refuses secret-shaped content (keys, tokens) outright. Cluster
  names, project ids, and log excerpts are your judgment: a submission is a
  public issue, so if the text carries identifiers the user did not visibly
  type themselves, stop and complete the card saying what would be exposed.

## Filing

- Write the confirmed text, verbatim, as JSON in the card workspace:

  ```json
  {
    "title": "<one-line summary>",
    "kind": "Bug | Feature request | Question | Other",
    "happened": "<what happened>",
    "expected": "<optional>",
    "environment": "<optional>",
    "user_confirmed": true
  }
  ```

- Run the helper **once**, from the card workspace so a retried card finds its
  own earlier attempt:

  ```bash
  python3 "$HERMES_HOME"/skills/kube-agents-feedback/scripts/file_feedback.py \
      submit --payload-file payload.json --state-dir .
  ```

- The script owns the whole wire path: it resolves the current form from the
  short link, maps fields by question label, POSTs once, and looks the
  resulting issue up on the tracker. **Never POST the form yourself, never
  fetch or construct a Google Forms URL, and never open the issue directly**
  — the one live test of a hand-rolled submission filed the same report twice
  (gke-labs/kube-agents#1345 and #1346).

## Reporting back

- `SUBMITTED` — put the full `issue_url` in the card `result`, plus the note
  that the issue is public and was opened by the form's bot account.
- `ALREADY_SUBMITTED` — this exact text is already on the tracker (an earlier
  attempt, or another card); it always carries the `issue_url`. Report it.
  This is a success, not an error. Do not retry.
- `UNCONFIRMED` — an attempt went out (this run or an earlier one) and no
  issue has appeared yet. Rerunning the same command once is safe: it only
  re-checks the tracker, never re-POSTs. If it stays `UNCONFIRMED`, the
  report may be lost — say so in the card `result`, with the title and the
  tracker link <https://github.com/gke-labs/kube-agents/issues>, so a human
  can decide. Never treat a persistent `UNCONFIRMED` as filed, and never file
  again yourself.
- `BLOCKED` (exit 1) — nothing was sent; put the `reason` in the card
  `result`. One `reason` worth knowing in advance: a one-line summary over
  120 characters is refused, because the pipeline truncates issue titles
  there and the filed issue could then never be found by title — have the
  front door shorten it with the user.
