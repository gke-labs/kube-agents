package hermesbridge

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"maps"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// chatCases is testdata/chat_cases.json: the Python presenter's own answers,
// which agents/platform/scripts/test_chat_message.py keeps in step with it.
type chatCases struct {
	SplitAnswer []struct {
		In       string   `json:"in"`
		Headline string   `json:"headline"`
		Body     []string `json:"body"`
	} `json:"split_answer"`
	Plain []struct {
		In  string `json:"in"`
		Out string `json:"out"`
	} `json:"plain"`
	AskKind []struct {
		In   string `json:"in"`
		Kind string `json:"kind"`
	} `json:"ask_kind"`
	AnswerChat []struct {
		In   string          `json:"in"`
		Chat json.RawMessage `json:"chat"`
	} `json:"answer_chat"`
}

func loadChatCases(t *testing.T) chatCases {
	t.Helper()
	raw, err := os.ReadFile("testdata/chat_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases chatCases
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases.SplitAnswer) == 0 || len(cases.Plain) == 0 || len(cases.AskKind) == 0 || len(cases.AnswerChat) == 0 {
		t.Fatal("testdata/chat_cases.json has an empty section")
	}
	return cases
}

func TestSplitAnswerMatchesThePresenter(t *testing.T) {
	for _, c := range loadChatCases(t).SplitAnswer {
		headline, body := splitAnswer(c.In)
		if body == nil {
			body = []string{}
		}
		if headline != c.Headline || !slices.Equal(body, c.Body) {
			t.Errorf("splitAnswer(%q) = %q, %q; want %q, %q", c.In, headline, body, c.Headline, c.Body)
		}
	}
}

func TestPlainLineMatchesThePresenter(t *testing.T) {
	for _, c := range loadChatCases(t).Plain {
		if got := plainLine(c.In); got != c.Out {
			t.Errorf("plainLine(%q) = %q, want %q", c.In, got, c.Out)
		}
	}
}

func TestAskKindMatchesThePresenter(t *testing.T) {
	for _, c := range loadChatCases(t).AskKind {
		if got := askKind(c.In); got != c.Kind {
			t.Errorf("askKind(%q) = %q, want %q", c.In, got, c.Kind)
		}
	}
}

// The whole answer layout, the clause cut and the options as choices
// included, is chat_message.answer_message's: null where it posts text.
func TestAnswerChatMatchesThePresenter(t *testing.T) {
	for _, c := range loadChatCases(t).AnswerChat {
		msg, ok := answerChat(c.In)
		got := []byte("null")
		if ok {
			var err error
			if got, err = json.Marshal(msg); err != nil {
				t.Fatal(err)
			}
		}
		if !sameChat(t, lib.Part{Data: got}, c.Chat) {
			t.Errorf("answerChat(%.60q) = %s\nwant %s", c.In, got, c.Chat)
		}
	}
}

// The answer: the rest as the detail, under "why" past the detail's cap, and
// no chat message where the presenter posts plain text.
func TestAnswerChat(t *testing.T) {
	msg, ok := answerChat("**Yes.** It restarted 14 times.\n\nRaise the limit.")
	if !ok || msg.Headline != "Yes." || msg.Detail != "It restarted 14 times.\n\nRaise the limit." || msg.Fold != nil {
		t.Fatalf("answerChat = %+v, %v", msg, ok)
	}
	if msg, ok := answerChat("Done."); !ok || msg.Detail != "" || msg.Fold != nil {
		t.Errorf("one sentence: %+v, %v; want a headline alone", msg, ok)
	}
	long := "X" + strings.Repeat("x", chatDetailMax)
	if msg, ok := answerChat("Done. " + long); !ok || msg.Detail != "" || msg.Fold == nil || msg.Fold.Title != "why" || msg.Fold.Text != long {
		t.Errorf("a rest past the detail's cap: %+v, %v; want it under why", msg, ok)
	}
	if _, ok := answerChat("```\nkubectl get pods\n```"); ok {
		t.Error("an answer opening on a fence has no headline, so no chat message")
	}
	if _, ok := answerChat("Done. X" + strings.Repeat("x", chatFoldTextMax)); ok {
		t.Error("a rest past the fold's cap posts as text")
	}
	part, ok := resultChatPart("**Yes.** It restarted 14 times.")
	if !ok || part.Kind != "data" || !strings.Contains(string(part.Data), `"schema":"kube-agents.chat/v1"`) {
		t.Errorf("resultChatPart = %+v, %v", part, ok)
	}
}

