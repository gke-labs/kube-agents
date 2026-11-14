package lib

import (
	"strings"
	"testing"
)

func TestValidateTriggerAcceptsTheShapesAGrantBinds(t *testing.T) {
	for _, c := range [][3]string{
		{"schedule/compliance-audit", TriggerOriginBuiltin, "compliance-audit"},
		{"schedule/compliance-audit", TriggerOriginBuiltin, ""},
		{"stall-watch", TriggerOriginBuiltin, ""},
	} {
		if err := ValidateTrigger(c[0], c[1], c[2]); err != nil {
			t.Errorf("%q: %v", c, err)
		}
	}
}

func TestValidateTriggerRefusesMalformedShapes(t *testing.T) {
	long := strings.Repeat("a", 64)
	for name, c := range map[string][3]string{
		"an empty name":             {"", TriggerOriginBuiltin, ""},
		"an empty name segment":     {"schedule/", TriggerOriginBuiltin, ""},
		"three name segments":       {"schedule/a/b", TriggerOriginBuiltin, ""},
		"a dotted name":             {"schedule/a.b", TriggerOriginBuiltin, ""},
		"a wildcard name":           {"schedule/>", TriggerOriginBuiltin, ""},
		"an upper-case source":      {"Schedule/a", TriggerOriginBuiltin, ""},
		"an over-long name":         {"schedule/" + long, TriggerOriginBuiltin, ""},
		"a non-ASCII name":          {"schedule/caf\u00e9", TriggerOriginBuiltin, ""},
		"no origin":                 {"schedule/a", "", ""},
		"an unknown origin":         {"schedule/a", "admin", ""},
		"a user origin":             {"schedule/a", "user", ""},
		"a dotted stream":           {"schedule/a.b", TriggerOriginBuiltin, "a.b"},
		"a stream with a path":      {"schedule/a", TriggerOriginBuiltin, "user/a"},
		"a doubled user prefix":     {"schedule/x", TriggerOriginBuiltin, "user/user/x"},
		"another schedule's stream": {"schedule/compliance-audit", TriggerOriginBuiltin, "ai-security-audit"},
		"a stream on a bare source": {"stall-watch", TriggerOriginBuiltin, "stall-watch"},
	} {
		if err := ValidateTrigger(c[0], c[1], c[2]); err == nil {
			t.Errorf("%s: accepted %q", name, c)
		}
	}
}
