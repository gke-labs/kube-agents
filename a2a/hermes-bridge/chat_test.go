package hermesbridge

import (
	"encoding/json"
	"maps"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

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
	if len(cases.SplitAnswer) == 0 || len(cases.Plain) == 0 || len(cases.AskKind) == 0 {
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
				run := &taskRun{origin: &lib.Envelope{}}
				run.origin.Payload, _ = json.Marshal(lib.Message{Parts: []lib.Part{{Kind: "text", Text: c.Input.Events[i-1].Ask}}})
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

// A direct answer's result carries the answer folded and its terminal no
// mark; a turn that filed a card ends on a mark-only terminal keeping the
// ask's kind, and a kanban_create that failed files nothing.
func TestBridge_TheResultIsFoldedAndATurnThatFiledCardsKeepsItsAsk(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
prompt = sys.argv[-1]
if "fan" in prompt:
    call("kanban_create", {"title": "seeded-a"}, "call_1")
elif "broken" in prompt:
    call("kanban_create", {"title": "seeded-a"}, "call_1", status="error", error_type="tool_error")
print("**Two clusters are restarting.** The first is seeded-a.\n\nseeded-b has 3.")
`), nil)
	c := gatewayClient(t, url)

	for _, tc := range []struct {
		id, prompt string
		mark       map[string]any
	}{
		{"task-direct", "is checkout-gateway restarting in seeded-a?", nil},
		{"task-fan", "which clusters have pods restarting right now? fan out", map[string]any{"ask": askFleet}},
		{"task-broken", "which clusters are broken right now?", nil},
	} {
		submit(t, c, tc.id, tc.prompt)
		task := waitTerminal(t, c, tc.id)
		if task.State != lib.StateCompleted {
			t.Fatalf("%s: state = %s, want completed", tc.id, task.State)
		}
		if err := task.ValidateArtifacts(); err != nil {
			t.Fatalf("%s: assertion 18: %v", tc.id, err)
		}
		result := task.Artifact(lib.ArtifactResult)
		if result.Parts[0].Text == "" {
			t.Fatalf("%s: the result lost its text", tc.id)
		}
		chat := chatData(t, result.Parts)
		if chat == nil || chat["headline"] != "Two clusters are restarting." || chat["detail"] != "The first is seeded-a.\n\nseeded-b has 3." {
			t.Errorf("%s: result chat = %v", tc.id, chat)
		}
		terminal := chatData(t, terminalParts(t, url, tc.id))
		switch {
		case tc.mark == nil && terminal != nil:
			t.Errorf("%s: terminal chat = %v, want none", tc.id, terminal)
		case tc.mark != nil && (terminal == nil || len(terminal) != 2 || !maps.Equal(terminal["mark"].(map[string]any), tc.mark)):
			t.Errorf("%s: terminal chat = %v, want only mark %v", tc.id, terminal, tc.mark)
		}
	}
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