// sharedCase is one tests/conformance/chat case, as far as the bridge reads
// it: its input's events, or its markdown, and the expected outputs.
type sharedCase struct {
	Name  string `json:"name"`
	Input struct {
		Markdown string `json:"markdown"`
		Events   []struct {
			Ask        string   `json:"ask"`
			Terminal   string   `json:"terminal"`
			FiledCards []string `json:"filed_cards"`
			TaskID     string   `json:"task_id"`
		} `json:"events"`
	} `json:"input"`
	Expected []*struct {
		Carrier string          `json:"carrier"`
		Chat    json.RawMessage `json:"chat"`
	} `json:"expected"`
}

const sharedCasesDir = "../../tests/conformance/chat"

func loadSharedCases(t *testing.T) []sharedCase {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(sharedCasesDir, "*.json"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no shared cases under %s: %v", sharedCasesDir, err)
	}
	var cases []sharedCase
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var c sharedCase
		if err := json.Unmarshal(raw, &c); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		cases = append(cases, c)
	}
	return cases
}

func sameChat(t *testing.T, got lib.Part, want json.RawMessage) bool {
	t.Helper()
	var g, w any
	if err := json.Unmarshal(got.Data, &g); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(want, &w); err != nil {
		t.Fatal(err)
	}
	return reflect.DeepEqual(g, w)
}

// The shared cases the bridge owns: the answer on the result, the ask on
// each heartbeat, and the terminal's mark (none for a direct answer, the ask
// for a turn that filed cards). The notifies are the kanban notifier's,
// which deploy/docker/patches/test_chat_conformance.py runs.
func TestBridgeSharedCases(t *testing.T) {
	t.Setenv(slackUXEnv, "1")
	ran := 0
	for _, c := range loadSharedCases(t) {
		if c.Input.Markdown != "" {
			ran++
			part, ok := resultChatPart(c.Input.Markdown)
			if out := c.Expected[0]; !ok || out.Carrier != string(chatmsg.Result) || !sameChat(t, part, out.Chat) {
				t.Errorf("%s: result chat = %s, want %s", c.Name, part.Data, out.Chat)
			}
			continue
		}
		ask := ""
		for i, ev := range c.Input.Events {
			want := c.Expected[i]
			switch {
			case ev.Ask != "":
				ran++
				ask = askKind(ev.Ask)
				parts := progressParts("running 2s, 0 tool call(s)", ask)
				if want == nil || want.Carrier != string(chatmsg.Progress) || len(parts) != 2 || !sameChat(t, parts[1], want.Chat) {
					t.Errorf("%s[%d]: progress parts = %+v, want %+v", c.Name, i, parts, want)
				}
			case ev.Terminal != "":
				ran++
				run := &taskRun{origin: slackOrigin(t, c.Input.Events[i-1].Ask)}
				run.routeRecorded.Store(true)
				act := newActivityState(false)
				act.cardsFiled.Store(len(ev.FiledCards) > 0)
				run.act.Store(act)
				part, ok := cardsMark(run, lib.TaskState(ev.Terminal))
				switch {
				case want == nil && ok:
					t.Errorf("%s[%d]: terminal carries %s, want no mark", c.Name, i, part.Data)
				case want != nil && (!ok || want.Carrier != string(chatmsg.Terminal) || !sameChat(t, part, want.Chat)):
					t.Errorf("%s[%d]: terminal = %s, want %s", c.Name, i, part.Data, want.Chat)
				}
			}
		}
	}
	if ran == 0 {
		t.Fatal("no shared case reached the bridge")
	}
}

// chatData is the kube-agents.chat/v1 object among parts, or nil.
func chatData(t *testing.T, parts []lib.Part) map[string]any {
	t.Helper()
	for _, p := range parts {
		if p.Kind != "data" {
			continue
		}
		var obj map[string]any
		if err := json.Unmarshal(p.Data, &obj); err != nil {
			t.Fatal(err)
		}
		if obj["schema"] == chatmsg.Schema {
			return obj
		}
	}
	return nil
}

// terminalParts is the final status-update's message parts.
func terminalParts(t *testing.T, url, taskID string) []lib.Part {
	t.Helper()
	for _, env := range replayEvents(t, url, taskID) {
		if !lib.IsFinalStatus(env) {
			continue
		}
		var su lib.StatusUpdate
		if err := json.Unmarshal(env.Payload, &su); err != nil {
			t.Fatal(err)
		}
		if su.Status.Message == nil {
			return nil
		}
		return su.Status.Message.Parts
	}
	t.Fatalf("%s has no terminal", taskID)
	return nil
}

