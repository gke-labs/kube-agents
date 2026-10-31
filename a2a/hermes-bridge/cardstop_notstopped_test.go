package hermesbridge

import (
	"encoding/json"
	"testing"
)

// The result names the cards that did not stop, for the Slack Stop's reply.
func TestCardStopResultNamesWhatDidNotStop(t *testing.T) {
	var r cardStopResponse
	if err := json.Unmarshal([]byte(`{
		"stopped": [{"id": "t_1", "title": "check pods", "prior_status": "running"}],
		"failed": [{"id": "t_2", "title": "audit prod", "error": "boom"}],
		"pending": [{"id": "t_3", "title": "drain node"}],
		"unresolved": [{"id": "t_4", "title": "late worker"}]
	}`), &r); err != nil {
		t.Fatal(err)
	}
	out := cardStopResult(&r)
	if out.Failed != 1 || out.Pending != 1 || out.Unresolved != 1 {
		t.Fatalf("counts changed: %+v", out)
	}
	want := []string{"t_2", "t_3", "t_4"}
	if len(out.NotStopped) != len(want) {
		t.Fatalf("notStopped = %+v, want %v", out.NotStopped, want)
	}
	for i, id := range want {
		if out.NotStopped[i].ID != id {
			t.Fatalf("notStopped[%d] = %+v, want %s", i, out.NotStopped[i], id)
		}
	}
	if out.NotStopped[0].Title != "audit prod" {
		t.Fatalf("a not-stopped card lost its title: %+v", out.NotStopped[0])
	}
}

func TestCardStopResultWithNothingUnstoppedNamesNone(t *testing.T) {
	if out := cardStopResult(&cardStopResponse{}); out.NotStopped != nil {
		t.Fatalf("an answer with every card stopped named some: %+v", out.NotStopped)
	}
}
