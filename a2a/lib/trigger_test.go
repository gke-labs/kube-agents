package lib

import (
	"strings"
	"testing"
)

func TestValidateTriggerAcceptsTheShapesAGrantBinds(t *testing.T) {
	for _, c := range [][3]string{
		{"schedule/compliance-audit", TriggerOriginBuiltin, "compliance-audit"},
		{"schedule/nightly", TriggerOriginUser, "user/nightly"},
		{"schedule/nightly", TriggerOriginUser, ""},
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
		"an empty name":         {"", TriggerOriginBuiltin, ""},
		"an empty name segment": {"schedule/", TriggerOriginBuiltin, ""},
		"three name segments":   {"schedule/a/b", TriggerOriginBuiltin, ""},
		"a dotted name":         {"schedule/a.b", TriggerOriginBuiltin, ""},
		"a wildcard name":       {"schedule/>", TriggerOriginBuiltin, ""},
		"an upper-case source":  {"Schedule/a", TriggerOriginBuiltin, ""},
		"an over-long name":     {"schedule/" + long, TriggerOriginBuiltin, ""},
		"no origin":             {"schedule/a", "", ""},
		"an unknown origin":     {"schedule/a", "admin", ""},
		"a dotted stream":       {"schedule/a", TriggerOriginBuiltin, "a.b"},
		"a foreign stream path": {"schedule/a", TriggerOriginUser, "team/a"},
		"an empty user stream":  {"schedule/a", TriggerOriginUser, "user/"},
		"a deep user stream":    {"schedule/a", TriggerOriginUser, "user/a/b"},
	} {
		if err := ValidateTrigger(c[0], c[1], c[2]); err == nil {
			t.Errorf("%s: accepted %q", name, c)
		}
	}
}
