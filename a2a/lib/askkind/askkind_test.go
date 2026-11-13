package askkind

import "testing"

// The full table is the presenter's, run against the bridge's askKind
// (a2a/hermes-bridge/chat_test.go, testdata/chat_cases.json), which is Kind;
// these pin the order rules the gateway's arrival mark relies on.
func TestKindFollowsThePresentersOrder(t *testing.T) {
	for in, want := range map[string]string{
		"<@U1> are all my clusters on the latest version?": Upgrade,
		"scale checkout-gateway to 4":                      Change,
		"which clusters are in the fleet?":                 Fleet,
		"checkout is down":                                 Incident,
		"is checkout crashlooping?":                        Investigate,
		"what is 2 + 3?":                                   Question,
		"":                                                 Question,
	} {
		if got := Kind(in); got != want {
			t.Errorf("Kind(%q) = %q, want %q", in, got, want)
		}
	}
}
