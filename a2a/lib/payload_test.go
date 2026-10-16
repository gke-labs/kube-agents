package lib

import (
	"encoding/json"
	"testing"
)

func TestSteerNoticeRoundTrips(t *testing.T) {
	in := SteerNotice{Steer: SteerRefused, EnvelopeID: "env-1", Reason: SteerReasonQueueFull}
	p, err := SteerNoticePart(in)
	if err != nil {
		t.Fatal(err)
	}
	if p.Kind != "data" {
		t.Fatalf("kind = %q, want data", p.Kind)
	}
	got, ok := SteerNoticeOf([]Part{{Kind: "text", Text: "not taken"}, p})
	if !ok || got != in {
		t.Fatalf("SteerNoticeOf = %+v, %v; want %+v", got, ok, in)
	}
}

func TestSteerNoticeOfIgnoresEverythingElse(t *testing.T) {
	for _, parts := range [][]Part{
		nil,
		{{Kind: "text", Text: `{"steerNotice":{"steer":"queued","envelopeId":"e"}}`}},
		{{Kind: "data", Data: json.RawMessage(`{"tool":"kubectl"}`)}},
		{{Kind: "data", Data: json.RawMessage(`{"steerNotice":{"steer":"maybe","envelopeId":"e"}}`)}},
		{{Kind: "data", Data: json.RawMessage(`{"steerNotice":{"steer":"queued"}}`)}},
		{{Kind: "data", Data: json.RawMessage(`[1,2]`)}},
	} {
		if n, ok := SteerNoticeOf(parts); ok {
			t.Errorf("%s read as a steer notice: %+v", mustJSON(t, parts), n)
		}
	}
}

// The request is exactly {"cardStop":{"v":1}}, whatever version the caller
// left in it, and reads back as a request.
func TestCardStopRequestRoundTrips(t *testing.T) {
	p, err := CardStopPart(CardStop{})
	if err != nil {
		t.Fatal(err)
	}
	if p.Kind != "data" || string(p.Data) != `{"cardStop":{"v":1}}` {
		t.Fatalf("request part = %s %s, want data {\"cardStop\":{\"v\":1}}", p.Kind, p.Data)
	}
	got, ok := CardStopOf([]Part{p})
	if !ok || got.V != CardStopVersion || got.CardStopResult != nil {
		t.Fatalf("CardStopOf = %+v, %v; want a v1 request", got, ok)
	}
	if !CardStopRequested([]Part{p}) {
		t.Fatal("CardStopRequested = false for the request alone")
	}
}

// The result round-trips beside its text, and an empty one still carries its
// lists, so it never reads as a request.
func TestCardStopResultRoundTrips(t *testing.T) {
	in := CardStop{CardStopResult: &CardStopResult{
		Stopped:    []CardStopCard{{ID: "t_1", Title: "check node pools", PriorStatus: "running"}},
		Settled:    []CardStopCard{{ID: "t_2", Title: "list clusters"}},
		Failed:     1,
		Pending:    2,
		Unresolved: 3,
		Truncated:  true,
		Deadline:   true,
		ListsCut:   true,
	}}
	p, err := CardStopPart(in)
	if err != nil {
		t.Fatal(err)
	}
	got, ok := CardStopOf([]Part{{Kind: "text", Text: "stopped 1 card"}, p})
	if !ok || got.CardStopResult == nil {
		t.Fatalf("CardStopOf = %+v, %v; want the result", got, ok)
	}
	if got.V != CardStopVersion || mustJSON(t, got.CardStopResult) != mustJSON(t, in.CardStopResult) {
		t.Fatalf("result = %s, want %s", mustJSON(t, got), mustJSON(t, in))
	}
	if CardStopRequested([]Part{p}) {
		t.Fatal("CardStopRequested = true for a result")
	}

	empty, err := CardStopPart(CardStop{CardStopResult: &CardStopResult{}})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"cardStop":{"v":1,"stopped":[],"settled":[],"failed":0,"pending":0,"unresolved":0,"truncated":false,"deadline":false,"listsCut":false}}`
	if string(empty.Data) != want {
		t.Fatalf("empty result = %s, want %s", empty.Data, want)
	}
	if CardStopRequested([]Part{empty}) {
		t.Fatal("CardStopRequested = true for an empty result")
	}
}

// A message that asks something is a question: a text part beside the
// request, even a blank one, or a file part, makes it no control. Neither is
// another key, another version, or the request spelled in text.
func TestCardStopRequestedRefusesEverythingElse(t *testing.T) {
	req, err := CardStopPart(CardStop{})
	if err != nil {
		t.Fatal(err)
	}
	for _, parts := range [][]Part{
		nil,
		{{Kind: "text", Text: "stop"}, req},
		{req, {Kind: "text", Text: " "}},
		{req, {Kind: "file", File: &FileRef{URI: "gs://b/o"}}},
		{{Kind: "text", Text: `{"cardStop":{"v":1}}`}},
		{{Kind: "data", Data: json.RawMessage(`{"cardStop":{"v":2}}`)}},
		{{Kind: "data", Data: json.RawMessage(`{"cardStop":{}}`)}},
		{{Kind: "data", Data: json.RawMessage(`{"steerNotice":{"steer":"queued","envelopeId":"e"}}`)}},
		{{Kind: "data", Data: json.RawMessage(`[1,2]`)}},
	} {
		if CardStopRequested(parts) {
			t.Errorf("%s read as a card-stop request", mustJSON(t, parts))
		}
	}
	// Another data part beside it is still data only.
	if !CardStopRequested([]Part{{Kind: "data", Data: json.RawMessage(`{"tool":"x"}`)}, req}) {
		t.Error("the request beside another data part was not read as one")
	}
}

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}
