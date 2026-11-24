package main

import (
	"slices"
	"strings"
	"time"
)

// agyBuilder is the Antigravity CLI's (`agy`) argv and env: the binary
// driven over its headless stream-json dialect (workeradapter's agyDriver).
//
// What it leaves to the other #6707 sub-issues, rather than inventing a
// mapping here:
//   - the tool allowlist and denylist, the delegate server and the system
//     prompts (harnessSpec.tools and what goes with it): #6711 and #6712;
//   - the model and the gateway (AGY_LLM_GATEWAY_*): #6714. The pod's
//     values reach agy unchanged, and the builder adds none;
//   - HOME: a session pod's agy uses the pod's own HOME, shared by every
//     turn of a reused pod, until #6710 gives each task its own and locks
//     it. That is part of what the A2A_HARNESS_UNISOLATED waiver accepts;
//   - a default path: the image is #6715's, so A2A_HARNESS_PATH is required
//     unless A2A_HARNESS_CMD replaces the whole argv (selectHarness refuses
//     otherwise);
//   - maxTurns: agy has no turn budget, and --print-timeout is its only
//     bound.
type agyBuilder struct{}

const (
	// agyPrintTimeoutFlag bounds agy's wall clock for one user message. Its
	// value is a Go duration. On expiry agy ends the turn with a result whose
	// finished_reason is print_timeout, which the driver fails with the
	// print-timeout reason.
	agyPrintTimeoutFlag = "--print-timeout"
	// agyPrintTimeoutMargin is how far below the turn budget agy's own bound
	// sits, so agy writes its result before the adapter's deadline. agy
	// retries a failing model call five times, about 50s, before its ERROR
	// result; a tighter margin turns a gateway error into a print-timeout.
	agyPrintTimeoutMargin = 60 * time.Second
	// agyMinPrintTimeout is the floor: a turn budget at or below the margin
	// plus the floor (120s), as in tests with a short deadline, gets 60s,
	// and there the adapter's own deadline may end the turn first.
	agyMinPrintTimeout = 60 * time.Second
)

func (agyBuilder) defaultPath() string { return "" }

// agyPersonaDir is where an Antigravity worker's rendered persona is to live
// (#6710, #6713), in a directory of its own that agy reads through links in
// its read-only home; agyPersonaNames are the names it may hold. Nothing
// writes it yet, so the session check finds it missing and logs the persona
// as unverified.
const agyPersonaDir = "/var/lib/agy-persona"

var agyPersonaNames = []string{"GEMINI.md", "AGENTS.md", "skills"}

func (agyBuilder) configDir(func(string) string) string { return agyPersonaDir }

func (agyBuilder) personaCovers(rel string) bool {
	return slices.Contains(agyPersonaNames, rel) || strings.HasPrefix(rel, "skills/")
}

// command: `-p=` because agy's print switch takes a value, and the message
// comes on stdin as stream-json.
func (agyBuilder) command(spec harnessSpec) []string {
	argv := []string{
		spec.path,
		"-p=",
		"--input-format", "stream-json",
		"--output-format", "stream-json",
		agyPrintTimeoutFlag, agyPrintTimeout(spec.turnBudget).String(),
	}
	return append(argv, spec.extra...)
}

// agyPrintTimeout is the turn budget less agyPrintTimeoutMargin, and never
// below agyMinPrintTimeout.
func agyPrintTimeout(budget time.Duration) time.Duration {
	if d := budget - agyPrintTimeoutMargin; d > agyMinPrintTimeout {
		return d
	}
	return agyMinPrintTimeout
}

// env passes the pod environment through: none of Claude Code's model-key
// placeholder or CLAUDE_CODE_* switches, which mean nothing to agy.
func (agyBuilder) env(env []string, _ func(string) bool) []string { return env }

// userSettings: none. agy's settings live under its HOME (see the type's
// comment), which #6710 makes per task and resets.
func (agyBuilder) userSettings(func(string) string) string { return "" }
