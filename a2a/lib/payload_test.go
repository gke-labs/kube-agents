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

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}
