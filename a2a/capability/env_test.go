package capability

import (
	"os"
	"testing"
)

// The PR this landed in claimed "an env var nobody set refuses rather than
// passes" and "only the exact string `false` relaxes it", and the tests that
// were credited with it asserted `(Config{}).CapabilityOptional == false` --
// the zero value of a Go bool, which is false by language definition. That
// assertion holds for `!= "true"` too, which is the regression it was meant
// to stop. This is the table that actually decides it.
func TestOnlyTheExactStringFalseRelaxesTheCapabilityRequirement(t *testing.T) {
	for _, tc := range []struct {
		name  string
		value string
		set   bool
		want  bool
	}{
		// The default install. Nothing renders the variable on a chart
		// that predates the mint, and nothing renders it for a local run.
		{name: "unset", set: false, want: false},
		// An empty value is what a Deployment with `value: ""` produces,
		// and it is not consent to run unauthorized work.
		{name: "empty", value: "", set: true, want: false},
		// The one relaxing value, and the one the operator writes:
		// spawn.go renders strconv.FormatBool, so this is a round trip.
		{name: "exactly false", value: "false", set: true, want: true},
		{name: "exactly true", value: "true", set: true, want: false},
		// Everything below is a human writing "off" and getting enforcement
		// anyway, which is the correct direction to fail.
		{name: "capital False", value: "False", set: true, want: false},
		{name: "shouting FALSE", value: "FALSE", set: true, want: false},
		{name: "zero", value: "0", set: true, want: false},
		{name: "no", value: "no", set: true, want: false},
		{name: "off", value: "off", set: true, want: false},
		{name: "padded", value: " false ", set: true, want: false},
		{name: "trailing newline", value: "false\n", set: true, want: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// t.Setenv first either way: it registers the cleanup that
			// restores whatever the outer environment had, and it is what
			// makes the unset case safe to produce with os.Unsetenv.
			t.Setenv(RequiredEnvVar, tc.value)
			if !tc.set {
				if err := os.Unsetenv(RequiredEnvVar); err != nil {
					t.Fatalf("could not unset %s: %v", RequiredEnvVar, err)
				}
			}
			if got := OptionalFromEnv(); got != tc.want {
				t.Errorf("OptionalFromEnv() = %v, want %v for %s=%q (set=%v)",
					got, tc.want, RequiredEnvVar, tc.value, tc.set)
			}
		})
	}
}

// The `!= "true"` regression this guards against, stated as the property it
// breaks rather than as a spelling: whatever the comparison is, an install
// that never heard of the variable must come out enforcing.
func TestAnInstallThatNeverSetTheSwitchIsEnforcing(t *testing.T) {
	t.Setenv(RequiredEnvVar, "")
	if err := os.Unsetenv(RequiredEnvVar); err != nil {
		t.Fatalf("could not unset %s: %v", RequiredEnvVar, err)
	}
	if OptionalFromEnv() {
		t.Fatalf("%s is unset and the requirement was relaxed; a default install "+
			"would execute submissions that carry no capability", RequiredEnvVar)
	}
}
