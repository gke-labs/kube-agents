package workeradapter

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// The policy and MCP servers travel in the stdin envelope with the message.
func TestTheCoreAgentEnvelopeCarriesThePolicy(t *testing.T) {
	d := coreAgentDriver{Policy: coreagentwire.Policy{Tools: []string{"bash"}, Allow: []string{"bash:echo *"}},
		MCP: []coreagentwire.MCPServer{{Name: "a2a", Command: "/self", Tools: []string{"delegate"}}}}
	b, err := d.EncodeUser("hello")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(string(b), "\n") || strings.Count(string(b), "\n") != 1 {
		t.Fatalf("envelope is not one line: %q", b)
	}
	var env coreagentwire.Envelope
	if err := json.Unmarshal(b, &env); err != nil {
		t.Fatal(err)
	}
	if env.Message != "hello" || len(env.Policy.Tools) != 1 || len(env.MCP) != 1 || env.MCP[0].Name != "a2a" {
		t.Errorf("envelope %+v", env)
	}
}

// Each harness line maps to its Event, one EventToolResult per tool step.
func TestTheCoreAgentDecoderMapsEachLine(t *testing.T) {
	lines := []string{
		`{"type":"init","session":"s1","model":"gemini-x"}`,
		`{"type":"thinking","text":"hmm"}`,
		`{"type":"tool_call","id":"c1","tool":"bash","input":{"command":"echo hi"}}`,
		`{"type":"tool_result","id":"c1","tool":"bash","text":"{\"stdout\":\"hi\"}"}`,
		`{"type":"tool_call","id":"c2","tool":"bash"}`,
		`{"type":"tool_result","id":"c2","tool":"bash","failed":true,"error":"refused"}`,
		`{"type":"text","text":"done"}`,
		`{"type":"result","text":"done","usage":{"input":10,"output":3}}`,
		`{"type":"some_future_event"}`,
	}
	d := coreAgentDriver{}.NewDecoder()
	var kinds []EventKind
	var last Event
	for _, l := range lines {
		evs, err := d.Decode([]byte(l))
		if err != nil {
			t.Fatalf("%s: %v", l, err)
		}
		for _, e := range evs {
			kinds = append(kinds, e.Kind)
			last = e
		}
	}
	want := []EventKind{EventInit, EventThinking, EventActivity, EventToolResult, EventActivity, EventToolResult, EventProgress, EventResult}
	if len(kinds) != len(want) {
		t.Fatalf("kinds %v, want %v", kinds, want)
	}
	for i := range want {
		if kinds[i] != want[i] {
			t.Fatalf("kinds %v, want %v", kinds, want)
		}
	}
	if last.Failed || last.Telemetry.usage.InputTokens != 10 || last.Telemetry.usage.Model != "gemini-x" {
		t.Errorf("result %+v", last)
	}
}

// A failed turn's result carries the failure token and the harness's reason.
func TestAFailedCoreAgentTurnCarriesItsReason(t *testing.T) {
	evs, err := coreAgentDriver{}.NewDecoder().Decode([]byte(`{"type":"result","failed":true,"error":"interrupted: context canceled"}`))
	if err != nil || len(evs) != 1 || !evs[0].Failed || !strings.HasPrefix(evs[0].Token, coreAgentErrorToken) || !strings.Contains(evs[0].Token, "interrupted") {
		t.Fatalf("events %+v, err %v", evs, err)
	}
}

