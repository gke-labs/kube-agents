package workeradapter

import (
	"encoding/json"
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
