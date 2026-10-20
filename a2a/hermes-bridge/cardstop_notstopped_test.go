package hermesbridge

import (
	"encoding/json"
	"testing"
)

// The result names the cards that did not stop and the pull requests the
// conversation's cards opened, for the Slack Stop's replies.
func TestCardStopResultNamesWhatDidNotStopAndThePullRequests(t *testing.T) {
	var r cardStopResponse
	if err := json.Unmarshal([]byte(`{
		"stopped": [{"id": "t_1", "title": "check pods", "prior_status": "running"}],
		"failed": [{"id": "t_2", "title": "audit prod", "error": "boom"}],
		"pending": [{"id": "t_3", "title": "drain node"}],
		"unresolved": [{"id": "t_4", "title": "late worker"}],
		"pull_requests": ["https://github.com/o/r/pull/412"]
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
	if len(out.PullRequests) != 1 || out.PullRequests[0] != "https://github.com/o/r/pull/412" {
		t.Fatalf("pullRequests = %v", out.PullRequests)
	}
}

func TestCardStopResultWithoutThemNamesNone(t *testing.T) {
	out := cardStopResult(&cardStopResponse{})
	if out.NotStopped != nil || out.PullRequests != nil {
		t.Fatalf("an answer with neither named some: %+v %+v", out.NotStopped, out.PullRequests)
	}
}
