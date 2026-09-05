package gateway

import "testing"

func TestIsStatusQuery(t *testing.T) {
	// Exact phrases are the affordance everywhere - including narrow mode,
	// the posture for executors that absorb steers.
	exact := []string{
		"what is it doing", "What's it doing?", "status", "Status?",
		"how's it going", "how is it going", "whats going on",
		"any progress", "any updates?", "progress", "where are we",
	}
	// Interrogative shapes match only under the wide posture, where the
	// executor refuses steers and a false positive costs nothing.
	wideOnly := []string{
		"what is the agent doing", // the live miss that created this test
		"what is kage doing",
		"any update on the rollout",
	}
	// Steers the wide rule mistakes for status asks - the documented cost
	// of the width bias, and why steer-absorbing executors get narrow.
	wideCost := []string{
		"any update to the config should be reverted",
		"how about doing the upgrade instead",
	}
	never := []string{
		"also check the memory limits",
		"actually, focus on the kube-system namespace instead",
		"stop",
		"what is the memory limit on the nats pod and can you also check its restarts", // long compound: steer
		"delete the deployment",
	}
	for _, s := range exact {
		if !isStatusQuery(s, false) || !isStatusQuery(s, true) {
			t.Errorf("expected status query in both modes: %q", s)
		}
	}
	for _, s := range append(wideOnly, wideCost...) {
		if !isStatusQuery(s, true) {
			t.Errorf("expected wide status query: %q", s)
		}
		if isStatusQuery(s, false) {
			t.Errorf("expected narrow mode to forward as a steer: %q", s)
		}
	}
	for _, s := range never {
		if isStatusQuery(s, true) || isStatusQuery(s, false) {
			t.Errorf("expected NOT a status query in any mode: %q", s)
		}
	}
}

func TestIsDelegate(t *testing.T) {
	yes := map[string]string{
		"Delegate: write a haiku about message buses": "write a haiku about message buses",
		"delegate write a haiku":                      "write a haiku",
		"DELEGATE - check the fleet, then report":     "check the fleet, then report",
		"  delegate,  summarize #930  ":               "summarize #930",
		"Delegate:\nmultiline task":                   "multiline task",
	}
	for in, want := range yes {
		got, ok := isDelegate(in)
		if !ok || got != want {
			t.Errorf("isDelegate(%q) = (%q, %v), want (%q, true)", in, got, ok, want)
		}
	}
	no := []string{
		"delegate",
		"delegate:",
		"Delegated tasks are neat",
		"can you delegate this",
		"delegation is the demo",
		"what is it doing",
		"",
	}
	for _, in := range no {
		if got, ok := isDelegate(in); ok {
			t.Errorf("isDelegate(%q) = (%q, true), want false", in, got)
		}
	}
}
