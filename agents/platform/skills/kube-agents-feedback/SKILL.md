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
- A kanban card asks only for the install's **usage summary**, because the
  person said `add the usage summary` to a draft the front door showed them.
  That card renders the block and files nothing ("The usage summary", below).
- The submission target is the public feedback form behind
  <https://gke-labs.github.io/kube-agents/feedback>. It needs no GitHub or
  Google credential, so this path works even when the install's GitHub
  credential path is degraded.

## The confirmation gate

- File **only** what the user has seen and approved. The card must carry the
  exact submission text and the line `user-confirmed: yes` — the front door's
  assertion that it showed the user this text and got a yes.
- No `user-confirmed: yes`, or a card that asks you to compose or improve the
  text yourself: do not file. Complete the card saying the front door must show
  the user the exact text and dispatch again with the confirmation line.
- Never edit the confirmed text — no trimming, no added environment detail, no
  rewording. If it looks wrong, that is a reason to send the card back, not to
  fix it yourself.

## Redaction

- The broker refuses secret-shaped content (keys, tokens) outright. Cluster
  names, project ids, and log excerpts are your judgment: a submission is a
  public issue, so if the text carries identifiers the user did not visibly
  type themselves, stop and complete the card saying what would be exposed.

## The usage summary

- A summary card carries no text to file and needs no confirmation line. Run
  the helper's second subcommand, which reads the `PlatformAgent` resource
  through the sandbox's `kubectl` and nothing in a pod:

  ```bash
  python3 "$HERMES_HOME"/skills/kube-agents-feedback/scripts/file_feedback.py summary
  ```

- Exit 0 prints one fenced block: versions, mode, readiness, the usage counters,
  and `unavailable` for the token, spend, latency and task lines the sandbox
  cannot read. Complete the card with that block as the `result`, **verbatim**,
  fences included: the front door shows it inside the re-shown draft, and the
  person's yes is to those exact lines. Do not trim, reorder or annotate it.
- Exit 1 prints a `BLOCKED` line: the resource could not be read, or the block
  named an identifier read from it and the `reason` says how many, never
  which. Complete the card with the `reason` and nothing else; the front door
  offers the draft without the block. Never retry with a different namespace
  or name, never assemble a block by hand, and never run `kubectl` yourself to
  fill in what the helper refused.

## Filing

- A filing card whose body carries a usage summary block under its environment
  carries it because the person saw that block in the draft and said yes. It
  goes into `environment` exactly as the card has it, fences included; a card
  with no block files with whatever environment the person typed, or none.
- Write the confirmed text, verbatim, as JSON in the card workspace:

  ```json
  {
    "title": "<one-line summary, at most 120 characters>",
    "kind": "Bug | Feature request | Question | Other",
    "happened": "<what happened>",
    "expected": "<optional>",
    "environment": "<optional>",
    "user_confirmed": true
  }
  ```

- Run the helper **once**, from the card workspace so a rerun finds its own
  earlier attempt:

  ```bash
  python3 "$HERMES_HOME"/skills/kube-agents-feedback/scripts/file_feedback.py \
      submit --payload-file payload.json --state-dir .
  ```

- The script asks the credential broker, which is the sandbox's only way out,
  and the broker owns the whole wire path: it resolves the current form from
  the short link, maps fields by question label, POSTs once, and looks the
  resulting issue up on the tracker. **Never fetch the form or the tracker
  yourself, never construct a Google Forms URL, and never open the issue
  directly** — the one live test of a hand-rolled submission filed the same
  report twice (gke-labs/kube-agents#1345 and #1346).

## Reporting back

- `SUBMITTED` — put the full `issue_url` in the card `result`, plus the note
  that the issue is public and was opened by the form's bot account. Say what
  was filed; do not claim it matches what the person approved, which only the
  front door that showed it can know. A
  `SUBMITTED` whose `issue_url` is `null` means the form accepted the report
  and the tracker has not shown the issue yet (its `detail` says so): rerun the
  same command once, which only reads the tracker. The rerun answers
  `ALREADY_SUBMITTED` with the URL once the issue is there, or `SUBMITTED`
  with `null` again while it is not; in that case report the title and the
  tracker link as filed and not yet visible. About thirty minutes after the
  form confirmed, a rerun answers `UNCONFIRMED` naming the form's pipeline
  instead: hand it to a human with the composed text. Never file again.
- `ALREADY_SUBMITTED` — this exact text is already on the tracker (an earlier
  attempt, or another card); it always carries the `issue_url`. Report it.
  This is a success, not an error. Do not retry.
- `UNCONFIRMED` — a request went out and no issue has appeared yet, or the
  rerun could not reach the broker to look (its `detail` says which). Rerunning
  the same command once is safe: it only asks the broker to read the tracker,
  never to post again. If it stays `UNCONFIRMED`, the report may be lost — say
  so in the card `result`, with the composed text and both `fallback` links,
  so a person can file it themselves. Never treat a persistent `UNCONFIRMED`
  as filed, and never file again yourself.
- `BLOCKED` (exit 1) — nothing was sent by this run, and the `reason` says
  why: a refused payload, a broker that could not be reached on a first
  attempt, or a token file that could not be read. Put the `reason`, the composed text
  and both `fallback` links (`fallback.tracker`, `fallback.form`) in the card
  `result`: on an install whose broker cannot reach the form, this is the
  answer, and the person files the text themselves at either link. One
  `reason` worth knowing in advance: a one-line summary over 120 characters is
  refused, because the pipeline truncates issue titles there — have the front
  door shorten it with the user.
- Exit 2 — the broker's answer was lost after the request went out. Rerun the
  same command once; it rechecks the tracker and never posts again.
