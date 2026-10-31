package lib

import (
	"encoding/json"
	"math"
	"testing"
)

// A usage part round-trips through UsagePart and UsageOf, and a text part
// with the same bytes is not taken for one.
func TestUsageRoundTrips(t *testing.T) {
	in := Usage{Model: "claude-opus-5-5", InputTokens: 1200, OutputTokens: 80, CostUsd: 0.0123, DurationMs: 4500}
	p, err := UsagePart(in)
	if err != nil {
		t.Fatal(err)
	}
	if p.Kind != "data" {
		t.Fatalf("kind = %q, want data", p.Kind)
	}
	got, ok := UsageOf(p)
	if !ok || got != in {
		t.Fatalf("UsageOf = %+v, %v; want %+v", got, ok, in)
	}
	if _, ok := UsageOf(Part{Kind: "text", Text: string(p.Data)}); ok {
		t.Fatal("a text part read as usage")
	}
}

// Every field is optional, but a part that carries none of the figures, a
// negative one, or an object that is not one is not usage. A key the reader
// does not know is tolerated.
func TestUsageOfRefusesEmptyAndNegative(t *testing.T) {
	for _, raw := range []string{
		`{}`,
		`{"model":"m"}`,
		`{"inputTokens":-1}`,
		`{"costUsd":-0.5}`,
		`{"durationMs":-2}`,
		`[1,2]`,
		`"usage"`,
		``,
	} {
		if u, ok := UsageOf(Part{Kind: "data", Data: json.RawMessage(raw)}); ok {
			t.Errorf("%s read as usage: %+v", raw, u)
		}
	}
	u, ok := UsageOf(Part{Kind: "data", Data: json.RawMessage(`{"outputTokens":3,"vendorNote":"x"}`)})
	if !ok || u.OutputTokens != 3 {
		t.Fatalf("usage with an unknown key = %+v, %v; want outputTokens 3", u, ok)
	}
}

// UsagePart omits what the producer does not have: a zero cost is not a
// figure, so a reader never mistakes "unknown" for "free".
func TestUsagePartOmitsZeroes(t *testing.T) {
	p, err := UsagePart(Usage{InputTokens: 10, OutputTokens: 2})
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(p.Data, &m); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"model", "costUsd", "durationMs"} {
		if _, present := m[k]; present {
			t.Errorf("%s present on a part with no figure for it: %s", k, p.Data)
		}
	}
}

// A part with no figure at all is refused at the producer, so an empty
// usage artifact never reaches the bus.
func TestUsagePartRefusesNoFigures(t *testing.T) {
	if _, err := UsagePart(Usage{Model: "m"}); err == nil {
		t.Fatal("UsagePart accepted a usage with no figure")
	}
}

// UsageFigure takes a finite, non-negative float below 2^63 as a whole
// figure and refuses the rest, so a producer drops that field rather than
// publish a wrapped value or lose the part.
func TestUsageFigure(t *testing.T) {
	for _, tc := range []struct {
		in   float64
		want int64
		ok   bool
	}{{0, 0, true}, {12.9, 12, true}, {1200, 1200, true}, {-1, 0, false}, {1e19, 0, false}, {math.NaN(), 0, false}, {math.Inf(1), 0, false}} {
		got, ok := UsageFigure(tc.in)
		if got != tc.want || ok != tc.ok {
			t.Errorf("UsageFigure(%v) = %d, %v; want %d, %v", tc.in, got, ok, tc.want, tc.ok)
		}
	}
}

// UsageTotal folds the turns of one task: figures sum, the model is the last
// one named, and an absent figure stays absent rather than reading as zero.
func TestUsageTotal(t *testing.T) {
	got := UsageTotal([]Usage{
		{Model: "a", InputTokens: 10, OutputTokens: 1, DurationMs: 100},
		{Model: "b", InputTokens: 5, OutputTokens: 2, CostUsd: 0.5, DurationMs: 50},
	})
	want := Usage{Model: "b", InputTokens: 15, OutputTokens: 3, CostUsd: 0.5, DurationMs: 150}
	if got != want {
		t.Fatalf("UsageTotal = %+v, want %+v", got, want)
	}
	if UsageTotal(nil) != (Usage{}) {
		t.Fatal("UsageTotal(nil) is not empty")
	}
}
