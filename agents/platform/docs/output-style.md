# Output Style

How anything you send a human should be written. This is the one home for that
rule set; the personas cite it rather than restating it.

It applies to all three delivery channels, because they share a renderer:

| Channel                    | Reaches the user by                                        |
| -------------------------- | ---------------------------------------------------------- |
| `kanban_complete(result=)` | the gateway posts it into the requester's thread, verbatim |
| `send_notification`        | `hermes send`, into the session thread or the home channel |
| a chat reply               | the adapter, in a `slack` / `google_chat` session          |

All three end in the same platform adapter, so the same formatting applies to
each. Write once, correctly, and it renders the same way wherever it lands.

## 1. The renderers, and what they will not forgive

You write one Markdown source and it reaches one of two destinations, which
disagree sharply about how much of it survives.

**Slack** renders through Block Kit, which turns Markdown into real structure:
`##` becomes a `header`, `|` tables become native `table` blocks, `-` bullets
become nested `rich_text` lists, `---` becomes a divider.

**Google Chat** renders almost none of it. Every `#`–`######` heading collapses
to bold, there are no tables and no dividers, and nested bullets are flattened.
Structure there has to come from short bolded labels, one-line `-` bullets, and
blank lines.

Improvised structure carries on neither: `=== Title ===` stays three equals
signs, `1. SECTION` is an ordinary list item, and hand-aligned columns stay a
wall of text.

**Write for the narrower of the two.** Markdown that reads well on Google Chat
still renders richly on Slack; Markdown that leans on tables and header blocks
degrades into prose on Chat. In particular, **a table is a Slack-only luxury:
never let it be the only place a fact lives.** Before you ship one, delete it in
your head — if the reader has now lost something, it belonged in a bullet
instead. Two or three columns of short cells survive flattening; anything wider
does not.

Two Block Kit limits fail loudly enough to design around:

- **Over 50 blocks and you lose all structure, not some.** The renderer refuses
  a message it cannot express safely and returns nothing, so the adapter falls
  back to flat text. A heading, a paragraph, a list, a table and a divider are
  one block each — roughly 50 structural elements is the cliff, and going over
  it costs the formatting of the entire message rather than the tail.
- **A message long enough to be split is never rendered as blocks at all.**
  Block Kit is applied only to a single-chunk message.

Two more truncate quietly: a section's text is cut at 3000 characters
(`MAX_SECTION_TEXT`) and a heading at 150 (`MAX_HEADER_TEXT`).

None of this is reachable by a message that follows §2. That is the point of
§2 — the length ceiling is not a matter of taste, it is the distance to a
cliff.

## 2. Length

**Aim for 2000 characters. 4000 is the hard ceiling.** Check before you send.
If you are over, tighten the prose — never drop a finding to fit. If the
findings genuinely do not fit in 4000 characters, that is a signal to send the
headline and offer the detail, not a licence to send 8000.

4000 is where Google Chat stops taking a single message, and what happens past
the cap depends on how you sent it:

- A kanban `result`, a notification, and a chat reply are **split** across
  several messages at the nearest line break below the cap, so one answer
  arrives as two, the second stripped of its context.
- A **cron** delivery is **truncated outright**, with a
  `... [truncated, full output saved to …]` footer where the rest of your
  report should be.

So overflow costs you a single coherent message everywhere, and on the
scheduled path it costs you the tail — which, in a message that ends with a
recommendation, is the part that mattered. Slack tolerates far more before
splitting, but its own cliff — the 50 blocks in §1 — sits well inside the
length that gets you there.

A report that takes a screen to skim has failed even if every word in it is
correct.

Completeness is not length. The answer to "which cron jobs are enabled" is the
list — all of it — and nothing else. Answering it with a titled multi-section
report is as wrong as answering it with a status line: a card that asked you to
_list_ something gets a list.

## 3. Markdown, never a platform's own dialect

Write **standard Markdown**. The adapter converts it to Slack's mrkdwn, or to
Google Chat's, for you.

Writing mrkdwn yourself defeats that conversion: `*bold*` passes through
untranslated and the inline parser sees a literal asterisk span, so the word
arrives with the asterisks still around it or in italic. Use `**bold**`.

Start headings at `##`. A `#` H1 duplicates the card title or the alert
headline the message already carries.

**Link every artifact you name.** `[text](url)` is converted on both platforms
— a Block Kit link on Slack, `<url|text>` on Google Chat — so there is no
destination where a bare identifier is the best you can do. Write the PR, the
issue, the ledger and the console view as Markdown links; your persona has the
GCP Console URL templates. A bare `#5` or a raw ID is not clickable anywhere,
and a finding the reader cannot act on without asking you where to look is not
finished.

## 4. Shape

The complaint that this product's output is a "moving target" is a complaint
about shape, not length: the same kind of message arrives looking different
every time. Same kind of message, same skeleton, every time.

**A notification** — one line of headline, then only what the reader needs to
act:

```
🔴 **CrashLoopBackOff** — `checkout-api` in `prod-us-east1`

Restarting every 40s since 09:12 UTC. The container exits 137 (OOM) against a
memory limit of 256Mi.

**Next:** raising the limit to 512Mi — PR #1841.
```

One severity glyph, at the front, and no others: 🔴 critical, 🟡 warning, 🔵
info. Emoji are a severity channel, not decoration; a glyph on every heading
spends the one signal the reader scans for.

**An incident synthesis** follows the incident communication playbook in the
Platform Agent's `SOUL.md` (§7, Incident Triage Communication Policy), which
owns that shape: exactly three `##` sections — What's wrong, Why, What to do —
and never a fourth. This document governs how those sections are written; the
playbook governs what they are.

**A list** is a list. No preamble, no closing offer of further help.

## 5. What to cut

- The restatement of the question you were asked.
- "I hope this helps", "Let me know if you'd like me to dig deeper", and every
  other closing offer. If a next step exists, name it as a next step.
- Your method, unless it is the finding. Which tools you called and in what
  order is not a result.
- Raw tool schemas, CLI flags and exit codes.
- Evidence beyond what grounds the claim. One quoted event line proves a
  CrashLoopBackOff; twelve do not prove it more.
- Any section that would be empty. Omit the heading too.

Grounding evidence stays — cluster, namespace, resource, timestamps. Cut the
padding around it, not the proof.

## 6. Before you send

1. Is it under 4000 characters?
2. Does it lead with the answer, or with preamble?
3. `**bold**` rather than `*bold*`, headings at `##`?
4. Would it still read on Google Chat, with the headings bolded and any table
   flattened?
5. Is every artifact you named a `[text](url)` link?
6. Exactly one severity glyph, at the front?
7. Would this be the same shape as the last message of its kind?
