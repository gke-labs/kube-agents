package hermesbridge

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// session-kv's writes reach the result part as they came, bounded.
func TestCardStopResultCarriesTheWrites(t *testing.T) {
	var r cardStopResponse
	if err := json.Unmarshal([]byte(`{
		"stopped": [{"id": "t_1", "title": "scale checkout", "prior_status": "running"}],
		"writes": [{"kind": "pull_request", "verb": "opened", "url": "https://github.com/acme/gitops/pull/412",
			"number": 412, "card": "t_1", "at": 1.5}]
	}`), &r); err != nil {
		t.Fatal(err)
	}
	out := cardStopResult(&r)
	want := lib.CardStopWrite{Kind: "pull_request", Verb: "opened", URL: "https://github.com/acme/gitops/pull/412", Number: 412}
	if len(out.Writes) != 1 || out.Writes[0] != want {
		t.Fatalf("writes = %+v, want [%+v]", out.Writes, want)
	}
}

func TestCardStopResultWritesAreBoundedAndOptional(t *testing.T) {
	if out := cardStopResult(&cardStopResponse{}); out.Writes != nil {
		t.Fatalf("an answer with no writes named some: %+v", out.Writes)
	}
	var entries []string
	for n := 1; n <= lib.CardStopWritesMax+5; n++ {
		entries = append(entries, fmt.Sprintf(`{"kind":"pull_request","verb":"opened","url":"https://github.com/a/b/pull/%d","number":%d}`, n, n))
	}
	var r cardStopResponse
	if err := json.Unmarshal([]byte(`{"writes":[`+strings.Join(entries, ",")+`]}`), &r); err != nil {
		t.Fatal(err)
	}
	if out := cardStopResult(&r); len(out.Writes) != lib.CardStopWritesMax || out.Writes[0].Number != 1 {
		t.Fatalf("writes not bounded to the first %d: %d", lib.CardStopWritesMax, len(out.Writes))
	}
}
