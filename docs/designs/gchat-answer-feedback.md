# Google Chat: rate the final answer, as on Slack

Design for [#2732](https://github.com/gke-labs/kube-agents/issues/2732), the next step of the
Google Chat next-mode parity work after the keyed home threads of #4390 and the DM thread per ask
of #4687 ([`gchat-dm-thread-per-ask.md`](gchat-dm-thread-per-ask.md)). It changes the A2A
gateway under `spec.mode: next`: the Google Chat adapter draws the answer-feedback mark that the
gateway already sets, takes the tap, and asks for the note a 👎 may carry, and the `PlatformAgent`
gains the switch that turns it off on a Google Chat install.

## Intent

Under `next` on Slack, the final answer of every ask ends with 👍 and 👎
([`spec-chatops-gateway.md`](spec-chatops-gateway.md), "Answer feedback"). A tap is counted,
keeps the question and the answer in the `feedback` bucket for thirty days so an operator can
turn the exchange into an eval case, and a 👎 opens a box for a one-line note. On Google Chat
nothing of this shows. The decision of which message is the answer is already made outside the
adapters: the relay and the notifier set `OutMessage.Feedback` (the root task id of the ask's
chain) whatever the backend, and the Google Chat adapter ignores it. The recorder, the bucket,
the rater check, the export and the metric are backend-neutral too. What is Slack-only is the
drawing, the tap, the note box, the Home-tab count, and the CRD field.

The intent is the same feedback on Google Chat, through Google Chat's own controls, with the same
records in the same bucket, so one export and one metric cover both backends.

## Goals

- The final answer of an ask on Google Chat, the agent's own or the card report the notifier
  flags, ends with two buttons, 👍 and 👎, on the same message, under its choice and link buttons
  when it has them. A text answer carries them on its last chunk, as on Slack.
- A tap from a sender the allowlist admits is recorded as a `FeedbackRecord` with
  `platform: googlechat`, under the same key rule (`<task>.<rater>`), and counts on
  `kubeagents_a2a_gateway_answer_feedback_total{platform="googlechat"}`.
- A tap is acknowledged on the message itself: the chosen button fills in, the other stays
  outlined, and tapping the other replaces the rating, note and all, as on Slack.
- A 👎 opens the note box: a card only the rater sees, in the answer's thread, with the optional
  one-line input, the sentence saying how long the question and the answer are kept, Send and
  Cancel. Send keeps the note with the rating and collapses the card to the thanks line and the
  feedback-form link. Cancel collapses it to the same lines without a note.
- A tap on a message posted before a gateway restart still works. Everything the tap needs rides
  in the button's parameters and in the click event's copy of the message.
- The operator can turn it off on a Google Chat install: `spec.integration.googleChat.answerFeedback:
false`, with the same effect as the Slack field. The chart carries the value.
- The spec, the site and the CRD docs say what Google Chat does and what it does not.

## Non-goals

- The weekly count. Google Chat has no Home tab; the tally stays on Slack's. The export and the
  metric are the record on Google Chat.
- A thanks line for a 👍. Slack shows nothing more after a 👍 and neither does Google Chat; the
  filled button is the acknowledgement.
- Reactions on the user's message, Working…, thread titles: Chat's app credential cannot draw
  them. Unchanged.
- The Slack follow-ups #3374 (file a 👎 as an issue), #3447 and #3481. They stay Slack issues and
  would reach Google Chat through the shared recorder when they land.
- Running Slack and Google Chat in one gateway (#3094). The gateway holds one chat backend under
  `next`, so the off switch stays one global flag with two names.
- Ratings on home-channel posts (alerts, scheduled reports). The notifier marks only the card
  report that answers an ask in its conversation; home posts are not marked on Slack either.

## Anti-goals

- A rating from a sender the allowlist does not admit must change nothing: no record, no metric,
  no filled button, no card.
- A tap must never be a turn, never answer an open question, and never close one. A rating
  button and a choice button on the same message are told apart by the function they call, never
  by position.
- The note must not reach anyone but the gateway: it goes into the bucket and nowhere else, and
  the card that asks for it is private to the rater in a shared space.
- No second mechanism for the decision: the adapter draws `OutMessage.Feedback` and nothing
  else decides which message is rated.

## Constraints

- Google Chat events reach the gateway through the credential proxy's Pub/Sub relay and are
  acked before they are handled. There is no synchronous reply to a click, so a dialog cannot
  open. The note box is therefore a card message with a text input, whose Send button's click
  event carries the typed value in `formInputs`.
- The adapter posts text messages with accessory widgets (one `buttonList`) because card text
  has no monospace; it has never posted a card. A text input lives only in a card, so the note
  box is the adapter's first `cardsV2` message, and it is a separate message rather than a card
  on the answer: the input belongs under a 👎, not under every answer.
- The relay refuses delete methods. A card the rater is done with is patched down to a line, not
  deleted.
- `spaces.messages.patch` is the edit, with a field mask: the adapter's existing mask for an
  answer (its spelling is proven live, whatever the reference page spells it), `cardsV2` for the
  note card. With app credentials the app can patch only its own messages, which these are.
- The Chat reference settles that a click event carries `formInputs` in both event layouts
  without a synchronous reply, that a plain submit button needs only `function` and
  `parameters`, that `privateMessageViewer` needs app credentials and no attachments, and that
  a whole message is capped at 32,000 bytes. It does not say whether a private message may be a
  thread reply, may be posted in a one-to-one DM, or may be patched by the app afterwards. The
  live run proves those three, and the design keeps a fallback for each.
- The chosen-button acknowledgement is an edit of the answer. The adapter rebuilds the message
  from its own record of the layout when it has one (a question it tracks, an answered line it
  drew) and from the click event's copy of the message otherwise, so a tap after a restart still
  patches. The filled state is a hint; the bucket is the record.
- The Slack adapter's strings (the labels, the kept line, the thanks line, the form line and
  URL) move to a shared file so both backends read one copy; the Slack constants keep their
  names as aliases or are renamed in the same change, builder's choice, with no wording change.
- The repository rules apply: no magic constants, no CodeQL-sensitive names on non-credentials,
  tests beside the code, docs generated where generated.

## What is decided

**Call 1: drawing.** `GoogleChatAdapter` implements `FeedbackSink`. When `PostMessage` is
handed a message whose `Feedback` is set and the recorder is present, it appends two buttons to
the message's one `buttonList`, after any choice and link buttons: text `👍`, alt text
`Good answer`, and text `👎`, alt text `Bad answer`, each with
`onClick.action.function = "kage.feedback"` and parameters `task=<root task id>` and
`rating=up` or `down`. A message with no layout (`Chat == nil`) and a mark posts as text with a
`buttonList` holding only the two; one with a layout posts its layout plus the two. The
`PostNotifyMessage` path (home posts) is unchanged. A message Chat refuses with the buttons posts
as it would have without them, as the Slack adapter does at its block cap.

**Call 2: the tap.** `classifyClick` branches on the function first. `kage.feedback` is never a
turn: it returns a reason after handling. Handling requires the recorder, a `task`, a `rating`
of `up` or `down`, the sender's email, the space and message names; a click lacking any is
logged and dropped. The click id is the message name and event time, deduplicated like a choice
click under its own prefix. `recorder.Admits("gchat", email)` false drops it silently. The
rating is `FeedbackRating{Platform: "googlechat", Backend: "gchat", Rater: email, Conversation:
the conversation a choice click there would bind to, MessageID: the message name, TaskID,
Rating, Answer: the message text as the event carries it}`. `Rate` runs off the pull loop under
a deadline, and under the message's rewrite lock with the patch, so taps on one message take
effect in turn. A tap older than the same rater's newest tap on the message is superseded and
gets no write. An older tap whose write was already running when the newer tap came in patches,
and the newer tap repaints after it. A failed write leaves the rater's newest tap time as
it is, so a late older tap stays dropped and the rater's next tap proceeds. On success the adapter patches the answer with the tapped rating button
`type: FILLED` and the other plain, and remembers the rating per message so later rewrites
redraw it. The text and the other widgets come from the adapter's record of the message when
it has one, so a tap can never restore choice buttons the answered rewrite removed, and from the
event's copy of the message otherwise. A `down` then posts the note card. On failure it logs and
leaves the message as it was.

**Call 3: the note card.** One `cardsV2` message in the answer's thread. In a one-to-one DM
it posts plainly, since the DM has one reader. Anywhere else it is created with
`privateMessageViewer` naming the rater (`users/<id>` from the event's user). Its sections: a
paragraph `What went wrong?`; a single-line `textInput` named `note`, label `Anything else?`,
hint naming the 500-character cap; a paragraph with the kept line, the Slack adapter's strings
verbatim (`Your question and this answer are kept on your install for %s so an operator can
turn them into a test.`, or its no-ask variant, from `Rate`'s `askKept` and `KeptDays()`); and
a `buttonList` with `Send` and `Cancel`, both calling `kage.feedback-note` with parameters
`task`, `rating=down`, and `cancel=1` on Cancel. The input carries `validation.characterLimit`
of 500, the Slack box's cap, and the adapter caps again on receipt. If Chat refuses the private
create as a thread reply in a space, the adapter retries once at the space's top level, still
private; if that is refused too it logs and posts nothing more: the filled 👎 already stands,
and the note is optional on Slack too.

**Call 4: Send and Cancel.** `kage.feedback-note` is never a turn either. Send reads
`formInputs.note.stringInputs.value[0]`, trims and caps it, and calls
`recorder.Note` with a rating rebuilt from the parameters and the sender; the record's key needs
only the task and the rater, so this works after a restart. Then it patches the card to one
paragraph: the thanks line, then the form line with the feedback-form URL as a link. Cancel
patches the card to the same paragraph without calling `Note`. `errFeedbackNotKept` (the 👎 was
replaced before the note arrived) patches the card to `This answer's rating changed, so the note
was not kept.` The decoder tells a click event with no `formInputs` object from one whose value
is empty: an empty value is an empty note, kept and thanked as on Slack; a Send with no
`formInputs` at all logs a warning and patches the card to `The note did not reach me; your
rating stands as it was.`, so the failure is visible rather than silent. If Chat refuses the patch
of the private card, the adapter posts the paragraph as a new private message instead, as the
answered-line rewrite falls back to a new message today. A second click on a collapsed card
finds no button; a redelivered click is a duplicate by its id.

**Call 5: the rewrites keep the ratings.** The choice-answered rewrite (`gchatAnsweredLayout`)
and later edits drop choice buttons and keep link buttons today; both now also keep rating
buttons, filled state included. The rewrite is built from memory. The task id and the filled
state live in a per-message rating memory (`ux.rated`), set when the answer is posted, when a tap
is recorded, and when a choice click's rewrite reads the buttons of a message the adapter does
not remember. A tap older than the one that filled the buttons on record does not move it. The answered rewrite's layout is kept per message (`ux.shown`), without the
rating buttons. A rewrite after a restart of a question the adapter
no longer remembers takes its buttons from the event's copy of the message, for which the button
struct also decodes `type` and `altText`. The rating memory starts empty after a restart, so
the filled state then comes from the event's copy, and the bucket still holds the rating. The restart-recovery of a choice click's link
buttons (`gchatMessageButtons`) tells the three kinds apart by function and link, not by index.
Buttons are classified by `onClick.action.function` (`kage.choice`, `kage.feedback`,
`kage.feedback-note`, and the Stop button's `kage.stop`, spec-chatops-gateway.md "The Stop button")
or `onClick.openLink`.

**Call 6: the switch.** `spec.integration.googleChat.answerFeedback *bool`, default `true`, the
Slack field's twin. The operator renders `A2A_GOOGLE_CHAT_ANSWER_FEEDBACK=false` on the gateway
only when Google Chat is the gateway's backend under `next` and the field is `false`, as it
renders the Slack variable only when Slack is armed. The gateway sets `AnswerFeedbackOff` when
either variable is `false`; the desk then never starts, no backend draws, and a tap on an older
message is logged and dropped. Chart: `platformAgent.integration.googleChat.answerFeedback`,
with schema and the CR template line; the CRD docs regenerate.

**Call 7: metrics and docs.** The zero series of `answer_feedback_total` are pre-created for
`googlechat` as for `slack`. The spec's "Answer feedback" section gains the Google Chat
paragraphs and stops saying every other backend ignores the mark (Discord and the console still
do). The site's "Answer feedback" section becomes platform-neutral with the two differences
called out: the 👎 box is a private card in the thread, and the week's count is Slack's Home tab
only. The CRD field pages and the upgrade page's row for the next release name the field.

## Acceptance, as a live run on gkedemos

gkedemos runs `next` with Google Chat on the gateway and a `feedback` bucket with a TTL. With the
branch's gateway image swapped in (the operator's `A2A_GATEWAY_IMAGE` override):

1. Ask a question in the app's DM. The answer, in its thread, ends with 👍 and 👎 after any
   next-step button.
2. Tap 👎. The 👎 fills in. A card appears in the thread with the input, the kept line and the
   two buttons. Type a note and Send. The card collapses to the thanks line and the form link.
   `/gateway feedback export --since 1h` in the gateway pod shows one record with
   `platform: googlechat`, the rating, the note, the ask and the answer.
3. Tap 👍 on the same answer. The 👍 fills, the 👎 clears, no card appears, and the export shows
   the record replaced with no note. The metric shows one `up` and one `down`.
4. Ask something the agent hands to a card. The card's report, not the acknowledgement, carries
   the buttons.
5. In a shared space with the app added and the rater on the allowlist (gkedemos's home channel
   is a DM, so this needs a space of the operator's; no second account is needed), mention the
   app and rate its answer 👎. The card posts as a private reply in the thread, a read-back as
   the rater shows it with the input, Send collapses it, and the record names the space
   conversation. This step proves the three things the reference leaves open: a private thread
   reply, a form value arriving over Pub/Sub, and the app's patch of its private message. That
   nobody else sees the card rests on the API's contract for `privateMessageViewer`.
6. Set `spec.integration.googleChat.answerFeedback: false` on the CR. The gateway env shows the
   variable, a new answer carries no buttons, and a tap on the earlier answer is logged and
   changes nothing. Restore the field.

## Left to the builder

- The wire structs for `formInputs` in both event layouts (legacy `common.formInputs`, add-on
  `commonEventObject.formInputs`), modelled on the existing `invokedFunction` handling.
- Where the shared strings file lives in `a2a/gateway/` and whether the Slack constants are
  renamed or aliased.
- The exact log lines, and the dedupe prefix for rating clicks.
- Test names, following the Slack feedback tests one for one where the behaviour is shared, plus
  the card, the private viewer, the restart case and the rewrite cases.