// The driver keeps the event lines a turn emits and sends them back in the
// next turn's envelope for the same conversation; nothing else carries them.
func TestTheCoreAgentDriverCarriesHistoryToTheNextTurn(t *testing.T) {
	d := newCoreAgentDriver()
	argv := d.WithResume([]string{"/h"}, "")
	if len(argv) != 1 {
		t.Fatalf("resume changed the argv: %v", argv)
	}
	dec := d.NewDecoder()
	for _, l := range []string{
		`{"type":"init","session":"s1","model":"m"}`,
		`{"type":"event","event":{"author":"user","n":1}}`,
		`{"type":"event","event":{"author":"model","n":2}}`,
		`{"type":"result","text":"ok"}`,
	} {
		if _, err := dec.Decode([]byte(l)); err != nil {
			t.Fatal(err)
		}
	}
	d.WithResume([]string{"/h"}, "s1")
	b, err := d.EncodeUser("next")
	if err != nil {
		t.Fatal(err)
	}
	var env coreagentwire.Envelope
	if err := json.Unmarshal(b, &env); err != nil {
		t.Fatal(err)
	}
	if env.Session != "s1" || len(env.History) != 2 || !strings.Contains(string(env.History[1]), `"n":2`) {
		t.Errorf("envelope %s", b)
	}
	d.WithResume([]string{"/h"}, "")
	b, _ = d.EncodeUser("fresh")
	env = coreagentwire.Envelope{}
	if err := json.Unmarshal(b, &env); err != nil {
		t.Fatal(err)
	}
	if env.Session != "" || len(env.History) != 0 {
		t.Errorf("a new conversation carried history: %s", b)
	}
}

// Every recorded harness turn decodes to one EventToolResult per tool call
// and ends in one EventResult, and its event lines reach the next envelope.
func TestTheRecordedCoreAgentTurnsDecode(t *testing.T) {
	files, err := filepath.Glob(filepath.Join("testdata", "coreagent", "*.jsonl"))
	if err != nil || len(files) == 0 {
		t.Fatalf("no recordings: %v", err)
	}
	for _, f := range files {
		t.Run(filepath.Base(f), func(t *testing.T) {
			raw, err := os.ReadFile(f)
			if err != nil {
				t.Fatal(err)
			}
			d := newCoreAgentDriver()
			dec := d.NewDecoder()
			var calls, results, ends int
			var session string
			for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
				evs, err := dec.Decode([]byte(line))
				if err != nil {
					t.Fatalf("%s: %v", line, err)
				}
				for _, e := range evs {
					switch e.Kind {
					case EventInit:
						session = e.Session
					case EventActivity:
						calls++
					case EventToolResult:
						results++
					case EventResult:
						ends++
					}
				}
			}
			if calls != results || ends != 1 || session == "" {
				t.Errorf("%d calls, %d tool results, %d results, session %q", calls, results, ends, session)
			}
			d.WithResume(nil, session)
			b, err := d.EncodeUser("next")
			if err != nil {
				t.Fatal(err)
			}
			var env coreagentwire.Envelope
			if err := json.Unmarshal(b, &env); err != nil || len(env.History) == 0 {
				t.Errorf("the next envelope carries %d history events (%v)", len(env.History), err)
			}
		})
	}
}

// TestACoreAgentDriverWithAPolicyKeepsItsHistory: the exported constructor
// carries the turn's policy and still keeps the conversation history from
// one turn to the next.
func TestACoreAgentDriverWithAPolicyKeepsItsHistory(t *testing.T) {
	d := NewCoreAgentDriver(coreagentwire.Policy{Tools: []string{"read_file"}, Allow: []string{"read_file:/w/*"}}, nil)
	dec := d.NewDecoder()
	for _, l := range []string{
		`{"type":"init","session":"s1","model":"m"}`,
		`{"type":"event","event":{"author":"user","n":1}}`,
		`{"type":"event","event":{"author":"model","n":2}}`,
		`{"type":"result","text":"ok"}`,
	} {
		if _, err := dec.Decode([]byte(l)); err != nil {
			t.Fatal(err)
		}
	}
	d.WithResume([]string{"/h"}, "s1")
	b, err := d.EncodeUser("next")
	if err != nil {
		t.Fatal(err)
	}
	var env coreagentwire.Envelope
	if err := json.Unmarshal(b, &env); err != nil {
		t.Fatal(err)
	}
	if len(env.History) != 2 {
		t.Errorf("history %d entries, want 2: %s", len(env.History), b)
	}
	if len(env.Policy.Tools) != 1 || env.Policy.Tools[0] != "read_file" || len(env.Policy.Allow) != 1 {
		t.Errorf("policy %+v not carried", env.Policy)
	}
}
