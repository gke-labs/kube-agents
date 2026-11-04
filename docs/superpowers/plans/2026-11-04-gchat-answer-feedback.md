# Google Chat Answer Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under `spec.mode: next`, the Google Chat adapter draws the 👍/👎 mark the gateway already sets (`OutMessage.Feedback`), records taps through the shared `FeedbackRecorder`, asks for a 👎 note in a private card, and the `PlatformAgent` gains `spec.integration.googleChat.answerFeedback`.

**Architecture:** One new adapter file, `a2a/gateway/gchat_feedback.go`, holds the drawing, the tap, the note card and the per-message rating memory. `gchat.go` (post), `gchat_clicks.go` (decode, classify, rewrites) and the shared `feedback_text.go` (words moved out of `slack_feedback.go`) are edited to match. The recorder, bucket, metric and export are already backend-neutral. The operator adds a CRD field and one env var; the gateway reads either env name into the one global `AnswerFeedbackOff`.

**Tech Stack:** Go (a2a gateway, k8s-operator with controller-gen), Helm chart, Astro/Markdown site docs.

**Spec:** docs/designs/gchat-answer-feedback.md

## Global Constraints

- Repository rule, quoted from AGENTS.md: "**No magic constants.** Declare every hardcoded value (number, string, duration, path, limit) as a named constant at the top of the file, after imports and before the first function, on lines you write or touch in Go, Python, and Bash (exempt: `0`, `1`, `-1`, `""`, a literal that is the subject of its line, and test files)".
- Repository rule, quoted: "**Name it for what it holds.** CodeQL treats `secret` or `trusted` in an identifier as a credential". No such word in any identifier this plan adds.
- Repository rule, quoted: "PR titles and commit messages use `type(optional-scope): description`"; "No AI co-author trailers (`Co-Authored-By:`) on commits".
- Go checks, from `a2a/`: `gofmt -l gateway` prints nothing, `go vet ./...` passes, `go test ./gateway -count=1` passes. From `k8s-operator/`: `go vet ./...` and `go test ./internal/controller -count=1`.
- Tests live beside the code (`a2a/gateway/*_test.go`, `k8s-operator/internal/controller/*_test.go`).
- After an operator API change, run `make -C k8s-operator generate manifests`. The `manifests` target also regenerates `docs/site/src/content/docs/operator/platformagent-fields.md` via `scripts/generate_docs.py`. Then run `make chart-sync` and confirm `make chart-check` exits 0 (the chart's mirrored CRD in `charts/kube-agents/crds/`).
- Site docs (`docs/site/src/content/docs/`) are for operators of their own install. They carry no maintainer identifiers and cite no PR or issue numbers in prose.
- Run `prettier --write` on every changed Markdown/YAML file, and `make docs-check` before the final commit.
- The upgrade page's 0.24.0 rows are the current release's; the new row is a 0.24.0 row.
- Spec values, verbatim. Function names are `kage.feedback` and `kage.feedback-note`. Button parameters are `task`, `rating` (`up`/`down`) and `cancel=1`. Button text is `👍`/`👎`, with alt text `Good answer`/`Bad answer`. The input is named `note`, labelled `Anything else?`, with `validation.characterLimit` 500 (`feedbackNoteChars`). Card lines are `What went wrong?`, `This answer's rating changed, so the note was not kept.` and `The note did not reach me, so the 👎 is kept without it.`. The rating carries `Platform: "googlechat"` (`platformGoogleChat`) and `Backend: "gchat"` (`gchatBackend`). The env var is `A2A_GOOGLE_CHAT_ANSWER_FEEDBACK`.
- Anti-goals. A sender the allowlist does not admit changes nothing: no record, no metric, no patch, no card. A tap is never a turn and never answers or claims a question. Buttons are classified by `onClick.action.function` or `onClick.openLink`, never by position. In a shared space the note card is private.
- Deviations from the spec's wording, decided here: the answer's feedback task id lives in a per-message `ux.rated` map, not on `gchatQuestion` (the answered rewrite drops the question record and later edits still need the id). The card's field mask is pinned `cards_v2` to match the adapter's existing `text,accessory_widgets` spelling; the live run confirms it. No new deadline constant: `Rate` has `feedbackRecorderDeadline` and the relay calls have `gchatRelayTimeout`.

## Review Focus

1. Feedback off (no recorder handed over, or the env set to `false`) and someone taps an older answer: nothing is recorded or patched, no card appears, and the drop is logged. Test: Task 3, `TestGchatRatingWithFeedbackOffChangesNothing`.
2. Two allowlisted people in one space rate the same answer differently: each keeps their own record (keyed by their email), and the buttons show the latest tap. Test: Task 3, `TestGchatTwoRatersInASpaceKeepTheirOwnRatings`.
3. The answer was deleted, so the filled-button patch fails with a 404: the rating stays recorded and a 👎 still opens the note card. Test: Task 4, `TestGchatDownWhoseAnswerPatchFailsStillAsksForTheNote`.
4. A note longer than the box allows (a client that ignores `characterLimit`, multibyte text): kept at exactly 500 characters, not 500 bytes, with no ellipsis. Test: Task 4, `TestGchatNoteIsCappedAtTheBoxsLimit`.
5. A home-channel post (alert, scheduled report) handed a mark: drawn without rating buttons. Test: Task 2, `TestGchatHomePostDrawsNoRatingButtons`.

---

### Task 1: One copy of the feedback words

**Files:**

- Create: `a2a/gateway/feedback_text.go`
- Modify: `a2a/gateway/slack_feedback.go:38-54` (word constants), `:276-279` (`slackFeedbackThanks`), `:283-291` (kept-line logic in `slackFeedbackModal`)
- Modify: `a2a/gateway/slack_feedback_test.go:312` (renamed constants)
- Test: `a2a/gateway/feedback_text_test.go`

**Interfaces:**

- Produces (in `feedback_text.go`): constants `feedbackGoodLabel`, `feedbackBadLabel`, `feedbackNoteTitle`, `feedbackNoteLabel`, `feedbackNoteSend`, `feedbackNoteCancel`, `feedbackKeptLine`, `feedbackKeptNoAsk`, `feedbackKeptDays`, `feedbackKeptOneDay`, `feedbackThanksLine`, `feedbackFormLine`, `feedbackFormURL`. Functions `func feedbackKeptText(days int, askKept bool) string` and `func feedbackThanks(link string) string`. `link` is the backend's own link markup around `feedbackFormURL`.
- `feedbackNoteChars` (500) stays in `feedback.go`, where it is already shared.

- [ ] **Step 1: Write the failing test** `TestFeedbackWordsAreOneCopyForEveryBackend`

```go
for got, want := range map[string]string{
	feedbackGoodLabel: "Good answer", feedbackBadLabel: "Bad answer",
	feedbackNoteTitle: "What went wrong?", feedbackNoteLabel: "Anything else?",
	feedbackNoteSend: "Send", feedbackNoteCancel: "Cancel",
	feedbackThanksLine: "Thanks. I kept this answer for your team to review.",
	feedbackFormURL: "https://gke-labs.github.io/kube-agents/feedback",
} { if got != want { t.Errorf("%q, want %q", got, want) } }
if got := feedbackKeptText(30, true); got != "Your question and this answer are kept on your install for 30 days so an operator can turn them into a test." { t.Error(got) }
if got := feedbackKeptText(1, true); !strings.Contains(got, "for 1 day so") { t.Error(got) }
if got := feedbackKeptText(30, false); got != "This answer is kept on your install for 30 days so an operator can turn it into a test." { t.Error(got) }
// Slack's thanks line is byte-for-byte what it was.
if got := slackFeedbackThanks(); got != "Thanks. I kept this answer for your team to review. To tell the kube-agents maintainers as well, use <https://gke-labs.github.io/kube-agents/feedback>. It opens a public issue, so leave out cluster names and other details from your install." { t.Error(got) }
```

- [ ] **Step 2: Run it and check that it fails**
      Run: `cd a2a && go test ./gateway -run TestFeedbackWordsAreOneCopyForEveryBackend -count=1`
      Expected: FAIL to build, with `undefined: feedbackGoodLabel`.

- [ ] **Step 3: Implement `feedback_text.go`, and rename the Slack constants with no aliases**
      `feedbackFormLine` is the Slack form line with `<%s>` replaced by `%s`, so each backend supplies its own link. Add `slackFeedbackFormLink = "<%s>"` to `slack_feedback.go`'s const block. `slackFeedbackThanks()` becomes `feedbackThanks(fmt.Sprintf(slackFeedbackFormLink, feedbackFormURL))`, and `feedbackThanks` joins the thanks line and the formatted form line with one space. `slackFeedbackModal` takes its line from `feedbackKeptText(days, askKept)`. Delete `slackFeedbackGood`/`Bad`/`Title`/`Label`/`Submit`/`Close`/`KeptLine`/`KeptNoAsk`/`Days`/`OneDay`/`ThanksLine`/`FormLine`/`FormURL` and update their uses, including the test at `slack_feedback_test.go:312`.

- [ ] **Step 4: Run the new test and every Slack feedback test**
      Run: `cd a2a && go test ./gateway -run 'TestFeedbackWords|TestSlack' -count=1`
      Expected: PASS.

- [ ] **Step 5: Commit**
      `git add a2a/gateway/feedback_text.go a2a/gateway/feedback_text_test.go a2a/gateway/slack_feedback.go a2a/gateway/slack_feedback_test.go`
      `git commit -m "refactor(a2a): share the answer-feedback words between chat backends"`

---

### Task 2: Draw the rating buttons on Google Chat

**Files:**

- Create: `a2a/gateway/gchat_feedback.go`
- Modify: `a2a/gateway/gchat.go:303-353` (adapter field `feedback FeedbackRecorder`, guarded by `mu`), `:526-536` (`PostMessage`), `:554-568` (`createLaidOut`)
- Modify: `a2a/gateway/gchat_clicks.go:89-103` (`gchatButton` gains fields), `:142-167` (`gchatUX` gains `rated`/`ratedOrder`, initialised in `uxLocked`)
- Modify: `a2a/gateway/gchat_test.go:278-338` (`fakeChatRelay` gains `queue map[string][]any`, responses popped per key before `responses` is read)
- Test: `a2a/gateway/gchat_feedback_test.go`

**Interfaces:**

- Constants (`gchat_feedback.go`): `gchatFeedbackFunction = "kage.feedback"`, `gchatFeedbackNoteFunction = "kage.feedback-note"`, `gchatFeedbackParamTask = "task"`, `gchatFeedbackParamRating = "rating"`, `gchatFeedbackParamCancel = "cancel"`, `gchatFeedbackCancelValue = "1"`, `gchatFeedbackUpText = "👍"`, `gchatFeedbackDownText = "👎"`.
- `type gchatButtonKind string`, with constants `gchatButtonOther = ""`, `gchatButtonChoice = "choice"`, `gchatButtonLink = "link"`, `gchatButtonRating = "rating"`, `gchatButtonNote = "note"`.
- `type gchatRating struct{ task, chosen string }`. An empty `task` means no buttons; `chosen` is `FeedbackUp`, `FeedbackDown` or "".
- `func (a *GoogleChatAdapter) SetFeedbackRecorder(r FeedbackRecorder)`, `func (a *GoogleChatAdapter) feedbackRecorder() FeedbackRecorder`, `func (a *GoogleChatAdapter) rates(m OutMessage) bool` (`m.Feedback != ""` and a recorder is held), and `var _ FeedbackSink = (*GoogleChatAdapter)(nil)`.
- `func gchatRatingButtons(r gchatRating) []map[string]any`: 👍 then 👎; the chosen one has `"type": gchatChatButtonFilled`, the other has no `type`. Each has `altText` and `onClick.action{function: gchatFeedbackFunction, parameters: [{key:"task"},{key:"rating"}]}`.
- `func gchatWithRating(widgets []map[string]any, r gchatRating) []map[string]any`: appends to the one `buttonList` (creating it when there is none) and returns new slices and maps. It never mutates its input, because `trackQuestion` keeps the layout. It returns the widgets unchanged when `r.task == ""`.
- `func classifyGchatButton(b gchatButton) gchatButtonKind` (by function and openLink only).
- `func gchatButtonWidget(b gchatButton) map[string]any` re-encodes a decoded button (text, type, altText, onClick).
- `gchatButton` gains `Type string \`json:"type"\``and`AltText string \`json:"altText"\``.
- Rating memory: `gchatUX.rated map[string]gchatRating` and `ratedOrder []string` (a `remember` ring capped at `uxCap`). `func (a *GoogleChatAdapter) ratingOf(message string) gchatRating` and `func (a *GoogleChatAdapter) rememberRating(message string, r gchatRating)`. The memory starts empty after a restart; a click then reads the filled state from the event's copy of the message (Task 5).
- `func (a *GoogleChatAdapter) postRatedText(conversation string, m OutMessage) (string, error)`.

- [ ] **Step 1: Write the failing tests** in `gchat_feedback_test.go`

Helper `newFeedbackGchatAdapter(t) (*GoogleChatAdapter, *fakeChatRelay, *fakeRecorder)`. The create response is `{"name":"spaces/_c2fixtureAA/messages/BOT1.BOT1","thread":{"name":"spaces/_c2fixtureAA/threads/BOT1"}}` and the patch response is the same name. It sets `syncRewrites = true` and uses `fakeRecorder{admit: {"sender@example.com": true}}` (from `slack_feedback_test.go`).

- `TestGchatRatedAnswerEndsWithFeedbackButtons`: post a Chat message with 2 choices and 1 link, with `Feedback: "t-root"`, to `gchat:dm/spaces/_c2fixtureAA`. The create body's one `buttonList` has 5 buttons. Button [3] is `text "👍"`, `altText "Good answer"`, function `kage.feedback`, params `task=t-root`, `rating=up`, with no `type`. Button [4] is `👎`/`Bad answer`/`down`. Buttons [0..2] are unchanged.
- `TestGchatRatedTextAnswerIsTextAndTheButtons`: `OutMessage{Text: "**It is OOMKilled.**", Feedback: "t-root"}` gives body `text == "*It is OOMKilled.*"` and one `buttonList` with exactly the two rating buttons.
- `TestGchatDrawsNoButtonsWithoutTheRecorder`: mirrors `TestSlackDrawsNoButtonsWithoutTheRecorder` on `a.rates`.
- `TestGchatRefusedRatedAnswerPostsWithoutTheButtons`: with `queue["spaces.messages/create"] = {400, ok}`, the laid-out post returns the name with nil error. The second body has 3 buttons and none of kind rating. The text post's second body has no `accessoryWidgets`. When both are refused, the laid-out post returns `ErrLayoutRefused` and the text post returns an error.
- `TestGchatHomePostDrawsNoRatingButtons` (Review Focus 5): `PostNotifyMessage("spaces/S1", "", OutMessage{Text:"x", Chat: <choices>, Feedback:"t-root"})` produces no button of kind rating, and `ratingOf(name).task == ""`.
- `TestGchatButtonKindsAreToldApartByFunction`: decoding the add-on fixture's buttons plus `gchatRatingButtons` plus a `kage.feedback-note` button gives choice, choice (the FILLED primary stays choice), link, rating, rating, note. `kage.other` gives `gchatButtonOther`.
- `TestGchatRatingButtonsRoundTrip`: `gchatRatingButtons(gchatRating{task:"t-root", chosen: FeedbackUp})` goes through JSON into `[]gchatButton`. The 👍 has `Type == "FILLED"`, the 👎 has `Type == ""`, and the alt texts are kept. `gchatButtonWidget(decoded[i])` marshals to the same JSON as the original.

- [ ] **Step 2: Run them and check that they fail**
      Run: `cd a2a && go test ./gateway -run 'TestGchatRated|TestGchatDrawsNo|TestGchatRefusedRated|TestGchatHomePostDraws|TestGchatButtonKinds|TestGchatRatingButtonsRoundTrip' -count=1`
      Expected: FAIL to build, with `undefined: gchatRatingButtons`.

- [ ] **Step 3: Implement**
      `PostMessage`: when `m.Chat == nil && a.rates(m)`, call `postRatedText`. It creates `{"text": toGchatText(m.Text), "accessoryWidgets": gchatWithRating(nil, …)}`; on `ErrLayoutRefused` (from `gchatLayoutError(err, false)`) it falls back to `a.Post(conversation, m.Text)`. `createLaidOut` draws the rating only when `conversation != "" && a.rates(m)`; on `ErrLayoutRefused` it creates once more without the rating and returns that outcome. After a create that carried the buttons, call `rememberRating(name, gchatRating{task: m.Feedback})`. `trackQuestion` keeps the layout without rating buttons.

- [ ] **Step 4: Run all gchat tests**
      Run: `cd a2a && go test ./gateway -run 'Gchat' -count=1`
      Expected: PASS.

- [ ] **Step 5: Commit**
      `git commit -m "feat(a2a): draw the answer-feedback buttons on Google Chat"` (the files above)

---

### Task 3: Take the tap

**Files:**

- Modify: `a2a/gateway/gchat_feedback.go`
- Modify: `a2a/gateway/gchat_clicks.go:277-355` (`classifyClick`: switch on `click.Function` first; extract the conversation binding)
- Test: `a2a/gateway/gchat_feedback_test.go`

**Interfaces:**

- Consumes (Task 2): `gchatRating`, `gchatRatingButtons`, `gchatWithRating`, `classifyGchatButton`, `gchatButtonWidget`, `ratingOf`, `rememberRating`, `feedbackRecorder`.
- Constant: `gchatFeedbackSeenPrefix = "feedback:"`.
- `func (a *GoogleChatAdapter) clickConversationLocked(ev *gchatEvent) string`, called under `a.mu`. It returns the remembered question's conversation, or else `gchatConversationID(ev.Space.Name, thread, kind)` from `gchatSurface(ev)`. `classifyClick`'s choice path uses it, with no behaviour change.
- `func (a *GoogleChatAdapter) feedbackClick(ev *gchatEvent) string` always returns a non-empty reason, so it is never a turn.
- `func (a *GoogleChatAdapter) ratedLayout(ev *gchatEvent, r gchatRating) gchatChatLayout`. Decision table: (a) a remembered question not yet answered gives `q.layout` plus the rating; (b) `ux.answered[name]` gives the event's text, the event's link buttons only, plus the rating; (c) anything else gives the event's text, its choice and link buttons re-encoded in event order, plus the rating.
- `func (a *GoogleChatAdapter) patchRating(message string, layout gchatChatLayout) error` patches `name`, `updateMask: gchatLayoutUpdateMask` and `body: gchatLayoutBody(layout)`.
- Produces for Task 4: inside the `goRewrite` closure, after a successful `Rate` and the patch attempt, a `down` calls `a.askForNote(ev, task, askKept)`. Task 4 defines it; Task 3 leaves that call out.

- [ ] **Step 1: Write the failing tests**

Helper `gchatRatingEvent(t, rating string) *gchatEvent` starts from `gchatClickEvent(t)` and sets `click.Function = gchatFeedbackFunction` and `click.Parameters = {task: "t-root", rating: rating}`. It appends `gchatRatingButtons(gchatRating{task:"t-root"})`, round-tripped through JSON, to the event message's `buttonList`. Helper `gchatSpaceEvent(ev)` sets the space to `spaces/AAA` (`SpaceType "SPACE"`, `THREADED_MESSAGES`), the message to `spaces/AAA/messages/M1` and the thread to `spaces/AAA/threads/T1`.

- `TestGchatRatingInBothEventLayoutsIsRecorded`: the add-on event, and the legacy fixture with `common.invokedFunction`/`parameters` rewritten to the rating, each give one `Rate` with the same `TaskID`/`Rating`/`Rater`.
- `TestGchatRatingTapsTheGatewayCannotReadAreDropped`: these give no `Rate`, no relay call, and `reason != ""`: no task; rating `"sideways"`; `User.Email == ""`; `Message.Name == ""`.
- `TestGchatUpRatingIsRecordedAndIsNoTurn`: `msg == InboundMessage{}` and `reason != ""`. The rating equals `FeedbackRating{Platform:"googlechat", Backend:"gchat", Rater:"sender@example.com", Conversation:"gchat:dm/spaces/_c2fixtureAA/threads/BOT1", MessageID:"spaces/_c2fixtureAA/messages/BOT1.BOT1", TaskID:"t-root", Rating:"up", Answer:"*Which cluster?*\n\nTwo clusters run one."}`.
- `TestGchatRatingAfterARestartPatchesFromTheEventsCopy`: with nothing posted first, one patch call. Its text is the event text. Its buttons are the event's two choices (the first keeps `type FILLED`), the link, `👍` with `type FILLED` and `👎` with no type.
- `TestGchatRatingOnARememberedAnswerPatchesFromItsLayout`: `PostMessage` a question with `Feedback`, then tap 👎. The patch text is the posted layout text, and the buttons are choices, link, 👍 (no type), 👎 (FILLED). A choice click classified afterwards is still a turn, so the tap claimed nothing (no `waiting`, no `answered`).
- `TestGchatRatingNeverRestoresAnsweredChoices`: post, choice click, `InboundVerified(…, true)`, then a 👍 tap whose event copy still shows the choices. The patch has no button of kind choice and keeps the link and both rating buttons.
- `TestGchatReRatingReplacesTheFilledButton`: 👎 then 👍. The second patch shows 👍 FILLED and 👎 with no type, and `ratingOf(name).chosen == FeedbackUp`.
- `TestGchatRatingFromAnUnverifiedSenderChangesNothing`: `admit` empty gives no `Rate`, no relay call, and `ratingOf(name).chosen == ""`.
- `TestGchatRedeliveredRatingIsHandledOnce`: classifying the same event twice gives 1 `Rate` and 1 patch, and the second reason contains `"duplicate"`.
- `TestGchatFailedRatingWriteLeavesTheMessage`: with `rateErr = errFeedbackContended`, no relay call and nothing remembered.
- `TestGchatRatingWithFeedbackOffChangesNothing` (Review Focus 1): with no `SetFeedbackRecorder`, the tap gives `reason != ""`, no relay call, and nothing remembered.
- `TestGchatTwoRatersInASpaceKeepTheirOwnRatings` (Review Focus 2): in a space, `a@example.com` taps 👍 and then `b@example.com` taps 👎, both admitted. There are two ratings with distinct `Rater`, both with `Conversation "gchat:spaces/AAA/threads/T1"`, and the last patch shows 👎 FILLED.

- [ ] **Step 2: Run them and check that they fail**
      Run: `cd a2a && go test ./gateway -run 'TestGchatRating|TestGchatUpRating|TestGchatReRating|TestGchatRedeliveredRating|TestGchatFailedRating|TestGchatTwoRaters' -count=1`
      Expected: FAIL (the rating click is dropped as "a button this adapter did not draw"; no `Rate`).

- [ ] **Step 3: Implement `feedbackClick` and the `classifyClick` branch**
      Order of checks: recorder present, `task` set, `rating` up/down, sender email, space and message names. Then dedupe `gchatFeedbackSeenPrefix + gchatClickID(name, eventTime)` through `seenLocked`, only when `EventTime != ""`, as the choice path does. Then `recorder.Admits(gchatBackend, email)`. Then `a.goRewrite` runs `Rate`, then `rememberRating`, then `patchRating(ratedLayout(...))`. A failure logs and leaves the message as it was. `Rate` is bounded by the recorder's own `feedbackRecorderDeadline` and the relay calls by `gchatRelayTimeout`; add no new deadline.

- [ ] **Step 4: Run all gchat tests**
      Run: `cd a2a && go test ./gateway -run 'Gchat' -count=1`
      Expected: PASS.

- [ ] **Step 5: Commit**
      `git commit -m "feat(a2a): record a Google Chat answer rating and fill the tapped button"`

---

### Task 4: The 👎 note card

**Files:**

- Modify: `a2a/gateway/gchat_feedback.go`
- Modify: `a2a/gateway/gchat_clicks.go:64-67` (`gchatCommonEvent.FormInputs`), `:108-113` (`gchatClick.FormInputs`), `:517-530` and `:540-553` (both decoders copy it)
- Test: `a2a/gateway/gchat_feedback_test.go`

**Interfaces:**

- Consumes: Task 3's `feedbackClick` closure (add the `askForNote` call on `down`) and its classify branch; Task 1's `feedbackKeptText`, `feedbackThanks`, `feedbackNoteTitle`/`Label`/`Send`/`Cancel` and `feedbackFormURL`.
- Constants: `gchatNoteCardID = "kage.feedback-note"`, `gchatNoteInputName = "note"`, `gchatNoteInputSingleLine = "SINGLE_LINE"`, `gchatNoteHint = "Up to %d characters."`, `gchatNoteCardsMask = "cards_v2"`, `gchatNoteLost = "The note did not reach me, so the 👎 is kept without it."`, `gchatNoteReplaced = "This answer's rating changed, so the note was not kept."`, `` gchatCardLink = `<a href="%s">%s</a>` ``, `gchatFeedbackNoteSeenPrefix = "feedback-note:"`, `gchatPrivateViewerKey = "privateMessageViewer"`.
- `type gchatFormInput struct{ StringInputs struct{ Value []string \`json:"value"\` } \`json:"stringInputs"\` }`. `gchatCommonEvent.FormInputs map[string]gchatFormInput \`json:"formInputs"\``. A nil map means absent; `{}` means present.
- `func (c *gchatClick) formValue(name string) (value string, present bool)`, where `present` is `c.FormInputs != nil`.
- `func gchatNoteText(raw string) string`: `strings.TrimSpace`, then at most `feedbackNoteChars` code points, no ellipsis.
- `func gchatNoteCard(task string, days int, askKept bool) map[string]any` returns `{"cardId": gchatNoteCardID, "card": {"sections": [{"widgets": [...]}]}}`. The widgets, in order: `textParagraph{feedbackNoteTitle}`; `textInput{name:"note", label:"Anything else?", type:"SINGLE_LINE", hintText:"Up to 500 characters.", validation:{characterLimit:500}}`; `textParagraph{feedbackKeptText(days, askKept)}`; `buttonList[Send{function:kage.feedback-note, params task, rating=down}, Cancel{same plus cancel=1}]`.
- `func gchatCollapsedCard(text string) map[string]any`: the same `cardId`, one `textParagraph`.
- `func gchatNoteThanks() string` is `feedbackThanks(fmt.Sprintf(gchatCardLink, feedbackFormURL, feedbackFormURL))`.
- `func (a *GoogleChatAdapter) createCard(space, thread, viewer string, card map[string]any) (string, error)` builds the body `{"cardsV2":[card]}` plus `privateMessageViewer:{name: viewer}` when the viewer is set, through `a.create`.
- `func (a *GoogleChatAdapter) askForNote(ev *gchatEvent, task string, askKept bool)`. The viewer is "" when `gchatSurface` gives `sessionKindDM`, and `click.User.Name` otherwise. In a space whose clicker has no `users/` name it logs and posts nothing. The thread comes from `gchatSurface`. If a private create with a thread fails, it retries once with thread ""; a second failure logs and stops.
- `func (a *GoogleChatAdapter) feedbackNoteClick(ev *gchatEvent) string`: never a turn. It patches through `collapseNoteCard`.
- `func (a *GoogleChatAdapter) collapseNoteCard(ev *gchatEvent, text string)` patches `ev.Message.Name` with `updateMask: gchatNoteCardsMask` and `body {"cardsV2":[gchatCollapsedCard(text)]}`. On error it calls `createCard(space, thread, viewer, gchatCollapsedCard(text))`.

- [ ] **Step 1: Write the failing tests**

Helper `gchatNoteEvent(t, formInputs map[string]any, cancel bool) *gchatEvent` builds the add-on JSON with `commonEventObject.invokedFunction "kage.feedback-note"`, parameters `task=t-root`, `rating=down` (plus `cancel=1`) and `formInputs` when non-nil, on message `spaces/_c2fixtureAA/messages/CARD1`, decoded through `decodeGchatEvent`.

- `TestDecodeGchatFormInputsInBothLayouts`: add-on `commonEventObject.formInputs.note.stringInputs.value ["x"]` gives `("x", true)`, and so does legacy `common.formInputs`. Absent gives `("", false)`. `"formInputs": {}` gives `("", true)`.
- `TestGchatDownRatingPostsTheNoteCardInTheThread` (DM): `rec.onRate` records the relay call count, which is 0 at Rate. The create after the patch has `parent "spaces/_c2fixtureAA"`, `thread.name "spaces/_c2fixtureAA/threads/BOT1"` and no `privateMessageViewer`. Its `cardsV2[0].cardId` is `"kage.feedback-note"`, and its widgets carry exactly the values in the Interfaces block. The kept line is `"Your question and this answer are kept on your install for 30 days so an operator can turn them into a test."`.
- `TestGchatDownRatingWithNoAskOnRecordSaysOnlyTheAnswerIsKept`: with `rec.noAsk`, the card has `"This answer is kept on your install for 30 days so an operator can turn it into a test."` and no "Your question".
- `TestGchatNoteCardInASpaceIsPrivateToTheRater`: `privateMessageViewer.name == "users/100000000000000000042"`, thread `spaces/AAA/threads/T1`.
- `TestGchatRefusedPrivateThreadReplyRetriesAtTheTopLevel`: with create queue `{answer patch is a patch, 400, ok}`, the second card create has no `thread` and still has `privateMessageViewer`. With `{400, 400}` there is no third create, and `rec.ratings` still holds the 👎.
- `TestGchatUpRatingPostsNoCard` and `TestGchatFailedDownWritePostsNoCard`: no create call.
- `TestGchatNoteJoinsTheRatingAndCollapsesTheCard`: Send with `note " the limit is 256Mi "` gives `rec.notes == ["sender@example.com t-root  gchat:dm/spaces/_c2fixtureAA/threads/BOT1 the limit is 256Mi"]`. The rebuilt rating has an empty `MessageID`, and `Conversation` comes from the card event's surface. The patch has `name "spaces/_c2fixtureAA/messages/CARD1"` and `updateMask "cards_v2"`, and its one widget's `textParagraph.text == gchatNoteThanks()`, which contains `<a href="https://gke-labs.github.io/kube-agents/feedback">` and `"public issue"`.
- `TestGchatEmptyNoteIsStillKeptAndThanked`: `value [""]` and `formInputs {}` each give one note ending in a space with an empty note, and the thanks patch.
- `TestGchatSendWithoutFormInputsSaysTheNoteDidNotArrive`: no `formInputs` gives `len(rec.notes) == 0` and patch text `gchatNoteLost`.
- `TestGchatCancelCollapsesTheCardWithoutANote`: `cancel=1` gives no `Note` and patch text `gchatNoteThanks()`.
- `TestGchatNoteThatWasNotKeptSaysTheRatingChanged`: `noteErr = errFeedbackNotKept` gives patch text `gchatNoteReplaced`. `noteErr = errFeedbackContended` gives no patch, so the card stays for another Send.
- `TestGchatRefusedCardPatchPostsTheLineAsANewPrivateMessage` (space): patch 400 gives a create with `privateMessageViewer`, `thread spaces/AAA/threads/T1` and the collapsed card.
- `TestGchatRedeliveredSendIsHandledOnce`: the same event twice gives 1 note and 1 patch.
- `TestGchatNoteFromAnUnverifiedSenderChangesNothing`: no `Note`, no relay call.
- `TestGchatNoteIsCappedAtTheBoxsLimit` (Review Focus 4): `strings.Repeat("é", 600)` gives a kept note with `utf8.RuneCountInString == 500`, no `…`.
- `TestGchatDownWhoseAnswerPatchFailsStillAsksForTheNote` (Review Focus 3): with the answer patch failing 404, the rating is recorded and the card create is made.

- [ ] **Step 2: Run them and check that they fail**
      Run: `cd a2a && go test ./gateway -run 'FormInputs|NoteCard|Note|PostsNoCard|PostsTheNoteCard|RetriesAtTheTopLevel|AsksForTheNote' -count=1`
      Expected: FAIL to build, with `undefined: gchatNoteCard`.

- [ ] **Step 3: Implement the card, the decode and the Send/Cancel handling**
      Checks: recorder present, `task` set, `rating == down`, email, space and message. Then dedupe `gchatFeedbackNoteSeenPrefix + gchatClickID(...)` and `Admits`. In `goRewrite`: Cancel collapses; Send with `present == false` logs a warning and collapses to `gchatNoteLost`; otherwise call `Note(rating, gchatNoteText(value))`, then thanks, `gchatNoteReplaced` on `errFeedbackNotKept`, or log only for other errors.

- [ ] **Step 4: Run all gchat tests**
      Run: `cd a2a && go test ./gateway -run 'Gchat' -count=1`
      Expected: PASS.

- [ ] **Step 5: Commit**
      `git commit -m "feat(a2a): ask for a note after a Google Chat thumbs-down"`

---

### Task 5: Rewrites keep the ratings

**Files:**

- Modify: `a2a/gateway/gchat_clicks.go:118-129` (`gchatPendingClick.rating gchatRating`), `:330-337` (both binding paths set `p.rating`), `:372-391` (`gchatMessageButtons`), `:399-425` (`InboundVerified`), `:465-490` (`gchatAnsweredLayout`, `answeredLayout`)
- Modify: `a2a/gateway/gchat_clicks_test.go:74,636,640` (three return values)
- Test: `a2a/gateway/gchat_feedback_test.go`

**Interfaces:**

- Consumes (Task 2): `gchatRating`, `gchatWithRating`, `classifyGchatButton`, `ratingOf`.
- `func gchatMessageButtons(m gchatMessage, index int, fallback string) (links []chatmsg.Link, label string, rating gchatRating)`. Kinds come from `classifyGchatButton`; `rating.task` is the rating button's `task` parameter, and `rating.chosen` is the rating of the button whose `Type == gchatChatButtonFilled`.
- `func gchatAnsweredLayout(layout gchatChatLayout, links []chatmsg.Link, rating gchatRating, line string) gchatChatLayout`, whose widgets are `gchatWithRating(gchatChatWidgets(nil, links), rating)`.
- In `InboundVerified`, build the layout inside the `goRewrite` closure with `rating := a.ratingOf(p.message)`, falling back to `p.rating` when `rating.task == ""`. `answeredLayout` passes `a.ratingOf(messageID)`.
- The task id of a remembered message lives in `ux.rated` (set at post time in Task 2), not on `gchatQuestion`, so it outlives the question record that the answered rewrite drops.

- [ ] **Step 1: Write the failing tests**

- `TestGchatAnsweredRewriteKeepsTheRatingButtons`: post a question with `Feedback`, tap 👍, choice click, verified. The rewrite's buttons are link, 👍 FILLED, 👎 with no type; its text ends with `"✓ Test Sender: Look at the first one"`.
- `TestGchatTypedAnswerRewriteKeepsTheRatingUnfilled`: post the question with `Feedback` and no tap, send a typed reply through `classify` of a MESSAGE event in its conversation, then `InboundVerified`. The rewrite has link, 👍 and 👎, neither with a type.
- `TestGchatClickAfterARestartKeepsTheRatingButtonsAsShown`: nothing posted. The event copy has choices, link, and 👍/👎 with 👎 `type FILLED`. Classify the choice click, then verify. The rewrite has no choice buttons, has the link, 👍 with no type and 👎 FILLED.
- `TestGchatMessageButtonsTellTheKindsApartByFunction`: rating buttons placed before the link, and a FILLED primary choice. The links are the link alone, the label comes from the index, and `rating == gchatRating{"t-root", ""}`. With 👎 FILLED, `chosen == "down"`.
- `TestGchatLaterEditOfAnAnsweredRatedMessageKeepsItsButtons`: after the answered rewrite, `EditMessage(conv, id, OutMessage{Text: "settled"})`. The patch text has the answered line, and the buttons are both rating buttons.

- [ ] **Step 2: Run them and check that they fail**
      Run: `cd a2a && go test ./gateway -run 'KeepsTheRating|MessageButtonsTell|LaterEditOfAnAnsweredRated' -count=1`
      Expected: FAIL (the rewritten buttons lack the rating buttons).

- [ ] **Step 3: Implement the signatures above and update the three callers in the tests.**

- [ ] **Step 4: Run all gchat tests**
      Run: `cd a2a && go test ./gateway -run 'Gchat' -count=1`
      Expected: PASS.

- [ ] **Step 5: Commit**
      `git commit -m "feat(a2a): keep Google Chat rating buttons through answered rewrites"`

---

### Task 6: The off switch, the chart and the metric

**Files:**

- Modify: `k8s-operator/api/v1alpha1/platformagent_types.go:260-295` (`GoogleChatSpec.AnswerFeedback`)
- Generated: `k8s-operator/api/v1alpha1/zz_generated.deepcopy.go`, `k8s-operator/config/crd/bases/kubeagents.x-k8s.io_platformagents.yaml`, `docs/site/src/content/docs/operator/platformagent-fields.md`, `charts/kube-agents/crds/kubeagents.x-k8s.io_platformagents.yaml`
- Modify: `k8s-operator/internal/controller/platformagent_a2a_manifests.go:593-595` (constant), `:4936-4965` (the `a2aChatArmed` block)
- Test: `k8s-operator/internal/controller/platformagent_a2a_gchat_test.go`
- Modify: `a2a/gateway/config.go:74-77,225-228,721`, `a2a/gateway/feedback.go:20-24` (header comment: Slack and Google Chat), `:212` (the log names both fields), `a2a/gateway/metrics.go:163-164,307-308`
- Test: `a2a/gateway/feedback_test.go`, `a2a/gateway/metrics_test.go` (create it if absent)
- Modify: `charts/kube-agents/values.yaml:1571-1582`, `charts/kube-agents/values.schema.json:1325-1350`, `charts/kube-agents/templates/platform-agent-cr.yaml:875-878`

**Interfaces:**

- CRD: `AnswerFeedback *bool \`json:"answerFeedback,omitempty"\``with`// +kubebuilder:default=true`and`// +optional`. Its doc comment is the Slack field's twin: "AnswerFeedback draws thumbs-up and thumbs-down buttons under each final answer on Google Chat under spec.mode next, and keeps each rating with its question and answer (a thumbs-down with its note) on the install for the feedback bucket's max age (30 days as provisioned). False hides the buttons and keeps nothing."
- Operator: `a2aGchatAnswerFeedbackEnvVar = "A2A_GOOGLE_CHAT_ANSWER_FEEDBACK"`, appended to `chatEnv` as `"false"` only inside `if a2aChatArmed(agent)` when `gchat.AnswerFeedback != nil && !*gchat.AnswerFeedback`.
- Gateway: `envGchatAnswerFeedback = "A2A_GOOGLE_CHAT_ANSWER_FEEDBACK"`. `cfg.AnswerFeedbackOff` is true when either the trimmed Slack or Google Chat value equals `strconv.FormatBool(false)`.
- Metrics: `metricsRatedPlatforms = []string{platformGoogleChat, platformSlack}` in the var block at `metrics.go:163`; zero series for each platform × `metricsRatings`.

- [ ] **Step 1: Write the failing tests**

- `TestGchatAnswerFeedbackOffIsTheOnlyRenderedState` (operator; mirrors `TestAnswerFeedbackOffIsTheOnlyRenderedState`, with `t.Setenv(a2aInjectBackendEnvVar, "")`). With `gchatTestAgent("next", true)`: unset renders nothing, `true` renders nothing, `false` renders `"false"`. With `gchatTestAgent("", true)` and `false`, nothing renders (Chat not on the gateway). With `gchatTestAgent("next", false)` and `false`, nothing renders.
- `TestGchatAnswerFeedbackOffIsReadFromItsEnv` (`feedback_test.go`, `setBaseEnv(t)`). (slack, gchat) pairs map to off: `("","")` gives false, `("","false")` true, `("", " false ")` true, `("false","")` true, `("true","true")` false, `("","true")` false.
- `TestAnswerFeedbackZeroSeriesCoverBothRatingBackends` (`metrics_test.go`), using `github.com/prometheus/client_golang/prometheus/testutil`: `m := NewMetrics()` (or the package's constructor, whichever `gateway.go` calls); `testutil.CollectAndCount(m.answerFeedback) == 4`, and `testutil.ToFloat64(m.answerFeedback.WithLabelValues(platformGoogleChat, FeedbackUp)) == 0`.

- [ ] **Step 2: Run them and check that they fail**
      Run: `cd k8s-operator && go test ./internal/controller -run TestGchatAnswerFeedbackOffIsTheOnlyRenderedState -count=1`, which should fail to build with `AnswerFeedback undefined`.
      Run: `cd a2a && go test ./gateway -run 'TestGchatAnswerFeedbackOff|TestAnswerFeedbackZeroSeries' -count=1`, which should FAIL.

- [ ] **Step 3: Implement the field, the env and the metric; regenerate**
      Run: `make -C k8s-operator generate manifests`, then `make chart-sync`. Expected: the diff adds `answerFeedback` (default `true`) under `googleChat` in both CRD copies, a DeepCopy branch for the pointer, and an `integration.googleChat.answerFeedback` row on the fields page.

- [ ] **Step 4: Add the chart value**
      In `values.yaml`, under `googleChat`, add `answerFeedback: true` with a three-line comment mirroring Slack's at `:1598-1600`. In the schema, add `"answerFeedback": {"type": "boolean"}` to the `googleChat` properties. In the template, after the `homeChannel` `with` block, add `{{- if eq (toString $integ.googleChat.answerFeedback) "false" }}` / `answerFeedback: false` / `{{- end }}`.

- [ ] **Step 5: Verify**
      Run: `cd a2a && gofmt -l gateway && go vet ./... && go test ./gateway -count=1`. Expected: no gofmt output, PASS.
      Run: `cd k8s-operator && go vet ./... && go test ./internal/controller -count=1`. Expected: PASS.
      Run: `make chart-check`. Expected: exit 0.
      Run: `helm template t charts/kube-agents --set platformAgent.harness.projectId=p --set platformAgent.integration.googleChat.enabled=true --set platformAgent.integration.googleChat.answerFeedback=false --show-only templates/platform-agent-cr.yaml | grep -n answerFeedback` (add any other `--set` the chart's `required` calls name). Expected: one `answerFeedback: false` line under `googleChat:`, and no line without the `--set`.

- [ ] **Step 6: Commit**
      `git commit -m "feat(operator): add googleChat.answerFeedback to turn Google Chat answer feedback off"` (the API, generated files, chart, gateway config, metrics and tests)

---

### Task 7: Docs

**Files:**

- Modify: `docs/designs/spec-chatops-gateway.md:2297-2368` (Answer feedback), the clicks paragraph in `:1746-1777`, and the metrics row `:2644` if needed
- Modify: `docs/site/src/content/docs/concepts/chatops.md:88-105` (move and rewrite), and the "On Google Chat and the A2A console" paragraphs (`:344-355`)
- Modify: `docs/site/src/content/docs/operator/platformagent-crd.md:1132`
- Modify: `docs/site/src/content/docs/install/upgrade.md` (new 0.24.0 row after `:263`)
- Check only: `docs/site/src/content/docs/concepts/observability.md:105`, which already names the platform label neutrally.

**Interfaces:** Consumes the field, env and chart names from Task 6, and the behaviour from Tasks 2–5.

- [ ] **Step 1: Spec.** Replace "Every other backend ignores the mark." with "The Google Chat adapter draws it too (below); Discord and the console ignore it." Add Google Chat paragraphs before "An install turns it off" covering: the buttons (`kage.feedback`, `task`/`rating`, after choices and links, text marks carry them on the last chunk, a refusal posts without them, home posts never); the tap (allowlist, dedupe, FILLED edit from the adapter's record or else the event copy, never restoring removed choices, restart); the private note card (`cardsV2`, DM plain, top-level retry, `kage.feedback-note`, `formInputs` absent versus empty, the three collapse lines, a refused patch posting a new private message); and the rewrites keeping the buttons. Extend the off paragraph with `spec.integration.googleChat.answerFeedback` / `A2A_GOOGLE_CHAT_ANSWER_FEEDBACK`, either one turning off the single global flag. In the Google Chat clicks section, add one sentence: rating and note clicks are never turns (link to "Answer feedback").
- [ ] **Step 2: Site concepts.** Move `### Answer feedback` from under `## Slack` to just before `### \`/feedback\``under`## Conversations under \`spec.mode: next\``, keeping the heading text so `#answer-feedback`still resolves. Rewrite it platform-neutrally: buttons on Slack and Google Chat; the tapped button fills in. State the two differences in prose: on Google Chat the 👎 box is a card in the answer's thread that only you see (in a DM, the DM itself), and Send or Cancel collapses it to the thanks line and the form link; the week's count is on Slack's Home tab only. The bucket's keys name "the rater's Slack member ID or Google Chat address". Turning it off:`spec.integration.slack.answerFeedback`or`spec.integration.googleChat.answerFeedback`, the chart's `platformAgent.integration.<slack|googleChat>.answerFeedback`, and `TF_VAR_extra_helm_values='{platformAgent={integration={googleChat={answerFeedback=false}}}}'`. Add a sentence to the Google Chat paragraphs pointing at it.
- [ ] **Step 3: CRD guide.** In the `googleChat` bullet, add `answerFeedback` (default `true`; under `mode: next`, `false` hides the 👍/👎 buttons under answers and keeps no rating, linking to [Answer feedback](/kube-agents/concepts/chatops/#answer-feedback)). The fields page was regenerated in Task 6; confirm the row is there.
- [ ] **Step 4: Upgrade row.** `| 0.24.0 | Google Chat answers can be rated under \`spec.mode: next\` | \`spec.mode: next\` only. The final answer to each ask on Google Chat ends with 👍 and 👎; a 👎 opens a card only the rater sees for an optional note, and each rating keeps the question and answer in the install's \`feedback\` bucket for its max age. To turn it off, set \`spec.integration.googleChat.answerFeedback: false\` (the chart's \`platformAgent.integration.googleChat.answerFeedback\`). See [Answer feedback](/kube-agents/concepts/chatops/#answer-feedback). |`
- [ ] **Step 5: Format and check.** Run `prettier --write` on the four changed Markdown files, then `make docs-check`. Expected: exit 0. If `docs-check-prose` reports a count below a ceiling, run `python3 scripts/check_docs_prose.py --write-ratchet` and re-run.
- [ ] **Step 6: Commit**
      `git commit -m "docs: answer feedback on Google Chat and its off switch"`
