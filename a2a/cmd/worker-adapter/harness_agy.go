package main

// agyBuilder is the Antigravity CLI's (`agy`) argv and env: the binary
// driven over its headless stream-json dialect (workeradapter's agyDriver).
//
// What it leaves to the other #6707 sub-issues, rather than inventing a
// mapping here:
//   - the tool allowlist and denylist, the delegate server and the system
//     prompts (harnessSpec.tools and what goes with it): #6711 and #6712;
//   - the model and the gateway (AGY_LLM_GATEWAY_*): #6714. The pod's
//     values reach agy unchanged, and the builder adds none;
//   - HOME: agy uses the pod's own HOME. Before every agy process the
//     driver clears agy's configuration entries from its conversation
//     directories there and from the workspace (#6710); the read-only HOME
//     layout comes with the image (#6715). Until the agy half of the
//     isolation canary lands, the A2A_HARNESS_UNISOLATED waiver stays;
//   - a default path: the image is #6715's, so A2A_HARNESS_PATH is required
//     unless A2A_HARNESS_CMD replaces the whole argv (selectHarness refuses
//     otherwise);
//   - maxTurns: agy has no turn budget, and --print-timeout is its only
//     bound.
type agyBuilder struct{}

func (agyBuilder) defaultPath() string { return "" }

// command: `-p=` because agy's print switch takes a value, and the message
// comes on stdin as stream-json. --print-timeout is the driver's, set per
// process from the time left on the task (agyDriver.WithTurnBudget).
func (agyBuilder) command(spec harnessSpec) []string {
	argv := []string{
		spec.path,
		"-p=",
		"--input-format", "stream-json",
		"--output-format", "stream-json",
	}
	return append(argv, spec.extra...)
}

// env passes the pod environment through: none of Claude Code's model-key
// placeholder or CLAUDE_CODE_* switches, which mean nothing to agy.
func (agyBuilder) env(env []string, _ func(string) bool) []string { return env }

// userSettings: none. agy's settings live under its HOME (see the type's
// comment).
func (agyBuilder) userSettings(func(string) string) string { return "" }
