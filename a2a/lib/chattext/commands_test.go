package chattext

import "testing"

func TestIsStatusQuery(t *testing.T) {
	// Exact phrases are the affordance everywhere - including narrow mode,
	// the posture for executors that run follow-ups.
	exact := []string{
		"what is it doing", "What's it doing?", "status", "Status?",
		"how's it going", "how is it going", "whats going on",
		"any progress", "any updates?", "progress", "where are we",
	}
	// Interrogative shapes match only under the wide posture, where the
	// executor refuses follow-ups (no-resume) and a false positive costs
	// nothing.
	wideOnly := []string{
		"what is the agent doing",
		"what is kage doing",
		"any update on the rollout",
	}
	// Steers the wide rule mistakes for status asks - the documented cost
	// of the width bias, and why executors that run follow-ups get narrow.
	wideCost := []string{
		"any update to the config should be reverted",
		"how about doing the upgrade instead",
	}
	never := []string{
		"also check the memory limits",
		"stop",
		"what is the memory limit on the nats pod and can you also check its restarts", // long compound: steer
		// Status-ish and interrogative, so only the WideMatchLenCap guard
		// keeps it a steer: past the cap it is a composed instruction.
		"what is the agent doing about the memory limit on the nats pod and its restarts",
		"delete the deployment",
	}
	for _, s := range exact {
		if !IsStatusQuery(s, false) || !IsStatusQuery(s, true) {
			t.Errorf("expected status query in both modes: %q", s)
		}
	}
	for _, s := range append(wideOnly, wideCost...) {
		if !IsStatusQuery(s, true) {
			t.Errorf("expected wide status query: %q", s)
		}
		if IsStatusQuery(s, false) {
			t.Errorf("expected narrow mode to forward as a steer: %q", s)
		}
	}
	for _, s := range never {
		if IsStatusQuery(s, true) || IsStatusQuery(s, false) {
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
		got, ok := IsDelegate(in)
		if !ok || got != want {
			t.Errorf("IsDelegate(%q) = (%q, %v), want (%q, true)", in, got, ok, want)
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
		if got, ok := IsDelegate(in); ok {
			t.Errorf("IsDelegate(%q) = (%q, true), want false", in, got)
		}
	}
}

func TestIsSessionCommand(t *testing.T) {
	yes := map[string]string{
		"/session":                             "",
		"/session off":                         "off",
		"/SESSION Off":                         "Off",
		"  /session what is running in ns x  ": "what is running in ns x",
		"/session\nmultiline first turn":       "multiline first turn",
		"/session\toff":                        "off",
	}
	for in, want := range yes {
		got, ok := IsSessionCommand(in)
		if !ok || got != want {
			t.Errorf("IsSessionCommand(%q) = (%q, %v), want (%q, true)", in, got, ok, want)
		}
	}
	no := []string{
		"/sessions", "/sessionoff", "/ session", "session", "session off",
		"delegate: /session", "/help", "/", "", "please /session",
	}
	for _, in := range no {
		if got, ok := IsSessionCommand(in); ok {
			t.Errorf("IsSessionCommand(%q) = (%q, true), want false", in, got)
		}
	}
	for _, rest := range []string{"off", "OFF", " off ", "off.", "off!", "Off,"} {
		if !IsSessionOff(rest) {
			t.Errorf("IsSessionOff(%q) = false, want true", rest)
		}
	}
	for _, rest := range []string{"", "offline", "turn off", "on"} {
		if IsSessionOff(rest) {
			t.Errorf("IsSessionOff(%q) = true, want false", rest)
		}
	}
}