// The result carries the answer laid out on either executor. The cli
// executor records no conversation route, so no card post can settle an ask,
// and its terminal carries no mark even when the turn filed a card.
func TestBridge_TheResultCarriesTheAnswerAndACliTerminalNoMark(t *testing.T) {
	t.Setenv(slackUXEnv, "1")
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
call("kanban_create", {"title": "seeded-a"}, "call_1")
print("**Two clusters are restarting.** The first is seeded-a.\n\nseeded-b has 3.")
`), nil)
	c := gatewayClient(t, url)
	submitFrom(t, c, "task-cli", "ctx-task-cli", "slack:C1/1.2", "which clusters have pods restarting right now?")
	task := waitTerminal(t, c, "task-cli")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("assertion 18: %v", err)
	}
	result := task.Artifact(lib.ArtifactResult)
	if result.Parts[0].Text == "" {
		t.Fatal("the result lost its text")
	}
	chat := chatData(t, result.Parts)
	if chat == nil || chat["headline"] != "Two clusters are restarting." || chat["detail"] != "The first is seeded-a.\n\nseeded-b has 3." {
		t.Errorf("result chat = %v", chat)
	}
	if terminal := chatData(t, terminalParts(t, url, "task-cli")); terminal != nil {
		t.Errorf("terminal chat = %v, want none: the cli executor records no route", terminal)
	}
}

// On the api executor a turn that filed a card, with its route recorded,
// ends on a mark-only terminal keeping the ask's kind. A failed kanban_create
// files nothing, a direct answer keeps the default, and so does a turn whose
// route the store refused.
func TestAPI_ATurnThatFiledCardsKeepsItsAsk(t *testing.T) {
	t.Setenv(slackUXEnv, "1")
	const shared = "pod-wide-activity-key"
	for _, tc := range []struct {
		name, prompt string
		storeStatus  int
		mark         map[string]any
	}{
		{"filed", "which clusters have pods restarting right now? fan out", http.StatusOK, map[string]any{"ask": askFleet}},
		{"failed-call", "which clusters have pods restarting right now? broken", http.StatusOK, nil},
		{"direct", "is checkout-gateway restarting in seeded-a?", http.StatusOK, nil},
		{"route-refused", "which clusters have pods restarting right now? fan out", http.StatusBadRequest, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, url := startServer(t)
			store := newRouteStore(t, tc.storeStatus)
			var door atomic.Value
			stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
				u := door.Load().(string)
				switch {
				case strings.Contains(tc.prompt, "fan out"):
					postDelivery(t, u, shared, c.sessionID, hookPreToolCall, kanbanCreateTool, "call_1")
					postDelivery(t, u, shared, c.sessionID, hookPostToolCall, kanbanCreateTool, "call_1")
				case strings.Contains(tc.prompt, "broken"):
					postDeliveryStatus(t, u, shared, c.sessionID, kanbanCreateTool, "call_1", ActivityStatusError)
				}
				writeCompletion(w, c.sessionID, "On it: one card per cluster.")
			})
			b := startAPIBridge(t, url, stub, func(cfg *Config) {
				cfg.ActivityListen = "127.0.0.1:0"
				cfg.ActivitySecret = shared
				cfg.RouteURL, cfg.RouteKey = store.srv.URL, "kv-key"
			})
			door.Store(b.ActivityURL())
			c := gatewayClient(t, url)
			submitFrom(t, c, "task-"+tc.name, "ctx-"+tc.name, "slack:C1/1.2", tc.prompt)
			if task := waitTerminal(t, c, "task-"+tc.name); task.State != lib.StateCompleted {
				t.Fatalf("state = %s, want completed", task.State)
			}
			terminal := chatData(t, terminalParts(t, url, "task-"+tc.name))
			switch {
			case tc.mark == nil && terminal != nil:
				t.Errorf("terminal chat = %v, want none", terminal)
			case tc.mark != nil && (terminal == nil || len(terminal) != 2 || !maps.Equal(terminal["mark"].(map[string]any), tc.mark)):
				t.Errorf("terminal chat = %v, want only mark %v", terminal, tc.mark)
			}
		})
	}
}

// A turn that filed kanban cards with its route recorded publishes the
// handoff artifact ahead of its terminal on a Google Chat conversation, where
// the terminal's cards mark never fires (cardsSettleTheAsk is Slack with
// KAGE_SLACK_UX on), so a delegating session hears that the work went to a
// card rather than reading "on it" as the answer (#3258). No card filed, a
// failed kanban_create or an unrecorded route: none.
func TestAPI_ATurnThatFiledCardsPublishesTheHandoff(t *testing.T) {
	t.Setenv(slackUXEnv, "")
	const shared = "pod-wide-activity-key"
	for _, tc := range []struct {
		name, prompt string
		storeStatus  int
		want         bool
	}{
		{"filed", "diagnose the pod restarts in crash-ns; fan out", http.StatusOK, true},
		{"failed-call", "diagnose the pod restarts in crash-ns; broken", http.StatusOK, false},
		{"direct", "is checkout-gateway restarting in seeded-a?", http.StatusOK, false},
		{"route-refused", "diagnose the pod restarts in crash-ns; fan out", http.StatusBadRequest, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, url := startServer(t)
			store := newRouteStore(t, tc.storeStatus)
			var door atomic.Value
			stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
				u := door.Load().(string)
				switch {
				case strings.Contains(tc.prompt, "fan out"):
					postDelivery(t, u, shared, c.sessionID, hookPreToolCall, kanbanCreateTool, "call_1")
					postDelivery(t, u, shared, c.sessionID, hookPostToolCall, kanbanCreateTool, "call_1")
				case strings.Contains(tc.prompt, "broken"):
					postDeliveryStatus(t, u, shared, c.sessionID, kanbanCreateTool, "call_1", ActivityStatusError)
				}
				writeCompletion(w, c.sessionID, "focusing on memory limits")
			})
			b := startAPIBridge(t, url, stub, func(cfg *Config) {
				cfg.ActivityListen = "127.0.0.1:0"
				cfg.ActivitySecret = shared
				cfg.RouteURL, cfg.RouteKey = store.srv.URL, "kv-key"
			})
			door.Store(b.ActivityURL())
			c := gatewayClient(t, url)
			submitFrom(t, c, "task-"+tc.name, "ctx-"+tc.name, "gchat:spaces/A/threads/B", tc.prompt)
			task := waitTerminal(t, c, "task-"+tc.name)
			if task.State != lib.StateCompleted {
				t.Fatalf("state = %s, want completed", task.State)
			}
			art := task.Artifact(lib.ArtifactHandoff)
			got := art != nil && len(art.Parts) == 1 && art.Parts[0].Text == lib.HandoffCards
			if got != tc.want {
				t.Fatalf("handoff artifact = %+v, want present=%v", art, tc.want)
			}
		})
	}
}

// The handoff is task-wide, like the cards mark: turn 1 files a card and
// acks, a queued follow-up answers directly as turn 2, and turn 1's card is
// still going to report into the conversation, so the terminal still carries
// the handoff. On Slack with KAGE_SLACK_UX on the mark says the same.
func TestAPI_AFollowUpAfterACardStillReportsTheHandoff(t *testing.T) {
	t.Setenv(slackUXEnv, "")
	const shared = "pod-wide-activity-key"
	_, url := startServer(t)
	store := newRouteStore(t, http.StatusOK)
	release := make(chan struct{})
	var door atomic.Value
	var turns atomic.Int32
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if turns.Add(1) == 1 {
			u := door.Load().(string)
			postDelivery(t, u, shared, c.sessionID, hookPreToolCall, kanbanCreateTool, "call_1")
			postDelivery(t, u, shared, c.sessionID, hookPostToolCall, kanbanCreateTool, "call_1")
			<-release
			writeCompletion(w, c.sessionID, "on it: a card is checking")
			return
		}
		writeCompletion(w, c.sessionID, "payments-worker exits: DB_URL is not set")
	})
	b := startAPIBridge(t, url, stub, func(cfg *Config) {
		cfg.ActivityListen = "127.0.0.1:0"
		cfg.ActivitySecret = shared
		cfg.RouteURL, cfg.RouteKey = store.srv.URL, "kv-key"
	})
	door.Store(b.ActivityURL())
	c := gatewayClient(t, url)
	origin := submitFromEnv(t, c, "task-followup", "ctx-followup", "gchat:spaces/A/threads/B", "diagnose crash-ns")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return turns.Load() == 1 })
	sendSteer(t, c, origin, "just tell me why it crashes")
	waitFor(t, 10*time.Second, "follow-up queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	close(release)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted || turns.Load() != 2 {
		t.Fatalf("state %s after %d turns, want completed after 2", task.State, turns.Load())
	}
	if art := task.Artifact(lib.ArtifactHandoff); art == nil {
		t.Fatal("no handoff for a task whose first turn filed a card that is still to report")
	}
}

// submitFromEnv is submitFrom returning the submission, for a test that
// steers it.
func submitFromEnv(t *testing.T, c *lib.Client, taskID, contextID, conversation, prompt string) *lib.Envelope {
	t.Helper()
	var auth map[string]any
	if err := json.Unmarshal(authorityFor(t, mintFor(t, c, taskID, "platform")), &auth); err != nil {
		t.Fatal(err)
	}
	auth["audience"] = map[string]any{"conversation": conversation, "kind": "dm"}
	raw, err := json.Marshal(auth)
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, contextID, "corr-"+taskID,
		messagePayload(t, taskID, contextID, prompt), lib.WithTo(lib.Party{Session: "platform"}), lib.WithAuthority(raw))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", taskID), env); err != nil {
		t.Fatalf("submission publish: %v", err)
	}
	return env
}

// postDeliveryStatus is postDelivery's closing delivery for a call that ended
// with status, as hermes reports a failed tool call.
func postDeliveryStatus(t *testing.T, url, key, sessionID, tool, callID, status string) {
	t.Helper()
	body, _ := json.Marshal(map[string]any{
		"hook_event_name": hookPostToolCall, "tool_name": tool, "tool_input": map[string]any{"q": "x"},
		"session_id": sessionID, "delivery_id": nuid.Next(), "timestamp": "2026-10-02T12:00:00Z",
		"extra": map[string]any{"tool_call_id": callID, "duration_ms": 7, "status": status, "error_type": "tool_error"},
	})
	req, _ := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write(body)
	req.Header.Set(hookSignatureHeader, hookSignaturePrefix+hex.EncodeToString(mac.Sum(nil)))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Errorf("delivery: %v", err)
		return
	}
	resp.Body.Close()
}

// slackOrigin is a submission from a Slack conversation asking prompt.
func slackOrigin(t *testing.T, prompt string) *lib.Envelope {
	t.Helper()
	payload, err := json.Marshal(lib.Message{Parts: []lib.Part{{Kind: "text", Text: prompt}}})
	if err != nil {
		t.Fatal(err)
	}
	return &lib.Envelope{ContextID: "ctx-1", Payload: payload,
		Authority: json.RawMessage(`{"audience": {"conversation": "slack:C1/1.2"}}`)}
}

// The terminal keeps the ask only where the cards' posts settle it: a Slack
// conversation with KAGE_SLACK_UX on. Off, on Google Chat, or with no
// conversation, the terminal's default takes it off.
func TestCardsSettleTheAsk(t *testing.T) {
	gchat := slackOrigin(t, "x")
	gchat.Authority = json.RawMessage(`{"audience": {"conversation": "gchat:spaces/A/threads/B"}}`)
	none := slackOrigin(t, "x")
	none.Authority = nil
	for _, tc := range []struct {
		name   string
		flag   *string
		origin *lib.Envelope
		want   bool
	}{
		{"slack, flag unset", nil, slackOrigin(t, "x"), true},
		{"slack, flag on", ptr(" TRUE "), slackOrigin(t, "x"), true},
		{"slack, flag off", ptr("false"), slackOrigin(t, "x"), false},
		{"slack, flag empty", ptr(""), slackOrigin(t, "x"), false},
		{"google chat", nil, gchat, false},
		{"no conversation", nil, none, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if tc.flag == nil {
				t.Setenv(slackUXEnv, "")
				os.Unsetenv(slackUXEnv)
			} else {
				t.Setenv(slackUXEnv, *tc.flag)
			}
			if got := cardsSettleTheAsk(tc.origin); got != tc.want {
				t.Errorf("cardsSettleTheAsk = %v, want %v", got, tc.want)
			}
		})
	}
}

func ptr(s string) *string { return &s }

// An invalid UTF-8 byte inside an open bracket at the sentence's end is
// masked at its own width: no write past the buffer, no panic.
func TestClauseCutSurvivesAnInvalidByteInABracket(t *testing.T) {
	sentence := strings.Repeat("word ", 40) + "and more (open \xff"
	splitAnswer(sentence)
	clauseCut(sentence)
}

// The presenter's linear-time cases: nothing here retries a line's end from
// every marker.
func TestSplitAnswerStaysLinear(t *testing.T) {
	for _, line := range []string{
		strings.Repeat("See e.g. A ", 4000) + "end.",
		"x " + strings.Repeat("`", 20000),
		strings.Repeat("**a ", 10000),
		strings.Repeat("__a ", 10000),
		strings.Repeat("[a", 20000),
	} {
		splitAnswer(line)
		plainLine(line)
	}
}
