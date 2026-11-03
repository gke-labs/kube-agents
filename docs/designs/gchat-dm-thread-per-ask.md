# Google Chat DMs: one conversation per ask, answered in its thread

Design for [#3108](https://github.com/gke-labs/kube-agents/issues/3108), step four of the
Google Chat next-mode parity work (after the line forms and the cardsV2 renderer of #4319, the
card emitters of #4369 and the keyed home threads of #4390). It changes the A2A gateway under
`spec.mode: next`: the Google Chat adapter's classification of DM messages, and the gateway
paths that already key on a top-level DM for Slack.

## Intent

In a Google Chat DM the gateway today keys every top-level message to the space,
`gchat:dm/spaces/AAA`, and answers at the top level. A message typed inside a thread is a side
conversation of its own. That is the main-flow rule of the Hermes adapter that `today` runs, and
[#2765](https://github.com/gke-labs/kube-agents/pull/2765) made the gateway match it. The cost
shows in #3108: a long answer and the completion of a card the ask delegated land flat in the DM
stream, and an operator who opens the ask's thread to follow up finds neither there and starts a
second session besides.

Slack under `next` already does the other thing: each DM question is its own conversation,
answered in its own thread, and a reply typed in that thread continues it. The decision for this
change is to match Slack as closely as possible. Google Chat makes that cheap: Chat attaches a
thread to every top-level DM message, so the gateway has a thread name to reply into without
creating one.

## Goals

- Every top-level message a user sends the app in a DM space is its own conversation, keyed by
  the thread Chat attached to it: `gchat:dm/spaces/AAA/threads/BBB`. The gateway answers inside
  that thread.
- A message typed inside a thread continues that thread's conversation, the same key, as a Slack
  thread reply does.
- Everything that follows the conversation key follows the ask into its thread: the answer's
  chunks, a card the ask delegated and that card's completion, `a2a notify --conversation`,
  follow-up questions, and a click on a card posted there.
- A top-level DM message is marked top-level, as Slack's is, so the gateway's top-level notices
  apply: a top-level `stop` or status phrase gets the "each question runs in its own thread" notice
  instead of a model turn, a top-level `/session off` gets its notice, and card-stop is not
  offered on a top-level message.
- Nothing about the key lives in process memory. The thread is in the key, so a gateway restart
  loses nothing, and no per-space map of "the latest ask" comes back.

## Non-goals

- The Hermes Google Chat adapter that `today` runs, and its inbound-count heuristic described in
  `cron-report-relay.md` ("The first reply into a report's thread"). Untouched.
- Group spaces and spaces whose threading state has no replies. Their rule is unchanged: a
  threaded space keys each thread, `UNTHREADED_MESSAGES` and `GROUP_CHAT` bind the space.
- Home-channel posts (#4390). An unkeyed home post still lands at the top level, a keyed one still
  edits on its home thread, and a click on a home card still binds to the card's thread.
- `openDirect`, the gateway-initiated DM. It keeps returning the thread-less key and posting at
  the top level. Slack's thread-less key is the same legacy shape.
- Records already minted under `gchat:dm/spaces/AAA`. They keep parsing and keep posting at the top
  level. A new top-level message does not join them; it gets its own thread.
- Slack, Discord, Microsoft Teams.

## Anti-goals

These are the regressions #2765 fixed, and the design must not bring them back:

- A reply typed in an old thread steering the task running in a different conversation. Each
  thread is its own key, so there is no shared main flow to steer.
- An unsolicited post landing in the thread of the user's latest ask. `openDirect` posts at the
  top level under the space key, and nothing maps a space to a thread.
- Per-space mutable presentation state. None is introduced.

## Constraints

- Conversation keys round-trip through the existing pair of functions (`gchatConversationID`
  and its inverse). The thread-less DM key still parses.
- A post with a thread set carries `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD`, as
  today.
- The gateway reads nothing from the Session KV server or the registry to decide a key.
- Pre-PR rules of this repository apply: adversarial and docs-drift passes in a clean context,
  the pull request template, Simplified Technical English in the PR text.
- Live validation runs on the gkedemos install under the live-test lease, with the gateway image
  swapped through the operator's override as `gkedemos-a2a-gateway-image-swap` describes. The
  same run settles the click #4390 left owed: a click on an alert edited into its triage, in the
  DM home space, binds to the alert's thread.

## What is decided

- **Thread per ask, continuing in the thread.** The user's decision: match Slack as closely as
  possible. The three calls below follow from it and were made by the designer, not the user.
- **The key change is in classification, not in posting.** In `gchatSurface`, the rule that
  clears the thread of a DM message whose `threadReply` is false goes away. Downstream code
  already keys a DM message with a thread as `gchat:dm/<thread>` and posts into a thread when the
  key has one. The `UNTHREADED_MESSAGES` and `GROUP_CHAT` clearing stays.
- **Call 1: the top-level mark is set for Google Chat too.** `classify` sets the inbound message's
  top-level mark when the space is a DM and `threadReply` is false, which is what Slack does with
  an empty `thread_ts`. The notices and the card-stop suppression that key on the mark then apply
  on both backends; the session-on acknowledgement's Slack-only wording branch becomes a DM
  branch. The `threadReply` flag therefore still decides one thing, the mark, and nothing about
  the key. The spec sentence that says the mark is set only for a top-level Slack DM changes to say
  both backends.
- **Call 2: the pushed-report splice applies to DM asks, as it does on Slack.** The hermes
  bridge exempts only the thread-less DM key from attaching a pushed report ahead of a follow-up,
  so every thread-keyed ask gets the splice. That is Slack's behaviour and stays. The bridge's
  comment that names the Google Chat DM exemption is reworded; its tests are unchanged unless they
  assert the exemption for a thread key. The live run tells session continuity from the splice by
  the gateway's "pushed report attached" log line: acceptance item 3 holds only if the follow-up
  continues the same session, whether or not a report was also attached.
- **Call 3: every DM click binds to the clicked card's thread.** With the DM clearing gone, a click
  classifies to its message's thread by the same rule as a typed message, and the two `home`
  guards in the click path no longer change an outcome. The `home` button parameter stays on the
  wire, since cards already posted carry it, and the guards that no longer branch are removed. The
  test that asserted an unmarked click after a restart binds to the DM root now asserts it binds to
  the card's thread. Consequence: a card with choices posted at a DM's top level under a legacy
  record or by `openDirect`, clicked after a restart, starts a conversation in the card's own
  thread rather than rejoining the legacy record. A question card posted by `notify` at a DM's top
  level must still take its click after a restart: the builder checks where the Google Chat
  adapter tracks such a question and, if it is under the thread-less key, tracks it under the
  message's thread, which is what Slack does.
- **Documentation says what the gateway does, not what Hermes does.** The conversation-keys
  paragraph of `spec-chatops-gateway.md` describes the thread-per-ask rule and names Slack as the
  model; the sentence that calls the old rule "the main-flow and side-thread rule of the Hermes
  adapter" goes, and `cron-report-relay.md` keeps its Hermes section but no longer says the
  gateway reads `threadReply` to split main flow from side thread. The site's chatops page says
  a Google Chat DM ask is answered in its own thread, as on Slack.

## Acceptance, as a live run on gkedemos

1. A top-level DM ask gets its answer inside the thread Chat attached to it, not in the DM
   stream. The gateway's log shows the conversation key with the thread.
2. A card the ask delegated posts into the same thread, and so does its completion.
3. A follow-up typed in that thread continues the same conversation: same key in the log, and the
   agent has the earlier context from the session, not only from a spliced report.
4. A second top-level ask gets a different thread and a different key.
5. A top-level `stop` typed in the DM gets the top-level notice and starts no turn.
6. A keyed home-channel alert still edits in place on its thread, and a click on it binds to the
   alert's thread (the #4390 debt).
7. A question card posted by `notify` at the DM's top level takes its click after a gateway
   restart.
8. A gateway restart between 1 and 3 changes nothing: the follow-up still continues the ask.

## Left to the builder

- The names of the tests that replace `TestGchatDMSideThreadsMatchToday` and
  `TestGchatDMSideThreadIsItsOwnSession`, and the fixtures they reuse from `testdata/gchat/`.
- Whether to log `threadReply` on the inbound classification line.
- Whether the eval-driven-development rule treats this as agent behaviour. The builder reads
  `.agents/rules/eval_driven_development.md` and records the outcome under Live validation the
  way #4390 did; no bench case can observe Google Chat threading, and the live run above is the
  evidence.
- The exact wording of the spec, the relay design note, the bridge comment and the site page.
